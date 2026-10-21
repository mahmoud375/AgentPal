"""Authenticated policy/budget HTTP and financial transaction regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import secrets
from threading import Barrier, Event
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agentpal.auth.config import AuthSettings
from agentpal.auth.passwords import hash_password
from agentpal.auth.service import token_hash
from agentpal.main import create_app
from agentpal import policy as policy_module
from test_persistence import capture, dispatch, prepared, purchase, reserve, revalidate, uncertain


ORIGIN = "https://agentpal.test"
PASSWORD = "fictional policy owner passphrase!"


@pytest.fixture(scope="session")
def policy_owner_hash():
    return hash_password(PASSWORD)


@pytest.fixture
def policy_context(db, policy_owner_hash):
    ids = {name:uuid4() for name in ("agent","setup","method")}
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.owners(login_name,password_hash,sandbox_business_account_id)
            VALUES ('policy-owner',:hash,'test-sandbox-business')"""), {"hash":policy_owner_hash})
    app = create_app(engine=db, settings=AuthSettings(AUTH_ORIGIN=ORIGIN))
    with TestClient(app,base_url=ORIGIN,headers={"Origin":ORIGIN}) as client:
        response = client.post("/auth/login",json={"login_name":"policy-owner","password":PASSWORD})
        assert response.status_code == 200
        with db.begin() as c:
            ids["session"] = c.scalar(text("SELECT id FROM agentpal.owner_sessions"))
            c.execute(text("INSERT INTO agentpal.agents(id,name) VALUES (:agent,'fixture-agent')"), ids)
            c.execute(text("""INSERT INTO agentpal.vault_setups(id,owner_session_id,state_hash,status,expires_at,verified_at)
                VALUES (:setup,:session,:state,'verified',clock_timestamp()+interval '1 hour',clock_timestamp())"""),
                ids|{"state":b"s"*32})
            c.execute(text("""INSERT INTO agentpal.payment_methods(id,vault_setup_id,reference_ciphertext,encryption_key_id,verified_at)
                SELECT :method,id,:cipher,'fictional-key',verified_at FROM agentpal.vault_setups WHERE id=:setup"""),
                ids|{"cipher":b"fictional fixture reference"})
        yield client, ids, app


def policy_body(expected=None, **changes):
    return {"expected_policy_id":str(expected) if expected else None,"monthly_limit":"100.00",
            "auto_approval_limit":"50.00","hard_purchase_limit":"300.00","purchase_ttl_seconds":3600,
            "merchant_allowlist":["allowed.test"]}|changes


def put(client, body):
    return client.put("/policy",json=body,headers={"X-CSRF-Token":client.cookies["__Host-agentpal_csrf"]})


@pytest.fixture
def configured(policy_context):
    client,ids,app = policy_context
    response = put(client,policy_body())
    assert response.status_code == 200,response.text
    ids["policy"] = UUID(response.json()["policy"]["id"])
    return client,ids,app


def test_initial_setup_and_budget_are_explicit_and_audited(policy_context, db):
    client,ids,_ = policy_context
    for path in ("/policy","/budget/current"):
        response = client.get(path)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "policy_setup_required"
    assert client.get("/policy/history").json()["items"] == []
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.budget_periods")) == 0
    response = put(client,policy_body(monthly_limit="1000.00"))
    assert response.status_code == 200,response.text
    body = response.json()
    assert body["changed"] is True
    assert Decimal(body["budget"]["limit_amount"]) == 1000
    assert body["policy"]["id"] == body["budget"]["active_policy_id"]
    assert client.get("/policy").json() == body["policy"]
    assert client.get("/budget/current").json() == body["budget"]
    with db.connect() as c:
        assert c.scalar(text("SELECT created_by_session_id FROM agentpal.policies")) == ids["session"]
        row = c.execute(text("SELECT actor_kind,owner_session_id FROM agentpal.audit_events WHERE kind='owner_policy_configured'")).one()
        assert row == ("owner",ids["session"])


