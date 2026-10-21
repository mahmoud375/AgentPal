"""Alembic environment; explicit URL or injected test connection, no dotenv."""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from agentpal.db.config import DatabaseSettings

config = context.config


def configured_url() -> str:
    explicit = config.get_main_option("sqlalchemy.url")
    settings = DatabaseSettings(DATABASE_URL=explicit) if explicit else DatabaseSettings()
    return settings.database_url.get_secret_value()


def migrate(connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=configured_url(), target_metadata=None,
        literal_binds=True, dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    migrate(config.attributes["connection"])
else:
    engine = create_engine(configured_url(), poolclass=NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
