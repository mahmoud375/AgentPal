"""Regression tests for lifecycle, committed capture accounting and limit updates."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Event
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from test_persistence import capture, dispatch, operation, prepared, purchase, reserve, revalidate, uncertain


def no_charge(db, pay):
    with db.begin() as c:
        obs = c.scalar(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts)
            VALUES (:pay,'orders_get','no_charge','{}') RETURNING id"""), {"pay": pay})
        c.execute(text("""UPDATE agentpal.payments SET status='confirmed_failed',resolution_observation_id=:obs
            WHERE id=:pay"""), {"pay": pay, "obs": obs})


def complete(db, p, pay):
    capture(db, pay)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:pay"), {"pay": pay})
        c.execute(text("UPDATE agentpal.budget_reservations SET status='settled' WHERE purchase_id=:p"), {"p": p["id"]})


@pytest.mark.parametrize("old,new", [
    ("completed", "prepared"), ("completed", "dispatching"), ("completed", "uncertain"),
    ("confirmed_failed", "prepared"), ("confirmed_failed", "dispatching"),
    ("payer_action_required", "dispatching"), ("payer_action_required", "uncertain"),
])
def test_closed_or_stopped_payment_cannot_reenter_execution(db, seed, old, new):
    p, pay, op = prepared(db, seed, window=60)
    dispatch(db, op)
    if old == "completed":
        complete(db, p, pay)
    elif old == "confirmed_failed":
        no_charge(db, pay)
    else:
        with db.begin() as c:
            c.execute(text("UPDATE agentpal.payments SET status='payer_action_required' WHERE id=:pay"), {"pay": pay})
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status=:status WHERE id=:pay"), {"pay": pay, "status": new})


def test_parent_dispatch_status_requires_an_actual_operation_claim(db, seed):
    p, pay, op = prepared(db, seed)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='dispatching' WHERE id=:pay"), {"pay": pay})


@pytest.mark.parametrize("status", ["uncertain", "completed", "confirmed_failed", "payer_action_required"])
def test_unsent_operation_cannot_claim_an_http_outcome(db, seed, status):
    p, pay, op = prepared(db, seed)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payment_operations SET status=:status WHERE id=:op"), {"op": op, "status": status})


def test_operation_completion_requires_operation_bound_provider_evidence(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payment_operations SET status='completed' WHERE id=:op"), {"op": op})
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.provider_observations(payment_id,operation_id,source,outcome,facts)
            VALUES (:pay,:op,'paypal_response','other','{}')"""), {"pay": pay, "op": op})
        c.execute(text("UPDATE agentpal.payment_operations SET status='completed' WHERE id=:op"), {"op": op})


def test_reviewed_full_capture_needs_fresh_reconciliation_before_completion(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='needs_review' WHERE id=:pay"), {"pay": pay})
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:pay"), {"pay": pay})
    with db.begin() as c:
        obs = c.scalar(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts)
            VALUES (:pay,'orders_get','capture','{}') RETURNING id"""), {"pay": pay})
        c.execute(text("UPDATE agentpal.payments SET status='completed',resolution_observation_id=:obs WHERE id=:pay"),
                  {"pay": pay, "obs": obs})


def test_new_capture_operation_cannot_overlap_uncertain_create(db, seed):
    p, pay, op = prepared(db, seed, window=60)
    dispatch(db, op)
    uncertain(db, pay, op)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET provider_order_id='test-known-order' WHERE id=:pay"), {"pay": pay})
        second = c.scalar(text("""INSERT INTO agentpal.payment_operations
            (payment_id,kind,endpoint,request_id,request_payload_ciphertext,encryption_key_id,request_body_sha256)
            VALUES (:pay,'capture_order','/v2/checkout/orders/test-known-order/capture',:key,:body,'test-key',:hash)
            RETURNING id"""), {"pay": pay, "key": str(uuid4()), "body": b"fictional", "hash": b"h"*32})
    revalidate(db, p)
    with pytest.raises(IntegrityError):
        dispatch(db, second)


def test_full_capture_commit_replaces_hold_before_status_or_settlement_commit(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay)  # Commit facts; simulate process death before status/settlement.
    with db.connect() as c:
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "held"
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (0,40)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:pay"), {"pay": pay})
    with db.connect() as c:
        # A second process interruption after status, before settling, is also safe.
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (0,40)
    other = purchase(db, seed, price="50.00")
    revalidate(db, other)
    reserve(db, other)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:pay"), {"pay": pay})
        c.execute(text("UPDATE agentpal.budget_reservations SET status='settled' WHERE purchase_id=:p"), {"p": p["id"]})
    with db.connect() as c:
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (50,40)