def test_history_without_an_active_pointer_remains_readable(policy_context, db):
    client,ids,_ = policy_context
    with db.begin() as c:
        old = c.scalar(text("""INSERT INTO agentpal.policies
            (monthly_limit,auto_approval_limit,hard_purchase_limit,purchase_ttl_seconds,merchant_allowlist,created_by_session_id)
            VALUES (100,50,300,3600,ARRAY['allowed.test'],:session) RETURNING id"""),ids)
    history = client.get("/policy/history")
    assert history.status_code == 200
    assert history.json()["items"][0]["id"] == str(old)
    assert history.json()["items"][0]["active"] is False
    assert client.get("/policy").status_code == 409
    assert put(client,policy_body()).status_code == 200
    assert len(client.get("/policy/history").json()["items"]) == 2


@pytest.mark.parametrize("state", ["missing","forged","expired","revoked","agent"])
def test_all_policy_and_budget_routes_require_a_live_owner(policy_context, db, state):
    client,ids,_ = policy_context
    headers = {"X-CSRF-Token":client.cookies["__Host-agentpal_csrf"]}
    if state == "missing":
        client.cookies.clear()
    elif state in ("forged","agent"):
        headers["Cookie"] = "__Host-agentpal_session="+secrets.token_urlsafe(32)
        headers.update({"Authorization":"Bearer fictional-agent-token","X-Agent-Id":str(ids["agent"]),"X-Owner-Id":"true"})
    elif state == "revoked":
        with db.begin() as c:
            c.execute(text("UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp() WHERE id=:session"),ids)
    else:
        token,csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        with db.begin() as c:
            c.execute(text("""INSERT INTO agentpal.owner_sessions(token_hash,csrf_token_hash,created_at,expires_at)
                VALUES (:token,:csrf,clock_timestamp()-interval '2 hours',clock_timestamp()-interval '1 hour')"""),
                {"token":token_hash(token,"session"),"csrf":token_hash(csrf,"csrf")})
        headers.update({"Cookie":"__Host-agentpal_session="+token,"X-CSRF-Token":csrf})
    for path in ("/policy","/policy/history","/budget/current"):
        assert client.get(path,headers=headers).status_code == 401
    assert client.put("/policy",json=policy_body(),headers=headers).status_code == 401
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 0


@pytest.mark.parametrize("csrf", [None,"wrong","a"*43])
def test_policy_writes_reject_missing_or_invalid_csrf(policy_context, db, csrf):
    client,_,_ = policy_context
    response = client.put("/policy",json=policy_body(),headers={"X-CSRF-Token":csrf} if csrf else {})
    assert response.status_code == 403
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 0


@pytest.mark.parametrize("field,value", [
    ("monthly_limit",1.25),("monthly_limit",100),("monthly_limit",True),("monthly_limit","-1"),
    ("monthly_limit","0.001"),("monthly_limit","NaN"),("monthly_limit","Infinity"),("monthly_limit","1e3"),
    ("auto_approval_limit","300.01"),("hard_purchase_limit","49.99"),
    ("purchase_ttl_seconds",0),("purchase_ttl_seconds",True),("purchase_ttl_seconds",1.5),
    ("purchase_ttl_seconds","60"),("purchase_ttl_seconds",9223372036854775808),
    ("purchase_ttl_seconds",9223372036854775807),("purchase_ttl_seconds",400000000000),
    ("expected_policy_id","not-an-id"),
])
def test_invalid_financial_policy_values_do_not_create_state(policy_context, db, field, value):
    client,_,_ = policy_context
    response = put(client,policy_body(**{field:value}))
    assert response.status_code == 422,response.text
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 0
        assert c.scalar(text("SELECT count(*) FROM agentpal.budget_periods")) == 0


@pytest.mark.parametrize("domains", [["Allowed.test"],["*.allowed.test"],["https://allowed.test"],[""],
    ["allowed.test."],["allowed.test","allowed.test"],[None],["a"*64+".test"]])
def test_invalid_allowlists_are_rejected_by_canonical_database_rules(policy_context, domains):
    client,_,_ = policy_context
    assert put(client,policy_body(merchant_allowlist=domains)).status_code == 422


