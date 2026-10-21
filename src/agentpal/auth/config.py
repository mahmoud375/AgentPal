"""Explicit browser security configuration; no credential-file loading."""

from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalized_origin(value: str, *, allow_path: bool = False) -> str:
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid origin")
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("Invalid origin")
    if not allow_path and (parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError("Origin must contain only scheme, host and optional port")
    host = parts.hostname.lower()
    if not host.isascii() or "\\" in host or "%" in host:
        raise ValueError("Invalid origin host")
    host = f"[{host}]" if ":" in host else host
    port = parts.port
    suffix = f":{port}" if port and port != (443 if parts.scheme == "https" else 80) else ""
    return f"{parts.scheme}://{host}{suffix}"


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)

    origin: str = Field(validation_alias="AUTH_ORIGIN")
    allow_insecure_localhost: bool = Field(default=False, validation_alias="AUTH_ALLOW_INSECURE_LOCALHOST")
    session_ttl_seconds: int = Field(default=28800, ge=60, le=86400, validation_alias="AUTH_SESSION_TTL_SECONDS")
    login_max_attempts: int = Field(default=5, ge=1, le=100, validation_alias="AUTH_LOGIN_MAX_ATTEMPTS")
    login_window_seconds: int = Field(default=60, ge=1, le=3600, validation_alias="AUTH_LOGIN_WINDOW_SECONDS")

    @model_validator(mode="after")
    def validate_transport(self):
        self.origin = normalized_origin(self.origin)
        parts = urlsplit(self.origin)
        if self.allow_insecure_localhost:
            if parts.scheme != "http" or parts.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Insecure development mode requires an HTTP loopback origin")
        elif parts.scheme != "https":
            raise ValueError("HTTPS is required unless loopback development is explicitly enabled")
        return self

    @property
    def secure_cookies(self) -> bool:
        return not self.allow_insecure_localhost

    @property
    def session_cookie(self) -> str:
        return "__Host-agentpal_session" if self.secure_cookies else "agentpal_session_dev"

    @property
    def csrf_cookie(self) -> str:
        return "__Host-agentpal_csrf" if self.secure_cookies else "agentpal_csrf_dev"