@pytest.mark.parametrize("amount,currency,reserved,spent", [
    ("10.00", "USD", 40, 10), ("150.001", "USD", 40, Decimal("150.001")), ("40", "EUR", 40, 0),
])
def test_unexpected_capture_conservatively_keeps_full_hold(db, seed, amount, currency, reserved, spent):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay, amount, currency)
    with db.connect() as c:
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (reserved,spent)
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "held"


def test_extra_capture_before_settlement_restores_conservative_protection(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay)
    capture(db, pay, "5.00")
    with db.connect() as c:
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (40,45)
        assert c.scalar(text("SELECT status FROM agentpal.payments")) == "needs_review"


@pytest.mark.parametrize("state,minimum", [("held",40), ("captured",40), ("partial",50)])
def test_limit_cannot_drop_below_confirmed_spend_and_outstanding_protection(db, seed, state, minimum):
    p, pay, op = prepared(db, seed)
    if state != "held":
        dispatch(db, op)
        capture(db, pay, "40.00" if state == "captured" else "10.00")
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=:limit"), {"limit": minimum-1})
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=:limit"), {"limit": minimum})
        c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=125"))
    with db.connect() as c:
        assert c.scalar(text("SELECT initial_limit FROM agentpal.budget_periods")) == 100
        assert c.scalar(text("SELECT limit_amount FROM agentpal.budget_periods")) == 125
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='period_limit_changed'")) == 2


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
def test_concurrent_budget_reduction_and_reservation_cannot_overcommit(db, seed, isolation):
    first = purchase(db, seed)
    revalidate(db, first)
    period = reserve(db, first)
    second = purchase(db, seed)
    revalidate(db, second)
    barrier = Barrier(2)
    def attempt(action):
        try:
            with db.connect().execution_options(isolation_level=isolation) as c, c.begin():
                c.execute(text("SELECT count(*) FROM agentpal.budget_reservations"))
                barrier.wait(timeout=10)
                # Required service lock order, shared by reservation and adjustment.
                c.execute(text("SELECT agentpal.lock_finances()"))
                if action == "reduce":
                    c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=60 WHERE id=:p"), {"p": period})
                else:
                    c.execute(text("INSERT INTO agentpal.budget_reservations(purchase_id,period_id,amount) VALUES (:id,:p,40)"),
                              {"id": second["id"], "p": period})
            return "committed"
        except DBAPIError as error:
            assert error.orig.sqlstate in ("23514", "40001")
            return "aborted"
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(attempt, ["reduce", "reserve"]))
    assert sorted(result) == ["aborted", "committed"]
    with db.connect() as c:
        row = c.execute(text("SELECT reserved_amount,spent_amount,limit_amount FROM agentpal.budget_balances")).one()
        assert row.reserved_amount + row.spent_amount <= row.limit_amount


def test_completed_create_can_continue_to_capture_without_reopening_create(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.provider_observations(payment_id,operation_id,source,outcome,facts)
            VALUES (:pay,:op,'orders_get','other','{}')"""), {"pay": pay,"op": op})
        c.execute(text("UPDATE agentpal.payment_operations SET status='completed' WHERE id=:op"), {"op": op})
        c.execute(text("UPDATE agentpal.payments SET provider_order_id='test-order-' || id::text WHERE id=:pay"), {"pay": pay})
        second = c.scalar(text("""INSERT INTO agentpal.payment_operations
            (payment_id,kind,endpoint,request_id,request_payload_ciphertext,encryption_key_id,request_body_sha256)
            VALUES (:pay,'capture_order','/v2/checkout/orders/test-order-' || CAST(:pay AS text) || '/capture',
                    :key,:body,'test-key',:hash) RETURNING id"""),
            {"pay": pay,"key": str(uuid4()),"body": b"fictional capture", "hash": b"h"*32})
    revalidate(db, p)
    dispatch(db, second)
    capture(db, pay)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:pay"), {"pay": pay})
        c.execute(text("UPDATE agentpal.budget_reservations SET status='settled' WHERE purchase_id=:p"), {"p": p["id"]})
    for target in (op, second):
        with pytest.raises(IntegrityError):
            dispatch(db, target)


def test_capture_rollback_leaves_original_protection_and_no_spend(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    with pytest.raises(RuntimeError, match="simulated interruption"), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET provider_order_id='rollback-order' WHERE id=:pay"), {"pay": pay})
        obs = c.scalar(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts)
            VALUES (:pay,'orders_get','capture','{}') RETURNING id"""), {"pay": pay})
        c.execute(text("""INSERT INTO agentpal.payment_captures
            (provider_capture_id,payment_id,observation_id,provider_order_id,amount,currency,captured_at)
            VALUES ('rollback-capture',:pay,:obs,'rollback-order',40,'USD',clock_timestamp())"""), {"pay": pay,"obs": obs})
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (0,40)
        raise RuntimeError("simulated interruption")
    with db.connect() as c:
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (40,0)
        assert c.scalar(text("SELECT count(*) FROM agentpal.payment_captures")) == 0