def test_zero_values_and_empty_allowlist_are_not_replaced_with_defaults(policy_context):
    client,_,_ = policy_context
    result = put(client,policy_body(monthly_limit="0",auto_approval_limit="0",hard_purchase_limit="0",merchant_allowlist=[]))
    assert result.status_code == 200
    assert result.json()["policy"]["merchant_allowlist"] == []
    assert Decimal(result.json()["budget"]["available_amount"]) == 0


def test_versions_immediate_increase_decrease_and_idempotent_replay(configured, db):
    client,ids,_ = configured
    initial = client.get("/policy").json()
    p = purchase(db,ids)
    revalidate(db,p)
    reserve(db,p)
    increased = put(client,policy_body(ids["policy"],monthly_limit="150.00"))
    assert increased.status_code == 200
    body = increased.json()
    assert Decimal(body["budget"]["limit_amount"]) == 150
    assert Decimal(body["budget"]["initial_limit"]) == 100
    assert Decimal(body["budget"]["available_amount"]) == 110
    request = policy_body(body["policy"]["id"],monthly_limit="50.00")
    lowered = put(client,request)
    assert lowered.status_code == 200 and lowered.json()["changed"]
    retry = put(client,request)
    assert retry.status_code == 200 and not retry.json()["changed"]
    assert retry.json()["policy"]["id"] == lowered.json()["policy"]["id"]
    history = client.get("/policy/history?limit=2").json()
    assert len(history["items"]) == 2 and history["has_more"]
    older = client.get("/policy/history?limit=2&offset=2").json()["items"]
    assert older[0]["id"] == initial["id"] and not older[0]["active"]
    assert older[0]["monthly_limit"] == initial["monthly_limit"]
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 3
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='owner_policy_configured'")) == 3
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.policies SET monthly_limit=999 WHERE id=:policy"),ids)


@pytest.mark.parametrize("outcome", ["uncertain", "full_capture"])
def test_budget_rejection_and_stale_policy_replacement_roll_back_everything(configured, db, outcome):
    client,ids,_ = configured
    p,pay,op = prepared(db,ids)
    dispatch(db,op)
    if outcome == "uncertain":
        uncertain(db,pay,op)
    else:
        capture(db,pay)
    old = client.get("/policy").json()
    budget = client.get("/budget/current").json()
    with db.connect() as c:
        audits = c.scalar(text("SELECT count(*) FROM agentpal.audit_events"))
    refused = put(client,policy_body(ids["policy"],monthly_limit="39.99",auto_approval_limit="10.00"))
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "budget_conflict"
    assert client.get("/policy").json() == old
    assert client.get("/budget/current").json() == budget
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 1
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events")) == audits
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "held"
    stale = put(client,policy_body(uuid4(),monthly_limit="150.00"))
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "policy_conflict"


def test_policy_change_blocks_old_unexecuted_proposal_but_preserves_uncertain_payment(configured, db):
    client,ids,_ = configured
    old_p,old_pay,old_op = prepared(db,ids)
    p,pay,op = prepared(db,ids,window=60)
    dispatch(db,op)
    uncertain(db,pay,op)
    response = put(client,policy_body(ids["policy"],monthly_limit="150.00",merchant_allowlist=[]))
    assert response.status_code == 200
    with pytest.raises(IntegrityError):
        dispatch(db,old_op)
    dispatch(db,op)  # Same uncertain operation, not new payment authorization.
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.budget_reservations WHERE status='held'")) == 2
        assert c.scalar(text("SELECT policy_id FROM agentpal.purchases WHERE id=:id"),{"id":old_p["id"]}) == ids["policy"]


@pytest.mark.parametrize("amount,currency,reserved,spent", [("40.00","USD","0","40"),
    ("10.00","USD","40","10"),("150.001","USD","40","150.001"),("40.00","EUR","40","0")])
