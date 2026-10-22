"""Short database transactions around durable provider claims and observations."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hmac
import json
import secrets
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import text

from agentpal.auth.service import AuthService, token_hash, valid_token
from agentpal.vault.agreement import TERMS, conflicts, evaluate, stored_state
from agentpal.vault.paypal import PayPal, approval_url
from agentpal.vault.security import Keyring, VaultSettings, VaultUnavailable


@dataclass(frozen=True)
class Authority:
    session_token: str | None = field(repr=False)
    csrf: str | None = field(repr=False)


def conflict(code="onboarding_conflict"):
    return HTTPException(409, {"code": code, "message": "Inspect the connection status before continuing"})


class VaultService:
    def __init__(self, auth: AuthService, settings: VaultSettings, provider: PayPal | None = None):
        self.auth, self.settings = auth, settings
        self.keys, self.provider = Keyring(settings), provider or PayPal(settings)

    @contextmanager
    def owner_transaction(self, authority: Authority):
        with self.auth.engine.begin() as c:
            owner = self.auth.session(c, authority.session_token, lock=True)
            if owner is None:
                raise HTTPException(401, "Authentication required")
            if not self.auth.csrf_matches(owner, authority.csrf):
                raise HTTPException(403, "CSRF validation failed")
            yield c, owner

    @staticmethod
    def row(c, identifier):
        row = c.execute(text("""SELECT *,expires_at<=clock_timestamp() AS expired
            FROM agentpal.vault_setups WHERE id=:id AND flow_phase IS NOT NULL FOR UPDATE"""),
                        {"id": identifier}).mappings().one_or_none()
        if row is None:
            raise HTTPException(404, "Onboarding not found")
        return row

    @staticmethod
    def same_session(row, owner):
        if row["owner_session_id"] != owner.session_id:
            raise conflict("original_session_required")

    def decode(self, row, purpose):
        names = {"flow": ("flow_ciphertext", "flow_key_id"),
                 "setup": ("setup_reference_ciphertext", "encryption_key_id"),
                 "token": ("token_ciphertext", "token_key_id"),
                 "approved-setup": ("approved_reference_ciphertext", "approved_key_id"),
                 "usage-agreement": ("agreement_evidence_ciphertext", "agreement_evidence_key_id")}
        cipher, key = names[purpose]
        return self.keys.open(row["id"], purpose, row[cipher], row[key])

    @staticmethod
    def view(row, *, url=None):
        phase = row["flow_phase"]
        state = {"setup_post": "review_required", "token_post": "review_required",
                 "review": "review_required", "returned": "completion_required",
                 "verifying_token": "verification_required"}.get(phase, phase)
        if row["expired"] and phase in ("awaiting_payer", "returned"):
            state = "expired"
        elif (row["expired"] and phase == "verifying_token") or (
                row.get("origin_session_active") is False and phase not in ("complete", "canceled", "expired")):
            state = "review_required"
        result = {"onboarding_id": str(row["id"]), "state": state,
                  "expires_at": row["expires_at"].isoformat(), "agreement_state": "unverified",
                  "replacement": row["replaces_payment_method_id"] is not None}
        if url is not None:
            result["approval_url"] = url
        return result

    def completed_view(self, c, row):
        result = self.view(row)
        if row["agreement_evidence_ciphertext"] is not None:
            method = c.execute(text("SELECT reference_ciphertext,encryption_key_id FROM agentpal.payment_methods WHERE vault_setup_id=:id"),
                {"id": row["id"]}).mappings().one()
            reference = self.keys.open(row["id"], "payment-method", method["reference_ciphertext"], method["encryption_key_id"])
            result["agreement_state"] = stored_state(self.keys, row, reference)
        return result

    def status(self):
        with self.auth.engine.connect() as c:
            active = c.execute(text("""SELECT s.*,s.expires_at<=clock_timestamp() AS expired FROM agentpal.payment_methods m JOIN agentpal.vault_setups s
                ON s.id=m.vault_setup_id WHERE m.revoked_at IS NULL""")).mappings().all()
            row = c.execute(text("""SELECT v.*,v.expires_at<=clock_timestamp() AS expired,
                s.revoked_at IS NULL AND s.expires_at>clock_timestamp() AS origin_session_active
                FROM agentpal.vault_setups v JOIN agentpal.owner_sessions s ON s.id=v.owner_session_id
                WHERE v.flow_phase IS NOT NULL ORDER BY v.created_at DESC LIMIT 1""")).mappings().one_or_none()
            result = {"connected": bool(active), "onboarding": (
                self.completed_view(c, row) if row["flow_phase"] == "complete" else self.view(row)) if row else None}
            if active:
                result["agreement_state"] = self.completed_view(c, active[0])["agreement_state"] if len(active) == 1 else "unverified"
            return result

    def start(self, authority: Authority, *, replace=False):
        with self.owner_transaction(authority) as (c, owner):
            active = c.execute(text("SELECT id FROM agentpal.payment_methods WHERE revoked_at IS NULL FOR SHARE")).scalars().all()
            if active and not replace:
                raise conflict("already_connected")
            if replace and len(active) != 1:
                raise conflict("replacement_target_required")
            target = active[0] if replace else None
            if target is not None:
                previous = c.execute(text("""SELECT s.*,s.expires_at<=clock_timestamp() AS expired FROM agentpal.vault_setups s JOIN agentpal.payment_methods m
                    ON m.vault_setup_id=s.id WHERE m.id=:id"""), {"id": target}).mappings().one()
                if self.completed_view(c, previous)["agreement_state"] == "verified":
                    raise conflict("agreement_already_verified")
            old = c.execute(text("""SELECT *,expires_at<=clock_timestamp() AS expired FROM agentpal.vault_setups
                WHERE flow_phase NOT IN ('complete','canceled','expired') FOR UPDATE""")).mappings().one_or_none()
            if old:
                if old["expired"] and old["flow_phase"] in ("awaiting_payer", "returned"):
                    c.execute(text("UPDATE agentpal.vault_setups SET flow_phase='expired',status='failed' WHERE id=:id"), {"id": old["id"]})
                else:
                    self.same_session(old, owner)
                    if old["replaces_payment_method_id"] != target:
                        raise conflict()
                    url = None
                    if old["flow_phase"] == "awaiting_payer":
                        setup = self.decode(old, "setup")
                        url = approval_url(setup["approval_url"], setup["id"])
                    return self.view(old, url=url)
            identifier, state = uuid4(), secrets.token_urlsafe(32)
            origin = self.auth.settings.origin
            payload = {"payment_source": {"paypal": {
                **TERMS, "permit_multiple_payment_tokens": True,
                "experience_context": {
                    "return_url": f"{origin}/paypal/vault/return?state={state}",
                    "cancel_url": f"{origin}/paypal/vault/cancel?state={state}"}}}}
            cipher, key = self.keys.seal(identifier, "flow", payload)
            setup_request_id, token_request_id = uuid4(), uuid4()
            c.execute(text("""INSERT INTO agentpal.vault_setups
                (id,owner_session_id,state_hash,expires_at,flow_phase,flow_ciphertext,flow_key_id,setup_request_id,token_request_id,replaces_payment_method_id)
                VALUES (:id,:session,:hash,clock_timestamp()+:ttl*interval '1 second','setup_post',:cipher,:key,:setup_request,:token_request,:target)"""),
                {"id": identifier, "session": owner.session_id, "hash": token_hash(state, "vault-state"),
                 "ttl": self.settings.ttl_seconds, "cipher": cipher, "key": key,
                 "setup_request": setup_request_id, "token_request": token_request_id, "target": target})
            c.execute(text("""INSERT INTO agentpal.audit_events(actor_kind,owner_session_id,kind,details)
                VALUES ('owner',:session,'vault_started',CAST(:details AS jsonb))"""),
                {"session": owner.session_id, "details": json.dumps({"onboarding_id": str(identifier)})})
        # The durable claim is committed before any provider I/O. No retry of a
        # provider POST, even after a crash or a response lost to this process.
        try:
            setup = self.provider.setup(payload, setup_request_id)
            cipher, key = self.keys.seal(identifier, "setup", setup)
        except VaultUnavailable:
            self.review(identifier, "setup_post")
            return self.operation(authority, identifier)
        with self.auth.engine.begin() as c:
            c.execute(text("SELECT id FROM agentpal.owners WHERE id=true FOR UPDATE"))
            fresh = self.fresh_setup(c, identifier, setup)
            c.execute(text("""UPDATE agentpal.vault_setups SET flow_phase=:phase,status='pending',
                setup_reference_ciphertext=:cipher,encryption_key_id=:key WHERE id=:id AND flow_phase='setup_post'"""),
                {"cipher": cipher, "key": key, "id": identifier,
                 "phase": "review" if conflicts(setup) or not fresh else "awaiting_payer"})
        # Recheck the original session after network I/O; observation durability
        # never depends on a still-live browser, but exposing/activating it does.
        return self.operation(authority, identifier, include_url=True)

    def operation(self, authority, identifier, *, include_url=False):
        with self.owner_transaction(authority) as (c, owner):
            row = self.row(c, identifier)
            self.same_session(row, owner)
            url = None
            if include_url and row["flow_phase"] == "awaiting_payer" and not row["expired"]:
                setup = self.decode(row, "setup")
                url = approval_url(setup["approval_url"], setup["id"])
            result = self.completed_view(c, row) if row["flow_phase"] == "complete" else self.view(row, url=url)
            flow = self.decode(row, "flow")
            if all(flow.get("payment_source", {}).get("paypal", {}).get(k) == v for k, v in TERMS.items()):
                result["requested_agreement"] = dict(TERMS)
            return result

    def review(self, identifier, expected):
        with self.auth.engine.begin() as c:
            c.execute(text("SELECT id FROM agentpal.owners WHERE id=true FOR UPDATE"))
            c.execute(text("UPDATE agentpal.vault_setups SET flow_phase='review',status='pending' WHERE id=:id AND flow_phase=:phase"),
                      {"id": identifier, "phase": expected})

    def callback(self, params: list[tuple[str, str]], *, canceled: bool):
        # Provider callback parameters confer no owner authority. State locates
        # only the pending flow; every supplied provider ID must match our record.
        values = dict(params)
        if (len(values) != len(params) or set(values) - {"state", "approval_token_id", "approval_session_id", "token"}
                or not valid_token(values.get("state"))):
            raise HTTPException(400, "Invalid PayPal return")
        with self.auth.engine.begin() as c:
            c.execute(text("SELECT id FROM agentpal.owners WHERE id=true FOR UPDATE"))
            row = c.execute(text("""SELECT v.*,v.expires_at<=clock_timestamp() AS expired FROM agentpal.vault_setups v
                JOIN agentpal.owner_sessions s ON s.id=v.owner_session_id
                WHERE v.state_hash=:hash AND v.flow_phase='awaiting_payer' AND s.revoked_at IS NULL
                AND s.expires_at>clock_timestamp() FOR UPDATE OF v"""),
                {"hash": token_hash(values["state"], "vault-state")}).mappings().one_or_none()
            if row is None or row["expired"]:
                raise HTTPException(400, "Invalid PayPal return")
            setup = self.decode(row, "setup")
            if any(value != setup["id"] for name, value in params if name != "state"):
                raise HTTPException(400, "Invalid PayPal return")
            receipt = secrets.token_urlsafe(32)
            c.execute(text("""UPDATE agentpal.vault_setups SET flow_phase=:phase,status=:status,
                receipt_hash=:receipt,returned_at=clock_timestamp() WHERE id=:id"""),
                {"id": row["id"], "phase": "canceled" if canceled else "returned",
                 "status": "canceled" if canceled else "pending", "receipt": token_hash(receipt, "vault-receipt")})
            return receipt

    @staticmethod
    def check_receipt(row, receipt):
        if (not valid_token(receipt) or row["receipt_hash"] is None or
                not hmac.compare_digest(row["receipt_hash"], token_hash(receipt, "vault-receipt"))):
            raise HTTPException(403, "PayPal browser return required")

    @staticmethod
    def check_target(c, row):
        active = c.execute(text("SELECT id FROM agentpal.payment_methods WHERE revoked_at IS NULL FOR SHARE")).scalars().all()
        target = row["replaces_payment_method_id"]
        if (target is None and active) or (target is not None and active != [target]):
            raise conflict("replacement_target_changed" if target else "already_connected")

    def fresh_reference(self, c, token):
        # Even a successful new consent must not silently reuse a historical token.
        # Keep old keys: unreadable history cannot prove a fresh reference.
        for old in c.execute(text("SELECT vault_setup_id,reference_ciphertext,encryption_key_id FROM agentpal.payment_methods")).mappings():
            reference = self.keys.open(old["vault_setup_id"], "payment-method", old["reference_ciphertext"], old["encryption_key_id"])
            if reference.get("id") == token["id"]:
                return False
        return True

    def fresh_setup(self, c, identifier, setup):
        for old in c.execute(text("""SELECT id,setup_reference_ciphertext,encryption_key_id FROM agentpal.vault_setups
                WHERE id<>:id AND setup_reference_ciphertext IS NOT NULL"""), {"id": identifier}).mappings():
            try:
                previous = self.keys.open(old["id"], "setup", old["setup_reference_ciphertext"], old["encryption_key_id"])
            except VaultUnavailable:
                return False
            if previous.get("id") == setup["id"]:
                return False
        return True

    def complete(self, authority: Authority, identifier: UUID, receipt: str | None):
        with self.owner_transaction(authority) as (c, owner):
            row = self.row(c, identifier)
            self.same_session(row, owner)
            if row["flow_phase"] == "complete":
                return self.completed_view(c, row)
            self.check_receipt(row, receipt)
            phase = row["flow_phase"]
            if phase not in ("returned", "verifying_token"):
                return self.view(row)
            if row["expired"]:
                # A known payment token remains protected, never discarded or
                # replaced because a browser deadline elapsed after its creation.
                c.execute(text("UPDATE agentpal.vault_setups SET flow_phase=:phase,status=:status WHERE id=:id"),
                          {"id": identifier, "phase": "review" if phase == "verifying_token" else "expired",
                           "status": "pending" if phase == "verifying_token" else "failed"})
                return self.view(self.row(c, identifier))
            setup = self.decode(row, "setup")
            token = self.decode(row, "token") if phase == "verifying_token" else None
        if token is None:
            try:
                approved = self.provider.approved(setup)
            except VaultUnavailable:
                # Safe, read-only GET may be repeated by the authenticated owner.
                return self.operation(authority, identifier)
            with self.owner_transaction(authority) as (c, owner):
                row = self.row(c, identifier)
                self.same_session(row, owner)
                if row["flow_phase"] != "returned" or row["expired"]:
                    return self.view(row)
                self.check_target(c, row)
                cipher, key = self.keys.seal(identifier, "approved-setup", approved)
                c.execute(text("""UPDATE agentpal.vault_setups SET flow_phase=:phase,
                    approved_reference_ciphertext=:cipher,approved_key_id=:key WHERE id=:id"""),
                    {"id": identifier, "cipher": cipher, "key": key, "phase": "review" if conflicts(approved) else "token_post"})
                if conflicts(approved):
                    return self.view(self.row(c, identifier))
                request_id = row["token_request_id"]
            try:
                token = self.provider.tokenize(setup, request_id)
                cipher, key = self.keys.seal(identifier, "token", token)
            except VaultUnavailable:
                self.review(identifier, "token_post")
                return self.operation(authority, identifier)
            with self.auth.engine.begin() as c:
                c.execute(text("SELECT id FROM agentpal.owners WHERE id=true FOR UPDATE"))
                c.execute(text("""UPDATE agentpal.vault_setups SET flow_phase=:phase,token_ciphertext=:cipher,
                    token_key_id=:key WHERE id=:id AND flow_phase='token_post'"""),
                    {"id": identifier, "cipher": cipher, "key": key, "phase": "review" if conflicts(token) else "verifying_token"})
            if conflicts(token):
                return self.operation(authority, identifier)
        try:
            retrieved = self.provider.verify(token, setup)
        except VaultUnavailable:
            return self.operation(authority, identifier)
        with self.owner_transaction(authority) as (c, owner):
            row = self.row(c, identifier)
            self.same_session(row, owner)
            if row["flow_phase"] == "complete":
                return self.completed_view(c, row)
            if row["flow_phase"] != "verifying_token":
                raise conflict()
            if row["expired"]:
                c.execute(text("UPDATE agentpal.vault_setups SET flow_phase='review' WHERE id=:id"), {"id": identifier})
                return {**self.view(row), "state": "review_required"}
            self.check_target(c, row)
            approved = self.decode(row, "approved-setup") if row["approved_reference_ciphertext"] is not None else {}
            token = self.decode(row, "token")
            agreement = evaluate(self.decode(row, "flow"), self.decode(row, "setup"), approved, token, retrieved)
            if agreement == "verified" and not self.fresh_reference(c, token):
                agreement = "conflict"
            cipher, key = self.keys.seal(identifier, "usage-agreement", {"version": 1, "payment_token": retrieved})
            if agreement == "conflict" or (row["replaces_payment_method_id"] is not None and agreement != "verified"):
                c.execute(text("""UPDATE agentpal.vault_setups SET flow_phase='review',agreement_evidence_ciphertext=:cipher,
                    agreement_evidence_key_id=:key WHERE id=:id"""), {"id": identifier, "cipher": cipher, "key": key})
                return self.view(self.row(c, identifier))
            verified_at = c.scalar(text("""UPDATE agentpal.vault_setups SET flow_phase='complete',status='verified',
                verified_at=clock_timestamp(),agreement_evidence_ciphertext=:cipher,agreement_evidence_key_id=:key
                WHERE id=:id RETURNING verified_at"""), {"id": identifier, "cipher": cipher, "key": key})
            if row["replaces_payment_method_id"] is not None:
                c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp() WHERE id=:id"),
                    {"id": row["replaces_payment_method_id"]})
            # Separate AAD purpose prevents a setup/recovery ciphertext from
            # being substituted for a usable payment-method reference.
            cipher, key = self.keys.seal(identifier, "payment-method", token)
            c.execute(text("""INSERT INTO agentpal.payment_methods(vault_setup_id,reference_ciphertext,encryption_key_id,verified_at)
                VALUES (:id,:cipher,:key,:verified)"""), {"id": identifier, "cipher": cipher, "key": key, "verified": verified_at})
            c.execute(text("""INSERT INTO agentpal.audit_events(actor_kind,owner_session_id,kind,details)
                VALUES ('owner',:session,'vault_connected',CAST(:details AS jsonb))"""),
                {"session": owner.session_id, "details": json.dumps({"onboarding_id": str(identifier),
                    "agreement_state": agreement, "replacement": row["replaces_payment_method_id"] is not None})})
            return self.completed_view(c, self.row(c, identifier))
