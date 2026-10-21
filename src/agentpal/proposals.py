"""Initial proposal decisions only: no human approval, reservation or payment.

The trusted runtime closes over RunContext. AI tools supply only ref + quantity.
Network I/O is always between short database scopes; synchronous DB work is moved
off the async event loop. Existing PostgreSQL guards compute immutable bindings.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import hmac
import json
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, DataError, SQLAlchemyError

from agentpal.auth.service import OwnerSession
from agentpal.channel3 import MATCHING_VERSION, Offer, exact_usd_price
from agentpal.discovery import DiscoveryService, InvalidOfferReference, RunContext, offer_reference_hash


_DECISIONS = {"REJECTED": "rejected", "AWAITING_HUMAN_APPROVAL": "awaiting_approval", "AUTO_AUTHORIZED": "auto_authorized"}
_PUBLIC_DECISIONS = {value: key for key, value in _DECISIONS.items()}


class ProposalError(Exception):
    """Fixed machine codes; never include source text, SQL or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RuleCheck:
    code: str
    passed: bool


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    decision: str
    reason_codes: tuple[str, ...]
    checks: tuple[RuleCheck, ...]


def evaluate_policy(*, merchant_domain: str, total: Decimal, merchant_allowlist: tuple[str, ...],
                    hard_limit: Decimal, auto_limit: Decimal, available_budget: Decimal) -> PolicyEvaluation:
    """Pure evaluation over trusted exact inputs; not an authorization endpoint."""
    checks = (
        RuleCheck("MERCHANT_ALLOWLIST", merchant_domain in merchant_allowlist),
        RuleCheck("HARD_PURCHASE_LIMIT", total <= hard_limit),
        RuleCheck("MONTHLY_BUDGET", total <= available_budget),
    )
    failures = tuple(reason for reason, check in zip(
        ("MERCHANT_NOT_ALLOWED", "HARD_PURCHASE_LIMIT_EXCEEDED", "INSUFFICIENT_BUDGET"), checks) if not check.passed)
    automatic = total <= auto_limit
    trace = checks + (RuleCheck("AUTO_APPROVAL_THRESHOLD", automatic),)
    if failures:
        return PolicyEvaluation("REJECTED", failures, trace)
    return PolicyEvaluation("AUTO_AUTHORIZED" if automatic else "AWAITING_HUMAN_APPROVAL",
                            ("AUTO_APPROVAL_THRESHOLD_MET" if automatic else "HUMAN_APPROVAL_REQUIRED",), trace)


def exact_total(unit_price: Decimal, quantity: int) -> Decimal:
    if type(quantity) is not int or not 1 <= quantity <= 9223372036854775807:
        raise ProposalError("INVALID_QUANTITY")
    try:
        price = exact_usd_price(unit_price)
    except ValueError:
        raise ProposalError("OFFER_UNSUPPORTED") from None
    # Isolate multiplication from the caller's default Decimal precision (28).
    with localcontext() as ctx:
        ctx.prec = len(price.as_tuple().digits) + len(str(quantity)) + 2
        return price * quantity