def test_budget_uses_database_capture_protection_and_keeps_anomalies_visible(configured, db, amount, currency, reserved, spent):
    client,ids,_ = configured
    p,pay,op = prepared(db,ids)
    dispatch(db,op)
    capture(db,pay,amount,currency)
    budget = client.get("/budget/current").json()
    assert Decimal(budget["reserved_amount"]) == Decimal(reserved)
    assert Decimal(budget["spent_amount"]) == Decimal(spent)
    if amount == "150.001":
        assert budget["overcommitted"] and Decimal(budget["available_amount"]) == Decimal("-90.001")
    if currency == "EUR":
        assert budget["non_usd_capture_count"] == 1
    if currency != "USD" or amount != "40.00":
        assert budget["review_required_payment_count"] == 1
    else:
        with db.begin() as c:
            c.execute(text("UPDATE agentpal.payments SET status='completed' WHERE id=:id"),{"id":pay})
            c.execute(text("UPDATE agentpal.budget_reservations SET status='settled' WHERE purchase_id=:id"),{"id":p["id"]})
        settled = client.get("/budget/current").json()
        assert settled["spent_amount"] == budget["spent_amount"]
        assert Decimal(settled["reserved_amount"]) == 0


def test_current_budget_utc_boundaries_and_safe_concurrent_on_demand_creation(policy_context, db):
    client,ids,_ = policy_context
    # A pre-existing policy without any period, as before the budget API existed.
    with db.begin() as c:
        policy_id = c.scalar(text("""INSERT INTO agentpal.policies
            (monthly_limit,auto_approval_limit,hard_purchase_limit,purchase_ttl_seconds,merchant_allowlist,created_by_session_id)
            VALUES (100,50,300,3600,ARRAY['allowed.test'],:session) RETURNING id"""),ids)
        c.execute(text("UPDATE agentpal.owners SET active_policy_id=:id"),{"id":policy_id})
    barrier = Barrier(3)
    def read(_):
        barrier.wait(timeout=5)
        return client.get("/budget/current")
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(read,range(3)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["period_id"] for r in responses}) == 1
    body = responses[0].json()
    start = datetime.fromisoformat(body["starts_at"].replace("Z","+00:00"))
    end = datetime.fromisoformat(body["ends_at"].replace("Z","+00:00"))
    assert (start.day,start.hour,start.minute,start.second,start.utcoffset()) == (1,0,0,0,timedelta(0))
    assert end.day == 1 and end > start and body["next_reset_at"] == body["ends_at"]
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.budget_periods")) == 1


def test_concurrent_different_policy_updates_do_not_lose_updates(configured, db):
    client,ids,_ = configured
    barrier = Barrier(2)
    def change(limit):
        barrier.wait(timeout=5)
        return put(client,policy_body(ids["policy"],monthly_limit=limit))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(change,["120.00","130.00"]))
    assert sorted(r.status_code for r in responses) == [200,409]
    final = client.get("/policy").json()
    assert final["monthly_limit"] == client.get("/budget/current").json()["limit_amount"]
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 2
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='owner_policy_configured'")) == 2


