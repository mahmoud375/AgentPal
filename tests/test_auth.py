"""Real Argon2id and HTTP authentication against disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
import secrets
import sys
from threading import Event, Lock

from argon2 import extract_parameters
from argon2.low_level import Type
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from agentpal.auth import bootstrap
from agentpal.auth import service as auth_service_module
from agentpal.auth.api import OwnerRead, OwnerWrite
from agentpal.auth.config import AuthSettings
from agentpal.auth.passwords import hash_password, verify_password
from agentpal.auth.service import token_hash
from agentpal.main import create_app


ORIGIN = "https://agentpal.test"
PASSWORD = "fictional owner passphrase for tests!"


@pytest.fixture(scope="session")
def encoded_password():
    return hash_password(PASSWORD)


@pytest.fixture
def owner(db, encoded_password):
    with db.begin() as c:
        c.execute(text("INSERT INTO agentpal.owners(login_name,password_hash) VALUES ('test-owner',:hash)"), {"hash": encoded_password})


@pytest.fixture
def auth_app(db, owner):
    app = create_app(engine=db, settings=AuthSettings(AUTH_ORIGIN=ORIGIN))

    @app.get("/test/protected")
    def protected(owner: OwnerRead):
        return {"login_name": owner.login_name}

    @app.post("/test/protected")
    def protected_write(context: OwnerWrite):
        context.connection.execute(text("""INSERT INTO agentpal.audit_events(actor_kind,owner_session_id,kind,details)
            VALUES ('owner',:id,'test_authenticated_write','{}')"""), {"id": context.owner.session_id})
        return {"ok": True}

    @app.post("/test/rollback")
    def rolled_back_write(context: OwnerWrite):
        context.connection.execute(text("""INSERT INTO agentpal.audit_events(actor_kind,owner_session_id,kind,details)
            VALUES ('owner',:id,'test_rollback','{}')"""), {"id": context.owner.session_id})
        raise SQLAlchemyError("fictional secret database failure")

    return app


@pytest.fixture
def client(auth_app):
    with TestClient(auth_app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        yield client


def sign_in(client):
    response = client.post("/auth/login", json={"login_name": "test-owner", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.cookies["__Host-agentpal_csrf"]


def test_bootstrap_stores_argon2id_and_never_overwrites(db):
    assert bootstrap.bootstrap_owner(db, "local-owner", PASSWORD)
    assert not bootstrap.bootstrap_owner(db, "replacement", "another fictional passphrase")
    with db.connect() as c:
        row = c.execute(text("SELECT login_name,password_hash FROM agentpal.owners")).one()
        assert row.login_name == "local-owner"
        assert row.password_hash != PASSWORD
        assert verify_password(row.password_hash, PASSWORD)
        assert not verify_password(row.password_hash, "another fictional passphrase")
        assert c.scalar(text("SELECT count(*) FROM agentpal.owners")) == 1
        assert c.scalar(text("SELECT count(*) FROM agentpal.policies")) == 0
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='owner_bootstrapped'")) == 1


def test_concurrent_bootstrap_keeps_exactly_one_owner(db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda name: bootstrap.bootstrap_owner(db,name,PASSWORD), ["one","two"]))
    assert sorted(result) == [False,True]
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.owners")) == 1


@pytest.mark.parametrize("password", ["short", "x"*129])
def test_bootstrap_password_limits(db, password):
    with pytest.raises(ValueError):
        bootstrap.bootstrap_owner(db,"owner",password)
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.owners")) == 0


def test_password_profile_salts_and_verification(encoded_password):
    params = extract_parameters(encoded_password)
    assert (params.type, params.memory_cost, params.time_cost) == (Type.ID,65536,3)
    assert hash_password(PASSWORD) != encoded_password
    assert verify_password(encoded_password,PASSWORD)
    assert not verify_password(encoded_password,"wrong password")
    assert not verify_password("$argon2id$broken",PASSWORD)


def test_login_cookies_hashed_storage_and_safe_owner_info(client, db, auth_app):
    response = client.post("/auth/login", json={"login_name":"test-owner","password":PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"login_name":"test-owner"}
    assert response.headers["cache-control"] == "no-store"
    cookies = SimpleCookie()
    for value in response.headers.get_list("set-cookie"):
        cookies.load(value)
    for name in ("__Host-agentpal_session","__Host-agentpal_csrf"):
        cookie = cookies[name]
        assert cookie["secure"] and cookie["samesite"] == "strict"
        assert cookie["path"] == "/" and not cookie["domain"]
        assert int(cookie["max-age"]) == 28800 and cookie["expires"]
    assert cookies["__Host-agentpal_session"]["httponly"]
    assert not cookies["__Host-agentpal_csrf"]["httponly"]
    token = response.cookies["__Host-agentpal_session"]
    csrf = response.cookies["__Host-agentpal_csrf"]
    with db.connect() as c:
        row = c.execute(text("SELECT token_hash,csrf_token_hash FROM agentpal.owner_sessions")).one()
        assert row.token_hash == token_hash(token,"session")
        assert row.csrf_token_hash == token_hash(csrf,"csrf")
        assert token.encode() not in row.token_hash and csrf.encode() not in row.csrf_token_hash
    assert client.get("/auth/me").json() == {"login_name":"test-owner"}
    assert client.get("/test/protected").status_code == 200
    assert client.get("/test/protected").headers["cache-control"] == "no-store"
    for secret in (PASSWORD,token,csrf,"password_hash","token_hash","session_id","vault"):
        assert secret not in response.text


@pytest.mark.parametrize("name,password", [("test-owner","wrong"),("unknown",PASSWORD)])
def test_invalid_credentials_are_generic_and_create_no_session(client, db, name, password):
    response = client.post("/auth/login", json={"login_name":name,"password":password})
    assert response.status_code == 401
    assert response.json() == {"detail":"Invalid credentials"}
    assert "set-cookie" not in response.headers
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.owner_sessions")) == 0


def test_unbootstrapped_login_is_generic(db):
    app = create_app(engine=db,settings=AuthSettings(AUTH_ORIGIN=ORIGIN))
    with TestClient(app,base_url=ORIGIN) as client:
        response = client.post("/auth/login",headers={"Origin":ORIGIN},json={"login_name":"unknown","password":PASSWORD})
    assert response.status_code == 401
    assert response.json() == {"detail":"Invalid credentials"}


@pytest.mark.parametrize("cookie", [None,"forged", "a"*43])
def test_missing_or_forged_session_has_no_owner_authority(client, cookie):
    headers = {"Cookie":f"__Host-agentpal_session={cookie}"} if cookie else {}
    assert client.get("/auth/me",headers=headers).status_code == 401
    assert client.get("/test/protected",headers=headers).status_code == 401
    assert client.post("/test/protected",headers=headers).status_code == 401


def test_expired_session_is_rejected_and_stale_logout_is_predictable(client, db):
    token,csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.owner_sessions(token_hash,csrf_token_hash,created_at,expires_at)
            VALUES (:token,:csrf,clock_timestamp()-interval '2 hours',clock_timestamp()-interval '1 hour')"""),
            {"token":token_hash(token,"session"),"csrf":token_hash(csrf,"csrf")})
    headers = {"Cookie":f"__Host-agentpal_session={token}","X-CSRF-Token":csrf}
    assert client.get("/auth/me",headers=headers).status_code == 401
    assert client.post("/test/protected",headers=headers).status_code == 401
    assert client.post("/auth/logout",headers=headers).status_code == 204


