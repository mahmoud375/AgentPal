"""Synthetic PayPal HTTP only, durable flows on private disposable PostgreSQL."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from threading import Event
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agentpal.auth.config import AuthSettings
from agentpal.auth.service import token_hash
from agentpal.main import create_app
from agentpal.vault.paypal import PayPal, approval_url
from agentpal.vault.security import Keyring, VaultSettings, VaultUnavailable
from agentpal.vault.service import Authority, VaultService
from agentpal.vault.agreement import DESCRIPTION


ORIGIN = "https://agentpal.test"
SESSION = "s" * 43
CSRF = "c" * 43
SETUP = "FICTIONAL_SETUP_01"
TOKEN = "FICTIONAL_TOKEN_01"
CUSTOMER = "fictional_customer"
COOKIE = "__Host-agentpal_session"
RECEIPT = "__Host-agentpal_vault_return"


def config(**overrides):
    values = dict(PAYPAL_ENVIRONMENT="sandbox", PAYPAL_BASE_URL="https://api-m.sandbox.paypal.com",
                  PAYPAL_CLIENT_ID="fictional_client", PAYPAL_CLIENT_SECRET="fictional_secret",
                  VAULT_ACTIVE_KEY_ID="test-key", VAULT_KEYS_JSON=json.dumps({"test-key": base64.b64encode(b"k"*32).decode()}))
    return VaultSettings(**(values | overrides))


class Provider:
    def __init__(self, engine):
        self.engine = engine
        self.calls = []
        self.payload = None
        self.failure = None
        self.block = None
        self.entered = Event()
        self.released = Event()
        self.transform = lambda path, value: value
        self.setup_id, self.token_id = SETUP, TOKEN

    def __call__(self, request):
        assert self.engine.pool.checkedout() == 0, "Network must not hold database connections"
        path = request.url.path
        assert request.url.scheme == "https" and request.url.host == "api-m.sandbox.paypal.com"
        assert path == "/v1/oauth2/token" or path.startswith(("/v3/vault/setup-tokens", "/v3/vault/payment-tokens"))
        self.calls.append((request.method, path, request.headers.get("PayPal-Request-Id")))
        if self.block == (request.method, path):
            self.entered.set()
            assert self.released.wait(10)
        if self.failure == (request.method, path):
            raise httpx.ReadTimeout("fictional_sensitive_provider_failure", request=request)
        if path == "/v1/oauth2/token":
            assert request.method == "POST" and b"grant_type=client_credentials" in request.content
            result = {"access_token": "fictional_oauth_secret", "token_type": "Bearer"}
        elif path == "/v3/vault/setup-tokens":
            assert request.method == "POST" and request.headers["PayPal-Request-Id"]
            number = self.count(path)
            self.setup_id = SETUP if number == 1 else f"FICTIONAL_SETUP_{number:02}"
            self.token_id = TOKEN if number == 1 else f"FICTIONAL_TOKEN_{number:02}"
            self.payload = json.loads(request.content)
            result = {"id": self.setup_id, "customer": {"id": CUSTOMER}, "status": "PAYER_ACTION_REQUIRED",
                      "links": [{"rel": "approve", "method": "GET", "href": f"https://sandbox.paypal.com/agreements/approve?approval_session_id={self.setup_id}"}]}
        elif path == f"/v3/vault/setup-tokens/{self.setup_id}":
            assert request.method == "GET"
            result = {"id": self.setup_id, "customer": {"id": CUSTOMER}, "status": "APPROVED", "payment_source": {"paypal": {}}}
        elif path == "/v3/vault/payment-tokens":
            assert request.method == "POST" and request.headers["PayPal-Request-Id"]
            assert json.loads(request.content) == {"payment_source": {"token": {"id": self.setup_id, "type": "SETUP_TOKEN"}}}
            result = {"id": self.token_id, "customer": {"id": CUSTOMER}}
        elif path == f"/v3/vault/payment-tokens/{self.token_id}":
            assert request.method == "GET"
            result = {"id": self.token_id, "customer": {"id": CUSTOMER}, "payment_source": {"paypal": {}}}
        else:
            pytest.fail("Unexpected provider operation")
        result = self.transform(path, result)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    def count(self, path, method="POST"):
        return sum(m == method and p == path for m, p, _ in self.calls)

    def return_url(self, cancel=False):
        return self.payload["payment_source"]["paypal"]["experience_context"]["cancel_url" if cancel else "return_url"]


@pytest.fixture
def flow(db):
    session_id = uuid4()
    with db.begin() as c:
        c.execute(text("INSERT INTO agentpal.owners(login_name,password_hash) VALUES ('vault-owner','$argon2id$fictional')"))
        c.execute(text("""INSERT INTO agentpal.owner_sessions(id,token_hash,csrf_token_hash,expires_at)
            VALUES (:id,:token,:csrf,clock_timestamp()+interval '1 hour')"""),
            {"id": session_id, "token": token_hash(SESSION, "session"), "csrf": token_hash(CSRF, "csrf")})
    app = create_app(engine=db, settings=AuthSettings(AUTH_ORIGIN=ORIGIN))
    provider = Provider(db)
    settings = config()
    app.state.vault_service = VaultService(app.state.auth_service, settings, PayPal(settings, transport=httpx.MockTransport(provider)))
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF}) as client:
        client.cookies.set(COOKIE, SESSION)
        yield client, app, provider, session_id


def start(flow):
    response = flow[0].post("/paypal/vault/start", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "awaiting_payer"
    return response.json()["onboarding_id"]


def callback(flow, *, cancel=False):
    client, _, provider, _ = flow
    # Explicitly omit the owner cookie, modeling the initial Strict cross-site return.
    response = client.get(provider.return_url(cancel), headers={"Cookie": "", "Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == ORIGIN + "/paypal/vault/returned"
    assert COOKIE not in response.headers.get("set-cookie", "")
    assert RECEIPT in response.cookies
    return response


def complete(flow, identifier):
    return flow[0].post(f"/paypal/vault/{identifier}/complete", json={})


def expire(db, identifier):
    # Simulate elapsed time on this disposable DB only, without sleeping or
    # weakening production immutability rules.
    with db.begin() as c:
        c.execute(text("ALTER TABLE agentpal.vault_setups DISABLE TRIGGER guard_setup"))
        c.execute(text("UPDATE agentpal.vault_setups SET expires_at=created_at+interval '1 microsecond' WHERE id=:id"), {"id": identifier})
        c.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        c.execute(text("ALTER TABLE agentpal.vault_setups ENABLE TRIGGER guard_setup"))


def test_success_strict_callback_encryption_and_boundaries(flow, db):
    client, app, provider, _ = flow
    assert client.get("/paypal/vault/status").json() == {"connected": False, "onboarding": None}
    identifier = start(flow)
    assert provider.payload == {"payment_source": {"paypal": {"usage_type": "MERCHANT", "customer_type": "CONSUMER",
        "usage_pattern": "IMMEDIATE", "description": DESCRIPTION, "permit_multiple_payment_tokens": True,
        "experience_context": {"return_url": provider.return_url(), "cancel_url": provider.return_url(True)}}}}
    assert client.post("/paypal/vault/start", json={}).json()["onboarding_id"] == identifier
    assert provider.count("/v3/vault/setup-tokens") == 1
    response = callback(flow)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie and "Domain=" not in cookie
    assert complete(flow, identifier).json()["state"] == "complete"
    assert complete(flow, identifier).json()["state"] == "complete"
    status = client.get("/paypal/vault/status")
    assert status.json()["connected"]
    assert client.post("/paypal/vault/start", json={}).status_code == 409
    assert provider.count("/v3/vault/payment-tokens") == 1
    assert provider.count(f"/v3/vault/setup-tokens/{SETUP}", "GET") == 1
    assert provider.count(f"/v3/vault/payment-tokens/{TOKEN}", "GET") == 1
    assert all(value not in status.text for value in (SETUP, TOKEN, CUSTOMER, "fictional_oauth_secret"))
    with db.connect() as c:
        row = c.execute(text("SELECT * FROM agentpal.payment_methods")).mappings().one()
        setup = c.execute(text("SELECT * FROM agentpal.vault_setups")).mappings().one()
        assert all(raw.encode() not in bytes(row["reference_ciphertext"]) for raw in (TOKEN, CUSTOMER))
        assert TOKEN.encode() not in bytes(setup["token_ciphertext"])
        assert SETUP.encode() not in bytes(setup["setup_reference_ciphertext"])
        assert app.state.vault_service.keys.open(UUID(identifier), "payment-method", row["reference_ciphertext"], row["encryption_key_id"])["id"] == TOKEN
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='vault_connected'")) == 1
        for table in ("purchases", "approvals", "payments", "budget_reservations"):
            assert c.scalar(text(f"SELECT count(*) FROM agentpal.{table}")) == 0
    for response in (status, response):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("headers,status", [({"Cookie": ""}, 401), ({"X-CSRF-Token": ""}, 403),
    ({"Origin": "https://attacker.test"}, 403), ({"Sec-Fetch-Site": "cross-site"}, 403)])
def test_start_write_authorization(flow, headers, status):
    assert flow[0].post("/paypal/vault/start", headers=headers, json={}).status_code == status
    assert flow[2].calls == []


@pytest.mark.parametrize("suffix", ["&state=duplicate", "&token=FORGED_REFERENCE", "&approval_token_id=FORGED_REFERENCE",
                                    "&redirect=https://attacker.test", "&unused=x"])
def test_forged_callback_rejected_without_consuming(flow, suffix):
    identifier = start(flow)
    response = flow[0].get(flow[2].return_url()+suffix, follow_redirects=False)
    assert response.status_code == 400
    assert "FORGED_REFERENCE" not in response.text
    callback(flow)
    assert complete(flow, identifier).json()["state"] == "complete"


def test_callback_replay_cancel_and_fresh_attempt(flow):
    identifier = start(flow)
    callback(flow, cancel=True)
    assert flow[0].get(flow[2].return_url(True), follow_redirects=False).status_code == 400
    assert complete(flow, identifier).json()["state"] == "canceled"
    assert flow[2].count("/v3/vault/payment-tokens") == 0
    assert start(flow) != identifier


def test_completion_requires_callback_receipt_and_original_session(flow, db):
    identifier = start(flow)
    assert complete(flow, identifier).status_code == 403
    callback(flow)
    client, _, _, _ = flow
    client.cookies.delete(RECEIPT)
    assert complete(flow, identifier).status_code == 403
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.owner_sessions(token_hash,csrf_token_hash,expires_at)
            VALUES (:token,:csrf,clock_timestamp()+interval '1 hour')"""),
            {"token": token_hash("n"*43, "session"), "csrf": token_hash(CSRF, "csrf")})
    client.cookies.set(COOKIE, "n"*43)
    assert complete(flow, identifier).status_code == 409
    assert client.post("/paypal/vault/start").status_code == 409
    assert flow[2].count("/v3/vault/payment-tokens") == 0


