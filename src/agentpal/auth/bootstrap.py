"""Interactive local owner initialization. No password arguments or defaults."""

import getpass
import re
import sys
import warnings

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from agentpal.auth.passwords import hash_password
from agentpal.db import DatabaseSettings, create_database_engine


def bootstrap_owner(engine: Engine, login_name: str, password: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", login_name):
        raise ValueError("Use 1 to 128 letters, digits or . _ @ - for the login name")
    encoded = hash_password(password)
    with engine.begin() as c:
        created = c.scalar(text("""INSERT INTO agentpal.owners(login_name,password_hash)
            VALUES (:name,:hash) ON CONFLICT (id) DO NOTHING RETURNING id"""), {"name": login_name,"hash": encoded})
        if created:
            c.execute(text("""INSERT INTO agentpal.audit_events(actor_kind,kind,details)
                VALUES ('system','owner_bootstrapped','{}')"""))
    return bool(created)


def main() -> int:
    if sys.argv[1:] == ["--help"]:
        print("Initialize the single owner through interactive prompts. Set DATABASE_URL privately first.")
        return 0
    if sys.argv[1:]:
        print("No command arguments accepted. Run without arguments for secure interactive prompts.", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print("An interactive terminal is required; password input cannot be piped.", file=sys.stderr)
        return 2
    engine = None
    try:
        engine = create_database_engine(DatabaseSettings())
        with engine.connect() as c:
            if c.scalar(text("SELECT EXISTS(SELECT 1 FROM agentpal.owners)")):
                print("Owner already exists; unchanged.", file=sys.stderr)
                return 1
        name = input("Owner login name: ")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Owner password (15–128 characters): ")
            confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords did not match; nothing changed.", file=sys.stderr)
            return 2
        if not bootstrap_owner(engine, name, password):
            print("Owner already exists; unchanged.", file=sys.stderr)
            return 1
        print("Owner created.")
        return 0
    except (ValueError, SQLAlchemyError, getpass.GetPassWarning):
        print("Bootstrap failed. Check configuration, database availability and input requirements.", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("Bootstrap cancelled; nothing changed.", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