def test_logout_is_durable_and_repeated_old_session_remains_revoked(client, db):
    csrf = sign_in(client)
    token = client.cookies["__Host-agentpal_session"]
    response = client.post("/auth/logout",headers={"X-CSRF-Token":csrf})
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "__Host-agentpal_session" not in client.cookies
    assert "__Host-agentpal_csrf" not in client.cookies
    assert client.get("/auth/me").status_code == 401
    assert client.post("/auth/logout").status_code == 204
    replay = {"Cookie":f"__Host-agentpal_session={token}","X-CSRF-Token":csrf}
    assert client.get("/auth/me",headers=replay).status_code == 401
    assert client.post("/test/protected",headers=replay).status_code == 401
    assert client.post("/auth/logout",headers=replay).status_code == 204
    with db.connect() as c:
        assert c.scalar(text("SELECT revoked_at IS NOT NULL FROM agentpal.owner_sessions"))
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='owner_session_revoked'")) == 1


@pytest.mark.parametrize("token", [None,"incorrect","a"*43])
def test_missing_or_invalid_csrf_rejects_logout_and_future_write(client, db, token):
    sign_in(client)
    headers = {"X-CSRF-Token":token} if token else {}
    assert client.post("/auth/logout",headers=headers).status_code == 403
    assert client.post("/test/protected",headers=headers).status_code == 403
    assert client.get("/auth/me").status_code == 200
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='test_authenticated_write'")) == 0