def test_unknown_state_and_expiration(flow, db):
    identifier = start(flow)
    assert flow[0].get("/paypal/vault/return?state=" + "f"*43).status_code == 400
    expire(db, identifier)
    assert flow[0].get(flow[2].return_url()).status_code == 400
    assert flow[0].get("/paypal/vault/status").json()["onboarding"]["state"] == "expired"
    assert start(flow) != identifier


@pytest.mark.parametrize("stage", ["setup", "token"])
def test_lost_post_response_never_retried_and_blocks_parallel_flow(flow, stage):
    client, app, provider, _ = flow
    path = "/v3/vault/setup-tokens" if stage == "setup" else "/v3/vault/payment-tokens"
    if stage == "setup":
        provider.failure = ("POST", path)
        response = client.post("/paypal/vault/start")
        identifier = response.json()["onboarding_id"]
    else:
        identifier = start(flow)
        callback(flow)
        provider.failure = ("POST", path)
        response = complete(flow, identifier)
    assert response.json()["state"] == "review_required"
    # Rebuild service to prove the no-retry decision is durable across restarts.
    old = app.state.vault_service
    app.state.vault_service = VaultService(app.state.auth_service, config(), old.provider)
    assert client.post("/paypal/vault/start").json()["onboarding_id"] == identifier
    assert provider.count(path) == 1
    if stage == "token":
        assert complete(flow, identifier).json()["state"] == "review_required"
        assert provider.count(path) == 1
    assert not client.get("/paypal/vault/status").json()["connected"]


