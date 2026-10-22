"""Real disposable PostgreSQL; every provider reference/agreement is fictional.

Explicit IMMEDIATE agreements below include synthetic provider observations.
Missing provider metadata deliberately cannot satisfy that contract gate.
No HTTP transport is reachable from materialization.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal, localcontext
import base64
import hashlib
import json
import secrets
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from agentpal.orders import (ENDPOINT, RETENTION_DOCUMENTATION, REPLAY_WINDOW_SECONDS,
                            MaterializationError, OrderMaterializationService, _purpose, _usd)
from agentpal.vault.security import Keyring, VaultKeySettings, VaultUnavailable
from agentpal.vault.agreement import DESCRIPTION
from test_persistence import dispatch, operation, revalidate
from test_proposals import change_policy
from test_readiness import PAYEE, counts, decide, run, setup  # noqa: F401 -- reuse real proposal/readiness fixture


VAULT_ID = "FICTIONAL_PAYMENT_TOKEN_01"
CUSTOMER = "fictional_customer_01"


@pytest.fixture
def keys():
    return Keyring(VaultKeySettings(VAULT_ACTIVE_KEY_ID="old", VAULT_KEYS_JSON=json.dumps({
        "old": base64.b64encode(secrets.token_bytes(32)).decode()})))


def connect(db, seed, keys, *, pattern="IMMEDIATE", customer=CUSTOMER):
    """Synthetic verified, complete flow; never retrofit a real method/consent."""
    setup_id, method_id = uuid4(), uuid4()
    paypal = {"usage_type": "MERCHANT", "customer_type": "CONSUMER", "description": DESCRIPTION}
    if pattern is not None:
        paypal["usage_pattern"] = pattern
    flow, flow_key = keys.seal(setup_id, "flow", {"payment_source": {"paypal": paypal}})
    fields = {k: paypal.get(k) for k in ("usage_type", "usage_pattern", "customer_type", "description")}
    setup_cipher, setup_key = keys.seal(setup_id, "setup", {"id": "FICTIONAL_SETUP_01", "customer_id": CUSTOMER,
        "status": "PAYER_ACTION_REQUIRED", "metadata": fields})
    approved, approved_key = keys.seal(setup_id, "approved-setup", {"id": "FICTIONAL_SETUP_01", "customer_id": CUSTOMER,
        "status": "APPROVED", "metadata": fields})
    token_value = {"id": VAULT_ID, "customer_id": customer, "metadata": fields}
    token, token_key = keys.seal(setup_id, "token", token_value)
    ref, ref_key = keys.seal(setup_id, "payment-method", token_value)
    proof, proof_key = keys.seal(setup_id, "usage-agreement", {"version": 1, "payment_token": token_value})
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL"))
        c.execute(text("""INSERT INTO agentpal.vault_setups(id,owner_session_id,state_hash,status,expires_at,verified_at,
            flow_phase,flow_ciphertext,flow_key_id,setup_request_id,token_request_id,
            setup_reference_ciphertext,encryption_key_id,token_ciphertext,token_key_id,
            approved_reference_ciphertext,approved_key_id,agreement_evidence_ciphertext,agreement_evidence_key_id)
            VALUES (:id,:session,:hash,'verified',clock_timestamp()+interval '1 hour',clock_timestamp(),
            'complete',:flow,:flow_key,:setup_req,:token_req,:setup,:setup_key,:token,:token_key,:approved,:approved_key,:proof,:proof_key)"""),
            {"id": setup_id, "session": seed["session"], "hash": secrets.token_bytes(32),
             "flow": flow, "flow_key": flow_key, "setup_req": uuid4(), "token_req": uuid4(),
             "setup": setup_cipher, "setup_key": setup_key, "token": token, "token_key": token_key,
             "approved": approved, "approved_key": approved_key, "proof": proof, "proof_key": proof_key})
        c.execute(text("""INSERT INTO agentpal.payment_methods(id,vault_setup_id,reference_ciphertext,encryption_key_id,verified_at)
            SELECT :method,id,:cipher,:key,verified_at FROM agentpal.vault_setups WHERE id=:id"""),
            {"method": method_id, "id": setup_id, "cipher": ref, "key": ref_key})
    return setup_id, method_id


@pytest.fixture
def prepared(setup, db, seed, keys):
    readiness, data, propose = setup
    setup_id, method_id = connect(db, seed, keys)
    context, proposal = propose(quantity=3)
    ready = run(readiness.prepare(context, proposal.id))
    assert ready.state == "PREPARED_UNSENT"
    data["calls"].clear()
    return OrderMaterializationService(readiness, keys), context, proposal, ready, data, setup_id, method_id


def stored(db):
    with db.connect() as c:
        return (dict(c.execute(text("SELECT * FROM agentpal.payment_operations")).mappings().one()),
                dict(c.execute(text("SELECT * FROM agentpal.payments")).mappings().one()),
                dict(c.execute(text("SELECT * FROM agentpal.purchases")).mappings().one()))


def decrypt(db, keys):
    op, pay, p = stored(db)
    return keys.open_bytes(op["id"], _purpose(op, pay, p), op["request_payload_ciphertext"], op["encryption_key_id"])


def assert_unsent(db):
    with db.connect() as c:
        pay = c.execute(text("SELECT * FROM agentpal.payments")).mappings().one()
        assert pay["status"] == "prepared" and pay["dispatch_started_at"] is None and pay["provider_order_id"] is None
        for op in c.execute(text("SELECT * FROM agentpal.payment_operations")).mappings():
            assert op["status"] == "prepared" and op["dispatch_count"] == 0
            assert op["first_dispatch_at"] is None and op["idempotency_valid_until"] is None
            assert op["dispatch_revalidation_id"] is None
        assert c.scalar(text("SELECT count(*) FROM agentpal.provider_observations")) == 0
        assert c.scalar(text("SELECT count(*) FROM agentpal.payment_captures")) == 0
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='payment_dispatch_claimed'")) == 0


def check(db, purchase_id, result="passed"):
    with db.connect() as c:
        p = dict(c.execute(text("SELECT * FROM agentpal.purchases WHERE id=:id"), {"id": purchase_id}).mappings().one())
    return revalidate(db, p, result=result)


def test_exact_documented_request_persisted_encrypted_with_no_dispatch(prepared, db, keys, caplog, capsys):
    svc, context, proposal, ready, data, _, method = prepared
    result = svc.materialize(context, proposal.id)
    assert result.state == "MATERIALIZED_UNSENT" and not result.replayed
    assert result.payment_id == ready.payment_id and isinstance(result.operation_id, UUID)
    raw = decrypt(db, keys)
    op, pay, p = stored(db)
    assert json.loads(raw) == {
        "intent": "CAPTURE",
        "purchase_units": [{"reference_id": str(proposal.id), "custom_id": str(ready.payment_id),
            "amount": {"currency_code": "USD", "value": "37.02"}, "payee": {"merchant_id": PAYEE}}],
        "payment_source": {"paypal": {"vault_id": VAULT_ID, "stored_credential": {
            "payment_initiator": "MERCHANT", "usage": "SUBSEQUENT", "usage_pattern": "IMMEDIATE"}}},
    }
    assert op["endpoint"] == ENDPOINT and op["kind"] == "create_order"
    assert str(UUID(op["request_id"])) == op["request_id"]
    assert UUID(op["request_id"]) != result.operation_id
    assert op["request_body_sha256"] == hashlib.sha256(raw).digest()
    assert op["replay_window_seconds"] == 21600 == REPLAY_WINDOW_SECONDS
    assert op["retention_documentation"] == RETENTION_DOCUMENTATION
    assert VAULT_ID.encode() not in op["request_payload_ciphertext"] and raw != op["request_payload_ciphertext"]
    assert pay["payment_method_id"] == method and p["total_amount"] == ready.amount
    assert counts(db) == dict(budget_reservations=1, payments=1, payment_operations=1, provider_observations=0, payment_captures=0)
    assert_unsent(db)
    assert not data["calls"]  # No source I/O, including on first materialization.
    public = json.dumps(result.public()) + repr(result) + caplog.text + repr(capsys.readouterr())
    for secret in (VAULT_ID, CUSTOMER, PAYEE, op["request_id"], raw.decode()):
        assert secret not in public
    assert result.public()["execution_ready"] is False


@pytest.mark.parametrize("value,expected", [("0.01", "0.01"), ("1", "1.00"), ("12.3400", "12.34"),
    ("1E+3", "1000.00"), ("99999999999999999999999999999.99", "99999999999999999999999999999.99")])
def test_exact_money_independent_of_decimal_context(value, expected):
    with localcontext() as ctx:
        ctx.prec = 2
        assert _usd(Decimal(value)) == expected


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-0"), Decimal("-1"), Decimal("0.001"), Decimal("12.345"),
    Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("1E+29"), Decimal("1E-999999"), "12.00", 12.0, None])
def test_unsupported_money_never_rounded(value):
    with pytest.raises(MaterializationError, match="^UNSUPPORTED_PAYMENT_AMOUNT$"):
        _usd(value)


def test_replay_restart_rotation_preserve_exact_committed_bytes(prepared, db, keys, monkeypatch):
    svc, context, p, _, _, _, _ = prepared
    first = svc.materialize(context, p.id)
    original = stored(db)[0]
    import agentpal.orders as module
    monkeypatch.setattr(module, "_body", lambda *args: pytest.fail("Replay must not build another request"))
    monkeypatch.setattr(keys, "seal_bytes", lambda *args: pytest.fail("Replay must not encrypt again"))
    second = svc.materialize(context, p.id)
    assert second.operation_id == first.operation_id and second.replayed
    # New process configuration with a new active key and retained historical key.
    rotated = Keyring(VaultKeySettings(VAULT_ACTIVE_KEY_ID="new", VAULT_KEYS_JSON=json.dumps({
        "old": base64.b64encode(keys._keys["old"]).decode(), "new": base64.b64encode(secrets.token_bytes(32)).decode()})))
    db.dispose()
    restarted = OrderMaterializationService(svc._readiness, rotated)
    third = restarted.materialize(context, p.id)
    assert third == second
    assert stored(db)[0] == original
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='order_materialized'")) == 1
    assert_unsent(db)


def test_concurrent_calls_materialize_one_identity(prepared, db):
    svc, context, p, *_ = prepared
    barrier = Barrier(4)
    def worker():
        barrier.wait(timeout=10)
        return svc.materialize(context, p.id)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: worker(), range(4)))
    assert len({r.operation_id for r in results}) == 1
    assert sum(not r.replayed for r in results) == 1
    assert counts(db)["budget_reservations"] == counts(db)["payment_operations"] == 1
    assert_unsent(db)


@pytest.mark.parametrize("pattern", [None, "UNSCHEDULED_POSTPAID", "UNSCHEDULED_PREPAID", "RECURRING_PREPAID", "DEFERRED"])
def test_unestablished_agreement_blocks_without_guessing(setup, db, seed, keys, pattern):
    readiness, _, propose = setup
    connect(db, seed, keys, pattern=pattern)
    context, p = propose()
    assert run(readiness.prepare(context, p.id)).state == "PREPARED_UNSENT"
    result = OrderMaterializationService(readiness, keys).materialize(context, p.id)
    assert result.state == "BLOCKED" and result.reason_codes == ("PAYMENT_CONTRACT_UNVERIFIED",)
    assert counts(db)["payment_operations"] == 0
    assert_unsent(db)
    assert readiness.release_unsent(context, p.id).state == "RELEASED_UNSENT"


def test_legacy_verified_method_cannot_be_adopted(setup, db, keys):
    readiness, _, propose = setup
    context, p = propose()
    run(readiness.prepare(context, p.id))
    result = OrderMaterializationService(readiness, keys).materialize(context, p.id)
    assert result.reason_codes == ("PAYMENT_CONTRACT_UNVERIFIED",)
    assert counts(db)["payment_operations"] == 0


def test_customer_binding_mismatch_is_safe_error(setup, db, seed, keys):
    readiness, _, propose = setup
    connect(db, seed, keys, customer="FICTIONAL_WRONG_CUSTOMER")
    context, p = propose()
    run(readiness.prepare(context, p.id))
    with pytest.raises(MaterializationError, match="^PAYMENT_REFERENCE_INVALID$"):
        OrderMaterializationService(readiness, keys).materialize(context, p.id)
    assert counts(db)["payment_operations"] == 0


@pytest.mark.parametrize("before", [True, False])
@pytest.mark.parametrize("kind", ["policy", "revoked", "released", "listing"])
def test_invalidated_preparation_or_materialization_cannot_authorize(prepared, db, seed, before, kind):
    svc, context, p, _, _, _, method = prepared
    initial = svc.materialize(context, p.id) if before else None
    if kind == "policy": change_policy(db, seed, monthly_limit="200")
    elif kind == "released": svc._readiness.release_unsent(context, p.id)
    else:
        with db.begin() as c:
            if kind == "revoked":
                c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp() WHERE id=:id"), {"id": method})
        if kind == "listing": check(db, p.id, result="price_changed")
    result = svc.materialize(context, p.id)
    assert result.state == "RELEASED_UNSENT"
    assert result.operation_id == (initial.operation_id if initial else None)
    assert counts(db)["payment_operations"] == int(before)
    assert_unsent(db)


def test_expired_proposal_releases_definitely_unsent(setup, db, seed, keys):
    readiness, _, propose = setup
    connect(db, seed, keys)
    change_policy(db, seed, purchase_ttl_seconds=2)
    context, p = propose()
    run(readiness.prepare(context, p.id))
    svc = OrderMaterializationService(readiness, keys)
    first = svc.materialize(context, p.id)
    with db.connect() as c:
        c.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (:expiry-clock_timestamp())))+0.01)"), {"expiry": p.expires_at})
    result = svc.materialize(context, p.id)
    assert result.state == "RELEASED_UNSENT" and "PROPOSAL_EXPIRED" in result.reason_codes
    assert result.operation_id == first.operation_id
    assert_unsent(db)


def corrupt(db, column, value):
    # Administrator-only damage injection in our disposable DB; runtime guards
    # prohibit these edits. Preserve the trigger for subsequent operations.
    assert column in {"request_payload_ciphertext", "request_body_sha256", "encryption_key_id", "request_id"}
    with db.begin() as c:
        c.execute(text("ALTER TABLE agentpal.payment_operations DISABLE TRIGGER guard_operation"))
        c.execute(text(f"UPDATE agentpal.payment_operations SET {column}=:value"), {"value": value})
        c.execute(text("ALTER TABLE agentpal.payment_operations ENABLE TRIGGER guard_operation"))


@pytest.mark.parametrize("kind", ["ciphertext", "hash", "missing_key", "aad"])
def test_committed_operation_integrity_fails_closed(prepared, db, kind):
    svc, context, p, *_ = prepared
    first = svc.materialize(context, p.id)
    op = stored(db)[0]
    if kind == "ciphertext": corrupt(db, "request_payload_ciphertext", bytes(op["request_payload_ciphertext"][:-1]) + bytes([op["request_payload_ciphertext"][-1] ^ 1]))
    elif kind == "hash": corrupt(db, "request_body_sha256", b"x"*32)
    elif kind == "missing_key": corrupt(db, "encryption_key_id", "removed")
    else: corrupt(db, "request_id", str(uuid4()))
    damaged = stored(db)[0]
    with pytest.raises(MaterializationError) as error:
        svc.materialize(context, p.id)
    assert error.value.code == ("REQUEST_INTEGRITY_FAILED" if kind == "hash" else "ENCRYPTION_UNAVAILABLE")
    assert VAULT_ID not in str(error.value) and stored(db)[0] == damaged
    assert damaged["id"] == first.operation_id and counts(db)["payment_operations"] == 1
    assert_unsent(db)


def test_missing_historical_key_never_rebuilds_operation(prepared, db):
    svc, context, p, *_ = prepared
    svc.materialize(context, p.id)
    original = stored(db)[0]
    no_old_key = Keyring(VaultKeySettings(VAULT_ACTIVE_KEY_ID="new", VAULT_KEYS_JSON=json.dumps({
        "new": base64.b64encode(secrets.token_bytes(32)).decode()})))
    with pytest.raises(MaterializationError, match="^ENCRYPTION_UNAVAILABLE$"):
        OrderMaterializationService(svc._readiness, no_old_key).materialize(context, p.id)
    assert stored(db)[0] == original


def test_encryption_failure_rolls_back_operation_and_audit(prepared, db, monkeypatch):
    svc, context, p, *_ = prepared
    def fail(*args): raise VaultUnavailable()
    monkeypatch.setattr(svc._keys, "seal_bytes", fail)
    with pytest.raises(MaterializationError, match="^ENCRYPTION_UNAVAILABLE$"):
        svc.materialize(context, p.id)
    assert counts(db)["payment_operations"] == 0
    with db.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM agentpal.audit_events WHERE kind='order_materialized'")) == 0
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "held"
    assert_unsent(db)


@pytest.mark.parametrize("state,expected", [("40001", "TRANSACTION_CONFLICT"), ("40P01", "TRANSACTION_CONFLICT"),
    ("23505", "TRANSACTION_CONFLICT"), ("23514", "MATERIALIZATION_CONFLICT"), ("08006", "MATERIALIZATION_STORAGE_UNAVAILABLE")])
def test_commit_failure_returns_no_success_and_safe_retry(prepared, db, state, expected):
    svc, context, p, *_ = prepared
    class Abort(Exception): sqlstate = state
    def abort(c): raise IntegrityError("sensitive SQL must not escape", {}, Abort())
    event.listen(db, "commit", abort)
    try:
        with pytest.raises(MaterializationError, match=f"^{expected}$"):
            svc.materialize(context, p.id)
    finally: event.remove(db, "commit", abort)
    assert counts(db)["payment_operations"] == 0
    assert svc.materialize(context, p.id).state == "MATERIALIZED_UNSENT"
    assert_unsent(db)


def test_dispatched_payment_returns_recovery_identity_and_keeps_hold(prepared, db):
    svc, context, p, *_ = prepared
    first = svc.materialize(context, p.id)
    check(db, p.id)
    dispatch(db, first.operation_id)
    result = svc.materialize(context, p.id)
    assert result.state == "EXISTING_PAYMENT" and result.operation_id == first.operation_id
    with db.connect() as c:
        assert c.scalar(text("SELECT status FROM agentpal.budget_reservations")) == "held"
        assert c.scalar(text("SELECT dispatch_count FROM agentpal.payment_operations")) == 1
        assert c.scalar(text("SELECT count(*) FROM agentpal.provider_observations")) == 0


def test_foreign_context_rejected_and_no_browser_authority(prepared, db):
    svc, context, p, *_ = prepared
    for bad in (replace(context, run_id=uuid4()), replace(context, owner_key="intruder"), None, {"owner": context.owner_key}):
        with pytest.raises(MaterializationError, match="^CONTEXT_REJECTED$"):
            svc.materialize(bad, p.id)
    with pytest.raises(TypeError):
        svc.materialize(context, p.id, amount="1.00")
    assert counts(db)["payment_operations"] == 0


def test_zero_amount_is_rejected_without_fabricating_order(setup, db, keys, seed):
    readiness, _, propose = setup
    connect(db, seed, keys)
    context, p = propose("0")
    run(readiness.prepare(context, p.id))
    with pytest.raises(MaterializationError, match="^UNSUPPORTED_PAYMENT_AMOUNT$"):
        OrderMaterializationService(readiness, keys).materialize(context, p.id)
    assert counts(db)["payment_operations"] == 0
    assert readiness.release_unsent(context, p.id).state == "RELEASED_UNSENT"


@pytest.mark.parametrize("kind", ["amount", "payee", "budget"])
def test_inconsistent_financial_evidence_is_not_materialized(prepared, db, kind):
    svc, context, p, *_ = prepared
    # Administrator-only corruption of our disposable fixtures; application
    # roles cannot normally violate these existing immutable/ledger guards.
    with db.begin() as c:
        if kind == "amount":
            c.execute(text("ALTER TABLE agentpal.budget_reservations DISABLE TRIGGER guard_reservation"))
            c.execute(text("UPDATE agentpal.budget_reservations SET amount=36.02"))
            c.execute(text("ALTER TABLE agentpal.budget_reservations ENABLE TRIGGER guard_reservation"))
        elif kind == "payee":
            c.execute(text("ALTER TABLE agentpal.payments DISABLE TRIGGER guard_payment"))
            c.execute(text("UPDATE agentpal.payments SET sandbox_payee='NPQRSTUVWXYZ2'"))
            c.execute(text("ALTER TABLE agentpal.payments ENABLE TRIGGER guard_payment"))
        else:
            c.execute(text("ALTER TABLE agentpal.budget_periods DISABLE TRIGGER guard_period"))
            c.execute(text("UPDATE agentpal.budget_periods SET limit_amount=1"))
            c.execute(text("ALTER TABLE agentpal.budget_periods ENABLE TRIGGER guard_period"))
    if kind == "budget":
        result = svc.materialize(context, p.id)
        assert result.state == "BLOCKED" and result.reason_codes == ("BUDGET_INVALIDATED",)
    else:
        with pytest.raises(MaterializationError, match="^" + (
            "PAYMENT_AMOUNT_MISMATCH" if kind == "amount" else "SANDBOX_PAYEE_INVALID") + "$"):
            svc.materialize(context, p.id)
    assert counts(db)["payment_operations"] == 0
    assert_unsent(db)


def test_existing_legacy_operation_never_gets_replaced(prepared, db):
    svc, context, p, ready, *_ = prepared
    identifier = operation(db, ready.payment_id)
    with pytest.raises(MaterializationError, match="^TRUSTED_RECOVERY_REQUIRED$"):
        svc.materialize(context, p.id)
    assert stored(db)[0]["id"] == identifier and counts(db)["payment_operations"] == 1
    assert svc._readiness.release_unsent(context, p.id).state == "RELEASED_UNSENT"
    assert svc.materialize(context, p.id).operation_id == identifier
    assert_unsent(db)


def test_expiry_during_encryption_rolls_back_and_leaves_safe_release(setup, db, seed, keys, monkeypatch):
    readiness, _, propose = setup
    connect(db, seed, keys)
    change_policy(db, seed, purchase_ttl_seconds=2)
    context, p = propose()
    run(readiness.prepare(context, p.id))
    svc = OrderMaterializationService(readiness, keys)
    original = keys.seal_bytes
    def slow(*args):
        sealed = original(*args)
        with db.connect() as c:
            c.execute(text("SELECT pg_sleep(greatest(0,extract(epoch FROM (:expiry-clock_timestamp())))+0.01)"), {"expiry": p.expires_at})
        return sealed
    monkeypatch.setattr(keys, "seal_bytes", slow)
    with pytest.raises(MaterializationError, match="^MATERIALIZATION_CONFLICT$"):
        svc.materialize(context, p.id)
    assert counts(db)["payment_operations"] == 0
    assert svc.materialize(context, p.id).state == "RELEASED_UNSENT"
    assert_unsent(db)


@pytest.mark.parametrize("kind", ["policy", "revocation"])
def test_materialization_serializes_with_concurrent_invalidation(prepared, db, seed, kind):
    svc, context, p, _, _, _, method = prepared
    entered, finish, started = Event(), Event(), Event()
    def block(c, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO agentpal.payment_operations"):
            entered.set()
            assert finish.wait(10)
    def invalidate():
        started.set()
        if kind == "policy": change_policy(db, seed, monthly_limit="200")
        else:
            with db.begin() as c:
                c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp() WHERE id=:id"), {"id": method})
    event.listen(db, "before_cursor_execute", block)
    try:
        with ThreadPoolExecutor(2) as pool:
            materializing = pool.submit(svc.materialize, context, p.id)
            assert entered.wait(10)
            invalidating = pool.submit(invalidate)
            assert started.wait(10)
            assert not invalidating.done()
            finish.set()
            first = materializing.result(timeout=10)
            invalidating.result(timeout=10)
    finally:
        finish.set()
        event.remove(db, "before_cursor_execute", block)
    assert first.state == "MATERIALIZED_UNSENT"
    later = svc.materialize(context, p.id)
    assert later.state == "RELEASED_UNSENT" and later.operation_id == first.operation_id
    assert counts(db)["payment_operations"] == 1
    assert_unsent(db)


def test_source_outage_evidence_cannot_use_previous_pass(prepared, db):
    svc, context, p, *_ = prepared
    check(db, p.id, result="source_error")
    result = svc.materialize(context, p.id)
    assert result.state == "BLOCKED" and result.reason_codes == ("SOURCE_CHECK_REQUIRED",)
    assert counts(db)["payment_operations"] == 0
    assert_unsent(db)


def test_actual_human_consent_path_materializes_without_browser_cookie(setup, db, seed, keys):
    readiness, _, propose = setup
    connect(db, seed, keys)
    context, p = propose("60")
    assert run(readiness.prepare(context, p.id)).state == "BLOCKED"
    decide(db, seed, p)
    run(readiness.prepare(context, p.id))
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp()"))
    result = OrderMaterializationService(readiness, keys).materialize(context, p.id)
    assert result.state == "MATERIALIZED_UNSENT"
    assert json.loads(decrypt(db, keys))["purchase_units"][0]["amount"]["value"] == "60.00"
    assert_unsent(db)


def test_no_payment_does_not_create_one(setup, db, keys):
    readiness, _, propose = setup
    context, p = propose()
    result = OrderMaterializationService(readiness, keys).materialize(context, p.id)
    assert result.state == "NOT_PREPARED" and counts(db)["payments"] == 0


def test_exact_bytes_aead_binds_record_purpose_and_key(keys):
    identifier = uuid4()
    raw = b'{ "exact_bytes": 1 }\n'
    cipher, key = keys.seal_bytes(identifier, "fixture-purpose", raw)
    assert keys.open_bytes(identifier, "fixture-purpose", cipher, key) == raw
    for wrong_id, wrong_purpose, wrong_key in ((uuid4(), "fixture-purpose", key),
            (identifier, "other-purpose", key), (identifier, "fixture-purpose", "missing")):
        with pytest.raises(VaultUnavailable):
            keys.open_bytes(wrong_id, wrong_purpose, cipher, wrong_key)


def test_different_purchases_get_distinct_record_bound_requests(prepared, setup, db, keys):
    svc, context, p, *_ = prepared
    first = svc.materialize(context, p.id)
    context2, p2 = setup[2]()
    run(svc._readiness.prepare(context2, p2.id))
    second = svc.materialize(context2, p2.id)
    with db.connect() as c:
        operations = c.execute(text("SELECT * FROM agentpal.payment_operations ORDER BY created_at")).mappings().all()
        payment2 = c.execute(text("SELECT * FROM agentpal.payments WHERE id=:id"), {"id": second.payment_id}).mappings().one()
    assert first.operation_id != second.operation_id
    assert operations[0]["request_id"] != operations[1]["request_id"]
    with pytest.raises(VaultUnavailable):
        keys.open_bytes(operations[0]["id"], _purpose(operations[0], payment2, {"id": p2.id}),
            operations[0]["request_payload_ciphertext"], operations[0]["encryption_key_id"])
    assert counts(db)["budget_reservations"] == counts(db)["payment_operations"] == 2


@pytest.mark.parametrize("kind", ["policy", "revocation"])
def test_invalidation_holding_owner_lock_wins_before_materialization(prepared, db, seed, kind):
    svc, context, p, _, _, _, method = prepared
    held, finish, waiting = Event(), Event(), Event()
    def hook(c, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE agentpal.owners SET active_policy_id") or (
                kind == "revocation" and statement.startswith("UPDATE agentpal.payment_methods SET revoked_at")):
            held.set()
            assert finish.wait(10)
        if statement == "SELECT agentpal.lock_finances()": waiting.set()
    def invalidate():
        if kind == "policy": change_policy(db, seed, monthly_limit="200")
        else:
            with db.begin() as c:
                c.execute(text("SELECT agentpal.lock_finances()"))
                c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp() WHERE id=:id"), {"id": method})
    event.listen(db, "before_cursor_execute", hook)
    try:
        with ThreadPoolExecutor(2) as pool:
            invalidating = pool.submit(invalidate)
            assert held.wait(10)
            waiting.clear()
            materializing = pool.submit(svc.materialize, context, p.id)
            assert waiting.wait(10)
            assert not materializing.done()
            finish.set()
            invalidating.result(timeout=10)
            result = materializing.result(timeout=10)
    finally:
        finish.set()
        event.remove(db, "before_cursor_execute", hook)
    assert result.state == "RELEASED_UNSENT" and counts(db)["payment_operations"] == 0
    assert_unsent(db)


def test_real_onboarding_with_missing_provider_metadata_remains_blocked(setup, db, seed):
    """Exercise actual browser/verification code with HTTPX's synthetic provider."""
    import httpx
    from fastapi.testclient import TestClient
    from agentpal.auth.config import AuthSettings
    from agentpal.auth.service import token_hash
    from agentpal.main import create_app
    from agentpal.vault.paypal import PayPal
    from agentpal.vault.service import VaultService
    from test_vault import (COOKIE, CSRF, ORIGIN, SESSION, Provider, callback, complete, config, start)
    readiness, _, propose = setup
    vault_session = uuid4()
    with db.begin() as c:
        c.execute(text("UPDATE agentpal.payment_methods SET revoked_at=clock_timestamp() WHERE revoked_at IS NULL"))
        c.execute(text("""INSERT INTO agentpal.owner_sessions(id,token_hash,csrf_token_hash,expires_at)
            VALUES (:id,:token,:csrf,clock_timestamp()+interval '1 hour')"""),
            {"id": vault_session, "token": token_hash(SESSION, "session"), "csrf": token_hash(CSRF, "csrf")})
    app = create_app(engine=db, settings=AuthSettings(AUTH_ORIGIN=ORIGIN))
    provider, vault_settings = Provider(db), config()
    app.state.vault_service = VaultService(app.state.auth_service, vault_settings, PayPal(vault_settings, transport=httpx.MockTransport(provider)))
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF}) as client:
        client.cookies.set(COOKIE, SESSION)
        flow = client, app, provider, vault_session
        identifier = start(flow)
        callback(flow)
        assert complete(flow, identifier).json()["state"] == "complete"
    context, p = propose()
    assert run(readiness.prepare(context, p.id)).state == "PREPARED_UNSENT"
    calls = list(provider.calls)
    result = OrderMaterializationService(readiness, Keyring(vault_settings)).materialize(context, p.id)
    assert result.state == "BLOCKED" and result.reason_codes == ("PAYMENT_CONTRACT_UNVERIFIED",)
    assert provider.calls == calls and counts(db)["payment_operations"] == 0
    assert_unsent(db)


