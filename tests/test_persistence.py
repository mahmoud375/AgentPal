"""Executable PostgreSQL invariants, including concurrent committed transactions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from uuid import uuid4
from io import StringIO

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import DBAPIError


def purchase(db, seed, price="40.00", quantity=1, domain="allowed.test", decision="auto_authorized", **extra):
    data = dict(id=uuid4(), policy=seed["policy"], agent=seed["agent"], ref=uuid4().bytes * 2,
                price=Decimal(price), quantity=quantity, domain=domain, decision=decision,
                created=datetime.now(timezone.utc)) | extra
    with db.begin() as c:
        return dict(c.execute(text("""INSERT INTO agentpal.purchases
            (id,policy_id,agent_id,origin_ref_hash,product_id,merchant_domain,title,url_fingerprint,
             matching_version,matching_offer_count,unit_price,quantity,source_availability,source_retrieved_at,
             initial_decision,created_at)
            VALUES (:id,:policy,:agent,:ref,'test-product',:domain,'test product',:url,
                    'test-v1',1,:price,:quantity,'InStock',:created,:decision,:created) RETURNING *"""),
            data | {"url": b"u" * 32}).mappings().one())


def revalidate(db, p, result="passed", **extra):
    data = dict(id=uuid4(), purchase=p["id"], result=result, product=p["product_id"],
                domain=p["merchant_domain"], url=p["url_fingerprint"], price=p["unit_price"],
                currency="USD", availability="InStock", count=1, condition=p["source_condition"]) | extra
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.offer_revalidations
            (id,purchase_id,result,product_id,merchant_domain,url_fingerprint,observed_price,currency,
             availability,matching_offer_count,source_condition)
            VALUES (:id,:purchase,:result,:product,:domain,:url,:price,:currency,:availability,:count,:condition)"""), data)
    return data["id"]


def reserve(db, p, period=None):
    with db.begin() as c:
        if period is None:
            period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
        c.execute(text("""INSERT INTO agentpal.budget_reservations(purchase_id,period_id,amount)
            VALUES (:id,:period,:amount)"""), dict(id=p["id"], period=period, amount=p["total_amount"]))
    return period


def payment(db, seed, p):
    payment_id = uuid4()
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.payments(id,purchase_id,payment_method_id,sandbox_payee)
            VALUES (:id,:purchase,:method,'test-sandbox-business')"""),
            dict(id=payment_id, purchase=p["id"], method=seed["method"]))
    return payment_id


def operation(db, pay, window=None):
    operation_id = uuid4()
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.payment_operations
            (id,payment_id,kind,endpoint,request_id,request_payload_ciphertext,encryption_key_id,
             request_body_sha256,replay_window_seconds,retention_documentation)
            VALUES (:id,:pay,'create_order','/v2/checkout/orders',:key,:cipher,'test-key',:hash,:window,:docs)"""),
            dict(id=operation_id, pay=pay, key=str(uuid4()), cipher=b"fictional-test-payload",
                 hash=b"h" * 32, window=window, docs="test-only retention evidence" if window else None))
        p = dict(c.execute(text("SELECT * FROM agentpal.purchases WHERE id=(SELECT purchase_id FROM agentpal.payments WHERE id=:id)"), {"id": pay}).mappings().one())
    revalidate(db, p)
    return operation_id


def dispatch(db, op):
    with db.begin() as c:
        c.execute(text("""UPDATE agentpal.payment_operations o SET dispatch_count=dispatch_count+1,
            dispatch_revalidation_id=CASE WHEN dispatch_count=0 THEN
              (SELECT v.id FROM agentpal.offer_revalidations v JOIN agentpal.payments p ON p.purchase_id=v.purchase_id
               WHERE p.id=o.payment_id ORDER BY v.checked_at DESC,v.id DESC LIMIT 1)
              ELSE dispatch_revalidation_id END WHERE o.id=:id"""), {"id": op})


def uncertain(db, pay, op):
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payment_operations SET status='uncertain' WHERE id=:id"), {"id":op})
        c.execute(text("UPDATE agentpal.payments SET status='uncertain' WHERE id=:id"), {"id":pay})


