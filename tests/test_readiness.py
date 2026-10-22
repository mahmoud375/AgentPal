"""Synthetic Channel3 plus real disposable PostgreSQL financial concurrency."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta, timezone
from decimal import Decimal
import json
from threading import Barrier, Event
from uuid import uuid4

from alembic import command
from alembic.config import Config
import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from agentpal.approvals import ApprovalInput, record_decision
from agentpal.auth.service import OwnerSession, OwnerWriteContext
from agentpal.channel3 import Channel3Client, Channel3Settings
from agentpal.discovery import DiscoveryService, OfferRegistry
from agentpal.proposals import ProposalService
from agentpal.readiness import ReadinessError, ReadinessResult, ReadinessService, ReadinessSettings
from test_discovery import KEY, offer, product
from test_persistence import dispatch, operation, revalidate, reserve
from test_proposals import build, change_policy, select


PAYEE = "ABCDEFGHJKLMN"  # Fictional format-valid Sandbox merchant ID.


def run(value):
    return asyncio.run(value)


def settings(**changes):
    return ReadinessSettings(**(dict(PAYPAL_ENVIRONMENT="sandbox", PAYPAL_SANDBOX_MERCHANT_ID=PAYEE,
                                    READINESS_MAX_SOURCE_AGE_SECONDS=30) | changes))


@pytest.fixture
def setup(db, seed):
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.owners SET sandbox_business_account_id=:payee"), {"payee": PAYEE})
    data = {"value": product(), "status": 200, "hook": None, "calls": [], "check_pool": True}
    def handler(request):
        if data["check_pool"]: assert db.pool.checkedout() == 0
        data["calls"].append(request.method)
        if data["hook"]: data["hook"](request)
        if data["status"] == 0: raise httpx.ReadTimeout(KEY, request=request)
        return httpx.Response(data["status"], content=json.dumps(data["value"]))
    adapter = Channel3Client(Channel3Settings(CHANNEL3_API_KEY=KEY), transport=httpx.MockTransport(handler))
    service = ReadinessService(db, adapter, settings())
    def propose(amount="12.34", quantity=1, domain="allowed.test", *, agent_id=None):
        payload = product(offer(domain, price={"price": amount, "currency": "USD"}))
        proposal, discovery, registry, context, _ = build(db, seed | ({"agent": agent_id} if agent_id else {}), payload=payload)
        result = run(proposal.create(context, select(discovery, context), quantity))
        data["value"] = payload
        return context, result.proposal
    return service, data, propose


def decide(db, seed, proposal, decision="APPROVE"):
    with db.begin() as c:
        record_decision(OwnerWriteContext(OwnerSession("test-owner", seed["session"], b"c"*32), c), proposal.id,
            ApprovalInput(decision=decision, snapshot_hash=proposal.snapshot_hash.hex(), snapshot_hash_version=1))


def counts(db):
    with db.connect() as c:
        return {name: c.scalar(text(f"SELECT count(*) FROM agentpal.{name}")) for name in
            ("budget_reservations", "payments", "payment_operations", "provider_observations", "payment_captures")}


def test_auto_preparation_exact_hold_audit_and_no_dispatch(setup, db, seed):
    service, data, propose = setup
    context, proposal = propose(quantity=3)
    result = run(service.prepare(context, proposal.id))
    assert result.state == "PREPARED_UNSENT" and not result.replayed
    assert result.amount == Decimal("37.02") and result.authorization_kind == "automatic"
    assert data["calls"] == ["GET"]
    assert counts(db) == dict(budget_reservations=1, payments=1, payment_operations=0, provider_observations=0, payment_captures=0)
    with db.connect() as c:
        pay = c.execute(text("SELECT * FROM agentpal.payments")).mappings().one()
        assert pay["status"] == "prepared" and pay["provider_order_id"] is None and pay["dispatch_started_at"] is None
        assert pay["payment_method_id"] == seed["method"] and pay["sandbox_payee"] == PAYEE
        assert pay["preparation_revalidation_id"] is not None
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances")) == Decimal("37.02")
        details = c.scalar(text("SELECT details FROM agentpal.audit_events WHERE kind='payment_prepared'"))
        assert details["snapshot_hash"] == proposal.snapshot_hash.hex()
        assert details["policy_id"] == str(proposal.policy_id) and details["authorization_kind"] == "automatic"
        assert "payment_dispatch_claimed" not in c.execute(text("SELECT kind FROM agentpal.audit_events")).scalars().all()
    public = json.dumps(result.public())
    assert KEY not in public and PAYEE not in public and "test-key" not in public
    assert result.public()["execution_ready"] is False


def test_human_approval_separate_from_automatic_and_session_lifetime(setup, db, seed):
    service, _, propose = setup
    context, proposal = propose("60.00")
    assert run(service.prepare(context, proposal.id)).reason_codes == ("HUMAN_APPROVAL_REQUIRED",)
    assert counts(db)["payments"] == 0
    decide(db, seed, proposal)
    # Consent was authenticated when recorded; logout does not turn a trusted
    # runtime into a human session or retroactively rewrite that immutable consent.
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp()"))
    result = run(service.prepare(context, proposal.id))
    assert result.state == "PREPARED_UNSENT" and result.authorization_kind == "human"


@pytest.mark.parametrize("kind", ["rejected", "denied", "expired", "policy", "merchant", "hard"])
def test_final_authorization_blocks_before_reservation(setup, db, seed, kind):
    service, data, propose = setup
    if kind == "expired": change_policy(db, seed, purchase_ttl_seconds=1)
    if kind == "hard": change_policy(db, seed, monthly_limit="1000")
    context, proposal = propose("60" if kind == "denied" else "301" if kind == "hard" else "101" if kind == "rejected" else "12.34",
                                domain="blocked.test" if kind == "merchant" else "allowed.test")
    if kind == "denied": decide(db, seed, proposal, "DENY")
    if kind == "policy": change_policy(db, seed, monthly_limit="200")
    if kind == "expired":
        with db.connect() as c:
            c.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (:expiry-clock_timestamp())))+0.01)"), {"expiry": proposal.expires_at})
    result = run(service.prepare(context, proposal.id))
    expected = {"rejected": "PROPOSAL_REJECTED", "denied": "HUMAN_DENIED", "expired": "PROPOSAL_EXPIRED",
                "policy": "POLICY_INVALIDATED", "merchant": "MERCHANT_NOT_ALLOWED", "hard": "HARD_PURCHASE_LIMIT_EXCEEDED"}
    assert result.state == "BLOCKED" and expected[kind] in result.reason_codes
    assert counts(db)["budget_reservations"] == 0 and not data["calls"]


@pytest.mark.parametrize("changed,reason", [
    (product(offer(price={"price": "12.35", "currency": "USD"})), "OFFER_PRICE_CHANGED"),
    (product(offer(price={"price": "12.33", "currency": "USD"})), "OFFER_PRICE_CHANGED"),
    (product(offer("other.test")), "OFFER_MISSING"),
    (product(offer(condition="Used")), "OFFER_IDENTITY_CHANGED"),
    (product(offer(url="https://affiliate.test/other")), "OFFER_IDENTITY_CHANGED"),
    (product(offer(), offer()), "OFFER_AMBIGUOUS"),
    (product(offer(availability="OutOfStock")), "OFFER_UNAVAILABLE"),
    (product(offer(availability=None)), "OFFER_UNAVAILABLE"),
    (product(offer(availability="Unknown")), "OFFER_UNAVAILABLE"),
    (product(offers=[]), "OFFER_MISSING"),
    (product(id="substituted_product"), "OFFER_SOURCE_ERROR"),
    (product(offer(price={"price": "12.34", "currency": "EUR"})), "OFFER_UNSUPPORTED"),
])
def test_fresh_offer_mismatch_persists_honest_evidence_and_blocks(setup, db, changed, reason):
    service, data, propose = setup
    context, proposal = propose()
    data["value"] = changed
    result = run(service.prepare(context, proposal.id))
    assert result.state == "BLOCKED" and reason in result.reason_codes
    assert counts(db)["payments"] == 0
    with db.connect() as c:
        evidence = c.execute(text("SELECT result,reason_code FROM agentpal.offer_revalidations")).one()
        assert evidence.result != "passed" and evidence.reason_code
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='readiness_blocked'")) == 1


@pytest.mark.parametrize("status", [0, 429, 500, 404])
def test_refresh_failure_has_no_cached_fallback(setup, db, status):
    service, data, propose = setup
    context, proposal = propose()
    data["status"] = status
    result = run(service.prepare(context, proposal.id))
    assert result.state == "BLOCKED" and counts(db)["budget_reservations"] == 0
    assert KEY not in str(result.public())
    if status != 404:
        data["status"] = 200
        assert run(service.prepare(context, proposal.id)).state == "PREPARED_UNSENT"


@pytest.mark.parametrize("kind", ["missing", "revoked", "unverified", "ambiguous", "payee_missing", "payee_mismatch", "payee_bad"])
def test_payment_method_and_payee_fail_closed(setup, db, seed, kind):
    service, _, propose = setup
    context, proposal = propose()
    with db.begin() as c:
        if kind in ("missing", "revoked"):
            c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp()"))
        elif kind == "unverified":
            # Simulate legacy inconsistent data in this disposable DB; production
            # guards prevent ordinary creation of an unverified method.
            c.execute(text("ALTER TABLE agentpal.vault_setups DISABLE TRIGGER guard_setup"))
            c.execute(text("UPDATE agentpal.vault_setups SET status='pending',verified_at=NULL"))
            c.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            c.execute(text("ALTER TABLE agentpal.vault_setups ENABLE TRIGGER guard_setup"))
        elif kind == "ambiguous":
            identifier = c.scalar(text("""INSERT INTO agentpal.vault_setups(owner_session_id,state_hash,status,expires_at,verified_at)
                VALUES (:session,:hash,'verified',clock_timestamp()+interval '1 hour',clock_timestamp()) RETURNING id"""),
                {"session": seed["session"], "hash": b"z"*32})
            c.execute(text("""INSERT INTO agentpal.payment_methods(vault_setup_id,reference_ciphertext,encryption_key_id,verified_at)
                SELECT id,:cipher,'fictional',verified_at FROM agentpal.vault_setups WHERE id=:id"""), {"id": identifier, "cipher": b"synthetic"})
        else:
            c.execute(text("UPDATE agentpal.owners SET sandbox_business_account_id=:value"),
                      {"value": None if kind == "payee_missing" else "BCDEFGHJKLMNP" if kind == "payee_mismatch" else "ambiguous@example.test"})
    result = run(service.prepare(context, proposal.id))
    assert result.state == "BLOCKED" and counts(db)["payments"] == 0
    assert any(code.startswith("PAYMENT_METHOD_") or code == "SANDBOX_PAYEE_INVALID" for code in result.reason_codes)


def test_insufficient_budget_rechecks_available_protection(setup, db, seed):
    service, data, propose = setup
    first_ctx, first = propose("49")
    second_ctx, second = propose("49")
    third_ctx, third = propose("12")
    data["value"] = product(offer(price={"price": "49", "currency": "USD"}))
    assert run(service.prepare(first_ctx, first.id)).state == "PREPARED_UNSENT"
    assert run(service.prepare(second_ctx, second.id)).state == "PREPARED_UNSENT"
    data["value"] = product(offer(price={"price": "12", "currency": "USD"}))
    assert "INSUFFICIENT_BUDGET" in run(service.prepare(third_ctx, third.id)).reason_codes
    with db.connect() as c:
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances")) == 98


def test_same_purchase_replay_restart_and_original_period(setup, db):
    service, data, propose = setup
    context, proposal = propose()
    first = run(service.prepare(context, proposal.id))
    calls = len(data["calls"])
    restarted = ReadinessService(db, service._source, settings())
    replay = run(restarted.prepare(context, proposal.id))
    assert replay.replayed and replay.payment_id == first.payment_id and replay.period_id == first.period_id
    assert len(data["calls"]) == calls and counts(db)["payments"] == 1
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='payment_prepared'")) == 1
        period = c.execute(text("SELECT * FROM agentpal.budget_periods WHERE id=:id"), {"id": first.period_id}).mappings().one()
        reserved = c.scalar(text("SELECT reserved_at FROM agentpal.budget_reservations"))
        assert period["starts_at"] <= reserved < period["ends_at"]
        assert period["starts_at"].astimezone(timezone.utc).hour == 0 and period["starts_at"].day == 1


@pytest.mark.parametrize("same_purchase", [True, False])
def test_concurrent_preparation_single_identity_and_shared_budget(setup, db, same_purchase):
    service, data, propose = setup
    first = propose("49", quantity=2)
    if same_purchase:
        second = first
    else:
        with db.begin() as c:
            another_agent = c.scalar(text("INSERT INTO agentpal.agents(name) VALUES ('second-readiness-agent') RETURNING id"))
        second = propose("49", quantity=2, agent_id=another_agent)
        assert first[0].agent_id != second[0].agent_id
    # Above automatic threshold: obtain real immutable owner consent first.
    with db.connect() as c: session = c.scalar(text("SELECT id FROM agentpal.owner_sessions"))
    for _, proposal in {p.id: (ctx, p) for ctx, p in (first, second)}.values(): decide(db, {"session": session}, proposal)
    barrier = Barrier(2)
    data["check_pool"] = False
    data["hook"] = lambda _: barrier.wait(10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: run(service.prepare(item[0], item[1].id)), (first, second)))
    assert counts(db)["payments"] == 1 and counts(db)["budget_reservations"] == 1
    if same_purchase:
        assert {r.state for r in results} == {"PREPARED_UNSENT"}
        assert len({r.payment_id for r in results}) == 1 and sum(r.replayed for r in results) == 1
    else:
        assert sorted(r.state for r in results) == ["BLOCKED", "PREPARED_UNSENT"]
        assert any("INSUFFICIENT_BUDGET" in r.reason_codes for r in results)
    with db.connect() as c:
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances")) == 98


def test_source_io_policy_and_method_changes_rechecked_in_actual_transaction(setup, db, seed):
    service, data, propose = setup
    context, proposal = propose()
    data["hook"] = lambda _: change_policy(db, seed, monthly_limit="200")
    result = run(service.prepare(context, proposal.id))
    assert result.state == "BLOCKED" and "POLICY_INVALIDATED" in result.reason_codes
    assert counts(db)["budget_reservations"] == 0


@pytest.mark.parametrize("change", ["policy", "expiry", "revoked"])
def test_invalidated_prepared_payment_is_released_provably_unsent(setup, db, seed, change):
    service, _, propose = setup
    if change == "expiry": change_policy(db, seed, purchase_ttl_seconds=1)
    context, proposal = propose()
    prepared = run(service.prepare(context, proposal.id))
    if change == "policy": change_policy(db, seed, monthly_limit="200")
    if change == "expiry":
        with db.connect() as c: c.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (:expiry-clock_timestamp())))+0.01)"), {"expiry": proposal.expires_at})
    if change == "revoked":
        with db.begin() as c: c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp()"))
    released = run(service.prepare(context, proposal.id))
    assert released.state == "RELEASED_UNSENT" and released.payment_id == prepared.payment_id
    assert run(service.prepare(context, proposal.id)).state == "RELEASED_UNSENT"
    with db.connect() as c:
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "released"
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances")) == 0
        assert c.scalar(text("SELECT status FROM agentpal.payments")) == "prepared"


def test_explicit_abandonment_cannot_reprepare_and_disabled_agent_can_release(setup, db, seed):
    service, _, propose = setup
    context, proposal = propose()
    prepared = run(service.prepare(context, proposal.id))
    with db.begin() as c: c.execute(text("UPDATE agentpal.agents SET enabled=false"))
    released = service.release_unsent(context, proposal.id)
    assert released.state == "RELEASED_UNSENT" and released.payment_id == prepared.payment_id
    assert service.release_unsent(context, proposal.id).replayed
    with pytest.raises(ReadinessError, match="CONTEXT_REJECTED"): run(service.prepare(context, proposal.id))
    with db.begin() as c: c.execute(text("UPDATE agentpal.agents SET enabled=true"))
    assert run(service.prepare(context, proposal.id)).state == "RELEASED_UNSENT"
    assert counts(db)["payments"] == 1


def test_potentially_dispatched_state_is_never_released_or_replaced(setup, db, seed):
    service, _, propose = setup
    context, proposal = propose()
    first = run(service.prepare(context, proposal.id))
    op = operation(db, first.payment_id)
    dispatch(db, op)
    change_policy(db, seed, monthly_limit="200")
    assert run(service.prepare(context, proposal.id)).state == "EXISTING_PAYMENT"
    with pytest.raises(ReadinessError): service.release_unsent(context, proposal.id)
    with db.connect() as c:
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "held"
        assert c.scalar(text("SELECT dispatch_count FROM agentpal.payment_operations")) == 1


@pytest.mark.parametrize("phase", ["reservation", "payment", "commit"])
def test_crash_before_commit_rolls_back_financial_state(setup, db, phase):
    service, _, propose = setup
    context, proposal = propose()
    if phase == "commit":
        with db.begin() as c:
            c.exec_driver_sql("""CREATE FUNCTION agentpal.test_abort_preparation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                IF NEW.kind='payment_prepared' THEN RAISE EXCEPTION 'synthetic failure' USING ERRCODE='40001'; END IF; RETURN NEW; END $$;
                CREATE CONSTRAINT TRIGGER test_abort_preparation AFTER INSERT ON agentpal.audit_events
                  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION agentpal.test_abort_preparation()""")
    def crash(conn, cursor, statement, params, ctx, executemany):
        match = "INSERT INTO agentpal.budget_reservations" if phase == "reservation" else "INSERT INTO agentpal.payments"
        if phase != "commit" and match in statement: raise RuntimeError("synthetic process interruption")
    event.listen(db, "after_cursor_execute", crash)
    try:
        with pytest.raises(ReadinessError if phase == "commit" else RuntimeError): run(service.prepare(context, proposal.id))
    finally: event.remove(db, "after_cursor_execute", crash)
    assert counts(db)["budget_reservations"] == counts(db)["payments"] == 0
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='payment_prepared'")) == 0


def test_commit_time_proposal_expiry_aborts_preparation(setup, db, seed):
    service, _, propose = setup
    change_policy(db, seed, purchase_ttl_seconds=1)
    context, proposal = propose()
    def pause(conn, cursor, statement, params, ctx, executemany):
        if "INSERT INTO agentpal.payments" in statement:
            conn.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (:expiry-clock_timestamp())))+0.02)"), {"expiry": proposal.expires_at})
    event.listen(db, "after_cursor_execute", pause)
    try:
        with pytest.raises(ReadinessError, match="READINESS_CONFLICT"): run(service.prepare(context, proposal.id))
    finally: event.remove(db, "after_cursor_execute", pause)
    assert counts(db)["payments"] == counts(db)["budget_reservations"] == 0


@pytest.mark.parametrize("wrong", ["owner", "agent", "run", "uuid", "dict"])
def test_context_is_server_owned_and_cannot_adopt_foreign_purchase(setup, wrong):
    service, data, propose = setup
    context, proposal = propose()
    if wrong == "owner": context = replace(context, owner_key="attacker")
    if wrong == "agent": context = replace(context, agent_id=uuid4())
    if wrong == "run": context = replace(context, run_id=uuid4())
    if wrong == "dict": context = {"owner": "test-owner"}
    with pytest.raises(ReadinessError, match="CONTEXT_REJECTED"):
        run(service.prepare(context, str(proposal.id) if wrong == "uuid" else proposal.id))
    assert data["calls"] == []


def test_forward_upgrade_preserves_and_downgrade_refuses_to_erase_preparation(setup, db):
    service, _, propose = setup
    context, proposal = propose()
    prepared = run(service.prepare(context, proposal.id))
    cfg = Config("alembic.ini")
    with pytest.raises(DBAPIError), db.begin() as c:
        cfg.attributes["connection"] = c
        command.downgrade(cfg, "0004")
    with db.connect() as c:
        assert c.scalar(text("SELECT id FROM agentpal.payments")) == prepared.payment_id
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0006"


def wait_for_lock(db, pid):
    from time import monotonic
    deadline = monotonic()+5
    with db.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        while monotonic() < deadline:
            if c.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))>0"), {"pid": pid}): return
            c.execute(text("SELECT pg_sleep(0.01)"))
    pytest.fail("Financial contender did not wait on the expected PostgreSQL lock")


@pytest.mark.parametrize("kind", ["policy", "budget", "vault"])
@pytest.mark.parametrize("winner", ["preparation", "mutation"])
def test_real_policy_budget_and_vault_races_observe_committed_winner(setup, db, seed, kind, winner):
    service, data, propose = setup
    context, proposal = propose()
    data["check_pool"] = False
    held, release, queued = Event(), Event(), Event()
    pids = []
    def mutate(c):
        c.execute(text("SELECT agentpal.lock_finances()"))
        if kind == "policy":
            from agentpal.policy import PolicyInput, configure_policy
            old = c.execute(text("SELECT * FROM agentpal.policies WHERE id=:id"), {"id": proposal.policy_id}).mappings().one()
            configure_policy(OwnerWriteContext(OwnerSession("test-owner", seed["session"], b"c"*32), c), PolicyInput(
                expected_policy_id=old["id"], monthly_limit="200", auto_approval_limit=str(old["auto_approval_limit"]),
                hard_purchase_limit=str(old["hard_purchase_limit"]), purchase_ttl_seconds=old["purchase_ttl_seconds"],
                merchant_allowlist=old["merchant_allowlist"]))
        elif kind == "budget": c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=10"))
        else: c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp()"))
    if winner == "preparation":
        def pause(conn, cursor, statement, params, ctx, executemany):
            if "INSERT INTO agentpal.payments" in statement:
                held.set()
                assert release.wait(10)
        event.listen(db, "after_cursor_execute", pause)
        def mutation():
            try:
                with db.begin() as c:
                    pids.append(c.scalar(text("SELECT pg_backend_pid()")))
                    queued.set()
                    mutate(c)
                return "changed"
            except IntegrityError: return "blocked"
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                preparing = pool.submit(lambda: run(service.prepare(context, proposal.id)))
                assert held.wait(10)
                changing = pool.submit(mutation)
                try:
                    assert queued.wait(5)
                    wait_for_lock(db, pids[0])
                finally: release.set()
                assert preparing.result(10).state == "PREPARED_UNSENT"
                assert changing.result(10) == ("blocked" if kind == "budget" else "changed")
        finally: event.remove(db, "after_cursor_execute", pause)
        with db.connect() as c:
            assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances")) == Decimal("12.34")
        if kind != "budget": assert run(service.prepare(context, proposal.id)).state == "RELEASED_UNSENT"
    else:
        def capture_pid(conn, cursor, statement, params, ctx, executemany):
            if statement == "SELECT agentpal.lock_finances()" and not queued.is_set():
                pids.append(conn.connection.driver_connection.info.backend_pid)
                queued.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.begin() as c:
                mutate(c)
                event.listen(db, "before_cursor_execute", capture_pid)
                try:
                    preparing = pool.submit(lambda: run(service.prepare(context, proposal.id)))
                    assert queued.wait(5)
                    wait_for_lock(db, pids[0])
                finally: event.remove(db, "before_cursor_execute", capture_pid)
            result = preparing.result(10)
        assert result.state == "BLOCKED" and counts(db)["budget_reservations"] == 0
        expected = {"policy": "POLICY_INVALIDATED", "budget": "INSUFFICIENT_BUDGET", "vault": "PAYMENT_METHOD_REQUIRED"}
        assert expected[kind] in result.reason_codes


def test_method_revoked_during_source_io_cannot_be_selected(setup, db):
    service, data, propose = setup
    context, proposal = propose()
    def revoke(_):
        with db.begin() as c:
            c.execute(text("SELECT agentpal.lock_finances()"))
            c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp()"))
    data["hook"] = revoke
    assert run(service.prepare(context, proposal.id)).reason_codes == ("PAYMENT_METHOD_REQUIRED",)
    assert counts(db)["budget_reservations"] == 0


@pytest.mark.parametrize("stage", ["before_transaction", "commit"])
def test_source_check_deadline_never_authorizes_stale_preparation(setup, db, stage):
    service, _, propose = setup
    context, proposal = propose()
    service = ReadinessService(db, service._source, settings(READINESS_MAX_SOURCE_AGE_SECONDS=1))
    if stage == "before_transaction":
        original = service._prepare
        def delayed(ctx, identifier, check):
            return original(ctx, identifier, replace(check, checked_at=check.checked_at-timedelta(seconds=2)))
        service._prepare = delayed
        # Artificially advancing the receipt deadline cannot make the source
        # observation predate the proposal; use an actual old proposal timestamp.
        with db.connect() as c: c.execute(text("SELECT pg_sleep(2.01)"))
        assert "SOURCE_CHECK_STALE" in run(service.prepare(context, proposal.id)).reason_codes
    else:
        def delay(conn, cursor, statement, params, ctx, executemany):
            if "INSERT INTO agentpal.payments" in statement: conn.execute(text("SELECT pg_sleep(1.1)"))
        event.listen(db, "after_cursor_execute", delay)
        try:
            with pytest.raises(ReadinessError, match="READINESS_CONFLICT"): run(service.prepare(context, proposal.id))
        finally: event.remove(db, "after_cursor_execute", delay)
    assert counts(db)["payments"] == counts(db)["budget_reservations"] == 0


def test_new_month_does_not_move_committed_unsent_obligation(setup, db):
    service, _, propose = setup
    context, proposal = propose()
    first = run(service.prepare(context, proposal.id))
    with db.begin() as c:
        definition = c.scalar(text("SELECT pg_get_functiondef('agentpal.ensure_current_budget_period()'::regprocedure)"))
        old = "period_month := date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')::date;"
        assert definition.count(old) == 1
        c.exec_driver_sql(definition.replace(old,
            "period_month := (date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')+interval '1 month')::date;"))
        new_period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
    assert new_period != first.period_id
    replay = run(service.prepare(context, proposal.id))
    assert replay.period_id == first.period_id and replay.payment_id == first.payment_id
    with db.connect() as c:
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances WHERE id=:id"), {"id": first.period_id}) == Decimal("12.34")
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances WHERE id=:id"), {"id": new_period}) == 0


def test_orphaned_legacy_reservation_has_safe_release_path(setup, db):
    service, _, propose = setup
    context, proposal = propose()
    with db.connect() as c: p = dict(c.execute(text("SELECT * FROM agentpal.purchases")).mappings().one())
    revalidate(db, p)
    reserve(db, p)
    assert run(service.prepare(context, proposal.id)).state == "EXISTING_FINANCIAL_STATE"
    assert service.release_unsent(context, proposal.id).state == "RELEASED_UNSENT"
    assert run(service.prepare(context, proposal.id)).state == "RELEASED_UNSENT"
    assert counts(db)["payments"] == 0


def test_provider_evidence_without_dispatch_marker_blocks_release(setup, db):
    service, _, propose = setup
    context, proposal = propose()
    prepared = run(service.prepare(context, proposal.id))
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts)
            VALUES (:id,'orders_get','unknown','{}')"""), {"id": prepared.payment_id})
    assert run(service.prepare(context, proposal.id)).state == "EXISTING_PAYMENT"
    with pytest.raises(ReadinessError): service.release_unsent(context, proposal.id)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_reservations SET status='released'"))
    with db.connect() as c: assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "held"