def test_known_token_get_recovers_after_restart_without_second_post(flow):
    identifier = start(flow)
    callback(flow)
    client, app, provider, _ = flow
    provider.failure = ("GET", f"/v3/vault/payment-tokens/{TOKEN}")
    assert complete(flow, identifier).json()["state"] == "verification_required"
    provider.failure = None
    app.state.vault_service = VaultService(app.state.auth_service, config(), app.state.vault_service.provider)
    assert complete(flow, identifier).json()["state"] == "complete"
    assert provider.count("/v3/vault/payment-tokens") == 1


@pytest.mark.parametrize("change", ["not_approved", "wrong_id", "wrong_customer", "wrong_source"])
def test_setup_independent_verification_blocks_token_creation(flow, change):
    identifier = start(flow)
    callback(flow)
    def transform(path, result):
        if path == f"/v3/vault/setup-tokens/{SETUP}":
            if change == "not_approved": result["status"] = "PAYER_ACTION_REQUIRED"
            if change == "wrong_id": result["id"] = "WRONG_SETUP_ID"
            if change == "wrong_customer": result["customer"]["id"] = "wrong_customer"
            if change == "wrong_source": result["payment_source"] = {"card": {}}
        return result
    flow[2].transform = transform
    assert complete(flow, identifier).json()["state"] == "completion_required"
    assert flow[2].count("/v3/vault/payment-tokens") == 0