def test_cross_session_csrf_is_rejected_and_correct_write_has_server_actor(client, auth_app, db):
    first_csrf = sign_in(client)
    with TestClient(auth_app,base_url=ORIGIN,headers={"Origin":ORIGIN}) as other:
        second_csrf = sign_in(other)
        assert other.post("/test/protected",headers={"X-CSRF-Token":first_csrf}).status_code == 403
        assert other.post("/auth/logout",headers={"X-CSRF-Token":first_csrf}).status_code == 403
        assert other.post("/test/protected",headers={"X-CSRF-Token":second_csrf}).json() == {"ok":True}
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='test_authenticated_write'")) == 1
        assert c.scalar(text("""SELECT e.owner_session_id=s.id FROM agentpal.audit_events e
            JOIN agentpal.owner_sessions s ON s.token_hash=:hash WHERE e.kind='test_authenticated_write'"""),
            {"hash":token_hash(other.cookies["__Host-agentpal_session"],"session")})


def test_login_rotates_old_session_and_survives_app_restart(client, db):
    sign_in(client)
    old = client.cookies["__Host-agentpal_session"]
    sign_in(client)
    new = client.cookies["__Host-agentpal_session"]
    assert old != new
    assert client.get("/auth/me",headers={"Cookie":f"__Host-agentpal_session={old}"}).status_code == 401
    fresh_app = create_app(engine=db,settings=AuthSettings(AUTH_ORIGIN=ORIGIN))
    with TestClient(fresh_app,base_url=ORIGIN) as restarted:
        assert restarted.get("/auth/me",headers={"Cookie":f"__Host-agentpal_session={new}"}).status_code == 200


@pytest.mark.parametrize("origin", [None,"null","https://evil.test","https://agentpal.test.evil.test","http://agentpal.test"])
def test_login_and_authenticated_writes_require_trusted_origin(client, origin):
    csrf = sign_in(client)
    client.headers.pop("origin")
    headers = {"Origin":origin} if origin is not None else {}
    assert client.post("/auth/login",headers=headers,json={"login_name":"test-owner","password":PASSWORD}).status_code == 403
    assert client.post("/auth/logout",headers=headers|{"X-CSRF-Token":csrf}).status_code == 403
    assert client.post("/test/protected",headers=headers|{"X-CSRF-Token":csrf}).status_code == 403


def test_referer_fallback_and_fetch_metadata(client):
    csrf = sign_in(client)
    client.headers.pop("origin")
    assert client.post("/test/protected",headers={"Referer":ORIGIN+"/owner","X-CSRF-Token":csrf}).status_code == 200
    for site in ("cross-site","same-site"):
        assert client.post("/auth/logout",headers={"Origin":ORIGIN,"Sec-Fetch-Site":site,"X-CSRF-Token":csrf}).status_code == 403


def test_agent_identity_and_bearer_headers_never_authenticate_an_owner(client):
    headers = {"Authorization":"Bearer fictional-agent-token","X-Agent-Id":"fictional-agent", "X-Owner-Id":"true"}
    assert client.get("/auth/me",headers=headers).status_code == 401
    assert client.post("/test/protected",headers=headers).status_code == 401
    assert client.post("/auth/login",json={"login_name":"test-owner","password":PASSWORD,"actor_kind":"owner"}).status_code == 400