@pytest.mark.parametrize("changes", [{"PAYPAL_ENVIRONMENT": "live"}, {"PAYPAL_SANDBOX_MERCHANT_ID": "user@example.test"},
    {"PAYPAL_SANDBOX_MERCHANT_ID": "[\"ABCDEFGHJKLMN\",\"BCDEFGHJKLMNP\"]"},
    {"PAYPAL_SANDBOX_MERCHANT_ID": "ABCDEFGHIJKLM"}, {"READINESS_MAX_SOURCE_AGE_SECONDS": 0}])
def test_configuration_cannot_select_live_or_ambiguous_recipient(changes):
    with pytest.raises(ValueError): settings(**changes)


@pytest.mark.parametrize("state,expected", [("40001", "TRANSACTION_CONFLICT"), ("40P01", "TRANSACTION_CONFLICT"), ("23505", "TRANSACTION_CONFLICT")])
def test_transaction_abort_can_retry_same_purchase_without_duplicate_hold(setup, db, state, expected):
    service, _, propose = setup
    context, proposal = propose()
    with db.begin() as c:
        c.exec_driver_sql("CREATE SEQUENCE agentpal.test_readiness_abort_seq")
        c.exec_driver_sql(f"""CREATE FUNCTION agentpal.test_readiness_abort() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.kind='payment_prepared' AND nextval('agentpal.test_readiness_abort_seq')=1 THEN
                RAISE EXCEPTION 'synthetic abort' USING ERRCODE='{state}'; END IF; RETURN NEW; END $$;
            CREATE CONSTRAINT TRIGGER test_readiness_abort AFTER INSERT ON agentpal.audit_events
              DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION agentpal.test_readiness_abort()""")
    with pytest.raises(ReadinessError, match=expected): run(service.prepare(context, proposal.id))
    assert counts(db)["payments"] == 0
    first = run(service.prepare(context, proposal.id))
    assert first.state == "PREPARED_UNSENT"
    assert run(service.prepare(context, proposal.id)).payment_id == first.payment_id
    assert counts(db)["payments"] == counts(db)["budget_reservations"] == 1