@pytest.mark.parametrize("change", ["wrong_id", "wrong_customer", "wrong_source", "wrong_post_customer"])
def test_payment_verification_rejects_unbound_token(flow, change):
    identifier = start(flow)
    callback(flow)
    def transform(path, result):
        if path == "/v3/vault/payment-tokens" and change == "wrong_post_customer": result["customer"]["id"] = "wrong_customer"
        if path == f"/v3/vault/payment-tokens/{TOKEN}":
            if change == "wrong_id": result["id"] = "WRONG_TOKEN_ID"
            if change == "wrong_customer": result["customer"]["id"] = "wrong_customer"
            if change == "wrong_source": result["payment_source"] = {"card": {}}
        return result
    flow[2].transform = transform
    assert complete(flow, identifier).json()["state"] == "verification_required"
    assert not flow[0].get("/paypal/vault/status").json()["connected"]
    assert flow[2].count("/v3/vault/payment-tokens") == 1


@pytest.mark.parametrize("when", ["before_callback", "before_complete"])
def test_revoked_session_rejected(flow, db, when):
    identifier = start(flow)
    if when == "before_complete": callback(flow)
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp() WHERE id=:id"), {"id": flow[3]})
    assert complete(flow, identifier).status_code == 401
    assert flow[0].get(flow[2].return_url(), follow_redirects=False).status_code == 400
    assert flow[2].count("/v3/vault/payment-tokens") == 0


def test_concurrent_completion_has_one_durable_token_post(flow):
    identifier = start(flow)
    callback(flow)
    _, app, provider, _ = flow
    service = app.state.vault_service
    authority = Authority(SESSION, CSRF)
    receipt = flow[0].cookies[RECEIPT]
    provider.block = ("POST", "/v3/vault/payment-tokens")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.complete, authority, UUID(identifier), receipt)
        try:
            assert provider.entered.wait(5)
            second = pool.submit(service.complete, authority, UUID(identifier), receipt)
            assert second.result(5)["state"] == "review_required"
        finally:
            provider.released.set()
        assert first.result(10)["state"] == "complete"
    assert provider.count("/v3/vault/payment-tokens") == 1


def test_logout_during_post_saves_token_but_does_not_activate(flow, db):
    identifier = start(flow)
    callback(flow)
    client, app, provider, session_id = flow
    service = app.state.vault_service
    provider.block = ("POST", "/v3/vault/payment-tokens")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.complete, Authority(SESSION, CSRF), UUID(identifier), client.cookies[RECEIPT])
        try:
            assert provider.entered.wait(5)
            with db.begin() as c:
                c.execute(text("UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp() WHERE id=:id"), {"id": session_id})
        finally:
            provider.released.set()
        with pytest.raises(Exception) as error:
            pending.result(10)
        assert getattr(error.value, "status_code", None) == 401
    with db.connect() as c:
        assert c.scalar(text("SELECT token_ciphertext IS NOT NULL FROM agentpal.vault_setups"))
        assert c.scalar(text("SELECT count(*) FROM agentpal.payment_methods")) == 0
    assert provider.count("/v3/vault/payment-tokens") == 1