def capture(db, pay, amount="40.00", currency="USD", capture_id=None):
    capture_id = capture_id or str(uuid4())
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET provider_order_id='test-order-' || id::text WHERE id=:id"), {"id": pay})
        observation = c.scalar(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts)
            VALUES (:id,'paypal_response','capture','{}') RETURNING id"""), {"id": pay})
        c.execute(text("""INSERT INTO agentpal.payment_captures
            (provider_capture_id,payment_id,observation_id,provider_order_id,amount,currency,captured_at)
            VALUES (:capture,:pay,:observation,'test-order-' || CAST(:pay AS text),:amount,:currency,clock_timestamp())"""),
            dict(capture=capture_id, pay=pay, observation=observation, amount=Decimal(amount), currency=currency))
    return capture_id


def prepared(db, seed, price="40.00", window=None):
    p = purchase(db, seed, price=price)
    revalidate(db, p)
    reserve(db, p)
    pay = payment(db, seed, p)
    op = operation(db, pay, window)
    return p, pay, op


def test_migration_round_trip_and_table_scope(db):
    names = set(inspect(db).get_table_names(schema="agentpal"))
    assert {"purchases", "policies", "payment_captures", "audit_events"} <= names
    assert not any("discovery" in name or "webhook" in name for name in names)
    config = Config("alembic.ini")
    with db.begin() as c:
        config.attributes["connection"] = c
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    assert set(inspect(db).get_table_names(schema="agentpal")) == names


def test_single_owner_and_foreign_keys(db, seed):
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("INSERT INTO agentpal.owners(id,login_name,password_hash) VALUES(false,'other','$argon2id$test')"))
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("INSERT INTO agentpal.agents(id,name,owner_id) VALUES (:id,'bad',false)"), {"id": uuid4()})


@pytest.mark.parametrize("amount", ["-0.01", "0.001", "NaN", "Infinity", "-Infinity"])
def test_money_rejects_invalid_values_without_rounding(db, seed, amount):
    with pytest.raises(IntegrityError):
        purchase(db, seed, price=amount)


def test_exact_money_quantity_ttl_and_immutable_snapshot(db, seed):
    p = purchase(db, seed, price="0.10", quantity=3)
    assert p["total_amount"] == Decimal("0.30")
    assert p["expires_at"] - p["created_at"] == timedelta(seconds=3600)
    assert len(p["snapshot_hash"]) == 32
    for statement in ["UPDATE agentpal.purchases SET unit_price=1 WHERE id=:id",
                      "DELETE FROM agentpal.purchases WHERE id=:id"]:
        with pytest.raises(IntegrityError), db.begin() as c:
            c.execute(text(statement), {"id": p["id"]})


def test_origin_and_payment_operation_uniqueness(db, seed):
    p, pay, op = prepared(db, seed)
    with pytest.raises(IntegrityError):
        purchase(db, seed, ref=p["origin_ref_hash"])
    with pytest.raises(IntegrityError):
        payment(db, seed, p)
    with pytest.raises(IntegrityError):
        operation(db, pay)


@pytest.mark.parametrize("field,value", [
    ("price", Decimal("39.99")), ("price", Decimal("40.01")), ("currency", "EUR"),
    ("availability", "Unknown"), ("count", 2), ("domain", "other.test"), ("condition", "used"),
])
def test_revalidation_cannot_claim_changed_data_passed(db, seed, field, value):
    p = purchase(db, seed)
    with pytest.raises(IntegrityError):
        revalidate(db, p, **{field: value})


def test_prohibited_merchant_proposal_is_durable_but_cannot_reserve(db, seed):
    p = purchase(db, seed, domain="blocked.test", decision="rejected")
    revalidate(db, p)
    with pytest.raises(IntegrityError):
        reserve(db, p)
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.purchases")) == 1
        assert c.scalar(text("SELECT count(*) FROM agentpal.payments")) == 0


def test_auto_label_cannot_bypass_human_approval(db, seed):
    p = purchase(db, seed, price="60.00", decision="auto_authorized")
    revalidate(db, p)
    with pytest.raises(IntegrityError):
        reserve(db, p)


def test_human_approval_binding_and_no_ttl_extension(db, seed):
    p = purchase(db, seed, price="60.00", decision="awaiting_approval")
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.approvals(purchase_id,owner_session_id,snapshot_hash,decision)
            VALUES (:id,:session,:hash,'approved')"""), dict(id=p["id"], session=seed["session"], hash=b"x" * 32))
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.approvals(purchase_id,owner_session_id,snapshot_hash,decision)
            VALUES (:id,:session,:hash,'approved')"""), dict(id=p["id"], session=seed["session"], hash=p["snapshot_hash"]))
    revalidate(db, p)
    reserve(db, p)
    with db.connect() as c:
        assert c.scalar(text("SELECT expires_at FROM agentpal.purchases WHERE id=:id"), {"id": p["id"]}) == p["expires_at"]


def test_revoked_session_cannot_approve(db, seed):
    p = purchase(db, seed, price="60.00", decision="awaiting_approval")
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp()"))
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.approvals(purchase_id,owner_session_id,snapshot_hash,decision)
            VALUES (:id,:session,:hash,'approved')"""), dict(id=p["id"], session=seed["session"], hash=p["snapshot_hash"]))