def _context_hash(context: RunContext) -> str:
    data = json.dumps([context.owner_key, str(context.agent_id), str(context.run_id)], separators=(",", ":"))
    return hashlib.sha256(b"agentpal-proposal-context-v1\0" + data.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Proposal:
    id: UUID
    agent_id: UUID
    policy_id: UUID
    offer: Offer
    quantity: int
    total_amount: Decimal
    initial_decision: str
    reason_codes: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    snapshot_hash: bytes = field(repr=False)
    snapshot_hash_version: int
    expired: bool
    policy_current: bool

    def public(self) -> dict:
        return dict(proposal_id=str(self.id), policy_id=str(self.policy_id),
                    product_id=self.offer.product_id, merchant_domain=self.offer.merchant_domain,
                    title=self.offer.title, unit_price=format(self.offer.unit_price, "f"),
                    quantity=self.quantity, total_amount=format(self.total_amount, "f"), currency="USD",
                    initial_decision=self.initial_decision, reason_codes=list(self.reason_codes),
                    created_at=_utc(self.created_at), expires_at=_utc(self.expires_at),
                    expired=self.expired, policy_current=self.policy_current)


@dataclass(frozen=True, slots=True)
class ProposalResult:
    proposal: Proposal
    replayed: bool

    def public(self) -> dict:
        return self.proposal.public() | {"replayed": self.replayed}


_COLUMNS = """p.id,p.agent_id,p.policy_id,p.product_id,p.merchant_domain,p.title,
    p.url_fingerprint,p.source_condition,p.matching_version,p.matching_offer_count,
    p.unit_price,p.quantity,p.total_amount,p.currency,p.source_availability,p.source_retrieved_at,
    p.initial_decision,p.reason_codes,p.created_at,p.expires_at,p.snapshot_hash,p.snapshot_hash_version,
    p.expires_at<=clock_timestamp() AS expired,
    coalesce(p.policy_id=o.active_policy_id,false) AS policy_current"""


class ProposalService:
    def __init__(self, engine: Engine, discovery: DiscoveryService):
        self._engine, self._discovery = engine, discovery

    def start_run(self, owner: OwnerSession, agent_id: UUID) -> RunContext:
        """Trusted launcher passes an already authenticated OwnerRead result.

        Recheck the active session and agent; never give this factory, the owner
        session or arbitrary identity arguments to the AI tool schema.
        """
        if not isinstance(owner, OwnerSession) or not isinstance(agent_id, UUID):
            raise ProposalError("CONTEXT_REJECTED")
        try:
            with self._engine.connect() as c:
                valid = c.scalar(text("""SELECT EXISTS(SELECT 1 FROM agentpal.owner_sessions s
                    JOIN agentpal.owners o ON o.id=s.owner_id
                    JOIN agentpal.agents a ON a.owner_id=o.id
                    WHERE s.id=:session AND s.csrf_token_hash=:csrf AND o.login_name=:owner
                      AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
                      AND a.id=:agent AND a.enabled)"""),
                    {"session": owner.session_id, "csrf": owner.csrf_hash, "owner": owner.login_name, "agent": agent_id})
                if not valid:
                    raise ProposalError("CONTEXT_REJECTED")
            return RunContext(owner.login_name, agent_id, uuid4())
        except SQLAlchemyError:
            raise ProposalError("PROPOSAL_STORAGE_UNAVAILABLE") from None

    @staticmethod
    def _check_context(c: Connection, context: RunContext, *, lock: bool = False) -> None:
        valid = c.scalar(text("""SELECT a.id FROM agentpal.agents a JOIN agentpal.owners o ON o.id=a.owner_id
            WHERE o.id=true AND o.login_name=:owner AND a.id=:agent AND a.enabled"""
            + (" FOR SHARE OF a" if lock else "")), {"owner": context.owner_key, "agent": context.agent_id})
        if valid is None:
            raise ProposalError("CONTEXT_REJECTED")

    @staticmethod
    def _existing(c: Connection, context: RunContext, digest: bytes, quantity: int) -> Proposal | None:
        row = c.execute(text("SELECT " + _COLUMNS + """ FROM agentpal.purchases p
            JOIN agentpal.owners o ON o.id=p.owner_id WHERE p.origin_ref_hash=:digest"""), {"digest": digest}).mappings().one_or_none()
        if row is None:
            return None
        # The existing append-only audit table stores run provenance plus exact
        # evaluation evidence, atomically with the unique purchase origin. Old
        # rows without this binding cannot be adopted by guessing a reference.
        audits = c.execute(text("""SELECT details FROM agentpal.audit_events
            WHERE purchase_id=:id AND kind='proposal_evaluated'"""), {"id": row["id"]}).scalars().all()
        binding = audits[0].get("context_hash") if len(audits) == 1 else None
        if (row["agent_id"] != context.agent_id or not isinstance(binding, str)
                or not hmac.compare_digest(binding.encode("utf-8"), _context_hash(context).encode("ascii"))):
            raise InvalidOfferReference()
        if row["quantity"] != quantity:
            raise ProposalError("REFERENCE_QUANTITY_CONFLICT")
        observed = Offer(row["product_id"], row["merchant_domain"], row["title"], bytes(row["url_fingerprint"]),
                         row["source_condition"], row["unit_price"], row["source_availability"], row["source_retrieved_at"],
                         currency=row["currency"], matching_version=row["matching_version"],
                         matching_offer_count=row["matching_offer_count"])
        return Proposal(row["id"], row["agent_id"], row["policy_id"], observed, row["quantity"], row["total_amount"],
                        _PUBLIC_DECISIONS[row["initial_decision"]], tuple(row["reason_codes"]), row["created_at"],
                        row["expires_at"], bytes(row["snapshot_hash"]), row["snapshot_hash_version"],
                        row["expired"], row["policy_current"])

    def _lookup(self, context: RunContext, digest: bytes, quantity: int, *, require_policy: bool = False) -> Proposal | None:
        with self._engine.connect() as c:
            self._check_context(c, context)
            existing = self._existing(c, context, digest, quantity)
            if existing is None and require_policy and c.scalar(text("SELECT active_policy_id FROM agentpal.owners WHERE id=true")) is None:
                raise ProposalError("POLICY_SETUP_REQUIRED")
            return existing

    def _record(self, context: RunContext, reference: str, digest: bytes, quantity: int, observed: Offer) -> ProposalResult:
        with self._engine.begin() as c:
            c.execute(text("SELECT agentpal.lock_finances()"))
            self._check_context(c, context, lock=True)
            existing = self._existing(c, context, digest, quantity)
            if existing is not None:
                return ProposalResult(existing, True)
            # A lock wait must not allow an expired/canceled selection to commit.
            self._discovery.resolve(context, reference)
            policy = c.execute(text("""SELECT p.* FROM agentpal.policies p
                JOIN agentpal.owners o ON o.active_policy_id=p.id AND o.id=p.owner_id WHERE o.id=true""")).mappings().one_or_none()
            if policy is None:
                raise ProposalError("POLICY_SETUP_REQUIRED")
            total = exact_total(observed.unit_price, quantity)
            period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
            budget = c.execute(text("""SELECT id,starts_at,ends_at,limit_amount,spent_amount,reserved_amount,
                limit_amount-spent_amount-reserved_amount AS available_amount,clock_timestamp() AS observed_at
                FROM agentpal.budget_balances WHERE id=:id"""), {"id": period}).mappings().one()
            created_at = budget["observed_at"]
            if not budget["starts_at"] <= created_at < budget["ends_at"]:
                raise ProposalError("BUDGET_PERIOD_CHANGED")
            if observed.retrieved_at > created_at:
                raise ProposalError("SOURCE_TIMESTAMP_INVALID")
            try:
                representable = c.scalar(text("""SELECT isfinite(expiry) AND expiry < timestamptz '10000-01-01 00:00:00+00'
                    FROM (SELECT CAST(:created AS timestamptz)+:ttl*interval '1 second' AS expiry) x"""),
                    {"created": created_at, "ttl": policy["purchase_ttl_seconds"]})
            except DataError:
                raise ProposalError("POLICY_EXPIRY_UNSUPPORTED") from None
            if not representable:
                raise ProposalError("POLICY_EXPIRY_UNSUPPORTED")
            evaluation = evaluate_policy(merchant_domain=observed.merchant_domain, total=total,
                merchant_allowlist=tuple(policy["merchant_allowlist"]), hard_limit=policy["hard_purchase_limit"],
                auto_limit=policy["auto_approval_limit"], available_budget=budget["available_amount"])
            purchase_id = c.scalar(text("""INSERT INTO agentpal.purchases
                (agent_id,origin_ref_hash,policy_id,provider,product_id,merchant_domain,title,url_fingerprint,
                 source_condition,matching_version,matching_offer_count,unit_price,quantity,currency,
                 source_availability,source_retrieved_at,initial_decision,reason_codes,created_at)
                VALUES (:agent,:digest,:policy,'channel3',:product,:domain,:title,:url,:condition,:version,
                        1,:price,:quantity,'USD','InStock',:retrieved,:decision,:reasons,:created) RETURNING id"""),
                {"agent": context.agent_id, "digest": digest, "policy": policy["id"], "product": observed.product_id,
                 "domain": observed.merchant_domain, "title": observed.title, "url": observed.url_fingerprint,
                 "condition": observed.source_condition, "version": observed.matching_version, "price": observed.unit_price,
                 "quantity": quantity, "retrieved": observed.retrieved_at, "decision": _DECISIONS[evaluation.decision],
                 "reasons": list(evaluation.reason_codes), "created": created_at})
            details = dict(evaluation_version=1, context_hash=_context_hash(context), run_id=str(context.run_id),
                           budget_period_id=str(period), observed_at=_utc(created_at), source_verification="passed",
                           total_amount=format(total, "f"), checks=[dict(code=rule.code, passed=rule.passed) for rule in evaluation.checks],
                           budget={name: format(budget[name], "f") for name in
                                   ("limit_amount", "spent_amount", "reserved_amount", "available_amount")})
            c.execute(text("""INSERT INTO agentpal.audit_events(purchase_id,actor_kind,agent_id,kind,details)
                VALUES (:id,'agent',:agent,'proposal_evaluated',CAST(:details AS jsonb))"""),
                {"id": purchase_id, "agent": context.agent_id, "details": json.dumps(details, separators=(",", ":"))})
            proposal = self._existing(c, context, digest, quantity)
        # Returning happens only after commit, including deferred constraints.
        return ProposalResult(proposal, False)

    async def create(self, context: RunContext, offer_ref: str, quantity: int) -> ProposalResult:
        if not isinstance(context, RunContext):
            raise ProposalError("CONTEXT_REJECTED")
        digest = offer_reference_hash(offer_ref)
        exact_total(Decimal(0), quantity)  # Strict BIGINT quantity validation before any I/O.
        try:
            existing = await asyncio.to_thread(self._lookup, context, digest, quantity, require_policy=True)
            if existing is not None:
                return ProposalResult(existing, True)
            try:
                self._discovery.resolve(context, offer_ref)
                refresh = await self._discovery.refresh(context, offer_ref)
            except InvalidOfferReference:
                existing = await asyncio.to_thread(self._lookup, context, digest, quantity)
                if existing is not None:
                    return ProposalResult(existing, True)
                raise
            if refresh.status != "passed" or refresh.latest is None:
                existing = await asyncio.to_thread(self._lookup, context, digest, quantity)
                if existing is not None:
                    return ProposalResult(existing, True)
                raise ProposalError("OFFER_" + refresh.status.upper())
            if (refresh.latest.currency != "USD" or refresh.latest.source_availability != "InStock"
                    or refresh.latest.matching_offer_count != 1 or refresh.latest.matching_version != MATCHING_VERSION):
                raise ProposalError("OFFER_UNSUPPORTED")
            return await asyncio.to_thread(self._record, context, offer_ref, digest, quantity, refresh.latest)
        except SQLAlchemyError as error:
            state = getattr(error.orig, "sqlstate", None) if isinstance(error, DBAPIError) else None
            unique_origin = state == "23505" and getattr(getattr(error.orig, "diag", None), "constraint_name", None) == "purchases_origin_ref_hash_key"
            if state in ("40001", "40P01") or unique_origin:
                try:
                    existing = await asyncio.to_thread(self._lookup, context, digest, quantity)
                except SQLAlchemyError:
                    raise ProposalError("PROPOSAL_STORAGE_UNAVAILABLE") from None
                if existing is not None:
                    return ProposalResult(existing, True)
                raise ProposalError("TRANSACTION_CONFLICT") from None
            raise ProposalError("PROPOSAL_STORAGE_UNAVAILABLE") from None