@pytest.mark.parametrize("initial", ["awaiting_approval", "auto_authorized"])
def test_inconsistent_initial_labels_cannot_invent_authorization(setup, db, initial):
    service, data, propose = setup
    context, original = propose("12.34" if initial == "awaiting_approval" else "60")
    with db.begin() as c:
        identifier = c.scalar(text("""INSERT INTO agentpal.purchases
            (agent_id,origin_ref_hash,policy_id,product_id,merchant_domain,title,url_fingerprint,source_condition,
             matching_version,matching_offer_count,unit_price,quantity,source_availability,source_retrieved_at,initial_decision)
            SELECT agent_id,:hash,policy_id,product_id,merchant_domain,title,url_fingerprint,source_condition,
              matching_version,matching_offer_count,unit_price,quantity,source_availability,source_retrieved_at,:initial
            FROM agentpal.purchases WHERE id=:id RETURNING id"""),
            {"id": original.id, "hash": uuid4().bytes*2, "initial": initial})
        c.execute(text("""INSERT INTO agentpal.audit_events(purchase_id,actor_kind,agent_id,kind,details)
            SELECT :id,'agent',agent_id,kind,details FROM agentpal.audit_events
            WHERE purchase_id=:original AND kind='proposal_evaluated'"""), {"id": identifier, "original": original.id})
    result = run(service.prepare(context, identifier))
    assert result.state == "BLOCKED"
    assert ("HUMAN_APPROVAL_REQUIRED" if initial == "awaiting_approval" else "AUTO_AUTHORIZATION_INVALID") in result.reason_codes
    if initial == "awaiting_approval":
        with db.connect() as c: p = dict(c.execute(text("SELECT * FROM agentpal.purchases WHERE id=:id"), {"id": identifier}).mappings().one())
        revalidate(db, p)
        with pytest.raises(IntegrityError): reserve(db, p)
    assert counts(db)["budget_reservations"] == 0