def test_global_login_limit_cannot_be_bypassed_by_names_or_forwarded_ips(client, auth_app):
    now = [100.0]
    auth_app.state.auth_service.limiter.clock = lambda: now[0]
    for number in range(5):
        assert client.post("/auth/login",headers={"X-Forwarded-For":f"192.0.2.{number}"},
                           json={"login_name":f"unknown-{number}","password":"wrong"}).status_code == 401
    blocked = client.post("/auth/login",json={"login_name":"test-owner","password":PASSWORD})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) == 60
    now[0] += 61
    sign_in(client)


@pytest.mark.parametrize("origin,allow", [("http://localhost:8000",False),("http://public.test",True),(ORIGIN,True)])
def test_unsafe_development_configuration_is_rejected(origin, allow):
    with pytest.raises(ValueError):
        AuthSettings(AUTH_ORIGIN=origin,AUTH_ALLOW_INSECURE_LOCALHOST=allow)


def test_explicit_loopback_development_cookies(db, owner):
    origin = "http://127.0.0.1:8000"
    app = create_app(engine=db,settings=AuthSettings(AUTH_ORIGIN=origin,AUTH_ALLOW_INSECURE_LOCALHOST=True))
    with TestClient(app,base_url=origin,headers={"Origin":origin}) as client:
        response = client.post("/auth/login",json={"login_name":"test-owner","password":PASSWORD})
        assert response.status_code == 200
        cookies = SimpleCookie()
        for value in response.headers.get_list("set-cookie"):
            cookies.load(value)
        assert cookies["agentpal_session_dev"]["httponly"]
        assert not cookies["agentpal_session_dev"]["secure"]
        assert cookies["agentpal_session_dev"]["samesite"] == "strict"
        assert client.get("/auth/me").status_code == 200


def test_production_http_and_wrong_host_are_rejected(auth_app):
    for origin in ("http://agentpal.test","https://evil.test"):
        with TestClient(auth_app,base_url=origin) as client:
            assert client.post("/auth/login",headers={"Origin":ORIGIN},json={"login_name":"test-owner","password":PASSWORD}).status_code == 403


@pytest.mark.parametrize("body,code", [
    ('{"password":"leak-canary","login_name":[]}',400),
    ('{"password":"leak-canary",',400),
    ('{"password":"leak-canary","login_name":"a","extra":true}',400),
    ("leak-canary"*500,413),
])
def test_malformed_authentication_never_echoes_sensitive_input(client, caplog, body, code):
    response = client.post("/auth/login",content=body,headers={"Content-Type":"application/json"})
    assert response.status_code == code
    assert "leak-canary" not in response.text and "leak-canary" not in caplog.text
    assert response.headers["cache-control"] == "no-store"


def test_write_dependency_rolls_back_and_hides_database_errors(client, db):
    csrf = sign_in(client)
    response = client.post("/test/rollback",headers={"X-CSRF-Token":csrf})
    assert response.status_code == 503
    assert "fictional secret" not in response.text
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='test_rollback'")) == 0


