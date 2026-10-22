"""Synthetic documented PayPal responses, real private PostgreSQL and owner API.

No test-only production switches. Positive cases exercise the actual hosted
callback, provider adapter, encrypted persistence and materialization path.
"""

from concurrent.futures import ThreadPoolExecutor
import base64
import json
import secrets
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from agentpal.auth.service import OwnerSession, token_hash
from agentpal.orders import MaterializationError, OrderMaterializationService
from agentpal.readiness import ReadinessService
from agentpal.vault.agreement import TERMS
from agentpal.vault.service import Authority, VaultService
from test_proposals import build, select
from test_readiness import PAYEE, run, settings
from test_persistence import dispatch
from test_persistence import operation as legacy_operation
from test_vault import (CSRF, CUSTOMER, RECEIPT, SESSION, SETUP, TOKEN, callback, complete,
                       config, expire, flow, start)  # noqa: F401 -- shared owner fixture


def echo(path, value):
    if path != "/v1/oauth2/token":
        value.setdefault("payment_source", {})["paypal"] = dict(TERMS)
    return value


@pytest.fixture
def verified_flow(flow):
    flow[2].transform = echo
    return flow


def onboard(flow, *, replacement=False):
    if replacement:
        response = flow[0].post("/paypal/vault/replace", json={})
        assert response.status_code == 200, response.text
        identifier = response.json()["onboarding_id"]
    else:
        identifier = start(flow)
    callback(flow)
    response = complete(flow, identifier)
    assert response.status_code == 200, response.text
    return UUID(identifier), response.json()


def method(db):
    with db.connect() as c:
        return dict(c.execute(text("SELECT * FROM agentpal.payment_methods WHERE revoked_at IS NULL")).mappings().one())


def row(db, identifier):
    with db.connect() as c:
        return dict(c.execute(text("SELECT *,expires_at<=clock_timestamp() AS expired FROM agentpal.vault_setups WHERE id=:id"),
            {"id": identifier}).mappings().one())


def prepare(flow, db):
    client, _, _, session = flow
    response = client.put("/policy", json=dict(expected_policy_id=None, monthly_limit="100", auto_approval_limit="50",
        hard_purchase_limit="300", purchase_ttl_seconds=3600, merchant_allowlist=["allowed.test"]))
    assert response.status_code == 200, response.text
    agent = uuid4()
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.owners SET sandbox_business_account_id=:payee"), {"payee": PAYEE})
        c.execute(text("INSERT INTO agentpal.agents(id,name) VALUES (:id,'fictional-agreement-agent')"), {"id": agent})
    proposals, discovery, _, _, _ = build(db, {"agent": agent})
    context = proposals.start_run(OwnerSession("vault-owner", session, token_hash(CSRF, "csrf")), agent)
    p = run(proposals.create(context, select(discovery, context), 1)).proposal
    readiness = ReadinessService(db, discovery._client, settings())
    ready = run(readiness.prepare(context, p.id))
    assert ready.state == "PREPARED_UNSENT"
    return readiness, context, p, ready


