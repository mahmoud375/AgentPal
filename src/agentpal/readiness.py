"""Trusted financial preparation, never a PayPal dispatch or human capability.

Only the server constructs RunContext and settings. Future tool wrappers may
close over a context and accept a purchase UUID; they cannot supply money,
instruments, payees, approval identities, request payloads or retry rules.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hmac
import json
from typing import Literal
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from agentpal.channel3 import Channel3Client, Channel3Error, MATCHING_VERSION, Offer, compare_offer
from agentpal.discovery import RunContext
from agentpal.proposals import _context_hash


class ReadinessSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    environment: Literal["sandbox"] = Field(validation_alias="PAYPAL_ENVIRONMENT")
    merchant_id: str = Field(pattern=r"^[2-9A-HJ-NP-Z]{13}$", validation_alias="PAYPAL_SANDBOX_MERCHANT_ID", repr=False)
    max_source_age_seconds: int = Field(default=30, ge=1, le=120, validation_alias="READINESS_MAX_SOURCE_AGE_SECONDS")


class ReadinessError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    purchase_id: UUID
    state: str
    reason_codes: tuple[str, ...] = ()
    payment_id: UUID | None = None
    period_id: UUID | None = None
    amount: Decimal | None = None
    authorization_kind: str | None = None
    replayed: bool = False

    def public(self):
        return dict(purchase_id=str(self.purchase_id), state=self.state, reason_codes=list(self.reason_codes),
                    payment_id=str(self.payment_id) if self.payment_id else None,
                    period_id=str(self.period_id) if self.period_id else None,
                    amount=format(self.amount, "f") if self.amount is not None else None,
                    currency="USD", authorization_kind=self.authorization_kind, replayed=self.replayed,
                    execution_ready=False)


@dataclass(frozen=True, slots=True)
class _Check:
    result: str
    checked_at: datetime
    latest: Offer | None = None
    count: int | None = None
    reason: str | None = None


_PURCHASE = """SELECT p.*,o.active_policy_id,o.sandbox_business_account_id,
    pol.auto_approval_limit,pol.hard_purchase_limit,pol.merchant_allowlist,
    a.decision,a.snapshot_hash AS approval_snapshot,
    p.expires_at<=clock_timestamp() AS expired,
    EXISTS(SELECT 1 FROM agentpal.offer_revalidations WHERE purchase_id=p.id
      AND result NOT IN ('passed','source_error')) AS source_invalidated
    FROM agentpal.purchases p JOIN agentpal.owners o ON o.id=p.owner_id
    JOIN agentpal.policies pol ON pol.id=p.policy_id
    LEFT JOIN agentpal.approvals a ON a.purchase_id=p.id WHERE p.id=:id"""


class ReadinessService:
    def __init__(self, engine: Engine, source: Channel3Client, settings: ReadinessSettings):
        self._engine, self._source, self._settings = engine, source, settings

    @staticmethod
    def _arguments(context, purchase_id):
        if not isinstance(context, RunContext) or not isinstance(purchase_id, UUID):
            raise ReadinessError("CONTEXT_REJECTED")

    @staticmethod
    def _purchase(c: Connection, context: RunContext, purchase_id: UUID, *, release=False):
        c.execute(text("SELECT agentpal.lock_finances()"))
        agent = c.scalar(text("""SELECT a.id FROM agentpal.agents a JOIN agentpal.owners o ON o.id=a.owner_id
            WHERE a.id=:agent AND o.login_name=:owner""" + ("" if release else " AND a.enabled") + " FOR SHARE OF a"),
            {"agent": context.agent_id, "owner": context.owner_key})
        row = c.execute(text(_PURCHASE), {"id": purchase_id}).mappings().one_or_none()
        audits = c.execute(text("SELECT details FROM agentpal.audit_events WHERE purchase_id=:id AND kind='proposal_evaluated'"),
                           {"id": purchase_id}).scalars().all()
        binding = audits[0].get("context_hash") if len(audits) == 1 else None
        if (agent is None or row is None or row["agent_id"] != context.agent_id or not isinstance(binding, str)
                or not hmac.compare_digest(binding.encode(), _context_hash(context).encode())):
            raise ReadinessError("CONTEXT_REJECTED")
        return row

    @staticmethod
    def _financial(c, purchase_id):
        return c.execute(text("""SELECT r.period_id,r.status AS reservation_status,r.amount,
            pay.id AS payment_id,pay.status AS payment_status,pay.payment_method_id,
            pay.preparation_revalidation_id,pay.dispatch_started_at,pay.provider_order_id
            FROM agentpal.budget_reservations r LEFT JOIN agentpal.payments pay ON pay.purchase_id=r.purchase_id
            WHERE r.purchase_id=:id FOR UPDATE OF r"""), {"id": purchase_id}).mappings().one_or_none()

    @staticmethod
    def _reasons(p):
        result = []
        if p["initial_decision"] == "rejected": result.append("PROPOSAL_REJECTED")
        if p["expired"]: result.append("PROPOSAL_EXPIRED")
        if p["policy_id"] != p["active_policy_id"]: result.append("POLICY_INVALIDATED")
        if p["merchant_domain"] not in p["merchant_allowlist"]: result.append("MERCHANT_NOT_ALLOWED")
        if p["total_amount"] > p["hard_purchase_limit"]: result.append("HARD_PURCHASE_LIMIT_EXCEEDED")
        if p["decision"] == "denied": result.append("HUMAN_DENIED")
        if p["initial_decision"] == "awaiting_approval" and (p["decision"] != "approved" or p["approval_snapshot"] != p["snapshot_hash"]):
            result.append("HUMAN_APPROVAL_REQUIRED")
        if p["initial_decision"] == "auto_authorized" and p["total_amount"] > p["auto_approval_limit"]:
            result.append("AUTO_AUTHORIZATION_INVALID")
        if (p["source_invalidated"] or p["currency"] != "USD" or p["source_availability"] != "InStock"
                or p["matching_version"] != MATCHING_VERSION or p["matching_offer_count"] != 1):
            result.append("OFFER_INVALIDATED")
        return result

    def _instrument(self, c, p, existing=None):
        if p["sandbox_business_account_id"] != self._settings.merchant_id:
            return None, ["SANDBOX_PAYEE_INVALID"]
        methods = c.execute(text("""SELECT m.id,m.verified_at,s.status,s.verified_at AS setup_verified,
            m.verified_at<=clock_timestamp() AS verification_current
            FROM agentpal.payment_methods m JOIN agentpal.vault_setups s ON s.id=m.vault_setup_id
            WHERE m.owner_id=true AND m.revoked_at IS NULL FOR SHARE OF m,s""")).mappings().all()
        if not methods: return None, ["PAYMENT_METHOD_REQUIRED"]
        if len(methods) != 1: return None, ["PAYMENT_METHOD_AMBIGUOUS"]
        method = methods[0]
        if method["status"] != "verified" or method["verified_at"] != method["setup_verified"] or not method["verification_current"]:
            return None, ["PAYMENT_METHOD_UNVERIFIED"]
        if existing is not None and method["id"] != existing["payment_method_id"]:
            return None, ["PAYMENT_METHOD_CHANGED"]
        return method["id"], []

    @staticmethod
    def _audit(c, p, kind, details, payment_id=None):
        c.execute(text("""INSERT INTO agentpal.audit_events(purchase_id,payment_id,actor_kind,kind,details)
            VALUES (:purchase,:payment,'system',:kind,CAST(:details AS jsonb))"""),
            {"purchase": p["id"], "payment": payment_id, "kind": kind, "details": json.dumps(details)})

    @staticmethod
    def _result(p, state, reasons=(), financial=None, *, replayed=False):
        return ReadinessResult(p["id"], state, tuple(reasons),
            financial["payment_id"] if financial else None, financial["period_id"] if financial else None,
            financial["amount"] if financial else None,
            ("human" if p["initial_decision"] == "awaiting_approval" else "automatic") if financial else None, replayed)

    def _replay(self, c, p, financial):
        activity = financial["payment_id"] is not None and (
            financial["payment_status"] != "prepared" or financial["dispatch_started_at"] is not None
            or financial["provider_order_id"] is not None or c.scalar(text("""SELECT EXISTS(
              SELECT 1 FROM agentpal.payment_operations WHERE payment_id=:id AND
                (dispatch_count>0 OR status<>'prepared' OR first_dispatch_at IS NOT NULL OR idempotency_valid_until IS NOT NULL)
              UNION ALL SELECT 1 FROM agentpal.provider_observations WHERE payment_id=:id
              UNION ALL SELECT 1 FROM agentpal.payment_captures WHERE payment_id=:id)"""), {"id": financial["payment_id"]}))
        if activity:
            return self._result(p, "EXISTING_PAYMENT", ("PAYMENT_RECOVERY_BOUNDARY",), financial, replayed=True)
        if financial["reservation_status"] == "released":
            return self._result(p, "RELEASED_UNSENT", financial=financial, replayed=True)
        if financial["payment_id"] is None or financial["preparation_revalidation_id"] is None:
            return self._result(p, "EXISTING_FINANCIAL_STATE", ("TRUSTED_RECOVERY_REQUIRED",), financial, replayed=True)
        reasons = self._reasons(p)
        _, instrument_errors = self._instrument(c, p, financial)
        reasons.extend(instrument_errors)
        if reasons:
            self._release(c, p, financial, reasons)
            return self._result(p, "RELEASED_UNSENT", reasons, financial, replayed=True)
        return self._result(p, "PREPARED_UNSENT", financial=financial, replayed=True)

    def _preflight(self, context, purchase_id):
        with self._engine.begin() as c:
            p = self._purchase(c, context, purchase_id)
            financial = self._financial(c, purchase_id)
            if financial is not None: return self._replay(c, p, financial)
            reasons = self._reasons(p)
            _, errors = self._instrument(c, p)
            reasons.extend(errors)
            if reasons:
                self._audit(c, p, "readiness_blocked", {"reason_codes": reasons})
                return self._result(p, "BLOCKED", reasons)
            return p

    async def _refresh(self, p):
        selected = Offer(p["product_id"], p["merchant_domain"], p["title"], bytes(p["url_fingerprint"]),
            p["source_condition"], p["unit_price"], p["source_availability"], p["source_retrieved_at"],
            currency=p["currency"], matching_version=p["matching_version"], matching_offer_count=p["matching_offer_count"])
        try:
            product = await self._source.retrieve(selected.product_id)
        except Channel3Error as error:
            return _Check("missing" if error.code == "not_found" else "source_error", datetime.now(timezone.utc), reason=error.code)
        checked = datetime.now(timezone.utc)
        result = compare_offer(selected, product)
        matches = [o for o in product.offers if o.merchant_domain == selected.merchant_domain]
        rejected = [r for r in product.rejected_offers if r.merchant_domain == selected.merchant_domain]
        return _Check(result, checked, matches[0] if len(matches) == 1 else None, len(matches)+len(rejected))

    @staticmethod
    def _record_check(c, p, check):
        latest = check.latest
        return c.scalar(text("""INSERT INTO agentpal.offer_revalidations
            (purchase_id,checked_at,result,product_id,merchant_domain,url_fingerprint,source_condition,
             matching_offer_count,observed_price,currency,availability,reason_code)
            VALUES (:purchase,:checked,:result,:product,:domain,:url,:condition,:count,:price,:currency,:availability,:reason)
            RETURNING id"""), {"purchase": p["id"], "checked": check.checked_at, "result": check.result,
            "product": latest.product_id if latest else None, "domain": latest.merchant_domain if latest else None,
            "url": latest.url_fingerprint if latest else None, "condition": latest.source_condition if latest else None,
            "count": check.count, "price": latest.unit_price if latest else None, "currency": latest.currency if latest else None,
            "availability": latest.source_availability if latest else None, "reason": check.reason or "OFFER_"+check.result.upper()})

    def _prepare(self, context, purchase_id, check):
        with self._engine.begin() as c:
            p = self._purchase(c, context, purchase_id)
            financial = self._financial(c, purchase_id)
            if financial and (financial["dispatch_started_at"] is not None or financial["payment_status"] != "prepared"):
                return self._replay(c, p, financial)
            revalidation = self._record_check(c, p, check)
            if financial:
                return self._replay(c, self._purchase(c, context, purchase_id), financial)
            reasons = self._reasons(p)
            if check.result != "passed": reasons.append("OFFER_" + check.result.upper())
            deadline = check.checked_at + timedelta(seconds=self._settings.max_source_age_seconds)
            if c.scalar(text("SELECT clock_timestamp()>=:deadline"), {"deadline": deadline}): reasons.append("SOURCE_CHECK_STALE")
            method, errors = self._instrument(c, p)
            reasons.extend(errors)
            if reasons:
                self._audit(c, p, "readiness_blocked", {"reason_codes": reasons, "revalidation_id": str(revalidation)})
                return self._result(p, "BLOCKED", reasons)
            period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
            c.execute(text("SELECT id FROM agentpal.budget_periods WHERE id=:id FOR UPDATE"), {"id": period})
            balance = c.execute(text("""SELECT limit_amount-spent_amount-reserved_amount AS available,
                clock_timestamp()>=starts_at AND clock_timestamp()<ends_at AS current
                FROM agentpal.budget_balances WHERE id=:id"""), {"id": period}).mappings().one()
            if not balance["current"]: raise ReadinessError("BUDGET_PERIOD_CHANGED")
            if p["total_amount"] > balance["available"]:
                self._audit(c, p, "readiness_blocked", {"reason_codes": ["INSUFFICIENT_BUDGET"], "revalidation_id": str(revalidation)})
                return self._result(p, "BLOCKED", ("INSUFFICIENT_BUDGET",))
            c.execute(text("INSERT INTO agentpal.budget_reservations(purchase_id,period_id,amount) VALUES (:id,:period,:amount)"),
                      {"id": purchase_id, "period": period, "amount": p["total_amount"]})
            payment_id = c.scalar(text("""INSERT INTO agentpal.payments
                (purchase_id,payment_method_id,sandbox_payee,preparation_revalidation_id,preparation_check_valid_until)
                VALUES (:id,:method,:payee,:check,:deadline) RETURNING id"""),
                {"id": purchase_id, "method": method, "payee": self._settings.merchant_id, "check": revalidation, "deadline": deadline})
            self._audit(c, p, "payment_prepared", {"snapshot_hash": bytes(p["snapshot_hash"]).hex(),
                "policy_id": str(p["policy_id"]), "revalidation_id": str(revalidation), "period_id": str(period),
                "authorization_kind": "human" if p["initial_decision"] == "awaiting_approval" else "automatic",
                "amount": format(p["total_amount"], "f"), "currency": "USD"}, payment_id)
            result = self._result(p, "PREPARED_UNSENT", financial=self._financial(c, purchase_id))
        return result  # Deferred eligibility checks and commit have succeeded.

    def _release(self, c, p, financial, reasons):
        if financial["payment_id"] is not None:
            c.execute(text("SELECT agentpal.assert_definitely_unsent(:id)"), {"id": financial["payment_id"]})
        c.execute(text("UPDATE agentpal.budget_reservations SET status='released' WHERE purchase_id=:id"), {"id": p["id"]})
        self._audit(c, p, "preparation_released_unsent", {"reason_codes": list(reasons)}, financial["payment_id"])

    def release_unsent(self, context: RunContext, purchase_id: UUID) -> ReadinessResult:
        """Trusted abandonment/recovery only; no browser session/AI authority."""
        self._arguments(context, purchase_id)
        try:
            with self._engine.begin() as c:
                p = self._purchase(c, context, purchase_id, release=True)
                financial = self._financial(c, purchase_id)
                if financial is None: return self._result(p, "NOT_PREPARED")
                if financial["reservation_status"] == "released":
                    if financial["payment_id"] is not None:
                        c.execute(text("SELECT agentpal.assert_definitely_unsent(:id)"), {"id": financial["payment_id"]})
                    return self._result(p, "RELEASED_UNSENT", financial=financial, replayed=True)
                if financial["reservation_status"] != "held": raise ReadinessError("PAYMENT_RECOVERY_REQUIRED")
                self._release(c, p, financial, ("ABANDONED_UNSENT",))
                return self._result(p, "RELEASED_UNSENT", ("ABANDONED_UNSENT",), financial)
        except SQLAlchemyError as error:
            self._storage_error(error)

    @staticmethod
    def _storage_error(error):
        state = getattr(error.orig, "sqlstate", None) if isinstance(error, DBAPIError) else None
        code = "TRANSACTION_CONFLICT" if state in ("40001", "40P01", "23505") else (
            "READINESS_CONFLICT" if state == "23514" else "READINESS_STORAGE_UNAVAILABLE")
        raise ReadinessError(code) from None

    async def prepare(self, context: RunContext, purchase_id: UUID) -> ReadinessResult:
        self._arguments(context, purchase_id)
        try:
            prior = await asyncio.to_thread(self._preflight, context, purchase_id)
            if isinstance(prior, ReadinessResult): return prior
            check = await self._refresh(prior)
            return await asyncio.to_thread(self._prepare, context, purchase_id, check)
        except SQLAlchemyError as error:
            self._storage_error(error)