def test_expired_purchase_cannot_reserve(db, seed):
    p = purchase(db, seed, created=datetime.now(timezone.utc)-timedelta(hours=2))
    revalidate(db, p)
    with pytest.raises(IntegrityError):
        reserve(db, p)


def test_changed_policy_blocks_prepared_dispatch(db, seed):
    p, pay, op = prepared(db, seed)
    with db.begin() as c:
        new_policy = c.scalar(text("""INSERT INTO agentpal.policies
            (monthly_limit,auto_approval_limit,hard_purchase_limit,purchase_ttl_seconds,merchant_allowlist,created_by_session_id)
            SELECT monthly_limit,auto_approval_limit,hard_purchase_limit,purchase_ttl_seconds,merchant_allowlist,created_by_session_id
            FROM agentpal.policies RETURNING id"""))
        c.execute(text("UPDATE agentpal.owners SET active_policy_id=:id"), {"id": new_policy})
    with pytest.raises(IntegrityError):
        dispatch(db, op)


def test_source_error_blocks_dispatch_without_releasing_reservation(db, seed):
    p, pay, op = prepared(db, seed)
    revalidate(db, p, result="source_error")
    with pytest.raises(IntegrityError):
        dispatch(db, op)
    with db.connect() as c:
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "held"
    revalidate(db, p)
    dispatch(db, op)


def test_material_change_cannot_be_erased_by_later_pass(db, seed):
    p, pay, op = prepared(db, seed)
    revalidate(db, p, result="price_changed", price=Decimal("41.00"))
    revalidate(db, p)
    with pytest.raises(IntegrityError):
        dispatch(db, op)


def test_concurrent_month_creation_has_one_period(db, seed):
    barrier = Barrier(4)
    def create():
        barrier.wait(timeout=10)
        with db.begin() as c:
            return c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda _: create(), range(4)))
    assert len(set(result)) == 1
    with db.connect() as c:
        row = c.execute(text("SELECT * FROM agentpal.budget_periods")).mappings().one()
        assert row["starts_at"].astimezone(timezone.utc).hour == 0
        assert row["starts_at"].day == 1
        assert row["ends_at"] > row["starts_at"]


def test_concurrent_reservations_cannot_overspend(db, seed):
    first = purchase(db, seed, price="40.00", quantity=2, decision="awaiting_approval")
    second = purchase(db, seed, price="40.00", quantity=2, decision="awaiting_approval")
    for p in (first, second):
        with db.begin() as c:
            c.execute(text("""INSERT INTO agentpal.approvals(purchase_id,owner_session_id,snapshot_hash,decision)
                VALUES (:id,:session,:hash,'approved')"""), dict(id=p["id"], session=seed["session"], hash=p["snapshot_hash"]))
        revalidate(db, p)
    barrier = Barrier(2)
    def attempt(p):
        barrier.wait(timeout=10)
        try:
            reserve(db, p)
            return True
        except IntegrityError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(attempt, (first, second)))
    assert sorted(result) == [False, True]
    with db.connect() as c:
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances")) == Decimal("80.00")


