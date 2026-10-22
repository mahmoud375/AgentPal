"""Externally supplied Sandbox configuration and record-bound AEAD envelopes."""

import base64
import json
import re
import secrets
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class VaultUnavailable(Exception):
    """Deliberately contains no underlying credential, ciphertext or provider data."""


class VaultKeySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)

    active_key_id: str = Field(validation_alias="VAULT_ACTIVE_KEY_ID")
    keys_json: SecretStr = Field(validation_alias="VAULT_KEYS_JSON", repr=False)

    @model_validator(mode="after")
    def validate_keys(self):
        Keyring(self)
        return self


class VaultSettings(VaultKeySettings):
    environment: str = Field(validation_alias="PAYPAL_ENVIRONMENT")
    base_url: str = Field(validation_alias="PAYPAL_BASE_URL")
    client_id: SecretStr = Field(validation_alias="PAYPAL_CLIENT_ID", repr=False)
    client_secret: SecretStr = Field(validation_alias="PAYPAL_CLIENT_SECRET", repr=False)
    ttl_seconds: int = Field(default=1200, ge=120, le=3600, validation_alias="VAULT_TTL_SECONDS")

    @model_validator(mode="after")
    def validate_sandbox(self):
        if self.environment != "sandbox" or self.base_url != "https://api-m.sandbox.paypal.com":
            raise ValueError("Explicit PayPal Sandbox configuration required")
        if not self.client_id.get_secret_value() or not self.client_secret.get_secret_value():
            raise ValueError("PayPal Sandbox credentials required")
        return self


class Keyring:
    def __init__(self, settings: VaultKeySettings):
        self.active = settings.active_key_id
        try:
            raw = json.loads(settings.keys_json.get_secret_value())
            if not isinstance(raw, dict) or not 1 <= len(raw) <= 16:
                raise ValueError
            self._keys = {}
            for name, value in raw.items():
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                    raise ValueError
                key = base64.b64decode(value, validate=True)
                if len(key) != 32:
                    raise ValueError
                self._keys[name] = key
            if self.active not in self._keys:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Invalid Vault encryption configuration") from None

    @staticmethod
    def aad(identifier: UUID, purpose: str, key_id: str) -> bytes:
        return f"agentpal:vault:v1:{identifier}:{purpose}:{key_id}".encode("ascii")

    def seal(self, identifier: UUID, purpose: str, value: dict) -> tuple[bytes, str]:
        return self.seal_bytes(identifier, purpose, json.dumps(value, sort_keys=True, separators=(",", ":")).encode())

    def seal_bytes(self, identifier: UUID, purpose: str, body: bytes) -> tuple[bytes, str]:
        """Encrypt exact bytes without reserializing financial request evidence."""
        try:
            if not isinstance(body, bytes):
                raise TypeError
            nonce = secrets.token_bytes(12)
            return nonce + AESGCM(self._keys[self.active]).encrypt(
                nonce, body, self.aad(identifier, purpose, self.active)), self.active
        except (KeyError, ValueError, TypeError, OverflowError):
            raise VaultUnavailable() from None

    def open_bytes(self, identifier: UUID, purpose: str, ciphertext: bytes, key_id: str) -> bytes:
        try:
            return AESGCM(self._keys[key_id]).decrypt(
                ciphertext[:12], ciphertext[12:], self.aad(identifier, purpose, key_id))
        except (KeyError, InvalidTag, ValueError, TypeError, UnicodeError):
            raise VaultUnavailable() from None

    def open(self, identifier: UUID, purpose: str, ciphertext: bytes, key_id: str) -> dict:
        try:
            value = json.loads(self.open_bytes(identifier, purpose, ciphertext, key_id))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (KeyError, InvalidTag, ValueError, TypeError, UnicodeError):
            raise VaultUnavailable() from None
