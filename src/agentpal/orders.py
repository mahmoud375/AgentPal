"""Trusted, offline Orders request materialization. Contains no HTTP client.

Only explicit provider-acknowledged Vault agreements pass the contract gate.
Never infer consent from a local flag or a successful historical charge.
"""

from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
import hashlib
import hmac
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from agentpal.discovery import RunContext
from agentpal.readiness import ReadinessError, ReadinessService
from agentpal.vault.security import Keyring, VaultUnavailable
from agentpal.vault.agreement import USAGE_PATTERN, stored_state


ENDPOINT = "/v2/checkout/orders"
# Orders Create documentation, checked 2026-10-22. No account-specific extension
# is assumed. The window begins at the future durable first-dispatch claim.
REPLAY_WINDOW_SECONDS = 21600
RETENTION_DOCUMENTATION = (
    "https://developer.paypal.com/sdk/orders/v2/orders-create/ "
    "(checked 2026-10-22; PayPal-Request-Id: six hours for single-step create "
    "including PayPal.vault_id; no negotiated extension assumed)"
)


class MaterializationError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MaterializationResult:
    purchase_id: UUID
    state: str
    reason_codes: tuple[str, ...] = ()
    payment_id: UUID | None = None
    operation_id: UUID | None = None
    replayed: bool = False

    def public(self):
        return dict(purchase_id=str(self.purchase_id), state=self.state,
                    reason_codes=list(self.reason_codes),
                    payment_id=str(self.payment_id) if self.payment_id else None,
                    operation_id=str(self.operation_id) if self.operation_id else None,
                    replayed=self.replayed, execution_ready=False)


def _usd(amount: Decimal) -> str:
    """Orders money.value is at most 32 characters; never round to invent cents."""
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0 or amount.adjusted() > 28:
        raise MaterializationError("UNSUPPORTED_PAYMENT_AMOUNT")
    try:
        with localcontext() as ctx:
            ctx.prec = 34
            cents = amount.quantize(Decimal("0.01"))
        if cents != amount:
            raise MaterializationError("UNSUPPORTED_PAYMENT_AMOUNT")
        return format(cents, ".2f")
    except DecimalException:
        raise MaterializationError("UNSUPPORTED_PAYMENT_AMOUNT") from None


def _body(purchase, payment, vault_id: str) -> dict:
    return {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": str(purchase["id"]), "custom_id": str(payment["id"]),
            "amount": {"currency_code": "USD", "value": _usd(purchase["total_amount"])},
            "payee": {"merchant_id": payment["sandbox_payee"]},
        }],
        "payment_source": {"paypal": {
            "vault_id": vault_id,
            "stored_credential": {"payment_initiator": "MERCHANT", "usage": "SUBSEQUENT", "usage_pattern": USAGE_PATTERN},
        }},
    }


def _purpose(operation, payment, purchase):
    # Authenticates all durable identities, not just the ciphertext's own row.
    return f"orders-create:v1:{payment['id']}:{purchase['id']}:{operation['request_id']}"