def test_uncertain_dispatch_cannot_release_and_unknown_window_cannot_replay(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    uncertain(db, pay, op)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_reservations SET status='released' WHERE purchase_id=:id"), {"id": p["id"]})
    with pytest.raises(IntegrityError):
        dispatch(db, op)


def test_safe_replay_preserves_request_and_payload(db, seed):
    p, pay, op = prepared(db, seed, window=60)
    dispatch(db, op)
    uncertain(db, pay, op)
    dispatch(db, op)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payment_operations SET request_id='fresh-key' WHERE id=:id"), {"id": op})
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payment_operations SET request_payload_ciphertext=:body WHERE id=:id"), {"id": op, "body": b"changed"})
    with db.connect() as c:
        assert c.scalar(text("SELECT dispatch_count FROM agentpal.payment_operations")) == 2


def test_full_capture_accounts_once_and_audit_is_immutable(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    ref = capture(db, pay)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:id"), {"id": pay})
        c.execute(text("UPDATE agentpal.budget_reservations SET status='settled' WHERE purchase_id=:id"), {"id": p["id"]})
    with pytest.raises(IntegrityError):
        capture(db, pay, capture_id=ref)
    with db.connect() as c:
        row = c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one()
        assert row == (Decimal("0"), Decimal("40.00"))
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='capture_recorded'")) == 1
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("DELETE FROM agentpal.audit_events"))


@pytest.mark.parametrize("amount,currency", [("10.00", "USD"), ("150.001", "USD"), ("40.00", "EUR")])
def test_unexpected_capture_facts_are_preserved(db, seed, amount, currency):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay, amount=amount, currency=currency)
    with db.connect() as c:
        assert c.scalar(text("SELECT amount FROM agentpal.payment_captures")) == Decimal(amount)
        assert c.scalar(text("SELECT status FROM agentpal.payments")) == "needs_review"
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "held"


def test_capture_evidence_cannot_cross_payments(db, seed):
    first = prepared(db, seed)[1]
    second_p = purchase(db, seed)
    revalidate(db, second_p)
    reserve(db, second_p)
    second = payment(db, seed, second_p)
    with db.begin() as c:
        evidence = c.scalar(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts)
            VALUES (:id,'orders_get','capture','{}') RETURNING id"""), {"id": first})
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.payment_captures
            (provider_capture_id,payment_id,observation_id,provider_order_id,amount,currency,captured_at)
            VALUES ('cross-payment',:pay,:obs,'order',1,'USD',clock_timestamp())"""), {"pay": second, "obs": evidence})


def test_failure_requires_evidence_then_releases(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='confirmed_failed' WHERE id=:id"), {"id": pay})
    with db.begin() as c:
        evidence = c.scalar(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts)
            VALUES (:id,'orders_get','no_charge','{}') RETURNING id"""), {"id": pay})
        c.execute(text("UPDATE agentpal.payments SET status='confirmed_failed',resolution_observation_id=:obs WHERE id=:id"), {"id": pay, "obs": evidence})
        c.execute(text("UPDATE agentpal.budget_reservations SET status='released' WHERE purchase_id=:id"), {"id": p["id"]})


def test_config_never_loads_dotenv_or_exposes_url_in_repr():
    from agentpal.db.config import DatabaseSettings, create_database_engine
    settings = DatabaseSettings(DATABASE_URL="postgresql+psycopg://test:fictional@localhost/test")
    assert "fictional" not in repr(settings)
    engine = create_database_engine(settings)
    assert engine.hide_parameters
    engine.dispose()
    with pytest.raises(ValueError) as error:
        DatabaseSettings(DATABASE_URL="mysql://test:fictional@localhost/test")
    assert "fictional" not in str(error.value)


def test_expired_replay_window_blocks_post(db, seed):
    p, pay, op = prepared(db, seed, window=1)
    dispatch(db, op)
    uncertain(db, pay, op)
    with db.begin() as c:
        c.execute(text("""SELECT pg_sleep(GREATEST(0,extract(epoch FROM
            (idempotency_valid_until-clock_timestamp())))::double precision+0.02)
            FROM agentpal.payment_operations WHERE id=:id"""), {"id": op})
    with pytest.raises(IntegrityError):
        dispatch(db, op)


def test_payer_action_stops_retries_and_preserves_reservation(db, seed):
    p, pay, op = prepared(db, seed, window=60)
    dispatch(db, op)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='payer_action_required' WHERE id=:id"), {"id": pay})
    with pytest.raises(IntegrityError):
        dispatch(db, op)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_reservations SET status='released' WHERE purchase_id=:id"), {"id": p["id"]})


def test_extra_capture_after_settlement_reopens_review_and_keeps_facts(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:id"), {"id": pay})
        c.execute(text("UPDATE agentpal.budget_reservations SET status='settled' WHERE purchase_id=:id"), {"id": p["id"]})
    capture(db, pay, amount="90.00")
    with db.connect() as c:
        assert c.scalar(text("SELECT spent_amount FROM agentpal.budget_balances")) == Decimal("130.00")
        assert c.scalar(text("SELECT status FROM agentpal.payments")) == "needs_review"
    another = purchase(db, seed, price="1.00")
    revalidate(db, another)
    with pytest.raises(IntegrityError):
        reserve(db, another)


def test_failure_cannot_contradict_existing_capture(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay, amount="10.00")
    with db.begin() as c:
        evidence = c.scalar(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts,reviewed_by_session_id)
            VALUES (:id,'manual_review','no_charge','{}',:session) RETURNING id"""), {"id": pay,"session":seed["session"]})
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='confirmed_failed',resolution_observation_id=:obs WHERE id=:id"),
                  {"id": pay, "obs": evidence})


