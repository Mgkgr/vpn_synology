"""Local owner authentication with server-side opaque sessions and CSRF checks."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent, DashboardAdmin, DashboardOwner, DashboardSession, LoginThrottleRecord


SESSION_COOKIE = "vpn_dashboard_session"
DEFAULT_SESSION_TTL = timedelta(hours=8)
_PASSWORD_HASHER = PasswordHasher(type=Type.ID)


class BootstrapUnavailable(RuntimeError):
    """Raised when an owner already exists and bootstrap must stay closed."""


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    owner_id: int
    username: str
    session_id: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedSession:
    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DashboardAdministrator:
    username: str
    bootstrap_owner: bool


class LoginThrottle:
    """Persisted, IP-scoped login limiter that survives application restarts."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        max_failures: int = 5,
        window: timedelta = timedelta(minutes=15),
        block_for: timedelta = timedelta(minutes=15),
    ) -> None:
        self._session_factory = session_factory
        self._max_failures = max_failures
        self._window = window
        self._block_for = block_for

    def is_blocked(self, ip_address: str, now: datetime) -> bool:
        with self._session_factory() as session:
            record = session.get(LoginThrottleRecord, ip_address)
            return record is not None and record.blocked_until is not None and record.blocked_until > now

    def record_failure(self, ip_address: str, now: datetime) -> None:
        with self._session_factory.begin() as session:
            record = session.get(LoginThrottleRecord, ip_address)
            if record is None or record.window_started_at + self._window <= now:
                record = LoginThrottleRecord(ip_address=ip_address, failures=0, window_started_at=now, blocked_until=None)
                session.add(record)
            record.failures += 1
            if record.failures >= self._max_failures:
                record.blocked_until = now + self._block_for

    def record_success(self, ip_address: str) -> None:
        with self._session_factory.begin() as session:
            record = session.get(LoginThrottleRecord, ip_address)
            if record is not None:
                session.delete(record)