def test_concurrent_identical_policy_replays_create_one_version(configured, db):
    client,ids,_ = configured
    barrier = Barrier(2)
    def change(_):
        barrier.wait(timeout=5)
        return put(client,policy_body(ids["policy"],monthly_limit="125.00"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(change,range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert sorted(r.json()["changed"] for r in responses) == [False,True]
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 2


def test_reservation_commits_before_decrease_and_prevents_partial_policy_update(configured, db):
    client,ids,_ = configured
    first = purchase(db,ids)
    revalidate(db,first)
    period = reserve(db,first)
    second = purchase(db,ids)
    revalidate(db,second)
    started = Event()
    def request():
        started.set()
        return put(client,policy_body(ids["policy"],monthly_limit="60.00"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.begin() as c:
            c.execute(text("SELECT agentpal.lock_finances()"))
            c.execute(text("INSERT INTO agentpal.budget_reservations(purchase_id,period_id,amount) VALUES (:id,:period,40)"),
                      {"id":second["id"],"period":period})
            future = pool.submit(request)
            assert started.wait(timeout=5)
        response = future.result(timeout=10)
    assert response.status_code == 409
    assert client.get("/policy").json()["id"] == str(ids["policy"])
    assert Decimal(client.get("/budget/current").json()["reserved_amount"]) == 80


def test_policy_wins_reservation_race_and_old_proposal_cannot_reserve(configured, db, monkeypatch):
    client,ids,_ = configured
    first = purchase(db,ids)
    revalidate(db,first)
    reserve(db,first)
    second = purchase(db,ids)
    revalidate(db,second)
    entered,release = Event(),Event()
    real_configure = policy_module.configure_policy
    def paused(context,payload):
        entered.set()
        assert release.wait(timeout=10)
        return real_configure(context,payload)
    monkeypatch.setattr(policy_module,"configure_policy",paused)
    def contender():
        try:
            reserve(db,second)
            return True
        except IntegrityError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        change = pool.submit(put,client,policy_body(ids["policy"],monthly_limit="60.00"))
        try:
            assert entered.wait(timeout=5)
            reservation = pool.submit(contender)
        finally:
            release.set()
        assert change.result(timeout=10).status_code == 200
        assert reservation.result(timeout=10) is False
    budget = client.get("/budget/current").json()
    assert Decimal(budget["limit_amount"]) == 60 and Decimal(budget["reserved_amount"]) == 40


def test_logout_during_body_upload_prevents_write_without_holding_financial_locks(configured, db):
    client,ids,_ = configured
    entered,release = Event(),Event()
    payload = json.dumps(policy_body(ids["policy"],monthly_limit="120.00")).encode()
    def slow_body():
        yield payload[:20]
        entered.set()
        assert release.wait(timeout=10)
        yield payload[20:]
    def request():
        return client.put("/policy",content=slow_body(),headers={"Content-Type":"application/json",
            "X-CSRF-Token":client.cookies["__Host-agentpal_csrf"]})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request)
        try:
            assert entered.wait(timeout=5)
            with db.begin() as c:
                c.execute(text("SET LOCAL lock_timeout='1s'"))
                c.execute(text("SELECT agentpal.lock_finances()"))
                c.execute(text("UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp() WHERE id=:session"),ids)
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 401
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 1


def test_deferred_serialization_abort_returns_conflict_and_retry_does_not_duplicate(configured, db):
    client,ids,_ = configured
    # PostgreSQL really aborts at COMMIT, after policy, budget and audit SQL ran.
    # The sequence is intentionally nontransactional so the next attempt can pass.
    with db.begin() as c:
        c.exec_driver_sql("""CREATE SEQUENCE agentpal.test_policy_abort_counter;
            CREATE FUNCTION agentpal.test_abort_policy_once() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
              IF NEW.kind='owner_policy_configured' AND nextval('agentpal.test_policy_abort_counter')=1 THEN
                RAISE EXCEPTION 'test serialization abort' USING ERRCODE='40001';
              END IF;
              RETURN NEW;
            END $$;
            CREATE CONSTRAINT TRIGGER test_policy_abort AFTER INSERT ON agentpal.audit_events
              DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION agentpal.test_abort_policy_once();""")
    body = policy_body(ids["policy"],monthly_limit="150.00")
    response = put(client,body)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "transaction_conflict"
    assert client.get("/policy").json()["id"] == str(ids["policy"])
    assert Decimal(client.get("/budget/current").json()["limit_amount"]) == 100
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 1
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='owner_policy_configured'")) == 1
    assert put(client,body).status_code == 200
    replay = put(client,body)
    assert replay.status_code == 200 and replay.json()["changed"] is False
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 2
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='owner_policy_configured'")) == 2


def test_month_renewal_uses_latest_policy_without_rollover_or_moving_old_exposure(configured, db):
    client,ids,_ = configured
    p,pay,op = prepared(db,ids)
    dispatch(db,op)
    uncertain(db,pay,op)
    old_period = client.get("/budget/current").json()["period_id"]
    raised = put(client,policy_body(ids["policy"],monthly_limit="150.00"))
    assert raised.status_code == 200
    # Advance ONLY the initializer's month clock in this disposable database.
    # All existing financial facts were established through their real guards;
    # no history is rewritten and this clock override never enters production.
    original = Path('migrations/versions/0001_persistence.sql').read_text()
    definition = re.search(r'CREATE FUNCTION agentpal.ensure_current_budget_period\(\).*?END \$\$;',original,re.S).group()
    old = "period_month := date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')::date;"
    assert definition.count(old) == 1
    definition = definition.replace(old,"period_month := (date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC') + interval '1 month')::date;")
    with db.begin() as c:
        c.exec_driver_sql(definition.replace('CREATE FUNCTION','CREATE OR REPLACE FUNCTION',1))
    next_budget = client.get("/budget/current").json()
    assert next_budget["period_id"] != old_period
    assert Decimal(next_budget["limit_amount"]) == Decimal(next_budget["available_amount"]) == 150
    assert Decimal(next_budget["spent_amount"]) == Decimal(next_budget["reserved_amount"]) == 0
    assert next_budget["active_policy_id"] == raised.json()["policy"]["id"]
    with db.connect() as c:
        row = c.execute(text("SELECT period_id,status FROM agentpal.budget_reservations WHERE purchase_id=:id"),{"id":p["id"]}).one()
        assert row == (UUID(old_period),"held")
        assert c.scalar(text("SELECT initial_limit FROM agentpal.budget_periods WHERE id=:id"),{"id":old_period}) == 100
        assert c.scalar(text("SELECT reserved_amount FROM agentpal.budget_balances WHERE id=:id"),{"id":old_period}) == 40


def test_budget_available_is_not_rounded_by_python_decimal_context(policy_context, db):
    client,ids,_ = policy_context
    response = put(client,policy_body(monthly_limit="123456789012345678901234567890.12"))
    assert response.status_code == 200
    ids["policy"] = UUID(response.json()["policy"]["id"])
    p = purchase(db,ids)
    revalidate(db,p)
    reserve(db,p)
    assert client.get("/budget/current").json()["available_amount"] == "123456789012345678901234567850.12"


def test_policy_change_wins_first_dispatch_race_without_releasing_protection(configured, db, monkeypatch):
    client,ids,_ = configured
    p,pay,op = prepared(db,ids)
    entered,release = Event(),Event()
    real_configure = policy_module.configure_policy
    def paused(context,payload):
        entered.set()
        assert release.wait(timeout=10)
        return real_configure(context,payload)
    monkeypatch.setattr(policy_module,"configure_policy",paused)
    def send():
        try:
            dispatch(db,op)
            return True
        except IntegrityError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        change = pool.submit(put,client,policy_body(ids["policy"],monthly_limit="150.00"))
        try:
            assert entered.wait(timeout=5)
            claim = pool.submit(send)
        finally:
            release.set()
        assert change.result(timeout=10).status_code == 200
        assert claim.result(timeout=10) is False
    with db.connect() as c:
        assert c.scalar(text("SELECT dispatch_count FROM agentpal.payment_operations WHERE id=:id"),{"id":op}) == 0
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations WHERE purchase_id=:id"),{"id":p["id"]}) == "held"


def test_dispatch_claim_wins_race_and_policy_change_preserves_payment(configured, db):
    client,ids,_ = configured
    p,pay,op = prepared(db,ids)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.begin() as c:
            c.execute(text("SELECT agentpal.lock_finances()"))
            c.execute(text("""UPDATE agentpal.payment_operations SET dispatch_count=1,
                dispatch_revalidation_id=(SELECT id FROM agentpal.offer_revalidations WHERE purchase_id=:purchase
                    ORDER BY checked_at DESC,id DESC LIMIT 1) WHERE id=:op"""),{"purchase":p["id"],"op":op})
            update = pool.submit(put,client,policy_body(ids["policy"],monthly_limit="150.00"))
        assert update.result(timeout=10).status_code == 200
    with db.connect() as c:
        assert c.scalar(text("SELECT status FROM agentpal.payments WHERE id=:id"),{"id":pay}) == "dispatching"
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations WHERE purchase_id=:id"),{"id":p["id"]}) == "held"


@pytest.mark.parametrize("winner", ["capture","policy"])
def test_decrease_and_unexpected_capture_preserve_facts_in_either_commit_order(configured, db, monkeypatch, winner):
    client,ids,_ = configured
    p,pay,op = prepared(db,ids)
    dispatch(db,op)
    if winner == "capture":
        capture(db,pay,"10.00")
        response = put(client,policy_body(ids["policy"],monthly_limit="45.00"))
        assert response.status_code == 409
        assert Decimal(client.get("/budget/current").json()["limit_amount"]) == 100
    else:
        entered,release = Event(),Event()
        real_configure = policy_module.configure_policy
        def paused(context,payload):
            entered.set()
            assert release.wait(timeout=10)
            return real_configure(context,payload)
        monkeypatch.setattr(policy_module,"configure_policy",paused)
        with ThreadPoolExecutor(max_workers=2) as pool:
            change = pool.submit(put,client,policy_body(ids["policy"],monthly_limit="45.00"))
            try:
                assert entered.wait(timeout=5)
                fact = pool.submit(capture,db,pay,"10.00")
            finally:
                release.set()
            assert change.result(timeout=10).status_code == 200
            fact.result(timeout=10)
        budget = client.get("/budget/current").json()
        assert budget["overcommitted"] and Decimal(budget["available_amount"]) == -5
    budget = client.get("/budget/current").json()
    assert Decimal(budget["reserved_amount"]) == 40 and Decimal(budget["spent_amount"]) == 10


def test_policy_update_and_on_demand_initialization_are_consistent(policy_context, db):
    client,ids,_ = policy_context
    with db.begin() as c:
        ids["policy"] = c.scalar(text("""INSERT INTO agentpal.policies
            (monthly_limit,auto_approval_limit,hard_purchase_limit,purchase_ttl_seconds,merchant_allowlist,created_by_session_id)
            VALUES (100,50,300,3600,ARRAY['allowed.test'],:session) RETURNING id"""),ids)
        c.execute(text("UPDATE agentpal.owners SET active_policy_id=:policy"),ids)
    barrier = Barrier(2)
    def read():
        barrier.wait(timeout=5)
        return client.get("/budget/current")
    def update():
        barrier.wait(timeout=5)
        return put(client,policy_body(ids["policy"],monthly_limit="150.00"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first,second = pool.submit(read),pool.submit(update)
        snapshot,changed = first.result(timeout=10),second.result(timeout=10)
    assert snapshot.status_code == changed.status_code == 200
    old_snapshot = snapshot.json()
    assert (Decimal(old_snapshot["limit_amount"]),old_snapshot["active_policy_id"]) in (
        (Decimal(100),str(ids["policy"])),(Decimal(150),changed.json()["policy"]["id"]))
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.budget_periods")) == 1
    assert Decimal(client.get("/budget/current").json()["limit_amount"]) == 150


def test_public_policy_responses_exclude_session_material_and_reject_unknown_fields(configured, db):
    client,ids,_ = configured
    for path in ("/policy","/policy/history","/budget/current"):
        response = client.get(path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        for secret in (PASSWORD,str(ids["session"]),client.cookies["__Host-agentpal_session"],
                       client.cookies["__Host-agentpal_csrf"],"password_hash","created_by_session_id","csrf_token_hash"):
            assert secret not in response.text
    response = put(client,policy_body(ids["policy"],owner_session_id="do-not-echo-canary"))
    assert response.status_code == 422 and "do-not-echo-canary" not in response.text
    csrf = client.cookies["__Host-agentpal_csrf"]
    response = client.put("/policy",content='"'+"x"*17000+'"',headers={"Content-Type":"application/json","X-CSRF-Token":csrf})
    assert response.status_code == 413


@pytest.mark.parametrize("origin", [None,"https://evil.test"])
def test_policy_write_requires_trusted_browser_origin(configured, origin):
    client,ids,_ = configured
    client.headers.pop("origin")
    headers = {"X-CSRF-Token":client.cookies["__Host-agentpal_csrf"]}
    if origin is not None:
        headers["Origin"] = origin
    assert client.put("/policy",json=policy_body(ids["policy"]),headers=headers).status_code == 403