def test_materialization_works_with_documented_nonowner_runtime_grants(prepared, db, keys):
    from sqlalchemy import create_engine
    from agentpal.readiness import ReadinessService
    svc, context, p, *_ = prepared
    role = "orders_test_" + uuid4().hex
    with db.begin() as c:
        c.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN')
        c.exec_driver_sql(f'GRANT USAGE ON SCHEMA agentpal TO "{role}"')
        c.exec_driver_sql(f'GRANT USAGE ON TYPE agentpal.usd_amount TO "{role}"')
        c.exec_driver_sql(f'GRANT SELECT,INSERT ON ALL TABLES IN SCHEMA agentpal TO "{role}"')
        c.exec_driver_sql(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA agentpal TO "{role}"')
        c.exec_driver_sql(f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA agentpal TO "{role}"')
        c.exec_driver_sql(f'GRANT UPDATE ON agentpal.owners,agentpal.owner_sessions,agentpal.agents,agentpal.budget_periods,agentpal.budget_reservations,agentpal.vault_setups,agentpal.payment_methods,agentpal.payments,agentpal.payment_operations TO "{role}"')
    runtime = create_engine(db.url, hide_parameters=True)
    def enter(c): c.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
    event.listen(runtime, "begin", enter)
    try:
        gate = ReadinessService(runtime, svc._readiness._source, svc._readiness._settings)
        scoped = OrderMaterializationService(gate, keys)
        first = scoped.materialize(context, p.id)
        assert first.state == "MATERIALIZED_UNSENT"
        assert scoped.materialize(context, p.id).operation_id == first.operation_id
        assert gate.release_unsent(context, p.id).state == "RELEASED_UNSENT"
        assert scoped.materialize(context, p.id).state == "RELEASED_UNSENT"
    finally: runtime.dispose()