def test_periods_do_not_roll_over_and_capture_stays_with_reservation(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    with db.begin() as c:
        other = c.scalar(text("""INSERT INTO agentpal.budget_periods
            (month_start,initial_policy_id,initial_limit,limit_amount)
            VALUES ((date_trunc('month',clock_timestamp() AT TIME ZONE 'UTC')+interval '1 month')::date,
                    :policy,100,100) RETURNING id"""), seed)
        observation = c.scalar(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts)
            VALUES (:id,'orders_get','capture','{}') RETURNING id"""), {"id": pay})
        c.execute(text("UPDATE agentpal.payments SET provider_order_id='later-order' WHERE id=:id"), {"id": pay})
        c.execute(text("""INSERT INTO agentpal.payment_captures
            (provider_capture_id,payment_id,observation_id,provider_order_id,amount,currency,captured_at)
            VALUES ('later-capture',:id,:obs,'later-order',40,'USD',clock_timestamp()+interval '1 month')"""), {"id": pay,"obs":observation})
    with db.connect() as c:
        assert c.scalar(text("SELECT spent_amount FROM agentpal.budget_balances WHERE id=:id"), {"id":other}) == 0
        assert c.scalar(text("SELECT sum(spent_amount) FROM agentpal.budget_balances")) == 40
        assert c.scalar(text("SELECT limit_amount FROM agentpal.budget_periods WHERE id=:id"), {"id":other}) == 100


def test_identity_and_actor_history_cannot_be_rewritten(db, seed):
    p, pay, op = prepared(db, seed)
    statements = [
        "UPDATE agentpal.owners SET sandbox_business_account_id='different-business'",
        "UPDATE agentpal.owner_sessions SET token_hash=decode(repeat('ff',32),'hex')",
        "UPDATE agentpal.policies SET monthly_limit=999",
        "UPDATE agentpal.vault_setups SET status='pending',verified_at=NULL",
        "UPDATE agentpal.budget_reservations SET period_id=gen_random_uuid()",
    ]
    for statement in statements:
        with pytest.raises(IntegrityError), db.begin() as c:
            c.execute(text(statement))


def test_concurrent_reservations_repeatable_read_abort_instead_of_overspend(db, seed):
    purchases = [purchase(db, seed, price="50.00") for _ in range(2)]
    for p in purchases:
        revalidate(db, p)
    with db.begin() as c:
        period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
    # Three contenders each want 50 against 100. Force snapshots before locks.
    purchases.append(purchase(db, seed, price="50.00"))
    revalidate(db, purchases[-1])
    barrier = Barrier(3)
    def attempt(p):
        try:
            with db.connect().execution_options(isolation_level="REPEATABLE READ") as c, c.begin():
                c.execute(text("SELECT count(*) FROM agentpal.budget_reservations"))
                barrier.wait(timeout=10)
                c.execute(text("""INSERT INTO agentpal.budget_reservations(purchase_id,period_id,amount)
                    VALUES (:id,:period,50)"""), {"id":p["id"],"period":period})
            return "committed"
        except DBAPIError as error:
            assert error.orig.sqlstate in ("40001", "23514")
            return "aborted"
    with ThreadPoolExecutor(max_workers=3) as pool:
        result = list(pool.map(attempt, purchases))
    assert result.count("committed") >= 1
    assert result.count("aborted") >= 1
    with db.connect() as c:
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances")) <= 100


def test_completion_requires_order_and_full_capture(db, seed):
    p, pay, op = prepared(db, seed)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:id"), {"id":pay})
    with db.begin() as c:
        observation = c.scalar(text("""INSERT INTO agentpal.provider_observations(payment_id,source,outcome,facts)
            VALUES (:id,'orders_get','capture','{}') RETURNING id"""), {"id":pay})
        c.execute(text("""INSERT INTO agentpal.payment_captures
            (provider_capture_id,payment_id,observation_id,provider_order_id,amount,currency,captured_at)
            VALUES ('order-not-linked',:id,:obs,'unlinked-order',40,'USD',clock_timestamp())"""), {"id":pay,"obs":observation})
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:id"), {"id":pay})


def test_rejected_proposal_generates_audit_evidence(db, seed):
    p = purchase(db, seed, domain="blocked.test", decision="rejected")
    with db.connect() as c:
        event = c.execute(text("SELECT details FROM agentpal.audit_events WHERE purchase_id=:id AND kind='proposal_recorded'"), {"id":p["id"]}).scalar_one()
        assert event["decision"] == "rejected"
        assert event["policy_id"] == str(seed["policy"])


def test_dispatch_cannot_use_another_purchase_revalidation(db, seed):
    p, pay, op = prepared(db, seed)
    other = purchase(db, seed)
    evidence = revalidate(db, other)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("""UPDATE agentpal.payment_operations SET dispatch_count=1,
            dispatch_revalidation_id=:check WHERE id=:id"""), {"id":op,"check":evidence})


def test_expiry_after_dispatch_does_not_release_or_forbid_safe_same_operation_replay(db, seed):
    with db.begin() as c:
        policy = c.scalar(text("""INSERT INTO agentpal.policies
            (monthly_limit,auto_approval_limit,hard_purchase_limit,purchase_ttl_seconds,merchant_allowlist,created_by_session_id)
            VALUES (100,50,300,1,ARRAY['allowed.test'],:session) RETURNING id"""), seed)
        c.execute(text("UPDATE agentpal.owners SET active_policy_id=:id"), {"id":policy})
    p, pay, op = prepared(db, seed | {"policy":policy}, window=60)
    dispatch(db, op)
    with db.begin() as c:
        c.execute(text("""SELECT pg_sleep(GREATEST(0,extract(epoch FROM
            (expires_at-clock_timestamp())))::double precision+0.02)
            FROM agentpal.purchases WHERE id=:id"""), {"id":p["id"]})
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.budget_reservations SET status='released' WHERE purchase_id=:id"), {"id":p["id"]})
    uncertain(db, pay, op)
    dispatch(db, op)


def test_invalid_month_and_missing_period_are_rejected(db, seed):
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.budget_periods(month_start,initial_policy_id,initial_limit,limit_amount)
            VALUES ('2026-10-15',:policy,100,100)"""), seed)
    p = purchase(db, seed)
    revalidate(db, p)
    with pytest.raises(IntegrityError):
        reserve(db, p, period=uuid4())


