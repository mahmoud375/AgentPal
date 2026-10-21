"""Durable sessions and bounded process-local login admission, without HTTP I/O."""

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import math
import re
import secrets
from threading import BoundedSemaphore, Lock
from time import monotonic
from uuid import UUID

from argon2 import extract_parameters
from argon2.exceptions import InvalidHashError
from argon2.low_level import Type
from sqlalchemy import Connection, Engine, text

from agentpal.auth.config import AuthSettings
from agentpal.auth.passwords import PASSWORD_HASHER, verify_password


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def token_hash(value: str, purpose: str) -> bytes:
    return hashlib.sha256(purpose.encode("ascii") + b"\0" + value.encode("ascii")).digest()


def valid_token(value: str | None) -> bool:
    return bool(value and TOKEN_PATTERN.fullmatch(value))


class LoginThrottled(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


class LoginLimiter:
    """One bounded bucket for the sole owner, across all names/IPs/forwarded headers."""

    def __init__(self, maximum: int, window: int, clock=monotonic):
        self.maximum, self.window, self.clock = maximum, window, clock
        self.attempts: deque[float] = deque()
        self.lock = Lock()
        self.hash_slots = BoundedSemaphore(2)

    @contextmanager
    def admit(self):
        with self.lock:
            now = self.clock()
            while self.attempts and self.attempts[0] <= now - self.window:
                self.attempts.popleft()
            if len(self.attempts) >= self.maximum:
                raise LoginThrottled(max(1, math.ceil(self.attempts[0] + self.window - now)))
            self.attempts.append(now)
        if not self.hash_slots.acquire(blocking=False):
            raise LoginThrottled(1)
        try:
            yield
        finally:
            self.hash_slots.release()


@dataclass(frozen=True)
class OwnerSession:
    login_name: str
    session_id: UUID
    csrf_hash: bytes = field(repr=False)


@dataclass(frozen=True)
class NewSession:
    login_name: str
    expires_at: datetime
    token: str = field(repr=False)
    csrf: str = field(repr=False)


@dataclass(frozen=True)
class OwnerWriteContext:
    owner: OwnerSession
    connection: Connection = field(repr=False)


class AuthService:
    def __init__(self, engine: Engine, settings: AuthSettings):
        self.engine, self.settings = engine, settings
        self.limiter = LoginLimiter(settings.login_max_attempts, settings.login_window_seconds)
        self._dummy_hash = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))

    def login(self, login_name: str, password: str, previous_token: str | None) -> NewSession | None:
        with self.limiter.admit():
            # Hashing is deliberately outside a database transaction/row lock.
            with self.engine.connect() as c:
                owner = c.execute(text("SELECT login_name,password_hash FROM agentpal.owners WHERE id=true")).mappings().one_or_none()
            encoded = owner["password_hash"] if owner else self._dummy_hash
            try:
                if extract_parameters(encoded).type != Type.ID:
                    encoded = self._dummy_hash
            except InvalidHashError:
                encoded = self._dummy_hash
            verified = verify_password(encoded, password)
            name_matches = hmac.compare_digest(login_name.encode(), owner["login_name"].encode() if owner else b"")
            if not (owner and verified and name_matches):
                return None
            replacement = PASSWORD_HASHER.hash(password) if PASSWORD_HASHER.check_needs_rehash(encoded) else None
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with self.engine.begin() as c:
                current = c.scalar(text("SELECT password_hash FROM agentpal.owners WHERE id=true FOR UPDATE"))
                if current is None or not hmac.compare_digest(current, encoded):
                    return None
                if replacement:
                    c.execute(text("UPDATE agentpal.owners SET password_hash=:hash WHERE id=true"), {"hash": replacement})
                if valid_token(previous_token):
                    self.revoke(c, previous_token)
                row = c.execute(text("""INSERT INTO agentpal.owner_sessions(token_hash,csrf_token_hash,expires_at)
                    VALUES (:token,:csrf,clock_timestamp()+:ttl*interval '1 second') RETURNING id,expires_at"""),
                    {"token": token_hash(token,"session"), "csrf": token_hash(csrf,"csrf"),
                     "ttl": self.settings.session_ttl_seconds}).one()
                c.execute(text("""INSERT INTO agentpal.audit_events(actor_kind,owner_session_id,kind,details)
                    VALUES ('owner',:id,'owner_session_created','{}')"""), {"id": row.id})
            return NewSession(owner["login_name"], row.expires_at, token, csrf)

    def session(self, c: Connection, token: str | None, *, lock: bool = False) -> OwnerSession | None:
        if not valid_token(token):
            return None
        params = {"hash": token_hash(token, "session")}
        if lock:
            # Same owner -> session order as login/logout and future financial services.
            c.execute(text("""SELECT o.id FROM agentpal.owners o JOIN agentpal.owner_sessions s ON s.owner_id=o.id
                WHERE s.token_hash=:hash FOR UPDATE OF o"""), params)
        row = c.execute(text("""SELECT o.login_name,s.id,s.csrf_token_hash FROM agentpal.owner_sessions s
            JOIN agentpal.owners o ON o.id=s.owner_id WHERE s.token_hash=:hash
            AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()""" + (" FOR SHARE OF s" if lock else "")), params).one_or_none()
        return OwnerSession(row.login_name, row.id, row.csrf_token_hash) if row else None

    @staticmethod
    def csrf_matches(session: OwnerSession, value: str | None) -> bool:
        return valid_token(value) and hmac.compare_digest(session.csrf_hash, token_hash(value, "csrf"))

    @staticmethod
    def revoke(c: Connection, token: str) -> None:
        row = c.execute(text("""UPDATE agentpal.owner_sessions SET revoked_at=clock_timestamp()
            WHERE token_hash=:hash AND revoked_at IS NULL RETURNING id"""), {"hash": token_hash(token,"session")}).one_or_none()
        if row:
            c.execute(text("""INSERT INTO agentpal.audit_events(actor_kind,owner_session_id,kind,details)
                VALUES ('owner',:id,'owner_session_revoked','{}')"""), {"id": row.id})