@pytest.mark.parametrize("url", ["https://paypal.com/agreements/approve?approval_session_id="+SETUP,
    "https://sandbox.paypal.com.attacker.test/agreements/approve?approval_session_id="+SETUP,
    "https://attacker@sandbox.paypal.com/agreements/approve?approval_session_id="+SETUP,
    "http://sandbox.paypal.com/agreements/approve?approval_session_id="+SETUP,
    "https://sandbox.paypal.com/agreements/approve?approval_session_id=WRONG_ID",
    "https://sandbox.paypal.com/agreements/approve?approval_session_id="+SETUP+"&redirect=https://attacker.test"])
def test_approval_url_allowlist(url):
    with pytest.raises(VaultUnavailable): approval_url(url, SETUP)


def test_aead_tampering_record_purpose_and_rotation():
    keys = Keyring(config())
    identifier = uuid4()
    encrypted, key = keys.seal(identifier, "token", {"id": TOKEN})
    rotated = Keyring(config(VAULT_ACTIVE_KEY_ID="new", VAULT_KEYS_JSON=json.dumps({
        "test-key": base64.b64encode(b"k"*32).decode(), "new": base64.b64encode(b"n"*32).decode()})))
    assert rotated.open(identifier, "token", encrypted, key) == {"id": TOKEN}
    assert rotated.seal(identifier, "token", {})[1] == "new"
    for args in [(uuid4(), "token", encrypted, key), (identifier, "setup", encrypted, key),
                 (identifier, "token", encrypted[:-1]+bytes([encrypted[-1]^1]), key),
                 (identifier, "token", encrypted, "missing")]:
        with pytest.raises(VaultUnavailable): keys.open(*args)
    assert keys.seal(identifier, "token", {"id": TOKEN})[0] != encrypted


@pytest.mark.parametrize("overrides", [{"PAYPAL_ENVIRONMENT": "live"}, {"PAYPAL_BASE_URL": "https://api-m.paypal.com"},
    {"VAULT_ACTIVE_KEY_ID": "missing"}, {"VAULT_KEYS_JSON": "{}"}, {"VAULT_KEYS_JSON": '{"test-key":"bad-key"}'}])
def test_explicit_configuration_fails_closed(overrides):
    with pytest.raises(ValueError): config(**overrides)


def test_provider_logging_suppressed_in_integration_context(flow, caplog):
    caplog.set_level(logging.DEBUG)
    # Exercise the service directly so caller-owned TestClient request URL logging
    # is not confused with server/provider logging under our control.
    service = flow[1].state.vault_service
    result = service.start(Authority(SESSION, CSRF))
    params = list(parse_qs(urlsplit(flow[2].return_url()).query).items())
    receipt = service.callback([(k, v[0]) for k, v in params], canceled=False)
    service.complete(Authority(SESSION, CSRF), UUID(result["onboarding_id"]), receipt)
    output = caplog.text
    for sensitive in (SETUP, TOKEN, CUSTOMER, "fictional_oauth_secret", "fictional_client", "fictional_secret", receipt):
        assert sensitive not in output


def test_unknown_body_and_redirect_response_do_not_echo_or_follow(flow):
    assert flow[0].post("/paypal/vault/start", json={"token": "SENSITIVE_INPUT_CANARY"}).status_code == 400
    flow[2].transform = lambda path, value: httpx.Response(302, headers={"Location": "https://attacker.test"})
    response = flow[0].post("/paypal/vault/start")
    assert response.json()["state"] == "review_required"
    assert flow[2].calls == [("POST", "/v1/oauth2/token", None)]


