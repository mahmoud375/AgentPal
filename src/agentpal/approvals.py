"""Owner-only purchase inspection and immutable snapshot-bound human decisions.

Approval is consent, not a reservation, source refresh, payment or execution claim.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hmac
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_serializer
from sqlalchemy import Connection, text

from agentpal.auth.api import OwnerRead, OwnerWrite, Service, check_request_origin
from agentpal.auth.service import OwnerWriteContext
from agentpal.policy import PolicyVersion


router = APIRouter(prefix="/purchases", tags=["owner purchase approvals"])
_INITIAL = {"rejected": "REJECTED", "awaiting_approval": "AWAITING_HUMAN_APPROVAL", "auto_authorized": "AUTO_AUTHORIZED"}
_HUMAN = {"approved": "APPROVE", "denied": "DENY"}


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    decision: Literal["APPROVE", "DENY"]
    snapshot_hash: Annotated[StrictStr, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
    snapshot_hash_version: Annotated[StrictInt, Field(ge=1, le=1)]


class HumanDecision(BaseModel):
    decision: Literal["APPROVE", "DENY"]
    decided_at: datetime

    @field_serializer("decided_at")
    def utc_time(self, value):
        return value.astimezone(timezone.utc).isoformat()


class Eligibility(BaseModel):
    status: str
    can_approve: bool
    can_deny: bool
    reason_codes: list[str]
    # Every future dispatch must perform its own source and atomic financial gate.
    execution_ready: Literal[False] = False


class PurchaseView(BaseModel):
    id: UUID
    agent_id: UUID
    agent_name: str
    provider: Literal["channel3"]
    product_id: str
    merchant_domain: str
    title: str  # Untrusted display text, never markup/instructions.
    source_condition: str | None
    unit_price: Decimal
    quantity: int
    total_amount: Decimal
    currency: Literal["USD"]
    source_availability: str
    source_retrieved_at: datetime
    initial_decision: Literal["REJECTED", "AWAITING_HUMAN_APPROVAL", "AUTO_AUTHORIZED"]
    initial_reason_codes: list[str]
    created_at: datetime
    expires_at: datetime
    snapshot_hash: str
    snapshot_hash_version: int
    policy: PolicyVersion
    human_decision: HumanDecision | None
    expired: bool
    policy_current: bool
    budget_available: Decimal | None
    observed_at: datetime
    latest_source_check: str | None
    eligibility: Eligibility
    payment_status: str  # Separate provider execution lifecycle, never inferred from consent.

    @field_serializer("created_at", "expires_at", "source_retrieved_at", "observed_at")
    def utc_time(self, value):
        return value.astimezone(timezone.utc).isoformat()


class ApprovalQueue(BaseModel):
    items: list[PurchaseView]
    limit: int
    offset: int
    has_more: bool


class ApprovalResult(BaseModel):
    purchase: PurchaseView
    replayed: bool


# One timestamp and one current-month accounting observation for the whole read.
# With no period row, the active configured base is what on-demand renewal would
# initialize; no current-period captures/reservations can exist without that row.
_READ = """WITH instant AS MATERIALIZED (SELECT clock_timestamp() AS observed_at),
    budget AS MATERIALIZED (
      SELECT coalesce(b.limit_amount,active.monthly_limit)-coalesce(b.spent_amount,0)-coalesce(b.reserved_amount,0) AS available
      FROM agentpal.owners owner CROSS JOIN instant i
      LEFT JOIN agentpal.policies active ON active.id=owner.active_policy_id
      LEFT JOIN agentpal.budget_balances b ON b.owner_id=owner.id
        AND b.month_start=date_trunc('month',i.observed_at AT TIME ZONE 'UTC')::date WHERE owner.id=true)
    SELECT p.id,p.agent_id,agent.name AS agent_name,p.provider,p.product_id,p.merchant_domain,p.title,
      p.source_condition,p.unit_price,p.quantity,p.total_amount,p.currency,p.source_availability,p.source_retrieved_at,
      p.initial_decision,p.reason_codes,p.created_at,p.expires_at,p.snapshot_hash,p.snapshot_hash_version,
      pol.id AS policy_id,pol.monthly_limit,pol.auto_approval_limit,pol.hard_purchase_limit,
      pol.purchase_ttl_seconds,pol.merchant_allowlist,pol.created_at AS policy_created_at,
      coalesce(p.policy_id=o.active_policy_id,false) AS policy_current,
      p.expires_at<=i.observed_at AS expired,i.observed_at,budget.available AS budget_available,
      a.decision,a.decided_at,pay.status AS payment_status,pay.dispatch_started_at,
      EXISTS(SELECT 1 FROM agentpal.budget_reservations WHERE purchase_id=p.id) AS has_reservation,
      (SELECT result FROM agentpal.offer_revalidations WHERE purchase_id=p.id ORDER BY checked_at DESC,id DESC LIMIT 1) AS latest_source_check,
      EXISTS(SELECT 1 FROM agentpal.offer_revalidations WHERE purchase_id=p.id
        AND result NOT IN ('passed','source_error')) AS offer_invalidated
    FROM agentpal.purchases p JOIN agentpal.owners o ON o.id=p.owner_id
    JOIN agentpal.agents agent ON agent.id=p.agent_id
    JOIN agentpal.policies pol ON pol.id=p.policy_id
    LEFT JOIN agentpal.approvals a ON a.purchase_id=p.id
    LEFT JOIN agentpal.payments pay ON pay.purchase_id=p.id CROSS JOIN instant i CROSS JOIN budget
    WHERE p.owner_id=true """


def _view(row) -> PurchaseView:
    reasons = []
    if row["initial_decision"] == "rejected":
        reasons.append("PROPOSAL_REJECTED")
    if row["expired"]:
        reasons.append("PROPOSAL_EXPIRED")
    if not row["policy_current"]:
        reasons.append("POLICY_INVALIDATED")
    if row["dispatch_started_at"] is not None:
        reasons.append("ALREADY_DISPATCHED")
    if row["merchant_domain"] not in row["merchant_allowlist"]:
        reasons.append("MERCHANT_NOT_ALLOWED")
    if row["total_amount"] > row["hard_purchase_limit"]:
        reasons.append("HARD_PURCHASE_LIMIT_EXCEEDED")
    if (not row["has_reservation"] and row["dispatch_started_at"] is None
            and row["budget_available"] is not None and row["total_amount"] > row["budget_available"]):
        reasons.append("INSUFFICIENT_BUDGET")
    if row["offer_invalidated"] or row["source_availability"] != "InStock":
        reasons.append("OFFER_INVALIDATED")
    if row["decision"] == "denied":
        reasons.append("HUMAN_DENIED")
    pending = (row["initial_decision"] == "awaiting_approval" and row["decision"] is None
               and not row["expired"] and row["policy_current"] and row["dispatch_started_at"] is None)
    if row["dispatch_started_at"] is not None:
        status = "DISPATCH_STARTED"
    elif row["decision"] == "denied":
        status = "DENIED"
    elif reasons:
        status = "BLOCKED"
    elif row["decision"] == "approved":
        status = "APPROVED_PENDING_DISPATCH_CHECKS"
    elif row["initial_decision"] == "auto_authorized":
        status = "AUTO_AUTHORIZED_PENDING_DISPATCH_CHECKS"
    else:
        status = "AWAITING_HUMAN_APPROVAL"
    data = {key: row[key] for key in (
        "id", "agent_id", "agent_name", "provider", "product_id", "merchant_domain", "title", "source_condition",
        "unit_price", "quantity", "total_amount", "currency", "source_availability", "source_retrieved_at",
        "created_at", "expires_at", "snapshot_hash_version", "expired", "policy_current", "budget_available",
        "observed_at", "latest_source_check")}
    return PurchaseView(**data, snapshot_hash=bytes(row["snapshot_hash"]).hex(),
        initial_decision=_INITIAL[row["initial_decision"]], initial_reason_codes=row["reason_codes"],
        policy=PolicyVersion(id=row["policy_id"], monthly_limit=row["monthly_limit"], auto_approval_limit=row["auto_approval_limit"],
            hard_purchase_limit=row["hard_purchase_limit"], purchase_ttl_seconds=row["purchase_ttl_seconds"],
            merchant_allowlist=row["merchant_allowlist"], created_at=row["policy_created_at"], active=row["policy_current"]),
        human_decision=HumanDecision(decision=_HUMAN[row["decision"]], decided_at=row["decided_at"]) if row["decision"] else None,
        eligibility=Eligibility(status=status, can_approve=pending and not reasons, can_deny=pending, reason_codes=reasons),
        payment_status=row["payment_status"].upper() if row["payment_status"] else "NOT_STARTED")


def purchase_detail(c: Connection, purchase_id: UUID) -> PurchaseView:
    row = c.execute(text(_READ + " AND p.id=:id"), {"id": purchase_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, {"code": "purchase_not_found", "message": "Purchase proposal not found"})
    return _view(row)


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


async def read_approval_input(request: Request, owner: OwnerRead, service: Service) -> ApprovalInput:
    check_request_origin(request, service.settings, required=True)
    if not service.csrf_matches(owner, request.headers.get("x-csrf-token")):
        raise HTTPException(403, "CSRF validation failed")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON approval request required")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 2048:
            raise HTTPException(413, "Approval request too large")
        body.extend(chunk)
    try:
        return ApprovalInput.model_validate(json.loads(body, object_pairs_hook=_json_object))
    except (ValueError, RecursionError):
        raise HTTPException(422, {"code": "invalid_approval", "message": "Supply an explicit decision and the inspected snapshot binding"}) from None


def record_decision(context: OwnerWriteContext, purchase_id: UUID, payload: ApprovalInput) -> ApprovalResult:
    c = context.connection
    c.execute(text("SELECT agentpal.lock_finances()"))
    current = purchase_detail(c, purchase_id)
    if payload.snapshot_hash_version != current.snapshot_hash_version or not hmac.compare_digest(payload.snapshot_hash, current.snapshot_hash):
        raise HTTPException(409, {"code": "snapshot_conflict", "message": "Reload and inspect the unchanged proposal before deciding"})
    if current.human_decision is not None:
        if current.human_decision.decision != payload.decision:
            raise HTTPException(409, {"code": "decision_conflict", "message": "A different immutable human decision is already recorded"})
        return ApprovalResult(purchase=current, replayed=True)
    eligible = current.eligibility.can_approve if payload.decision == "APPROVE" else current.eligibility.can_deny
    if not eligible:
        raise HTTPException(409, {"code": "approval_ineligible", "message": "The proposal cannot receive this human decision",
                                  "reason_codes": current.eligibility.reason_codes or ["APPROVAL_NOT_REQUIRED"]})
    c.execute(text("""INSERT INTO agentpal.approvals(purchase_id,owner_session_id,snapshot_hash,decision)
        VALUES (:purchase,:session,:snapshot,:decision)"""),
        {"purchase": purchase_id, "session": context.owner.session_id, "snapshot": bytes.fromhex(current.snapshot_hash),
         "decision": "approved" if payload.decision == "APPROVE" else "denied"})
    return ApprovalResult(purchase=purchase_detail(c, purchase_id), replayed=False)


@router.get("/approval-queue", response_model=ApprovalQueue)
def approval_queue(owner: OwnerRead, service: Service, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                   offset: Annotated[int, Query(ge=0, le=10000)] = 0):
    with service.engine.connect() as c:
        rows = c.execute(text(_READ + """ AND p.initial_decision='awaiting_approval' AND a.purchase_id IS NULL
            AND p.expires_at>i.observed_at AND p.policy_id=o.active_policy_id AND pay.dispatch_started_at IS NULL
            ORDER BY p.created_at DESC,p.id DESC LIMIT :limit OFFSET :offset"""), {"limit": limit+1, "offset": offset}).mappings().all()
    return ApprovalQueue(items=[_view(row) for row in rows[:limit]], limit=limit, offset=offset, has_more=len(rows)>limit)


@router.get("", response_model=ApprovalQueue)
def purchase_history(owner: OwnerRead, service: Service, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                     offset: Annotated[int, Query(ge=0, le=10000)] = 0):
    with service.engine.connect() as c:
        rows = c.execute(text(_READ + " ORDER BY p.created_at DESC,p.id DESC LIMIT :limit OFFSET :offset"),
                         {"limit": limit+1, "offset": offset}).mappings().all()
    return ApprovalQueue(items=[_view(row) for row in rows[:limit]], limit=limit, offset=offset, has_more=len(rows)>limit)


@router.get("/{purchase_id}", response_model=PurchaseView)
def get_purchase(purchase_id: UUID, owner: OwnerRead, service: Service):
    with service.engine.connect() as c:
        return purchase_detail(c, purchase_id)


@router.put("/{purchase_id}/approval", response_model=ApprovalResult, openapi_extra={"requestBody": {
    "required": True, "content": {"application/json": {"schema": ApprovalInput.model_json_schema()}}}})
def put_approval(purchase_id: UUID, payload: Annotated[ApprovalInput, Depends(read_approval_input)], context: OwnerWrite):
    # Body is fully parsed before OwnerWrite opens its transaction/row locks.
    return record_decision(context, purchase_id, payload)
