"""Owner policy versions and current UTC budget, using the database balance view."""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictInt, StrictStr, field_serializer, field_validator, model_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import DataError

from agentpal.auth.api import OwnerRead, OwnerWrite, Service, check_request_origin
from agentpal.auth.service import OwnerWriteContext


router = APIRouter(tags=["owner policy and budget"])


def exact_money(value) -> Decimal:
    # JSON floats have already lost precision before Decimal could inspect them.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError("Use a nonnegative decimal string with at most two fractional digits")
    return Decimal(value)


MoneyInput = Annotated[Decimal, BeforeValidator(exact_money, json_schema_input_type=str)]


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_policy_id: UUID | None
    monthly_limit: MoneyInput
    auto_approval_limit: MoneyInput
    hard_purchase_limit: MoneyInput
    purchase_ttl_seconds: Annotated[StrictInt, Field(gt=0, le=9223372036854775807)]
    merchant_allowlist: list[StrictStr]

    @field_validator("merchant_allowlist")
    @classmethod
    def stable_allowlist_order(cls, value):
        # Duplicates/format remain checked by the canonical PostgreSQL function.
        return sorted(value)

    @model_validator(mode="after")
    def validate_thresholds(self):
        if self.auto_approval_limit > self.hard_purchase_limit:
            raise ValueError("Auto-approval limit must not exceed the hard limit")
        return self


class PolicyVersion(BaseModel):
    id: UUID
    monthly_limit: Decimal
    auto_approval_limit: Decimal
    hard_purchase_limit: Decimal
    purchase_ttl_seconds: int
    merchant_allowlist: list[str]
    created_at: datetime
    active: bool

    @field_serializer("created_at")
    def utc_timestamp(self, value):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class PolicyHistory(BaseModel):
    items: list[PolicyVersion]
    limit: int
    offset: int
    has_more: bool


class CurrentBudget(BaseModel):
    period_id: UUID
    month_start: date
    starts_at: datetime
    ends_at: datetime
    next_reset_at: datetime
    active_policy_id: UUID
    initial_policy_id: UUID
    currency: Literal["USD"] = "USD"
    initial_limit: Decimal
    limit_amount: Decimal
    spent_amount: Decimal
    reserved_amount: Decimal
    available_amount: Decimal
    overcommitted: bool
    unresolved_payment_count: int
    review_required_payment_count: int
    non_usd_capture_count: int

    @field_serializer("starts_at", "ends_at", "next_reset_at")
    def utc_timestamp(self, value):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class PolicyResult(BaseModel):
    policy: PolicyVersion
    budget: CurrentBudget
    changed: bool


POLICY_COLUMNS = """p.id,p.monthly_limit,p.auto_approval_limit,p.hard_purchase_limit,
    p.purchase_ttl_seconds,p.merchant_allowlist,p.created_at,coalesce(p.id=o.active_policy_id,false) AS active"""


def active_policy(c: Connection):
    return c.execute(text("SELECT " + POLICY_COLUMNS + """ FROM agentpal.owners o
        JOIN agentpal.policies p ON p.id=o.active_policy_id WHERE o.id=true""")).mappings().one_or_none()


def setup_required():
    return HTTPException(409, {"code": "policy_setup_required", "message": "Configure the initial owner policy first"})