def test_concurrent_dispatch_claims_cannot_both_start(db, seed):
    p, pay, op = prepared(db, seed, window=60)
    barrier = Barrier(2)
    def attempt(_):
        barrier.wait(timeout=10)
        try:
            dispatch(db, op)
            return True
        except IntegrityError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(attempt, range(2)))
    assert sorted(result) == [False, True]
    with db.connect() as c:
        assert c.scalar(text("SELECT dispatch_count FROM agentpal.payment_operations")) == 1


def test_offline_migration_generation():
    output = StringIO()
    config = Config("alembic.ini", output_buffer=output)
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg:///fictional_offline_database")
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE SCHEMA agentpal" in sql
    assert "CREATE TRIGGER guard_reservation" in sql
    assert "COMMIT;" in sql


def test_nonowner_runtime_role_can_reserve_but_cannot_rewrite_or_truncate(db, seed):
    p = purchase(db, seed)
    revalidate(db, p)
    role = "agentpal_runtime_" + uuid4().hex
    with db.begin() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN')
        grants = [
            "USAGE ON SCHEMA agentpal", "USAGE ON TYPE agentpal.usd_amount",
            "SELECT, INSERT ON ALL TABLES IN SCHEMA agentpal",
            "USAGE ON ALL SEQUENCES IN SCHEMA agentpal",
            "EXECUTE ON ALL FUNCTIONS IN SCHEMA agentpal",
            "UPDATE ON agentpal.owners,agentpal.owner_sessions,agentpal.agents,agentpal.budget_periods,"
            "agentpal.budget_reservations,agentpal.vault_setups,agentpal.payment_methods,"
            "agentpal.payments,agentpal.payment_operations",
        ]
        for grant in grants:
            c.exec_driver_sql(f'GRANT {grant} TO "{role}"')
        c.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
        period = c.scalar(text("SELECT agentpal.ensure_current_budget_period()"))
        c.execute(text("""INSERT INTO agentpal.budget_reservations(purchase_id,period_id,amount)
            VALUES (:id,:period,40)"""), {"id":p["id"],"period":period})
        for statement in ("UPDATE agentpal.purchases SET quantity=99", "TRUNCATE agentpal.audit_events"):
            with pytest.raises(DBAPIError) as error, c.begin_nested():
                c.execute(text(statement))
            assert error.value.orig.sqlstate == "42501"