@pytest.mark.parametrize("capture_amount,expected", [("49", "PREPARED_UNSENT"), ("10", "BLOCKED")])
def test_preparation_reuses_authoritative_capture_protection_accounting(setup, db, capture_amount, expected):
    from test_persistence import capture
    service, _, propose = setup
    first_ctx, first = propose("49")
    second_ctx, second = propose("49")
    prepared = run(service.prepare(first_ctx, first.id))
    op = operation(db, prepared.payment_id)
    dispatch(db, op)
    capture(db, prepared.payment_id, amount=capture_amount)
    result = run(service.prepare(second_ctx, second.id))
    assert result.state == expected
    if expected == "BLOCKED": assert "INSUFFICIENT_BUDGET" in result.reason_codes
    with db.connect() as c:
        assert c.scalar(text("SELECT spent_amount FROM agentpal.budget_balances")) == Decimal(capture_amount)
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances")) == 49


def test_zero_value_preparation_does_not_invent_provider_payment(setup, db):
    service, _, propose = setup
    context, proposal = propose("0.00")
    result = run(service.prepare(context, proposal.id))
    assert result.amount == 0 and result.state == "PREPARED_UNSENT"
    assert counts(db)["payment_operations"] == counts(db)["provider_observations"] == counts(db)["payment_captures"] == 0