def test_explicit_agreement_actual_pipeline_and_persisted_evidence(verified_flow, db, caplog):
    client, app, provider, _ = verified_flow
    identifier = start(verified_flow)
    payload = provider.payload["payment_source"]["paypal"]
    assert payload["usage_pattern"] == "IMMEDIATE" and payload["usage_type"] == "MERCHANT"
    assert payload["customer_type"] == "CONSUMER" and payload["permit_multiple_payment_tokens"] is True
    assert len(payload["description"]) <= 128
    assert all(word in payload["description"] for word in ("Sandbox", "variable", "on-demand", "no retailer", "fulfillment"))
    assert "payment_initiator" not in payload and "usage" not in payload and "billing_plan" not in payload
    callback(verified_flow)
    response = complete(verified_flow, identifier)
    assert response.json()["state"] == "complete" and response.json()["agreement_state"] == "verified"
    assert client.get("/paypal/vault/status").json()["agreement_state"] == "verified"
    service = app.state.vault_service
    saved = row(db, identifier)
    approved = service.keys.open(UUID(identifier), "approved-setup", saved["approved_reference_ciphertext"], saved["approved_key_id"])
    assert approved == {"id": SETUP, "customer_id": CUSTOMER, "status": "APPROVED", "metadata": TERMS}
    proof = service.keys.open(UUID(identifier), "usage-agreement", saved["agreement_evidence_ciphertext"], saved["agreement_evidence_key_id"])
    assert proof == {"version": 1, "payment_token": {"id": TOKEN, "customer_id": CUSTOMER, "metadata": TERMS}}
    assert TOKEN.encode() not in saved["agreement_evidence_ciphertext"]
    app.state.vault_service = VaultService(app.state.auth_service, config(), service.provider)
    assert complete(verified_flow, identifier).json()["agreement_state"] == "verified"
    readiness, context, p, ready = prepare(verified_flow, db)
    calls = list(provider.calls)
    result = OrderMaterializationService(readiness, service.keys).materialize(context, p.id)
    assert result.state == "MATERIALIZED_UNSENT" and provider.calls == calls
    with db.connect() as c:
        operation = c.execute(text("SELECT * FROM agentpal.payment_operations")).mappings().one()
        assert operation["dispatch_count"] == 0 and operation["first_dispatch_at"] is None
        assert c.scalar(text("SELECT count(*) FROM agentpal.provider_observations")) == 0
        assert c.scalar(text("SELECT count(*) FROM agentpal.payment_captures")) == 0
        assert c.scalar(text("SELECT payment_method_id FROM agentpal.payments")) == method(db)["id"]
    assert TOKEN not in response.text + caplog.text and CUSTOMER not in response.text


@pytest.mark.parametrize("stage", ["approved", "retrieved"])
@pytest.mark.parametrize("field", ["usage_pattern", "usage_type", "customer_type", "description"])
def test_missing_required_get_fields_never_imply_agreement(flow, db, stage, field):
    target = f"/v3/vault/{'setup-tokens/'+SETUP if stage == 'approved' else 'payment-tokens/'+TOKEN}"
    def transform(path, value):
        echo(path, value)
        if path == target: del value["payment_source"]["paypal"][field]
        return value
    flow[2].transform = transform
    identifier, result = onboard(flow)
    assert result["state"] == "complete" and result["agreement_state"] == "unverified"
    readiness, context, p, _ = prepare(flow, db)
    blocked = OrderMaterializationService(readiness, flow[1].state.vault_service.keys).materialize(context, p.id)
    assert blocked.reason_codes == ("PAYMENT_CONTRACT_UNVERIFIED",)
    assert flow[0].get("/paypal/vault/status").json()["agreement_state"] == "unverified"


@pytest.mark.parametrize("stage", ["setup_post", "approved", "token_post", "retrieved"])
@pytest.mark.parametrize("field,value", [("usage_pattern", "UNSCHEDULED_POSTPAID"), ("usage_type", "PLATFORM"),
    ("customer_type", "BUSINESS"), ("description", "misleading retailer fulfillment")])
def test_provider_conflicts_are_quarantined_with_no_activation(flow, db, stage, field, value):
    target = {"setup_post": "/v3/vault/setup-tokens", "approved": f"/v3/vault/setup-tokens/{SETUP}",
              "token_post": "/v3/vault/payment-tokens", "retrieved": f"/v3/vault/payment-tokens/{TOKEN}"}[stage]
    def transform(path, result):
        echo(path, result)
        if path == target: result["payment_source"]["paypal"][field] = value
        return result
    flow[2].transform = transform
    identifier = flow[0].post("/paypal/vault/start").json()["onboarding_id"]
    if stage != "setup_post":
        callback(flow)
        result = complete(flow, identifier).json()
    else: result = flow[0].get("/paypal/vault/status").json()["onboarding"]
    assert result["state"] == "review_required" and result["agreement_state"] == "unverified"
    assert not flow[0].get("/paypal/vault/status").json()["connected"]
    assert flow[2].count("/v3/vault/payment-tokens") == int(stage in ("token_post", "retrieved"))
    assert flow[0].post("/paypal/vault/start").json()["onboarding_id"] == identifier
    if stage in ("token_post", "retrieved"):
        assert row(db, identifier)["token_ciphertext"] is not None


def test_post_fields_are_not_assumed_when_absent_but_gets_can_confirm(verified_flow, db):
    def transform(path, value):
        if path not in ("/v3/vault/setup-tokens", "/v3/vault/payment-tokens"):
            echo(path, value)
        return value
    verified_flow[2].transform = transform
    identifier, result = onboard(verified_flow)
    assert result["agreement_state"] == "verified"
    saved = row(db, identifier)
    created = verified_flow[1].state.vault_service.decode(saved, "setup")
    assert all(value is None for value in created["metadata"].values())