class OrderMaterializationService:
    def __init__(self, readiness: ReadinessService, keys: Keyring):
        # Compose the existing provenance, eligibility and unsent-release gates.
        # Neither prepare() nor the Channel3 adapter is invoked here.
        self._readiness, self._keys = readiness, keys

    @staticmethod
    def _result(p, state, reasons=(), payment=None, operation=None, *, replayed=False):
        return MaterializationResult(p["id"], state, tuple(reasons),
            payment["payment_id"] if payment else None,
            operation["id"] if operation else None, replayed)

    def _reference(self, c, payment):
        row = c.execute(text("""SELECT s.*,m.reference_ciphertext,m.encryption_key_id AS method_key_id
            FROM agentpal.payment_methods m JOIN agentpal.vault_setups s ON s.id=m.vault_setup_id
            WHERE m.id=:id"""), {"id": payment["payment_method_id"]}).mappings().one()
        if row["flow_phase"] != "complete" or row["flow_ciphertext"] is None:
            return None
        setup_id = row["id"]
        flow = self._keys.open(setup_id, "flow", row["flow_ciphertext"], row["flow_key_id"])
        source = flow.get("payment_source")
        paypal = source.get("paypal") if isinstance(source, dict) else None
        # Supported subset: explicit non-recurring on-demand agreement. Missing,
        # recurring, deferred and the old spike's POSTPAID model are not adopted.
        if not isinstance(paypal, dict) or any(paypal.get(k) != v for k, v in
            (("usage_type", "MERCHANT"), ("customer_type", "CONSUMER"), ("usage_pattern", "IMMEDIATE"))):
            return None
        setup = self._keys.open(setup_id, "setup", row["setup_reference_ciphertext"], row["encryption_key_id"])
        reference = self._keys.open(setup_id, "payment-method", row["reference_ciphertext"], row["method_key_id"])
        vault_id, customer = reference.get("id"), reference.get("customer_id")
        if (not isinstance(vault_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", vault_id)
                or not isinstance(customer, str) or not customer or customer != setup.get("customer_id")):
            raise MaterializationError("PAYMENT_REFERENCE_INVALID")
        if stored_state(self._keys, row, reference) != "verified":
            return None
        return vault_id

    def _open_request(self, operation, payment, purchase, vault_id):
        """Backend-only integrity check; plaintext never leaves materialize().

        A future executor may reuse this inside its own final eligibility/claim
        boundary. Reading bytes grants no authority to transmit them.
        """
        raw = self._keys.open_bytes(operation["id"], _purpose(operation, payment, purchase),
            operation["request_payload_ciphertext"], operation["encryption_key_id"])
        if not hmac.compare_digest(hashlib.sha256(raw).digest(), bytes(operation["request_body_sha256"])):
            raise MaterializationError("REQUEST_INTEGRITY_FAILED")
        try:
            body = json.loads(raw)
            # Validate the exact constrained shape against immutable DB evidence;
            # return the original bytes, never a reserialization of this object.
            unit = body["purchase_units"][0]
            paypal = body["payment_source"]["paypal"]
            valid = (
                set(body) == {"intent", "purchase_units", "payment_source"} and body["intent"] == "CAPTURE"
                and len(body["purchase_units"]) == 1
                and set(unit) == {"reference_id", "custom_id", "amount", "payee"}
                and unit["reference_id"] == str(purchase["id"]) and unit["custom_id"] == str(payment["id"])
                and unit["amount"] == {"currency_code": "USD", "value": _usd(purchase["total_amount"])}
                and unit["payee"] == {"merchant_id": payment["sandbox_payee"]}
                and set(body["payment_source"]) == {"paypal"} and set(paypal) == {"vault_id", "stored_credential"}
                and paypal["vault_id"] == vault_id and paypal["stored_credential"] == {
                    "payment_initiator": "MERCHANT", "usage": "SUBSEQUENT", "usage_pattern": USAGE_PATTERN})
        except (ValueError, KeyError, TypeError, IndexError, UnicodeError):
            valid = False
        if not valid:
            raise MaterializationError("REQUEST_INTEGRITY_FAILED")
        return raw

    def _materialize(self, context, purchase_id):
        r = self._readiness
        with r._engine.begin() as c:
            p = r._purchase(c, context, purchase_id)
            financial = r._financial(c, purchase_id)
            if financial is None:
                return self._result(p, "NOT_PREPARED")
            prior = r._replay(c, p, financial)
            operations = c.execute(text("SELECT * FROM agentpal.payment_operations WHERE payment_id=:id FOR UPDATE"),
                {"id": financial["payment_id"]}).mappings().all() if financial["payment_id"] else []
            operation = next((o for o in operations if o["kind"] == "create_order"), None)
            if prior.state != "PREPARED_UNSENT":
                return self._result(p, prior.state, prior.reason_codes, financial, operation, replayed=bool(operation))
            if financial["reservation_status"] != "held":
                return self._result(p, "EXISTING_FINANCIAL_STATE", ("TRUSTED_RECOVERY_REQUIRED",),
                    financial, operation, replayed=bool(operation))
            pay = c.execute(text("SELECT * FROM agentpal.payments WHERE id=:id FOR UPDATE"),
                {"id": financial["payment_id"]}).mappings().one()
            c.execute(text("SELECT agentpal.assert_definitely_unsent(:id)"), {"id": pay["id"]})
            if financial["amount"] != p["total_amount"] or p["currency"] != "USD":
                raise MaterializationError("PAYMENT_AMOUNT_MISMATCH")
            if pay["sandbox_payee"] != r._settings.merchant_id:
                raise MaterializationError("SANDBOX_PAYEE_INVALID")
            _usd(p["total_amount"])
            c.execute(text("SELECT id FROM agentpal.budget_periods WHERE id=:id FOR UPDATE"), {"id": financial["period_id"]})
            if not c.scalar(text("SELECT spent_amount+reserved_amount<=limit_amount FROM agentpal.budget_balances WHERE id=:id"),
                {"id": financial["period_id"]}):
                return self._result(p, "BLOCKED", ("BUDGET_INVALIDATED",), financial, operation, replayed=bool(operation))
            if c.scalar(text("""SELECT result FROM agentpal.offer_revalidations WHERE purchase_id=:id
                ORDER BY checked_at DESC,id DESC LIMIT 1"""), {"id": purchase_id}) != "passed":
                return self._result(p, "BLOCKED", ("SOURCE_CHECK_REQUIRED",), financial, operation, replayed=bool(operation))
            if operations and (len(operations) != 1 or operation is None):
                return self._result(p, "BLOCKED", ("TRUSTED_RECOVERY_REQUIRED",), financial, operation, replayed=True)
            vault_id = self._reference(c, pay)
            if vault_id is None:
                return self._result(p, "BLOCKED", ("PAYMENT_CONTRACT_UNVERIFIED",), financial, operation, replayed=bool(operation))
            if operation is not None:
                evidence = c.execute(text("""SELECT details FROM agentpal.audit_events
                    WHERE payment_id=:id AND kind='order_materialized'"""), {"id": pay["id"]}).scalars().all()
                if (len(evidence) != 1 or evidence[0] != {"operation_id": str(operation["id"]), "version": 1}
                        or operation["endpoint"] != ENDPOINT or operation["replay_window_seconds"] != REPLAY_WINDOW_SECONDS
                        or operation["retention_documentation"] != RETENTION_DOCUMENTATION):
                    raise MaterializationError("TRUSTED_RECOVERY_REQUIRED")
                self._open_request(operation, pay, p, vault_id)
                result = self._result(p, "MATERIALIZED_UNSENT", payment=financial, operation=operation, replayed=True)
            else:
                operation = {"id": uuid4(), "request_id": str(uuid4())}
                raw = json.dumps(_body(p, pay, vault_id), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                cipher, key = self._keys.seal_bytes(operation["id"], _purpose(operation, pay, p), raw)
                c.execute(text("""INSERT INTO agentpal.payment_operations
                    (id,payment_id,kind,endpoint,request_id,request_payload_ciphertext,encryption_key_id,
                     request_body_sha256,replay_window_seconds,retention_documentation)
                    VALUES (:id,:payment,'create_order',:endpoint,:request,:cipher,:key,:hash,:window,:docs)"""),
                    {"id": operation["id"], "payment": pay["id"], "endpoint": ENDPOINT, "request": operation["request_id"],
                     "cipher": cipher, "key": key, "hash": hashlib.sha256(raw).digest(),
                     "window": REPLAY_WINDOW_SECONDS, "docs": RETENTION_DOCUMENTATION})
                r._audit(c, p, "order_materialized", {"operation_id": str(operation["id"]), "version": 1}, pay["id"])
                result = self._result(p, "MATERIALIZED_UNSENT", payment=financial, operation=operation)
            # Catch expiry during construction/encryption and any in-transaction
            # source invalidation. This remains an observation, not a dispatch
            # permission: time can advance after this check or after commit.
            c.execute(text("SELECT agentpal.assert_authorized(:id)"), {"id": purchase_id})
        return result  # No success before commit; no network or dispatch marker.

    def materialize(self, context: RunContext, purchase_id: UUID) -> MaterializationResult:
        """Trusted synchronous server call; accepts no financial/provider fields."""
        try:
            self._readiness._arguments(context, purchase_id)
            return self._materialize(context, purchase_id)
        except ReadinessError as error:
            raise MaterializationError(error.code) from None
        except VaultUnavailable:
            raise MaterializationError("ENCRYPTION_UNAVAILABLE") from None
        except SQLAlchemyError as error:
            state = getattr(error.orig, "sqlstate", None) if isinstance(error, DBAPIError) else None
            code = "TRANSACTION_CONFLICT" if state in ("40001", "40P01", "23505") else (
                "MATERIALIZATION_CONFLICT" if state == "23514" else "MATERIALIZATION_STORAGE_UNAVAILABLE")
            raise MaterializationError(code) from None