@pytest.mark.parametrize("winner", ["release", "dispatch"])
def test_release_and_future_dispatch_claim_are_mutually_exclusive(setup, db, winner):
    from threading import get_ident
    service, _, propose = setup
    context, proposal = propose()
    prepared = run(service.prepare(context, proposal.id))
    op = operation(db, prepared.payment_id)  # Synthetic executor state, no HTTP.
    entered, proceed, waiting = Event(), Event(), Event()
    pid = []
    loser_thread = []
    target = "UPDATE agentpal.budget_reservations SET status='released'" if winner == "release" else "UPDATE agentpal.payment_operations"
    def hold(conn, cursor, statement, params, ctx, executemany):
        if target in statement:
            entered.set()
            assert proceed.wait(10)
    event.listen(db, "after_cursor_execute", hold)
    def observe_contender(conn, cursor, statement, params, ctx, executemany):
        if loser_thread and get_ident() == loser_thread[0] and not waiting.is_set():
            pid.append(conn.connection.driver_connection.info.backend_pid)
            waiting.set()
    event.listen(db, "before_cursor_execute", observe_contender)
    def losing():
        loser_thread.append(get_ident())
        try:
            if winner == "release": dispatch(db, op)
            else: service.release_unsent(context, proposal.id)
            return "unexpected_success"
        except (ReadinessError, IntegrityError): return "blocked"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.release_unsent, context, proposal.id) if winner == "release" else pool.submit(dispatch, db, op)
            assert entered.wait(10)
            second = pool.submit(losing)
            try:
                assert waiting.wait(5)
                wait_for_lock(db, pid[0])
            finally: proceed.set()
            first.result(10)
            assert second.result(10) == "blocked"
    finally:
        event.remove(db, "after_cursor_execute", hold)
        event.remove(db, "before_cursor_execute", observe_contender)
    with db.connect() as c:
        r = c.scalar(text("SELECT status FROM agentpal.budget_reservations"))
        count = c.scalar(text("SELECT dispatch_count FROM agentpal.payment_operations"))
        assert (r, count) == (("released", 0) if winner == "release" else ("held", 1))