def test_unverified_owner_replacement_is_atomic_and_old_payment_cannot_switch(flow, db):
    old_setup, old_result = onboard(flow)  # No provider metadata: explicitly unverified.
    old = method(db)
    readiness, context, p, ready = prepare(flow, db)
    svc = OrderMaterializationService(readiness, flow[1].state.vault_service.keys)
    assert svc.materialize(context, p.id).reason_codes == ("PAYMENT_CONTRACT_UNVERIFIED",)
    flow[2].transform = echo
    response = flow[0].post("/paypal/vault/replace")
    identifier = response.json()["onboarding_id"]
    assert response.json()["replacement"]
    assert flow[0].post("/paypal/vault/replace").json()["onboarding_id"] == identifier
    assert method(db)["id"] == old["id"]
    callback(flow)
    result = complete(flow, identifier).json()
    assert result["agreement_state"] == "verified" and result["state"] == "complete"
    assert method(db)["id"] != old["id"]
    assert flow[0].post("/paypal/vault/replace").status_code == 409
    with db.connect() as c:
        assert c.scalar(text("SELECT revoked_at IS NOT NULL FROM agentpal.payment_methods WHERE id=:id"), {"id": old["id"]})
        assert c.scalar(text("SELECT payment_method_id FROM agentpal.payments")) == old["id"]
        assert c.scalar(text("SELECT count(*) FROM agentpal.payment_methods")) == 2
        assert c.scalar(text("SELECT replaces_payment_method_id FROM agentpal.vault_setups WHERE id=:id"), {"id": identifier}) == old["id"]
    assert svc.materialize(context, p.id).state == "RELEASED_UNSENT"
    assert method(db)["id"] != old["id"]


@pytest.mark.parametrize("failure", ["cancel", "missing", "mismatch", "timeout", "expired", "session"])
def test_failed_replacement_preserves_original_method_and_history(flow, db, failure):
    onboard(flow)
    old = method(db)
    flow[2].transform = echo
    identifier = flow[0].post("/paypal/vault/replace").json()["onboarding_id"]
    if failure == "cancel":
        callback(flow, cancel=True)
    else:
        callback(flow)
        if failure == "expired": expire(db, identifier)
        if failure == "session":
            with db.begin() as c:
                c.execute(text("UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp() WHERE id=:id"), {"id": flow[3]})
        if failure == "timeout": flow[2].failure = ("POST", "/v3/vault/payment-tokens")
        if failure in ("missing", "mismatch"):
            def transform(path, value):
                echo(path, value)
                if path.endswith("/" + flow[2].token_id):
                    if failure == "missing": del value["payment_source"]["paypal"]["usage_pattern"]
                    else: value["payment_source"]["paypal"]["usage_type"] = "PLATFORM"
                return value
            flow[2].transform = transform
    complete(flow, identifier)
    assert method(db) == old
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.payment_methods")) == 1
    assert row(db, identifier)["flow_phase"] != "complete"


def test_known_token_restart_retains_approved_evidence_and_no_second_post(verified_flow, db):
    identifier = start(verified_flow)
    callback(verified_flow)
    verified_flow[2].failure = ("GET", f"/v3/vault/payment-tokens/{TOKEN}")
    assert complete(verified_flow, identifier).json()["state"] == "verification_required"
    before = row(db, identifier)
    assert before["approved_reference_ciphertext"] is not None
    service = verified_flow[1].state.vault_service
    verified_flow[1].state.vault_service = VaultService(verified_flow[1].state.auth_service, config(), service.provider)
    verified_flow[2].failure = None
    assert complete(verified_flow, identifier).json()["agreement_state"] == "verified"
    assert row(db, identifier)["approved_reference_ciphertext"] == before["approved_reference_ciphertext"]
    assert verified_flow[2].count("/v3/vault/payment-tokens") == 1


@pytest.mark.parametrize("column", ["approved_reference_ciphertext", "approved_key_id", "agreement_evidence_ciphertext", "agreement_evidence_key_id", "replaces_payment_method_id"])
def test_once_set_evidence_and_target_are_immutable(verified_flow, db, column):
    identifier, _ = onboard(verified_flow)
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text(f"UPDATE agentpal.vault_setups SET {column}=NULL WHERE id=:id"), {"id": identifier})