def test_partial_capture_can_be_manually_reconciled_without_claiming_full_success(db, seed):
    p, pay, op = prepared(db, seed)
    dispatch(db, op)
    capture(db, pay, amount="10.00")
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:id"), {"id":pay})
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.provider_observations
            (payment_id,source,outcome,facts,reviewed_by_session_id)
            VALUES (:id,'manual_review','resolved','{}',:session)"""), {"id":pay,"session":seed["session"]})
    with db.begin() as c:
        observation = c.scalar(text("""INSERT INTO agentpal.provider_observations
            (payment_id,source,outcome,facts,reviewed_by_session_id)
            VALUES (:id,'manual_review','resolved',jsonb_build_object('no_further_charge',true),:session) RETURNING id"""),
            {"id":pay,"session":seed["session"]})
        c.execute(text("UPDATE agentpal.payments SET status='reconciled',resolution_observation_id=:obs WHERE id=:id"),
                  {"id":pay,"obs":observation})
        c.execute(text("UPDATE agentpal.budget_reservations SET status='settled' WHERE purchase_id=:id"), {"id":p["id"]})
    with db.connect() as c:
        assert c.execute(text("SELECT reserved_amount,spent_amount FROM agentpal.budget_balances")).one() == (0,10)
        assert c.scalar(text("SELECT status FROM agentpal.payments")) == "reconciled"
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='provider_manual_review'")) == 1


@pytest.mark.parametrize("change", ["release", "reduce_limit"])
def test_reservation_release_or_limit_reduction_blocks_unsent_payment(db, seed, change):
    p, pay, op = prepared(db, seed)
    with db.begin() as c:
        if change == "release":
            c.execute(text("UPDATE agentpal.budget_reservations SET status='released' WHERE purchase_id=:id"), {"id":p["id"]})
        else:
            # Preserve this dispatch regression for an underfunded legacy period.
            # Revision 0002 rejects this reduction earlier on a current schema.
            config = Config("alembic.ini")
            config.attributes["connection"] = c
            command.downgrade(config, "0001")
            c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=10"))
            command.upgrade(config, "head")
    with pytest.raises(IntegrityError, match="held, within-limit reservation"):
        dispatch(db, op)
