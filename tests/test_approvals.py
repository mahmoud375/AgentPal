"""Owner approval HTTP/security and real isolated PostgreSQL race tests.

All provider/account/session data are synthetic fixtures; no external I/O.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import secrets
from threading import Barrier, Event
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agentpal.auth.service import token_hash
from agentpal import approvals as approval_module
from agentpal import policy as policy_module
from test_policy import ORIGIN, PASSWORD, configured, policy_context, policy_owner_hash, policy_body, put
from test_persistence import dispatch, operation, payment, purchase, reserve, revalidate, uncertain
from test_proposals import build, select, run
from agentpal.discovery import RunContext


def pending(db, ids, **changes):
    return purchase(db, ids, price="60.00", decision="awaiting_approval", **changes)


def body(p, decision="APPROVE", **changes):
    return dict(decision=decision, snapshot_hash=bytes(p["snapshot_hash"]).hex(), snapshot_hash_version=p["snapshot_hash_version"]) | changes


def decide(client, p, decision="APPROVE", **changes):
    return client.put(f'/purchases/{p["id"]}/approval', json=body(p, decision, **changes),
                      headers={"X-CSRF-Token": client.cookies["__Host-agentpal_csrf"]})


def detail(client, p):
    response = client.get(f'/purchases/{p["id"]}')
    assert response.status_code == 200, response.text
    return response.json()


def audit_count(db, purchase_id):
    with db.connect() as c:
        return c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE purchase_id=:id AND kind='human_decision_recorded'"),
                        {"id": purchase_id})


def test_queue_pagination_safe_details_and_policy_context(configured, db):
    client, ids, _ = configured
    proposals = [pending(db, ids) for _ in range(3)]
    auto = purchase(db, ids)
    rejected = purchase(db, ids, decision="rejected")
    old = pending(db, ids, created=datetime.now(timezone.utc)-timedelta(hours=2))
    first = client.get('/purchases/approval-queue', params={"limit": 2})
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["items"]) == 2 and page["has_more"]
    second = client.get('/purchases/approval-queue', params={"limit": 2, "offset": 2}).json()
    assert len(second["items"]) == 1 and not second["has_more"]
    assert {p["id"] for p in page["items"] + second["items"]} == {str(p["id"]) for p in proposals}
    item = detail(client, proposals[0])
    assert item["unit_price"] == "60.00" and item["total_amount"] == "60.00" and item["quantity"] == 1
    assert item["policy"]["id"] == str(ids["policy"]) and item["policy"]["active"]
    assert item["policy"]["merchant_allowlist"] == ["allowed.test"]
    assert item["initial_decision"] == "AWAITING_HUMAN_APPROVAL"
    assert item["human_decision"] is None and item["payment_status"] == "NOT_STARTED"
    assert item["eligibility"] == dict(status="AWAITING_HUMAN_APPROVAL", can_approve=True, can_deny=True,
                                       reason_codes=[], execution_ready=False)
    assert item["snapshot_hash"] == bytes(proposals[0]["snapshot_hash"]).hex()
    for key in ["origin_ref_hash", "url_fingerprint", "owner_session_id", "password_hash", "csrf_token_hash", "sandbox_payee", "provider_order_id"]:
        assert key not in first.text and key not in json.dumps(item)
    assert detail(client, old)["expired"]
    assert detail(client, auto)["initial_decision"] == "AUTO_AUTHORIZED"
    assert detail(client, rejected)["initial_decision"] == "REJECTED"
    assert first.headers["cache-control"] == "no-store" and first.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("decision,stored,status", [("APPROVE", "approved", "APPROVED_PENDING_DISPATCH_CHECKS"), ("DENY", "denied", "DENIED")])
def test_human_decision_exact_binding_attribution_ttl_and_no_financial_side_effects(configured, db, decision, stored, status):
    client, ids, _ = configured
    p = pending(db, ids)
    result = decide(client, p, decision)
    assert result.status_code == 200, result.text
    result = result.json()
    assert not result["replayed"]
    item = result["purchase"]
    assert item["initial_decision"] == "AWAITING_HUMAN_APPROVAL"
    assert item["human_decision"]["decision"] == decision
    assert item["payment_status"] == "NOT_STARTED" and item["eligibility"]["status"] == status
    assert not item["eligibility"]["execution_ready"]
    assert datetime.fromisoformat(item["expires_at"]) == p["expires_at"]
    assert client.get('/purchases/approval-queue').json()["items"] == []
    with db.connect() as c:
        row = c.execute(text("SELECT * FROM agentpal.approvals WHERE purchase_id=:id"), {"id": p["id"]}).mappings().one()
        assert row["owner_session_id"] == ids["session"] and row["decision"] == stored and row["snapshot_hash"] == p["snapshot_hash"]
        assert c.scalar(text("SELECT expires_at FROM agentpal.purchases WHERE id=:id"), {"id": p["id"]}) == p["expires_at"]
        audit = c.execute(text("SELECT actor_kind,owner_session_id,details FROM agentpal.audit_events WHERE kind='human_decision_recorded'")).one()
        assert audit == ("owner", ids["session"], {"decision": stored})
        for table in ("budget_reservations", "payments", "payment_operations", "offer_revalidations"):
            assert c.scalar(text(f"SELECT count(*) FROM agentpal.{table}")) == 0


def test_actual_discovery_proposal_to_owner_approval_flow(configured, db):
    client, ids, _ = configured
    from test_discovery import offer, product
    service, discovery, _, ctx, _ = build(db, ids, ctx=RunContext("policy-owner", ids["agent"], uuid4()),
                                        payload=product(offer(price={"price": "60", "currency": "USD"})))
    proposal = run(service.create(ctx, select(discovery, ctx), 1)).proposal
    response = client.get(f'/purchases/{proposal.id}')
    assert response.status_code == 200 and response.json()["initial_decision"] == "AWAITING_HUMAN_APPROVAL"
    response = client.put(f'/purchases/{proposal.id}/approval', json={key: response.json()[key] for key in ("snapshot_hash", "snapshot_hash_version")} | {"decision": "APPROVE"},
                          headers={"X-CSRF-Token": client.cookies["__Host-agentpal_csrf"]})
    assert response.status_code == 200, response.text
    assert response.json()["purchase"]["human_decision"]["decision"] == "APPROVE"


@pytest.mark.parametrize("state", ["missing", "forged", "revoked", "expired", "agent"])
def test_all_routes_require_live_owner_session(configured, db, state):
    client, ids, _ = configured
    p = pending(db, ids)
    csrf = client.cookies["__Host-agentpal_csrf"]
    headers = {"X-CSRF-Token": csrf}
    if state == "missing":
        client.cookies.clear()
    elif state in ("forged", "agent"):
        headers.update({"Cookie": "__Host-agentpal_session=" + secrets.token_urlsafe(32), "Authorization": "Bearer synthetic-agent-token",
                        "X-Agent-Id": str(ids["agent"]), "X-Owner-Id": "true"})
    elif state == "revoked":
        with db.begin() as c:
            c.execute(text("UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp() WHERE id=:session"), ids)
    else:
        token = secrets.token_urlsafe(32)
        with db.begin() as c:
            c.execute(text("""INSERT INTO agentpal.owner_sessions(token_hash,csrf_token_hash,created_at,expires_at)
                VALUES (:token,:csrf,clock_timestamp()-interval '2 hours',clock_timestamp()-interval '1 hour')"""),
                {"token": token_hash(token, "session"), "csrf": b"x" * 32})
        headers["Cookie"] = "__Host-agentpal_session=" + token
    for path in ['/purchases/approval-queue', f'/purchases/{p["id"]}']:
        assert client.get(path, headers=headers).status_code == 401
    assert client.put(f'/purchases/{p["id"]}/approval', json=body(p), headers=headers).status_code == 401
    assert audit_count(db, p["id"]) == 0


@pytest.mark.parametrize("csrf", [None, "wrong", "x" * 43])
def test_csrf_rejected(configured, db, csrf):
    client, ids, _ = configured
    p = pending(db, ids)
    response = client.put(f'/purchases/{p["id"]}/approval', json=body(p), headers={"X-CSRF-Token": csrf} if csrf else {})
    assert response.status_code == 403 and audit_count(db, p["id"]) == 0


@pytest.mark.parametrize("origin", [None, "https://evil.test", "null", "https://sub.agentpal.test"])
def test_origin_required_and_exact(configured, db, origin):
    client, ids, _ = configured
    p = pending(db, ids)
    client.headers.pop("origin")
    headers = {"X-CSRF-Token": client.cookies["__Host-agentpal_csrf"]}
    if origin:
        headers["Origin"] = origin
    assert client.put(f'/purchases/{p["id"]}/approval', json=body(p), headers=headers).status_code == 403


def test_referer_supported_and_cross_site_fetch_metadata_rejected(configured, db):
    client, ids, _ = configured
    p = pending(db, ids)
    assert client.get(f'/purchases/{p["id"]}', headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    client.headers.pop("origin")
    response = client.put(f'/purchases/{p["id"]}/approval', json=body(p), headers={"Referer": ORIGIN+'/dashboard',
                          "X-CSRF-Token": client.cookies["__Host-agentpal_csrf"]})
    assert response.status_code == 200


@pytest.mark.parametrize("changes", [{"decision": "approve"}, {"decision": "MAYBE"}, {"snapshot_hash": "wrong"},
    {"snapshot_hash_version": True}, {"snapshot_hash_version": 2}, {"snapshot_hash_version": "1"},
    {"owner_session_id": "synthetic-secret-do-not-echo"}, {"price": "0"}, {"quantity": 1}])
def test_payload_strict_and_sanitized(configured, db, changes):
    client, ids, _ = configured
    p = pending(db, ids)
    response = decide(client, p, **changes)
    assert response.status_code == 422 and 'synthetic-secret-do-not-echo' not in response.text
    assert audit_count(db, p["id"]) == 0


def test_body_bound_duplicate_keys_and_content_type(configured, db):
    client, ids, _ = configured
    p = pending(db, ids)
    headers = {"X-CSRF-Token": client.cookies["__Host-agentpal_csrf"], "Content-Type": "application/json"}
    path = f'/purchases/{p["id"]}/approval'
    assert client.put(path, content='"'+'x'*2048+'"', headers=headers).status_code == 413
    assert client.put(path, content='{"decision":"APPROVE","decision":"DENY"}', headers=headers).status_code == 422
    assert client.put(path, content='[]', headers=headers).status_code == 422
    assert client.put(path, content='not-json', headers=headers).status_code == 422
    headers["Content-Type"] = "text/plain"
    assert client.put(path, content=json.dumps(body(p)), headers=headers).status_code == 415


@pytest.mark.parametrize("kind,code", [("expired", "PROPOSAL_EXPIRED"), ("policy", "POLICY_INVALIDATED"),
    ("rejected", "PROPOSAL_REJECTED"), ("auto", "APPROVAL_NOT_REQUIRED"), ("snapshot", "snapshot_conflict")])
def test_invalid_or_stale_proposals_cannot_receive_decisions(configured, db, kind, code):
    client, ids, _ = configured
    if kind == "expired":
        p = pending(db, ids, created=datetime.now(timezone.utc)-timedelta(hours=2))
    elif kind in ("rejected", "auto"):
        p = purchase(db, ids, price="40", decision="rejected" if kind == "rejected" else "auto_authorized")
    else:
        p = pending(db, ids)
    if kind == "policy":
        assert put(client, policy_body(ids["policy"], monthly_limit="200")).status_code == 200
    for decision in ("APPROVE", "DENY"):
        response = decide(client, p, decision, **({"snapshot_hash": "0" * 64} if kind == "snapshot" else {}))
        assert response.status_code == 409, response.text
        assert code in response.text
    assert audit_count(db, p["id"]) == 0
    assert not detail(client, p)["eligibility"]["can_approve"] or kind == "snapshot"


def test_budget_cannot_be_overridden_but_refusal_can_be_recorded(configured, db):
    client, ids, _ = configured
    p = pending(db, ids)
    other = purchase(db, ids, price="50")
    revalidate(db, other)
    reserve(db, other)
    row = detail(client, p)
    assert row["budget_available"] == "50.00" and not row["eligibility"]["can_approve"] and row["eligibility"]["can_deny"]
    assert decide(client, p).status_code == 409
    assert decide(client, p, "DENY").status_code == 200


def test_known_source_failure_blocks_approval_without_external_calls(configured, db):
    client, ids, _ = configured
    p = pending(db, ids)
    revalidate(db, p, result="price_changed", price=59)
    assert "OFFER_INVALIDATED" in detail(client, p)["eligibility"]["reason_codes"]
    assert decide(client, p).status_code == 409
    assert decide(client, p, "DENY").status_code == 200


def test_defensive_financial_checks_for_imported_awaiting_rows(configured, db):
    client, ids, _ = configured
    blocked = pending(db, ids, domain="prohibited.test")
    assert "MERCHANT_NOT_ALLOWED" in decide(client, blocked).text
    excessive = purchase(db, ids, price="301", decision="awaiting_approval")
    assert "HARD_PURCHASE_LIMIT_EXCEEDED" in decide(client, excessive).text


@pytest.mark.parametrize("decision", ["APPROVE", "DENY"])
def test_identical_decisions_replay_conflicts_do_not_overwrite(configured, db, decision):
    client, ids, _ = configured
    p = pending(db, ids)
    first = decide(client, p, decision)
    assert first.status_code == 200
    for _ in range(3):
        response = decide(client, p, decision)
        assert response.status_code == 200 and response.json()["replayed"]
        assert response.json()["purchase"]["human_decision"] == first.json()["purchase"]["human_decision"]
    assert decide(client, p, "DENY" if decision == "APPROVE" else "APPROVE").status_code == 409
    assert decide(client, p, decision, snapshot_hash="0"*64).status_code == 409
    assert audit_count(db, p["id"]) == 1
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.approvals SET decision='denied' WHERE purchase_id=:id"), {"id": p["id"]})
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.audit_events SET details='{}' WHERE purchase_id=:id"), {"id": p["id"]})


def test_decision_replay_preserves_first_owner_session_and_stale_policy(configured, db):
    client, ids, _ = configured
    p = pending(db, ids)
    assert decide(client, p).status_code == 200
    assert client.post('/auth/login', json={"login_name": "policy-owner", "password": PASSWORD}).status_code == 200
    assert put(client, policy_body(ids["policy"], monthly_limit="200")).status_code == 200
    replay = decide(client, p)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert not replay.json()["purchase"]["policy_current"]
    with db.connect() as c:
        assert c.scalar(text("SELECT owner_session_id FROM agentpal.approvals WHERE purchase_id=:id"), {"id": p["id"]}) == ids["session"]
    assert audit_count(db, p["id"]) == 1


@pytest.mark.parametrize("mixed", [False, True])
def test_concurrent_decisions_one_winner_one_audit(configured, db, mixed):
    client, ids, app = configured
    p = pending(db, ids)
    barrier = Barrier(4)
    cookie = client.headers.get('cookie') or '; '.join(f'{key}={value}' for key, value in client.cookies.items())
    csrf = client.cookies["__Host-agentpal_csrf"]

    def submit(index):
        with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN, "Cookie": cookie, "X-CSRF-Token": csrf}) as browser:
            barrier.wait(timeout=10)
            return browser.put(f'/purchases/{p["id"]}/approval', json=body(p, "DENY" if mixed and index % 2 else "APPROVE"))

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(submit, range(4)))
    assert [r.status_code for r in results].count(409) == (2 if mixed else 0)
    successful = [r.json() for r in results if r.status_code == 200]
    assert sum(not r["replayed"] for r in successful) == 1
    assert len({r["purchase"]["human_decision"]["decision"] for r in successful}) == 1
    assert audit_count(db, p["id"]) == 1


def test_dispatched_purchase_replay_never_restarts_payment(configured, db):
    client, ids, _ = configured
    p = pending(db, ids)
    assert decide(client, p).status_code == 200
    revalidate(db, p)
    reserve(db, p)
    pay = payment(db, ids, p)
    op = operation(db, pay)
    dispatch(db, op)
    uncertain(db, pay, op)
    response = decide(client, p)
    assert response.status_code == 200 and response.json()["replayed"]
    assert response.json()["purchase"]["payment_status"] == "UNCERTAIN"
    assert response.json()["purchase"]["eligibility"]["status"] == "DISPATCH_STARTED"
    assert decide(client, p, "DENY").status_code == 409
    with db.connect() as c:
        assert c.scalar(text("SELECT dispatch_count FROM agentpal.payment_operations WHERE id=:id"), {"id": op}) == 1


def test_unknown_purchase_and_bounded_pagination(configured):
    client, _, _ = configured
    unknown = uuid4()
    assert client.get(f'/purchases/{unknown}').status_code == 404
    assert client.put(f'/purchases/{unknown}/approval', json={"decision": "APPROVE", "snapshot_hash": "0"*64, "snapshot_hash_version": 1},
                      headers={"X-CSRF-Token": client.cookies["__Host-agentpal_csrf"]}).status_code == 404
    for params in [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 10001}]:
        assert client.get('/purchases/approval-queue', params=params).status_code == 422


@pytest.mark.parametrize("winner", ["approval", "policy"])
def test_policy_update_and_approval_serialize_in_both_orders(configured, db, monkeypatch, winner):
    client, ids, _ = configured
    p = pending(db, ids)
    entered, release = Event(), Event()
    module, name = (approval_module, "record_decision") if winner == "approval" else (policy_module, "configure_policy")
    original = getattr(module, name)

    def paused(*args, **kwargs):
        # Both handlers are reached after OwnerWrite has authenticated and locked.
        entered.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, paused)
    approve = lambda: decide(client, p)
    replace_policy = lambda: put(client, policy_body(ids["policy"], monthly_limit="200"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(approve if winner == "approval" else replace_policy)
        try:
            assert entered.wait(timeout=10)
            second = pool.submit(replace_policy if winner == "approval" else approve)
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200
        assert second.result(timeout=10).status_code == (200 if winner == "approval" else 409)
    assert audit_count(db, p["id"]) == (1 if winner == "approval" else 0)
    row = detail(client, p)
    assert not row["policy_current"] and not row["eligibility"]["can_approve"]
    assert datetime.fromisoformat(row["expires_at"]) == p["expires_at"]


def test_logout_during_upload_rechecks_session_before_mutation(configured, db):
    client, ids, app = configured
    p = pending(db, ids)
    entered, release = Event(), Event()
    payload = json.dumps(body(p)).encode()
    csrf = client.cookies["__Host-agentpal_csrf"]
    cookies = '; '.join(f'{key}={value}' for key, value in client.cookies.items())

    def slow_body():
        yield payload[:20]
        entered.set()
        assert release.wait(timeout=10)
        yield payload[20:]

    def request():
        return client.put(f'/purchases/{p["id"]}/approval', content=slow_body(),
                          headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request)
        try:
            assert entered.wait(timeout=10)
            # A successful real logout also proves no financial/session lock is
            # held during the unfinished upload.
            # Separate portal: TestClient synchronously consumes the generator
            # on its event loop, unlike a real asynchronous network upload.
            with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN, "Cookie": cookies}) as logout_browser:
                assert logout_browser.post('/auth/logout', headers={"X-CSRF-Token": csrf}).status_code == 204
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 401
    assert audit_count(db, p["id"]) == 0


def test_approval_commits_before_racing_logout(configured, db, monkeypatch):
    client, ids, _ = configured
    p = pending(db, ids)
    entered, release = Event(), Event()
    original = approval_module.record_decision

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(approval_module, "record_decision", pause)
    csrf = client.cookies["__Host-agentpal_csrf"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(lambda: decide(client, p))
        try:
            assert entered.wait(timeout=10)
            logout = pool.submit(lambda: client.post('/auth/logout', headers={"X-CSRF-Token": csrf}))
        finally:
            release.set()
        assert approval.result(timeout=10).status_code == 200
        assert logout.result(timeout=10).status_code == 204
    assert audit_count(db, p["id"]) == 1
    with db.connect() as c:
        assert c.scalar(text("SELECT revoked_at IS NOT NULL FROM agentpal.owner_sessions WHERE id=:session"), ids)


@pytest.mark.parametrize("expiry", ["purchase", "session"])
def test_expiry_after_insert_before_commit_returns_conflict_and_rolls_back(configured, db, expiry):
    client, ids, _ = configured
    if expiry == "purchase":
        policy = put(client, policy_body(ids["policy"], purchase_ttl_seconds=1))
        assert policy.status_code == 200
        from uuid import UUID
        ids = ids | {"policy": UUID(policy.json()["policy"]["id"])}
    p = pending(db, ids)
    headers = {"X-CSRF-Token": client.cookies["__Host-agentpal_csrf"]}
    if expiry == "session":
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with db.begin() as c:
            c.execute(text("""INSERT INTO agentpal.owner_sessions(token_hash,csrf_token_hash,created_at,expires_at)
                VALUES (:token,:csrf,clock_timestamp()-interval '1 minute',clock_timestamp()+interval '1 second')"""),
                {"token": token_hash(token, "session"), "csrf": token_hash(csrf, "csrf")})
        headers = {"Cookie": "__Host-agentpal_session="+token, "X-CSRF-Token": csrf}
    # Immediate audit trigger delays the transaction AFTER the approval INSERT,
    # before the deferred production guard runs at COMMIT. Test database only.
    target = "(SELECT expires_at FROM agentpal.purchases WHERE id=NEW.purchase_id)" if expiry == "purchase" else "(SELECT expires_at FROM agentpal.owner_sessions WHERE id=NEW.owner_session_id)"
    with db.begin() as c:
        c.exec_driver_sql(f"""CREATE FUNCTION agentpal.test_delay_decision() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.kind='human_decision_recorded' THEN
              PERFORM pg_sleep(greatest(0,extract(epoch FROM ({target}-clock_timestamp())))+0.01);
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER test_delay_decision AFTER INSERT ON agentpal.audit_events
            FOR EACH ROW EXECUTE FUNCTION agentpal.test_delay_decision()""")
    response = client.put(f'/purchases/{p["id"]}/approval', json=body(p), headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "approval_ineligible"
    assert audit_count(db, p["id"]) == 0
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.approvals")) == 0


@pytest.mark.parametrize("state,code", [("40001", "transaction_conflict"), ("40P01", "transaction_conflict"), ("23505", "decision_conflict")])
def test_deferred_database_abort_has_no_false_success_and_retry_is_idempotent(configured, db, state, code):
    client, ids, _ = configured
    p = pending(db, ids)
    with db.begin() as c:
        c.exec_driver_sql(f"""CREATE SEQUENCE agentpal.test_approval_abort_counter;
            CREATE FUNCTION agentpal.test_abort_approval_once() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
              IF NEW.kind='human_decision_recorded' AND nextval('agentpal.test_approval_abort_counter')=1 THEN
                RAISE EXCEPTION 'synthetic private DB failure' USING ERRCODE='{state}';
              END IF;
              RETURN NEW;
            END $$;
            CREATE CONSTRAINT TRIGGER test_abort_approval_once AFTER INSERT ON agentpal.audit_events
              DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION agentpal.test_abort_approval_once()""")
    response = decide(client, p)
    assert response.status_code == 409 and response.json()["detail"]["code"] == code
    assert 'private DB failure' not in response.text and 'INSERT' not in response.text
    assert audit_count(db, p["id"]) == 0
    assert detail(client, p)["human_decision"] is None
    assert decide(client, p).status_code == 200
    assert decide(client, p).json()["replayed"]
    assert audit_count(db, p["id"]) == 1


def test_new_decision_cannot_be_added_after_dispatch(configured, db):
    client, ids, _ = configured
    p = purchase(db, ids, price="40")
    revalidate(db, p)
    reserve(db, p)
    pay = payment(db, ids, p)
    op = operation(db, pay)
    dispatch(db, op)
    response = decide(client, p)
    assert response.status_code == 409 and "ALREADY_DISPATCHED" in response.text
    assert audit_count(db, p["id"]) == 0


def test_denial_cannot_authorize_a_reservation(configured, db):
    client, ids, _ = configured
    p = pending(db, ids)
    assert decide(client, p, "DENY").status_code == 200
    revalidate(db, p)
    with pytest.raises(IntegrityError):
        reserve(db, p)
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.budget_reservations")) == 0


def test_read_and_decision_do_not_initialize_a_missing_month(db, seed):
    p = purchase(db, seed, price="60", decision="awaiting_approval")
    from agentpal.auth.service import OwnerSession, OwnerWriteContext
    with db.connect() as c:
        assert approval_module.purchase_detail(c, p["id"]).budget_available == 100
    with db.begin() as c:
        approval_module.record_decision(OwnerWriteContext(OwnerSession("test-owner", seed["session"], b"c"*32), c), p["id"],
                                        approval_module.ApprovalInput.model_validate(body(p)))
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.budget_periods")) == 0


def test_forward_migration_preserves_prior_decisions_and_is_reproducible(db, seed):
    config = Config('alembic.ini')
    with db.begin() as c:
        config.attributes['connection'] = c
        command.downgrade(config, '0002')
    p = purchase(db, seed, price="60", decision="awaiting_approval")
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.approvals(purchase_id,owner_session_id,snapshot_hash,decision)
            VALUES (:id,:session,:hash,'denied')"""), {"id": p["id"], "session": seed["session"], "hash": p["snapshot_hash"]})
    with db.connect() as c:
        before = c.execute(text("SELECT * FROM agentpal.approvals")).one()
    with db.begin() as c:
        config.attributes['connection'] = c
        command.upgrade(config, 'head')
        command.upgrade(config, 'head')
        assert c.execute(text("SELECT * FROM agentpal.approvals")).one() == before
        assert c.execute(text("SELECT tgdeferrable,tginitdeferred FROM pg_trigger WHERE tgname='approval_commit_guard'")).one() == (True, True)
    assert audit_count(db, p["id"]) == 1


def test_database_rechecks_expiry_at_commit(db, seed):
    with db.begin() as c:
        policy = c.scalar(text("""INSERT INTO agentpal.policies
            (monthly_limit,auto_approval_limit,hard_purchase_limit,purchase_ttl_seconds,merchant_allowlist,created_by_session_id)
            SELECT monthly_limit,auto_approval_limit,hard_purchase_limit,1,merchant_allowlist,created_by_session_id
            FROM agentpal.policies WHERE id=:policy RETURNING id"""), seed)
        c.execute(text("UPDATE agentpal.owners SET active_policy_id=:id"), {"id": policy})
    p = purchase(db, seed | {"policy": policy}, price="60", decision="awaiting_approval")
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.approvals(purchase_id,owner_session_id,snapshot_hash,decision)
            VALUES (:id,:session,:hash,'approved')"""), {"id": p["id"], "session": seed["session"], "hash": p["snapshot_hash"]})
        c.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (:expiry-clock_timestamp())))+0.01)"), {"expiry": p["expires_at"]})
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.approvals")) == 0