@pytest.mark.parametrize("kind", ["tamper", "missing_key", "local_flag"])
def test_proof_integrity_and_locally_asserted_verification_never_grant_authority(verified_flow, db, kind):
    identifier, _ = onboard(verified_flow)
    readiness, context, p, _ = prepare(verified_flow, db)
    with db.begin() as c:
        c.execute(text("ALTER TABLE agentpal.vault_setups DISABLE TRIGGER guard_setup"))
        c.execute(text("ALTER TABLE agentpal.vault_setups DISABLE TRIGGER guard_agreement_material"))
        if kind == "tamper":
            c.execute(text("UPDATE agentpal.vault_setups SET agreement_evidence_ciphertext=decode(repeat('ff',32),'hex') WHERE id=:id"), {"id": identifier})
        elif kind == "missing_key":
            c.execute(text("UPDATE agentpal.vault_setups SET agreement_evidence_key_id='removed' WHERE id=:id"), {"id": identifier})
        else:
            service = verified_flow[1].state.vault_service
            cipher, key = service.keys.seal(identifier, "usage-agreement", {"version": 1, "verified": True, "usage_pattern": "IMMEDIATE"})
            c.execute(text("UPDATE agentpal.vault_setups SET agreement_evidence_ciphertext=:cipher,agreement_evidence_key_id=:key WHERE id=:id"),
                {"id": identifier, "cipher": cipher, "key": key})
        c.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        c.execute(text("ALTER TABLE agentpal.vault_setups ENABLE TRIGGER guard_setup"))
        c.execute(text("ALTER TABLE agentpal.vault_setups ENABLE TRIGGER guard_agreement_material"))
    svc = OrderMaterializationService(readiness, verified_flow[1].state.vault_service.keys)
    if kind == "local_flag":
        assert svc.materialize(context, p.id).reason_codes == ("PAYMENT_CONTRACT_UNVERIFIED",)
    else:
        with pytest.raises(MaterializationError, match="^ENCRYPTION_UNAVAILABLE$"):
            svc.materialize(context, p.id)
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.payment_operations")) == 0


@pytest.mark.parametrize("headers", [{"Cookie": ""}, {"X-CSRF-Token": ""}, {"Origin": "https://attacker.test"}])
def test_replacement_is_owner_csrf_origin_protected(flow, headers):
    assert flow[0].post("/paypal/vault/replace", headers=headers).status_code in (401, 403)
    assert flow[2].calls == []


def test_agreement_cannot_be_defined_by_request_body(flow):
    assert flow[0].post("/paypal/vault/start", json={"usage_pattern": "IMMEDIATE"}).status_code == 400
    assert flow[0].post("/paypal/vault/replace", json={"verified": True}).status_code == 400
    assert not flow[2].calls


def test_new_proof_prevents_destructive_downgrade(verified_flow, db):
    identifier, _ = onboard(verified_flow)
    cfg = Config("alembic.ini")
    with pytest.raises(DBAPIError), db.begin() as c:
        cfg.attributes["connection"] = c
        command.downgrade(cfg, "0005")
    assert row(db, identifier)["agreement_evidence_ciphertext"] is not None
    with db.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0006"


@pytest.mark.parametrize("sent", [False, True])
def test_replacement_preserves_original_operation_identity_and_financial_protection(flow, db, sent):
    onboard(flow)
    old = method(db)
    readiness, context, p, ready = prepare(flow, db)
    op = legacy_operation(db, ready.payment_id)
    if sent: dispatch(db, op)  # SQL boundary only; no HTTP is sent.
    with db.connect() as c:
        original = dict(c.execute(text("SELECT * FROM agentpal.payment_operations")).mappings().one())
    flow[2].transform = echo
    onboard(flow, replacement=True)
    result = OrderMaterializationService(readiness, flow[1].state.vault_service.keys).materialize(context, p.id)
    assert result.state == ("EXISTING_PAYMENT" if sent else "RELEASED_UNSENT")
    assert result.operation_id == op
    with db.connect() as c:
        assert dict(c.execute(text("SELECT * FROM agentpal.payment_operations")).mappings().one()) == original
        assert c.scalar(text("SELECT payment_method_id FROM agentpal.payments")) == old["id"]
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == ("held" if sent else "released")
        assert c.scalar(text("SELECT count(*) FROM agentpal.provider_observations")) == 0