class AuthService:
    """Owns authentication state without retaining plaintext passwords or tokens."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        session_ttl: timedelta = DEFAULT_SESSION_TTL,
        cookie_secure: bool = True,
        csrf_encryption_key: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session_ttl = session_ttl
        self.cookie_secure = cookie_secure
        key = csrf_encryption_key.encode("ascii") if csrf_encryption_key is not None else Fernet.generate_key()
        self._csrf_cipher = Fernet(key)

    def bootstrap(self, username: str, password: str) -> IssuedSession:
        normalized_username = _username(username)
        _password(password)
        now = _now()
        try:
            with self._session_factory.begin() as session:
                if session.scalar(select(DashboardOwner.id).limit(1)) is not None:
                    raise BootstrapUnavailable()
                owner = DashboardOwner(
                    singleton_marker=1,
                    username=normalized_username,
                    password_hash=_PASSWORD_HASHER.hash(password),
                    created_at=now,
                )
                session.add(owner)
                session.flush()
                issued = self._create_session(session, owner, now, actor_username=owner.username)
                _audit(session, actor=owner.username, action="owner_bootstrap", succeeded=True)
                return issued
        except IntegrityError as error:
            raise BootstrapUnavailable() from error

    def login(self, username: str, password: str) -> IssuedSession | None:
        normalized_username = _username(username)
        if not isinstance(password, str) or not password:
            return None
        now = _now()
        with self._session_factory.begin() as session:
            owner = session.scalar(select(DashboardOwner).where(DashboardOwner.username == normalized_username))
            administrator = None if owner is not None else session.scalar(
                select(DashboardAdmin).where(DashboardAdmin.username == normalized_username)
            )
            record = owner or administrator
            if record is None or not _verify_password(record.password_hash, password):
                _audit(session, actor="anonymous", action="login_failed", succeeded=False)
                return None
            session_owner = owner or session.scalar(select(DashboardOwner).limit(1))
            if session_owner is None:
                _audit(session, actor="anonymous", action="login_failed", succeeded=False)
                return None
            issued = self._create_session(session, session_owner, now, actor_username=record.username)
            _audit(session, actor=record.username, action="login", succeeded=True)
            return issued

    def create_administrator(self, actor: str, username: str, password: str) -> DashboardAdministrator:
        """Create an additional local administrator without relaxing bootstrap ownership."""

        normalized_actor = _username(actor)
        normalized_username = _username(username)
        _password(password)
        now = _now()
        try:
            with self._session_factory.begin() as session:
                duplicate_owner = session.scalar(select(DashboardOwner.id).where(DashboardOwner.username == normalized_username))
                duplicate_admin = session.scalar(select(DashboardAdmin.id).where(DashboardAdmin.username == normalized_username))
                if duplicate_owner is not None or duplicate_admin is not None:
                    raise ValueError("username is already in use")
                session.add(
                    DashboardAdmin(
                        username=normalized_username,
                        password_hash=_PASSWORD_HASHER.hash(password),
                        created_at=now,
                    )
                )
                _audit(session, actor=normalized_actor, action="dashboard_admin_create", succeeded=True)
        except IntegrityError as error:
            raise ValueError("username is already in use") from error
        return DashboardAdministrator(username=normalized_username, bootstrap_owner=False)

    def list_administrators(self) -> list[DashboardAdministrator]:
        with self._session_factory() as session:
            owner = session.scalar(select(DashboardOwner).limit(1))
            additional = session.scalars(select(DashboardAdmin).order_by(DashboardAdmin.username)).all()
        result: list[DashboardAdministrator] = []
        if owner is not None:
            result.append(DashboardAdministrator(username=owner.username, bootstrap_owner=True))
        result.extend(DashboardAdministrator(username=item.username, bootstrap_owner=False) for item in additional)
        return result

    def authenticate(self, raw_token: str | None) -> AuthenticatedSession | None:
        if not isinstance(raw_token, str) or not raw_token:
            return None
        now = _now()
        token_hash = _token_hash(raw_token)
        with self._session_factory.begin() as session:
            record = session.scalar(select(DashboardSession).where(DashboardSession.token_hash == token_hash))
            if record is None:
                return None
            if record.expires_at <= now:
                session.delete(record)
                return None
            owner = session.get(DashboardOwner, record.owner_id)
            if owner is None:
                session.delete(record)
                return None
            return AuthenticatedSession(owner.id, record.actor_username or owner.username, record.id, record.expires_at)

    def current_csrf(self, principal: AuthenticatedSession) -> str:
        """Return the session-bound CSRF token without changing persisted state."""

        with self._session_factory() as session:
            record = session.get(DashboardSession, principal.session_id)
            if record is None or record.expires_at <= _now():
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
            try:
                token = self._csrf_cipher.decrypt(record.csrf_token_ciphertext.encode("ascii")).decode("ascii")
            except (InvalidToken, UnicodeError):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required") from None
            if not hmac.compare_digest(record.csrf_token_hash, _token_hash(token)):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
            return token

    def verify_csrf(self, principal: AuthenticatedSession, submitted: str | None) -> bool:
        if not isinstance(submitted, str) or not submitted:
            return False
        with self._session_factory() as session:
            record = session.get(DashboardSession, principal.session_id)
            if record is None or record.expires_at <= _now():
                return False
            return hmac.compare_digest(record.csrf_token_hash, _token_hash(submitted))

    def logout(self, principal: AuthenticatedSession) -> None:
        with self._session_factory.begin() as session:
            record = session.get(DashboardSession, principal.session_id)
            if record is not None:
                session.delete(record)
            _audit(session, actor=principal.username, action="logout", succeeded=True)

    def revoke_actor_sessions(self, actor: str, username: str) -> int:
        """Invalidate every browser session created by one dashboard account."""

        normalized_actor = _username(actor)
        normalized_username = _username(username)
        with self._session_factory.begin() as session:
            result = session.execute(delete(DashboardSession).where(DashboardSession.actor_username == normalized_username))
            _audit(session, actor=normalized_actor, action="sessions_revoke", succeeded=True)
            return result.rowcount or 0

    def _create_session(
        self,
        session: Session,
        owner: DashboardOwner,
        now: datetime,
        *,
        actor_username: str,
    ) -> IssuedSession:
        token = _new_token()
        csrf_token = _new_token()
        expires_at = now + self._session_ttl
        session.add(
            DashboardSession(
                owner_id=owner.id,
                token_hash=_token_hash(token),
                csrf_token_hash=_token_hash(csrf_token),
                csrf_token_ciphertext=self._csrf_cipher.encrypt(csrf_token.encode("ascii")).decode("ascii"),
                actor_username=actor_username,
                expires_at=expires_at,
                created_at=now,
            )
        )
        return IssuedSession(token, csrf_token, expires_at)


def get_auth_service(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth_service", None)
    if not isinstance(service, AuthService):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "dashboard is unavailable")
    return service


def require_admin(request: Request) -> AuthenticatedSession:
    principal = get_auth_service(request).authenticate(request.cookies.get(SESSION_COOKIE))
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return principal


def require_csrf(request: Request, principal: AuthenticatedSession) -> AuthenticatedSession:
    if not get_auth_service(request).verify_csrf(principal, request.headers.get("X-CSRF-Token")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "csrf verification failed")
    return principal


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _username(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("username is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise ValueError("username is invalid")
    return normalized


def _password(value: str) -> None:
    if not isinstance(value, str) or len(value) < 12 or len(value) > 1024:
        raise ValueError("password is invalid")


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def _audit(session: Session, *, actor: str, action: str, succeeded: bool) -> None:
    session.add(
        AuditEvent(
            observed_at=_now(),
            actor=actor,
            action=action,
            succeeded=succeeded,
        )
    )