def test_concurrent_start_does_not_issue_second_setup_post(flow):
    client, app, provider, _ = flow
    service = app.state.vault_service
    authority = Authority(SESSION, CSRF)
    provider.block = ("POST", "/v3/vault/setup-tokens")
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.start, authority)
        try:
            assert provider.entered.wait(5)
            duplicate = service.start(authority)
            assert duplicate["state"] == "review_required"
        finally:
            provider.released.set()
        assert first.result(10)["onboarding_id"] == duplicate["onboarding_id"]
    assert provider.count("/v3/vault/setup-tokens") == 1


@pytest.mark.parametrize("phase", ["setup_post", "token_post"])
def test_crash_after_durable_claim_before_observation_is_review_only(flow, db, phase):
    client, app, provider, _ = flow
    if phase == "setup_post":
        def crashed(*args): raise RuntimeError("simulated process interruption")
        app.state.vault_service.provider.setup = crashed
        with pytest.raises(RuntimeError): client.post("/paypal/vault/start")
    else:
        identifier = start(flow)
        callback(flow)
        def crashed(*args): raise RuntimeError("simulated process interruption")
        app.state.vault_service.provider.tokenize = crashed
        with pytest.raises(RuntimeError): complete(flow, identifier)
    with db.connect() as c:
        assert c.scalar(text("SELECT flow_phase FROM agentpal.vault_setups")) == phase
    settings = config()
    app.state.vault_service = VaultService(app.state.auth_service, settings, PayPal(settings, transport=httpx.MockTransport(provider)))
    assert client.post("/paypal/vault/start").json()["state"] == "review_required"
    assert provider.count("/v3/vault/payment-tokens") == 0


@pytest.mark.parametrize("stage", ["returned", "verifying_token"])
def test_expiry_never_activates_or_replaces_method(flow, db, stage):
    identifier = start(flow)
    callback(flow)
    if stage == "verifying_token":
        flow[2].failure = ("GET", f"/v3/vault/payment-tokens/{TOKEN}")
        assert complete(flow, identifier).json()["state"] == "verification_required"
    before = len(flow[2].calls)
    expire(db, identifier)
    assert complete(flow, identifier).json()["state"] == ("expired" if stage == "returned" else "review_required")
    assert len(flow[2].calls) == before
    assert not flow[0].get("/paypal/vault/status").json()["connected"]
    if stage == "verifying_token":
        assert flow[0].post("/paypal/vault/start").json()["onboarding_id"] == identifier
        assert flow[2].count("/v3/vault/payment-tokens") == 1


def test_scope_and_response_remove_callback_correlation_before_access_logging(flow):
    from uvicorn.protocols.utils import get_path_with_query_string
    start(flow)
    app = flow[1]
    observed = []
    async def inspect_scope(scope, receive, send):
        async def inspect_send(message):
            if message["type"] == "http.response.start":
                observed.append((get_path_with_query_string(scope), scope.get("vault_callback_query")))
            await send(message)
        await app(scope, receive, inspect_send)
    with TestClient(inspect_scope, base_url=ORIGIN) as browser:
        result = browser.get(flow[2].return_url(), follow_redirects=False)
    assert result.status_code == 303
    assert observed == [("/paypal/vault/return", None)]
    assert "state=" not in result.text + result.headers["location"]


@pytest.mark.parametrize("mutation", ["phase_jump", "state", "session", "request_id", "setup_cipher", "receipt"])
def test_database_guards_preserve_binding_and_once_set_material(flow, db, mutation):
    identifier = start(flow)
    callback(flow)
    changes = {"phase_jump": "flow_phase='complete',status='verified',verified_at=clock_timestamp()",
               "state": "state_hash=decode(repeat('ff',32),'hex')", "session": "owner_session_id=gen_random_uuid()",
               "request_id": "token_request_id=gen_random_uuid()", "setup_cipher": "setup_reference_ciphertext=decode('ff','hex')",
               "receipt": "receipt_hash=decode(repeat('ff',32),'hex')"}
    with pytest.raises(IntegrityError), db.begin() as c:
        c.execute(text("UPDATE agentpal.vault_setups SET " + changes[mutation] + " WHERE id=:id"), {"id": identifier})
    assert complete(flow, identifier).json()["state"] == "complete"


