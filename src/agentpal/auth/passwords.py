"""Argon2id with the RFC 9106 low-memory profile (64 MiB, three passes)."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.profiles import RFC_9106_LOW_MEMORY


PASSWORD_HASHER = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)


def hash_password(password: str) -> str:
    if not 15 <= len(password) <= 128:
        raise ValueError("Use a password or passphrase of 15 to 128 characters")
    return PASSWORD_HASHER.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    if not encoded.startswith("$argon2id$"):
        return False
    try:
        return PASSWORD_HASHER.verify(encoded, password)
    except (InvalidHashError, VerificationError):
        return False