@pytest.mark.parametrize("winner", ["reduce", "reserve"])
def test_waiting_financial_writer_checks_the_winners_committed_balance(db, seed, winner):
    first = purchase(db, seed)
    revalidate(db, first)
    period = reserve(db, first)
    second = purchase(db, seed)
    revalidate(db, second)
    started = Event()
    def write(c, action):
        if action == "reduce":
            c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=60 WHERE id=:p"), {"p": period})
        else:
            c.execute(text("INSERT INTO agentpal.budget_reservations(purchase_id,period_id,amount) VALUES (:id,:p,40)"),
                      {"id": second["id"],"p": period})
    def loser():
        try:
            with db.begin() as c:
                c.execute(text("SET LOCAL statement_timeout='5s'"))
                started.set()
                c.execute(text("SELECT agentpal.lock_finances()"))
                write(c, "reserve" if winner == "reduce" else "reduce")
        except IntegrityError:
            return "rejected"
        return "committed"
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.begin() as c:
            c.execute(text("SELECT agentpal.lock_finances()"))
            write(c, winner)
            future = pool.submit(loser)
            assert started.wait(timeout=5)
        assert future.result(timeout=10) == "rejected"


def test_late_capture_reopens_failed_payment_and_cannot_reuse_old_review(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    no_charge(db, pay)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_reservations SET status='released' WHERE purchase_id=:p"), {"p": p["id"]})
    capture(db, pay, "10.00")
    with db.begin() as c:
        obs = c.scalar(text("""INSERT INTO agentpal.provider_observations
            (payment_id,source,outcome,facts,reviewed_by_session_id)
            VALUES (:pay,'manual_review','resolved',jsonb_build_object('no_further_charge',true),:session) RETURNING id"""),
            {"pay": pay,"session": seed["session"]})
        c.execute(text("UPDATE agentpal.payments SET status='reconciled',resolution_observation_id=:obs WHERE id=:pay"),
                  {"pay": pay,"obs": obs})
    capture(db, pay, "5.00")
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='reconciled',resolution_observation_id=:obs WHERE id=:pay"),
                  {"pay": pay,"obs": obs})
    with db.connect() as c:
        assert c.scalar(text("SELECT status FROM agentpal.payments")) == "needs_review"
        assert c.scalar(text("SELECT spent_amount FROM agentpal.budget_balances")) == 15
    with pytest.raises(IntegrityError):
        dispatch(db, op)


def test_additive_migration_preserves_committed_facts_and_reproduces_guards(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay)
    config = Config("alembic.ini")
    with db.begin() as c:
        config.attributes["connection"] = c
        command.downgrade(config, "0001")
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (40,40)
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
        assert c.scalar(text("SELECT count(*) FROM agentpal.payment_captures WHERE payment_id=:pay"), {"pay": pay}) == 1
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (0,40)


def test_stale_unused_owner_review_cannot_resolve_a_later_capture(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    with db.begin() as c:
        old = c.scalar(text("""INSERT INTO agentpal.provider_observations
            (payment_id,source,outcome,facts,reviewed_by_session_id)
            VALUES (:pay,'manual_review','resolved',jsonb_build_object('no_further_charge',true),:session) RETURNING id"""),
            {"pay": pay,"session": seed["session"]})
    capture(db, pay, "10.00")
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='reconciled',resolution_observation_id=:obs WHERE id=:pay"),
                  {"pay": pay,"obs": old})


def test_existing_overspend_is_preserved_and_limit_can_increase_toward_it(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay, "150.00")
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=125"))
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=124"))
    with db.connect() as c:
        assert c.execute(text("SELECT reserved_amount,spent_amount,limit_amount FROM agentpal.budget_balances")).one() == (40,150,125)


def test_payer_stop_can_resolve_by_retrieval_without_allowing_another_post(db, seed):
    p, pay, op = prepared(db, seed, window=60)
    dispatch(db, op)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='payer_action_required' WHERE id=:pay"), {"pay": pay})
    no_charge(db, pay)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_reservations SET status='released' WHERE purchase_id=:p"), {"p": p["id"]})
    with pytest.raises(IntegrityError):
        dispatch(db, op)


def test_capture_already_recorded_blocks_replay_even_before_parent_completion(db, seed):
    p, pay, op = prepared(db, seed, window=60)
    dispatch(db, op)
    uncertain(db, pay, op)
    capture(db, pay)
    with pytest.raises(IntegrityError):
        dispatch(db, op)