def test_health_and_public_route_scope_without_configuration(monkeypatch):
    monkeypatch.delenv("AUTH_ORIGIN",raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status":"ok"}
        assert client.post("/auth/login",json={"login_name":"test","password":PASSWORD}).status_code == 503
        assert client.post("/auth/register").status_code == 404
        assert client.post("/auth/signup").status_code == 404


def test_interactive_cli_bootstrap_never_prints_password_or_hash(db, monkeypatch, capsys):
    monkeypatch.setattr(sys,"argv",["agentpal-bootstrap"])
    monkeypatch.setattr(sys.stdin,"isatty",lambda: True)
    monkeypatch.setattr(sys.stderr,"isatty",lambda: True)
    monkeypatch.setattr(bootstrap,"DatabaseSettings",lambda: object())
    monkeypatch.setattr(bootstrap,"create_database_engine",lambda _: db)
    monkeypatch.setattr("builtins.input",lambda _: "cli-owner")
    monkeypatch.setattr(bootstrap.getpass,"getpass",lambda _: PASSWORD)
    assert bootstrap.main() == 0
    output = capsys.readouterr()
    assert "Owner created" in output.out
    assert PASSWORD not in output.out+output.err and "$argon2id$" not in output.out+output.err
    monkeypatch.setattr(bootstrap.getpass,"getpass",lambda _: pytest.fail("Duplicate must not prompt for password"))
    assert bootstrap.main() == 1


def test_cli_rejects_piped_input_and_password_arguments_without_echo(monkeypatch, capsys):
    monkeypatch.setattr(sys,"argv",["agentpal-bootstrap","--password","do-not-echo-canary"])
    assert bootstrap.main() == 2
    assert "do-not-echo-canary" not in capsys.readouterr().err
    monkeypatch.setattr(sys,"argv",["agentpal-bootstrap"])
    monkeypatch.setattr(sys.stdin,"isatty",lambda: False)
    assert bootstrap.main() == 2


def test_login_hash_concurrency_is_bounded(auth_app, monkeypatch):
    reached,release = Event(),Event()
    lock = Lock()
    active = [0]
    actual_verify = auth_service_module.verify_password
    def blocked_verify(encoded,password):
        with lock:
            active[0] += 1
            if active[0] == 2:
                reached.set()
        assert release.wait(timeout=10)
        return actual_verify(encoded,password)
    monkeypatch.setattr(auth_service_module,"verify_password",blocked_verify)
    def attempt():
        with TestClient(auth_app,base_url=ORIGIN) as c:
            return c.post("/auth/login",headers={"Origin":ORIGIN},json={"login_name":"unknown","password":"wrong"}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        try:
            assert reached.wait(timeout=5)
            assert attempt() == 429
        finally:
            release.set()
        assert [f.result(timeout=10) for f in futures] == [401,401]


def test_authenticated_write_serializes_with_logout(auth_app, db):
    entered,release = Event(),Event()
    @auth_app.post("/test/held-write")
    def held_write(context: OwnerWrite):
        entered.set()
        assert release.wait(timeout=10)
        context.connection.execute(text("""INSERT INTO agentpal.audit_events(actor_kind,owner_session_id,kind,details)
            VALUES ('owner',:id,'test_held_write','{}')"""), {"id":context.owner.session_id})
        return {"ok":True}
    with TestClient(auth_app,base_url=ORIGIN,headers={"Origin":ORIGIN}) as c:
        csrf = sign_in(c)
        cookie = f"__Host-agentpal_session={c.cookies['__Host-agentpal_session']}"
        headers = {"Cookie":cookie,"X-CSRF-Token":csrf,"Origin":ORIGIN}
        with ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(c.post,"/test/held-write",headers=headers)
            try:
                assert entered.wait(timeout=5)
                logout = pool.submit(c.post,"/auth/logout",headers=headers)
            finally:
                release.set()
            assert write.result(timeout=10).status_code == 200
            assert logout.result(timeout=10).status_code == 204
        assert c.post("/test/protected",headers=headers).status_code == 401
    with db.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='test_held_write'")) == 1


def test_login_rehashes_valid_older_argon2id_parameters(db):
    from argon2 import PasswordHasher
    older = PasswordHasher(time_cost=2,memory_cost=19456,parallelism=1,type=Type.ID).hash(PASSWORD)
    with db.begin() as c:
        c.execute(text("INSERT INTO agentpal.owners(login_name,password_hash) VALUES ('test-owner',:hash)"), {"hash":older})
    app = create_app(engine=db,settings=AuthSettings(AUTH_ORIGIN=ORIGIN))
    with TestClient(app,base_url=ORIGIN,headers={"Origin":ORIGIN}) as c:
        sign_in(c)
    with db.connect() as c:
        stored = c.scalar(text("SELECT password_hash FROM agentpal.owners"))
        assert extract_parameters(stored).memory_cost == 65536
        assert verify_password(stored,PASSWORD)


def test_login_rejects_form_posts_and_exposes_only_safe_openapi_schema(client):
    assert client.post("/auth/login",data={"login_name":"test-owner","password":PASSWORD}).status_code == 415
    schema = client.get("/openapi.json").json()
    password_schema = schema["paths"]["/auth/login"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["password"]
    assert password_schema["writeOnly"] is True
    assert PASSWORD not in str(schema)