def test_guarded_downgrade_preserves_onboarding_evidence(flow, db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import DBAPIError
    identifier = start(flow)
    cfg = Config("alembic.ini")
    with pytest.raises(DBAPIError), db.begin() as c:
        cfg.attributes["connection"] = c
        command.downgrade(cfg, "0003")
    with db.connect() as c:
        assert c.scalar(text("SELECT id FROM agentpal.vault_setups")) == UUID(identifier)
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0006"


@pytest.mark.parametrize("failure", ["bad_link", "bad_id", "malformed_json", "large", "provider_error"])
def test_provider_failures_are_sanitized_and_never_reposted(flow, failure):
    def transform(path, value):
        if path != "/v3/vault/setup-tokens": return value
        if failure == "bad_link": value["links"][0]["href"] = "https://attacker.test/SENSITIVE_CANARY"
        if failure == "bad_id": value["id"] = "../../SENSITIVE_CANARY"
        if failure == "malformed_json": return httpx.Response(200, content=b"SENSITIVE_CANARY", headers={"Content-Type": "application/json"})
        if failure == "large": return httpx.Response(200, json={"id": "SENSITIVE_CANARY"*10000})
        if failure == "provider_error": return httpx.Response(422, json={"debug_id": "SENSITIVE_CANARY"})
        return value
    flow[2].transform = transform
    response = flow[0].post("/paypal/vault/start")
    assert response.status_code == 200 and response.json()["state"] == "review_required"
    assert "SENSITIVE_CANARY" not in response.text
    assert flow[0].post("/paypal/vault/start").json()["onboarding_id"] == response.json()["onboarding_id"]
    assert flow[2].count("/v3/vault/setup-tokens") == 1


def test_completion_csrf_origin_and_expired_session(flow, db):
    identifier = start(flow)
    callback(flow)
    for headers in ({"X-CSRF-Token": ""}, {"Origin": "https://attacker.test"}, {"Sec-Fetch-Site": "cross-site"}):
        assert flow[0].post(f"/paypal/vault/{identifier}/complete", headers=headers).status_code == 403
    with db.begin() as c:
        c.execute(text("ALTER TABLE agentpal.owner_sessions DISABLE TRIGGER guard_session"))
        c.execute(text("UPDATE agentpal.owner_sessions SET expires_at=created_at+interval '1 microsecond'"))
        c.execute(text("ALTER TABLE agentpal.owner_sessions ENABLE TRIGGER guard_session"))
    assert complete(flow, identifier).status_code == 401
    assert flow[2].count("/v3/vault/payment-tokens") == 0


def test_crypto_failure_is_safe_and_does_not_issue_provider_requests(flow):
    start(flow)
    settings = config(VAULT_ACTIVE_KEY_ID="different", VAULT_KEYS_JSON=json.dumps({"different": base64.b64encode(b"x"*32).decode()}))
    flow[1].state.vault_service = VaultService(flow[1].state.auth_service, settings, flow[1].state.vault_service.provider)
    before = len(flow[2].calls)
    response = flow[0].post("/paypal/vault/start")
    assert response.status_code == 503 and response.json() == {"detail": "Vault unavailable"}
    assert len(flow[2].calls) == before


def test_commit_time_session_expiry_rolls_back_activation(flow, db):
    from sqlalchemy import event
    identifier = start(flow)
    callback(flow)
    with db.begin() as c:
        c.execute(text("ALTER TABLE agentpal.owner_sessions DISABLE TRIGGER guard_session"))
        c.execute(text("UPDATE agentpal.owner_sessions SET expires_at=clock_timestamp()+interval '1 second'"))
        c.execute(text("ALTER TABLE agentpal.owner_sessions ENABLE TRIGGER guard_session"))
    def delay_commit(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO agentpal.payment_methods"):
            conn.execute(text("SELECT pg_sleep(1.1)"))
    event.listen(db, "after_cursor_execute", delay_commit)
    try:
        assert complete(flow, identifier).status_code == 503
    finally:
        event.remove(db, "after_cursor_execute", delay_commit)
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.payment_methods")) == 0
        assert c.scalar(text("SELECT flow_phase FROM agentpal.vault_setups")) == "verifying_token"
        assert c.scalar(text("SELECT token_ciphertext IS NOT NULL FROM agentpal.vault_setups"))
    assert flow[2].count("/v3/vault/payment-tokens") == 1