def test_concurrent_replacement_starts_and_completions_do_not_duplicate_posts(flow, db):
    onboard(flow)
    old = method(db)
    flow[2].transform = echo
    service, provider = flow[1].state.vault_service, flow[2]
    provider.block = ("POST", "/v3/vault/setup-tokens")
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(service.start, Authority(SESSION, CSRF), replace=True)
        try:
            assert provider.entered.wait(10)
            replay = pool.submit(service.start, Authority(SESSION, CSRF), replace=True).result(10)
        finally: provider.released.set()
        initial = first.result(10)
    assert replay["onboarding_id"] == initial["onboarding_id"]
    assert provider.count("/v3/vault/setup-tokens") == 2  # One original, one replacement.
    callback(flow)
    provider.entered.clear()
    provider.released.clear()
    provider.block = ("POST", "/v3/vault/payment-tokens")
    identifier = UUID(initial["onboarding_id"])
    receipt = flow[0].cookies[RECEIPT]
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(service.complete, Authority(SESSION, CSRF), identifier, receipt)
        try:
            assert provider.entered.wait(10)
            assert method(db)["id"] == old["id"]
            replay = pool.submit(service.complete, Authority(SESSION, CSRF), identifier, receipt).result(10)
            assert replay["state"] == "review_required"
        finally: provider.released.set()
        assert first.result(10)["agreement_state"] == "verified"
    assert provider.count("/v3/vault/payment-tokens") == 2
    assert method(db)["id"] != old["id"]