def budget_snapshot(c: Connection, period: UUID) -> CurrentBudget:
    # All monetary aggregation/protection comes from the hardened view. Available
    # is computed in PostgreSQL NUMERIC, never with Python's Decimal context.
    row = c.execute(text("""SELECT b.id AS period_id,b.month_start,b.starts_at,b.ends_at,
        b.ends_at AS next_reset_at,o.active_policy_id,b.initial_policy_id,b.initial_limit,
        b.limit_amount,b.spent_amount,b.reserved_amount,
        b.limit_amount-b.spent_amount-b.reserved_amount AS available_amount,
        (b.limit_amount-b.spent_amount-b.reserved_amount<0) AS overcommitted,
        (SELECT count(*) FROM agentpal.payments p JOIN agentpal.budget_reservations r ON r.purchase_id=p.purchase_id
          WHERE r.period_id=b.id AND p.status IN ('prepared','dispatching','uncertain','payer_action_required','needs_review')) AS unresolved_payment_count,
        (SELECT count(*) FROM agentpal.payments p JOIN agentpal.budget_reservations r ON r.purchase_id=p.purchase_id
          WHERE r.period_id=b.id AND p.status IN ('payer_action_required','needs_review')) AS review_required_payment_count,
        (SELECT count(*) FROM agentpal.payment_captures cap JOIN agentpal.payments p ON p.id=cap.payment_id
          JOIN agentpal.budget_reservations r ON r.purchase_id=p.purchase_id
          WHERE r.period_id=b.id AND cap.currency<>'USD') AS non_usd_capture_count
        FROM agentpal.budget_balances b JOIN agentpal.owners o ON o.id=b.owner_id WHERE b.id=:id"""),
        {"id": period}).mappings().one()
    return CurrentBudget.model_validate(dict(row))


async def read_policy_input(request: Request, owner: OwnerRead, service: Service) -> PolicyInput:
    # Read/validate the entire upload BEFORE OwnerWrite opens its financial
    # transaction; a slow body must not retain owner/session/financial row locks.
    check_request_origin(request, service.settings, required=True)
    if not service.csrf_matches(owner, request.headers.get("x-csrf-token")):
        raise HTTPException(403, "CSRF validation failed")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON policy request required")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16384:
            raise HTTPException(413, "Policy request too large")
        body.extend(chunk)
    try:
        return PolicyInput.model_validate(json.loads(body))
    except (ValueError, RecursionError):
        raise HTTPException(422, {"code": "invalid_policy", "message": "Invalid policy fields or monetary precision"}) from None


def configure_policy(context: OwnerWriteContext, payload: PolicyInput) -> PolicyResult:
    c = context.connection
    c.execute(text("SELECT agentpal.lock_finances()"))
    values = payload.model_dump(exclude={"expected_policy_id"})
    if not c.scalar(text("SELECT agentpal.valid_allowlist(CAST(:domains AS text[]))"), {"domains": values["merchant_allowlist"]}):
        raise HTTPException(422, {"code": "invalid_allowlist", "message": "Use unique canonical lowercase merchant domains"})
    try:
        # Reject technically unrepresentable expiry durations without inventing
        # a product TTL maximum or storing an unusable policy.
        finite = c.scalar(text("""SELECT isfinite(expiry) AND expiry < timestamptz '10000-01-01 00:00:00+00'
            FROM (SELECT clock_timestamp()+:ttl*interval '1 second' AS expiry) t"""),
            {"ttl": payload.purchase_ttl_seconds})
    except DataError as error:
        if error.orig.sqlstate not in ("22003", "22008", "22015"):
            raise
        raise HTTPException(422, {"code": "invalid_policy", "message": "Proposal TTL cannot be represented"}) from None
    if not finite:
        raise HTTPException(422, {"code": "invalid_policy", "message": "Proposal expiry must fit PostgreSQL and Python timestamps"})
    previous = active_policy(c)
    matches = previous is not None and all(
        (sorted(previous[key]) if key == "merchant_allowlist" else previous[key]) == value
        for key, value in values.items())
    previous_id = previous["id"] if previous else None
    if payload.expected_policy_id != previous_id and not matches:
        raise HTTPException(409, {"code": "policy_conflict", "message": "Refresh the current policy before replacing it",
                                  "current_policy_id": str(previous_id) if previous_id else None})
    # If a period must be initialized for an existing policy, preserve that
    # policy's original base before applying this owner's immediate adjustment.
    period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()")) if previous else None
    before = budget_snapshot(c, period) if period else None
    if payload.expected_policy_id != previous_id and before and before.limit_amount != payload.monthly_limit:
        raise HTTPException(409, {"code": "policy_conflict", "message": "Refresh the current policy before replacing it",
                                  "current_policy_id": str(previous_id)})
    if before and c.scalar(text("""SELECT :limit < limit_amount AND :limit < reserved_amount+spent_amount
        FROM agentpal.budget_balances WHERE id=:id"""), {"limit": payload.monthly_limit, "id": period}):
        raise HTTPException(409, {"code": "budget_conflict", "message": "The limit cannot fall below confirmed spending and outstanding protection"})
    if not matches:
        policy_id = c.scalar(text("""INSERT INTO agentpal.policies
            (monthly_limit,auto_approval_limit,hard_purchase_limit,purchase_ttl_seconds,merchant_allowlist,created_by_session_id)
            VALUES (:monthly_limit,:auto_approval_limit,:hard_purchase_limit,:purchase_ttl_seconds,:merchant_allowlist,:session)
            RETURNING id"""), values | {"session": context.owner.session_id})
        c.execute(text("UPDATE agentpal.owners SET active_policy_id=:id WHERE id=true"), {"id": policy_id})
    else:
        policy_id = previous_id
    if period is None:
        period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
    c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=:limit WHERE id=:id"),
              {"limit": payload.monthly_limit, "id": period})
    changed = not matches or (before is not None and before.limit_amount != payload.monthly_limit)
    if changed:
        c.execute(text("""INSERT INTO agentpal.audit_events(actor_kind,owner_session_id,kind,details)
            VALUES ('owner',:session,'owner_policy_configured',jsonb_build_object(
              'previous_policy_id',CAST(:previous AS uuid),'policy_id',CAST(:policy AS uuid),'period_id',CAST(:period AS uuid),
              'previous_limit',CAST(:old_limit AS numeric),'limit_amount',CAST(:new_limit AS numeric)))"""),
            {"session": context.owner.session_id, "previous": previous_id, "policy": policy_id,
             "period": period, "old_limit": before.limit_amount if before else None, "new_limit": payload.monthly_limit})
    return PolicyResult(policy=PolicyVersion.model_validate(dict(active_policy(c))),
                        budget=budget_snapshot(c, period), changed=changed)