def test_preparation_works_with_nonowner_runtime_grants(setup, db):
    from sqlalchemy import create_engine
    service, _, propose = setup
    context, proposal = propose()
    role = "readiness_test_" + uuid4().hex
    with db.begin() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN')
        c.exec_driver_sql(f'GRANT USAGE ON SCHEMA agentpal TO "{role}"')
        c.exec_driver_sql(f'GRANT USAGE ON TYPE agentpal.usd_amount TO "{role}"')
        c.exec_driver_sql(f'GRANT SELECT,INSERT ON ALL TABLES IN SCHEMA agentpal TO "{role}"')
        c.exec_driver_sql(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA agentpal TO "{role}"')
        c.exec_driver_sql(f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA agentpal TO "{role}"')
        c.exec_driver_sql(f'GRANT UPDATE ON agentpal.owners,agentpal.agents,agentpal.budget_periods,agentpal.budget_reservations,agentpal.payment_methods,agentpal.vault_setups,agentpal.payments TO "{role}"')
    runtime = create_engine(db.url, hide_parameters=True)
    def enter(conn): conn.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
    event.listen(runtime, "begin", enter)
    try:
        scoped = ReadinessService(runtime, service._source, settings())
        result = run(scoped.prepare(context, proposal.id))
        assert result.state == "PREPARED_UNSENT"
        assert scoped.release_unsent(context, proposal.id).state == "RELEASED_UNSENT"
        with runtime.connect() as c:
            assert not c.scalar(text("SELECT pg_has_role(current_user,(SELECT tableowner FROM pg_tables WHERE schemaname='agentpal' AND tablename='payments'),'USAGE')"))
    finally: runtime.dispose()


@pytest.mark.parametrize("hardened", [False, True])
def test_reservation_commit_expiry_regression_and_legacy_upgrade_preservation(setup, db, seed, hardened):
    _, _, propose = setup
    if not hardened:
        cfg = Config("alembic.ini")
        with db.begin() as c:
            cfg.attributes["connection"] = c
            command.downgrade(cfg, "0004")
    change_policy(db, seed, purchase_ttl_seconds=1)
    _, proposal = propose()
    with db.connect() as c: p = dict(c.execute(text("SELECT * FROM agentpal.purchases")).mappings().one())
    revalidate(db, p)
    def reserve_then_expire():
        with db.begin() as c:
            period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
            c.execute(text("INSERT INTO agentpal.budget_reservations(purchase_id,period_id,amount) VALUES (:id,:period,:amount)"),
                      {"id": proposal.id, "period": period, "amount": proposal.total_amount})
            c.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (:expiry-clock_timestamp())))+0.02)"), {"expiry": proposal.expires_at})
    if hardened:
        with pytest.raises(IntegrityError): reserve_then_expire()
    else:
        reserve_then_expire()  # Reproduces the pre-0005 commit-time eligibility hole.
        with db.begin() as c:
            cfg.attributes["connection"] = c
            command.upgrade(cfg, "head")  # Definitions only; preserve historical facts.
    assert counts(db)["budget_reservations"] == (0 if hardened else 1)
