"""Explicit database configuration. Never automatically load credential files."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr = Field(validation_alias="DATABASE_URL", repr=False)

    @field_validator("database_url")
    @classmethod
    def require_postgresql(cls, value: SecretStr) -> SecretStr:
        try:
            driver = make_url(value.get_secret_value()).drivername
        except Exception:
            raise ValueError("DATABASE_URL must be a PostgreSQL psycopg URL") from None
        if driver != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        return value


def create_database_engine(settings: DatabaseSettings) -> Engine:
    """Create a lazy, synchronous engine without connecting or logging parameters."""
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        isolation_level="READ COMMITTED",
    )