def test_failed_replacement_commit_rolls_back_revocation_and_activation(flow, db):
    onboard(flow)
    old = method(db)
    flow[2].transform = echo
    identifier = flow[0].post("/paypal/vault/replace").json()["onboarding_id"]
    callback(flow)
    class Abort(Exception): sqlstate = "40001"
    def abort(c, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO agentpal.payment_methods"):
            raise IntegrityError("synthetic private failure", {}, Abort())
    event.listen(db, "after_cursor_execute", abort)
    try: assert complete(flow, identifier).status_code == 503
    finally: event.remove(db, "after_cursor_execute", abort)
    assert method(db) == old
    saved = row(db, identifier)
    assert saved["flow_phase"] == "verifying_token" and saved["token_ciphertext"] is not None
    assert saved["agreement_evidence_ciphertext"] is None
    assert complete(flow, identifier).json()["agreement_state"] == "verified"
    assert flow[2].count("/v3/vault/payment-tokens") == 2


def test_absent_metadata_cannot_be_upgraded_by_replaying_completed_flow(flow, db):
    identifier, result = onboard(flow)
    assert result["agreement_state"] == "unverified"
    saved = row(db, identifier)
    calls = list(flow[2].calls)
    flow[2].transform = echo
    assert complete(flow, str(identifier)).json()["agreement_state"] == "unverified"
    assert flow[2].calls == calls and row(db, identifier) == saved


def test_duplicate_historical_token_is_preserved_for_review_and_never_activated(flow, db):
    onboard(flow)
    old = method(db)
    def duplicate(path, result):
        echo(path, result)
        if path == "/v3/vault/payment-tokens":
            flow[2].token_id = TOKEN
            result["id"] = TOKEN
        return result
    flow[2].transform = duplicate
    identifier, result = onboard(flow, replacement=True)
    assert result["state"] == "review_required" and method(db) == old
    assert row(db, identifier)["token_ciphertext"] is not None


def test_duplicate_historical_setup_cannot_reuse_old_hosted_approval(flow, db):
    onboard(flow)
    old = method(db)
    def duplicate(path, result):
        echo(path, result)
        if path == "/v3/vault/setup-tokens":
            result["id"] = SETUP
            result["links"][0]["href"] = f"https://sandbox.paypal.com/agreements/approve?approval_session_id={SETUP}"
        return result
    flow[2].transform = duplicate
    result = flow[0].post("/paypal/vault/replace").json()
    assert result["state"] == "review_required" and "approval_url" not in result
    assert method(db) == old and flow[2].count("/v3/vault/payment-tokens") == 1


def test_approved_snapshot_is_immutable_before_completion(verified_flow, db):
    identifier = start(verified_flow)
    callback(verified_flow)
    verified_flow[2].failure = ("GET", f"/v3/vault/payment-tokens/{TOKEN}")
    assert complete(verified_flow, identifier).json()["state"] == "verification_required"
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.vault_setups SET approved_key_id='locally-asserted' WHERE id=:id"), {"id": identifier})


def test_legacy_upgrade_roundtrip_does_not_invent_agreement(db, seed):
    with db.connect() as c:
        original = c.scalar(text("SELECT reference_ciphertext FROM agentpal.payment_methods"))
    cfg = Config("alembic.ini")
    with db.begin() as c:
        cfg.attributes["connection"] = c
        command.downgrade(cfg, "0005")
        command.upgrade(cfg, "head")
    saved = row(db, seed["setup"])
    assert saved["approved_reference_ciphertext"] is None and saved["agreement_evidence_ciphertext"] is None
    assert saved["replaces_payment_method_id"] is None
    with db.connect() as c:
        assert c.scalar(text("SELECT reference_ciphertext FROM agentpal.payment_methods")) == original


def test_agreement_evidence_survives_supported_key_rotation(verified_flow, db):
    identifier, _ = onboard(verified_flow)
    old = row(db, identifier)
    original = verified_flow[1].state.vault_service
    rotated = config(VAULT_ACTIVE_KEY_ID="new", VAULT_KEYS_JSON=json.dumps({
        "test-key": base64.b64encode(b"k"*32).decode(),
        "new": base64.b64encode(secrets.token_bytes(32)).decode()}))
    verified_flow[1].state.vault_service = VaultService(verified_flow[1].state.auth_service, rotated, original.provider)
    assert complete(verified_flow, str(identifier)).json()["agreement_state"] == "verified"
    assert row(db, identifier) == old


def test_provider_duplicate_metadata_keys_are_rejected(flow):
    def invalid(path, value):
        if path == "/v3/vault/setup-tokens":
            return httpx.Response(200, content=b'{"id":"one","id":"two"}', headers={"Content-Type": "application/json"})
        return value
    flow[2].transform = invalid
    result = flow[0].post("/paypal/vault/start").json()
    assert result["state"] == "review_required"
    assert flow[2].count("/v3/vault/setup-tokens") == 1


@pytest.mark.parametrize("stage", ["setup", "token"])
def test_explicit_wrong_post_payment_source_cannot_be_ignored(verified_flow, db, stage):
    target = "/v3/vault/setup-tokens" if stage == "setup" else "/v3/vault/payment-tokens"
    def wrong_source(path, value):
        echo(path, value)
        if path == target: value["payment_source"] = {"card": {}}
        return value
    verified_flow[2].transform = wrong_source
    identifier = verified_flow[0].post("/paypal/vault/start").json()["onboarding_id"]
    if stage == "token":
        callback(verified_flow)
        assert complete(verified_flow, identifier).json()["state"] == "review_required"
    assert row(db, identifier)["flow_phase"] == "review"
    assert not verified_flow[0].get("/paypal/vault/status").json()["connected"]


def test_database_rejects_nonatomic_replacement_even_with_a_new_method(flow, db):
    onboard(flow)
    old = method(db)
    flow[2].transform = echo
    identifier = flow[0].post("/paypal/vault/replace").json()["onboarding_id"]
    callback(flow)
    flow[2].failure = ("GET", "/v3/vault/payment-tokens/" + flow[2].token_id)
    assert complete(flow, identifier).json()["state"] == "verification_required"
    service = flow[1].state.vault_service
    saved = row(db, identifier)
    cipher, key = service.keys.seal(UUID(identifier), "payment-method", service.decode(saved, "token"))
    # The older completion guard sees a matching method, but the new deferred
    # check must reject leaving the old replacement target active alongside it.
    with pytest.raises(IntegrityError, match="replacement activation must be atomic"), db.begin() as c:
        verified = c.scalar(text("""UPDATE agentpal.vault_setups SET status='verified',flow_phase='complete',
            verified_at=clock_timestamp() WHERE id=:id RETURNING verified_at"""), {"id": identifier})
        c.execute(text("""INSERT INTO agentpal.payment_methods(vault_setup_id,reference_ciphertext,encryption_key_id,verified_at)
            VALUES (:id,:cipher,:key,:verified)"""), {"id": identifier, "cipher": cipher, "key": key, "verified": verified})
    assert method(db) == old and row(db, identifier)["flow_phase"] == "verifying_token"
