"""Every integration test gets a fresh database on our own disposable cluster.

No DATABASE_URL, dotenv, existing server, password file, or real credentials.
All monetary values and provider references below are fictional test fixtures.
"""

import getpass
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def postgres_url():
    required = {name: shutil.which(name) for name in ("initdb", "pg_ctl")}
    if not all(required.values()):
        pytest.skip("PostgreSQL initdb and pg_ctl are required for disposable integration tests")
    folder = Path(tempfile.mkdtemp(prefix="agentpal-pg-"))
    socket = folder / "socket"
    socket.mkdir(mode=0o700)
    passfile = folder / "empty-passfile"
    passfile.touch(mode=0o600)
    data = folder / "data"
    started = False
    try:
        subprocess.run(
            [required["initdb"], "-D", str(data), "--auth-local=trust", "--auth-host=reject", "--no-instructions"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [required["pg_ctl"], "-D", str(data), "-l", str(folder / "server.log"),
             "-o", f"-k {socket} -c listen_addresses= -c fsync=off", "-w", "start"],
            check=True, capture_output=True, text=True,
        )
        started = True
        yield URL.create(
            "postgresql+psycopg", username=getpass.getuser(), password="", database="postgres",
            query={"host": str(socket), "passfile": str(passfile), "sslmode": "disable"},
        )
    finally:
        if started:
            subprocess.run([required["pg_ctl"], "-D", str(data), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True, text=True)
        shutil.rmtree(folder)


@pytest.fixture
def db(postgres_url):
    name = "agentpal_test_" + uuid4().hex
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine = create_engine(postgres_url.set(database=name), hide_parameters=True)
    try:
        config = Config(str(ROOT / "alembic.ini"))
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def seed(db):
    values = {name: uuid4() for name in ("session", "policy", "agent", "setup", "method")}
    with db.begin() as c:
        c.execute(text("""INSERT INTO agentpal.owners(login_name,password_hash,sandbox_business_account_id)
            VALUES ('test-owner','$argon2id$test-fixture-only','test-sandbox-business')"""))
        c.execute(text("""INSERT INTO agentpal.owner_sessions(id,token_hash,csrf_token_hash,expires_at)
            VALUES (:session,:token,:csrf,clock_timestamp()+interval '1 hour')"""),
            values | {"token": b"t" * 32, "csrf": b"c" * 32})
        c.execute(text("""INSERT INTO agentpal.policies(id,monthly_limit,auto_approval_limit,hard_purchase_limit,
            purchase_ttl_seconds,merchant_allowlist,created_by_session_id)
            VALUES (:policy,100,50,300,3600,ARRAY['allowed.test'],:session)"""), values)
        c.execute(text("UPDATE agentpal.owners SET active_policy_id=:policy"), values)
        c.execute(text("INSERT INTO agentpal.agents(id,name) VALUES (:agent,'test-agent')"), values)
        c.execute(text("""INSERT INTO agentpal.vault_setups(id,owner_session_id,state_hash,status,expires_at,verified_at)
            VALUES (:setup,:session,:state,'verified',clock_timestamp()+interval '1 hour',clock_timestamp())"""),
            values | {"state": b"s" * 32})
        c.execute(text("""INSERT INTO agentpal.payment_methods(id,vault_setup_id,reference_ciphertext,encryption_key_id,verified_at)
            SELECT :method,id,:cipher,'test-key',verified_at FROM agentpal.vault_setups WHERE id=:setup"""),
            values | {"cipher": b"test-fixture-not-a-real-vault-reference"})
    return values