@router.get("/policy", response_model=PolicyVersion)
def get_policy(owner: OwnerRead, service: Service):
    with service.engine.connect() as c:
        row = active_policy(c)
    if row is None:
        raise setup_required()
    return dict(row)


@router.put("/policy", response_model=PolicyResult, openapi_extra={"requestBody": {
    "required": True, "content": {"application/json": {"schema": PolicyInput.model_json_schema()}}}})
def put_policy(payload: Annotated[PolicyInput, Depends(read_policy_input)], context: OwnerWrite):
    # Parameter order keeps body upload/validation outside the write transaction.
    return configure_policy(context, payload)


@router.get("/policy/history", response_model=PolicyHistory)
def policy_history(owner: OwnerRead, service: Service, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                   offset: Annotated[int, Query(ge=0)] = 0):
    with service.engine.connect() as c:
        rows = c.execute(text("SELECT " + POLICY_COLUMNS + """ FROM agentpal.policies p JOIN agentpal.owners o ON o.id=p.owner_id
            WHERE o.id=true ORDER BY p.created_at DESC,p.id DESC LIMIT :limit OFFSET :offset"""),
            {"limit": limit+1, "offset": offset}).mappings().all()
    return PolicyHistory(items=[PolicyVersion.model_validate(dict(row)) for row in rows[:limit]],
                         limit=limit, offset=offset, has_more=len(rows)>limit)


@router.get("/budget/current", response_model=CurrentBudget)
def current_budget(request: Request, owner: OwnerRead, service: Service):
    # This read may initialize a derived period. Recheck revocation under the
    # shared owner/session locks before the idempotent database initialization.
    with service.engine.begin() as c:
        session = service.session(c, request.cookies.get(service.settings.session_cookie), lock=True)
        if session is None or session.session_id != owner.session_id:
            raise HTTPException(401, "Authentication required")
        if active_policy(c) is None:
            raise setup_required()
        period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
        return budget_snapshot(c, period)
