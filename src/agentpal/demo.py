"""Loopback-only rehearsal using a disposable PostgreSQL cluster, never runtime.env.

Uses the existing migration chain and production application services. This is
not an alternate payment implementation. No provider network request is made.
"""

from contextlib import contextmanager
import getpass
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import warnings

from alembic import command
from alembic.config import Config
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
import uvicorn

from agentpal.auth.bootstrap import bootstrap_owner
from agentpal.auth.config import AuthSettings
from agentpal.auth.service import OwnerWriteContext
from agentpal.main import create_app
from agentpal.policy import PolicyInput, configure_policy
from agentpal.rehearsal import RehearsalVault, rehearsal_runtime


@contextmanager
def rehearsal_app(password: str, *, port: int = 8010):
    """Only ever constructs its own private unix-socket PostgreSQL cluster."""
    required = {name: shutil.which(name) for name in ("initdb", "pg_ctl")}
    if not all(required.values()):
        raise RuntimeError("PostgreSQL initdb and pg_ctl are required")
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="agentpal-rehearsal-") as folder:
        location = Path(folder)
        socket = location / "socket"
        socket.mkdir(mode=0o700)
        data = location / "data"
        passfile = location / "empty-passfile"
        passfile.touch(mode=0o600)
        started = False
        engine = None
        try:
            subprocess.run([required["initdb"], "-D", str(data), "--auth-local=trust", "--auth-host=reject", "--no-instructions"],
                           check=True, capture_output=True)
            subprocess.run([required["pg_ctl"], "-D", str(data), "-l", str(location / "postgres.log"),
                            "-o", f"-k {socket} -c listen_addresses= -c fsync=off", "-w", "start"],
                           check=True, capture_output=True)
            started = True
            url = URL.create("postgresql+psycopg", username=getpass.getuser(), database="postgres",
                             query={"host": str(socket), "passfile": str(passfile), "sslmode": "disable"})
            engine = create_engine(url, hide_parameters=True)
            config = Config(str(root / "alembic.ini"))
            with engine.begin() as c:
                config.attributes["connection"] = c
                command.upgrade(config, "head")
            bootstrap_owner(engine, "demo-owner", password)
            application = create_app(engine=engine, settings=AuthSettings(
                AUTH_ORIGIN=f"http://127.0.0.1:{port}", AUTH_ALLOW_INSECURE_LOCALHOST=True))
            # Configure only this newly created, disposable database. The session
            # is revoked before browser access; it exists solely for seed attribution.
            service = application.state.auth_service
            session = service.login("demo-owner", password, None)
            with engine.begin() as c:
                c.execute(text("INSERT INTO agentpal.agents(name) VALUES ('Shopping agent')"))
                owner = service.session(c, session.token, lock=True)
                configure_policy(OwnerWriteContext(owner, c), PolicyInput(
                    expected_policy_id=None, monthly_limit="500.00", auto_approval_limit="50.00",
                    hard_purchase_limit="150.00", purchase_ttl_seconds=3600, merchant_allowlist=["demo-shop.test"]))
                service.revoke(c, session.token)
            application.state.shopping_runtime = rehearsal_runtime()
            application.state.vault_service = RehearsalVault()

            @application.middleware("http")
            async def no_paypal_actions(request: Request, call_next):
                if request.url.path.startswith("/paypal/") and request.url.path != "/paypal/vault/status":
                    return JSONResponse(status_code=403, content={"detail": "PayPal actions are disabled in offline rehearsal"})
                return await call_next(request)

            yield application
        finally:
            if engine is not None:
                engine.dispose()
            if started:
                subprocess.run([required["pg_ctl"], "-D", str(data), "-m", "immediate", "-w", "stop"],
                               check=True, capture_output=True)


def main():
    if sys.argv[1:] == ["--help"]:
        print("Offline rehearsal on http://127.0.0.1:8010. Temporary PostgreSQL; scripted model; no provider calls.")
        return 0
    if sys.argv[1:] or not sys.stdin.isatty() or not sys.stderr.isatty():
        print("Run directly in an interactive terminal. No arguments or piped passwords accepted.", file=sys.stderr)
        return 2
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Choose a disposable demo password (15–128 characters): ")
            if not 15 <= len(password) <= 128:
                print("Password must be 15–128 characters.", file=sys.stderr)
                return 2
            if getpass.getpass("Confirm disposable demo password: ") != password:
                print("Passwords did not match.", file=sys.stderr)
                return 2
        with rehearsal_app(password) as application:
            del password
            print("OFFLINE REHEARSAL: scripted model, synthetic listings, disposable database. No real AI or PayPal calls.")
            print("Open http://127.0.0.1:8010 and sign in as demo-owner. Ctrl-C removes the disposable database.")
            uvicorn.run(application, host="127.0.0.1", port=8010, workers=1, proxy_headers=False, access_log=False)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Offline rehearsal stopped. No runtime configuration or persistent database was used.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
