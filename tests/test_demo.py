"""The explicit rehearsal never consumes persistent runtime/provider settings."""

from fastapi.testclient import TestClient
from sqlalchemy import text

from agentpal.demo import rehearsal_app
from test_policy import PASSWORD


def test_disposable_rehearsal_full_flow_and_provider_denial(monkeypatch):
    # Deliberately unusable real configuration must have no effect on rehearsal.
    monkeypatch.setenv("DATABASE_URL", "this must never be used")
    monkeypatch.setenv("OPENAI_API_KEY", "this must never be used")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    origin = "http://127.0.0.1:8010"
    with rehearsal_app(PASSWORD) as application:
        engine = application.state.auth_service.engine
        with TestClient(application, base_url=origin, headers={"Origin": origin}) as client:
            assert client.post("/auth/login", json={"login_name": "demo-owner", "password": PASSWORD}).status_code == 200
            csrf = {"X-CSRF-Token": client.cookies["agentpal_csrf_dev"]}
            assert client.get("/shopping/status").json()["mode"] == "offline_rehearsal"
            assert client.get("/paypal/vault/status").json() == {
                "connected": False, "onboarding": None, "agreement_state": "unverified"}
            response = client.post("/shopping/runs", json={"request": "Find a wireless keyboard"}, headers=csrf)
            assert response.status_code == 200, response.text
            purchase = response.json()["purchase"]
            assert purchase["initial_decision"] == "AWAITING_HUMAN_APPROVAL"
            assert client.put(f'/purchases/{purchase["id"]}/approval', json={"decision": "APPROVE",
                "snapshot_hash": purchase["snapshot_hash"], "snapshot_hash_version": 1}, headers=csrf).status_code == 200
            for path in ("/paypal/vault/start", "/paypal/vault/replace", "/paypal/vault/anything/complete"):
                assert client.post(path, json={}, headers=csrf).status_code == 403
            with engine.connect() as c:
                assert c.scalar(text("SELECT count(*) FROM agentpal.payment_methods")) == 0
                assert c.scalar(text("SELECT count(*) FROM agentpal.payments")) == 0
                assert c.scalar(text("SELECT count(*) FROM agentpal.budget_reservations")) == 0
            assert client.post("/auth/logout", json={}, headers=csrf).status_code == 204
            assert client.get("/purchases").status_code == 401
