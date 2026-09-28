"""Atomic, password-confirmed owner grants. No password reaches the worker."""

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, text

from app.auth import AuthService, AuthenticatedSession, _verify_password
from app.models import MaintenanceGrant, MaintenanceStepUpThrottle, MaintenanceSubmitIntent


class MaintenanceAuthorizationError(RuntimeError):
    def __init__(self):
        super().__init__("Требуется подтверждение владельца для этой операции.")


class MaintenanceStepUpThrottled(MaintenanceAuthorizationError):
    pass


def canonical_operation(operation: dict) -> str:
    return json.dumps(operation, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def operation_hash(operation: dict) -> str:
    return hashlib.sha256(canonical_operation(operation).encode("utf-8")).hexdigest()


class MaintenanceGrantService:
    def __init__(self, session_factory, auth: AuthService):
        self.sessions, self.auth = session_factory, auth

    def issue(self, principal: AuthenticatedSession, request_hash: str, password: str, now: datetime) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", request_hash) or not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise MaintenanceAuthorizationError()
        failure = False
        token = secrets.token_urlsafe(32)
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            owner = self.auth.owner_for_session(session, principal, now)
            if owner is None:
                raise MaintenanceAuthorizationError()
            throttle = session.get(MaintenanceStepUpThrottle, principal.session_id)
            if throttle is not None and throttle.blocked_until is not None and throttle.blocked_until > now:
                raise MaintenanceStepUpThrottled()
            if not _verify_password(owner.password_hash, password):
                if throttle is None:
                    throttle = MaintenanceStepUpThrottle(session_id=principal.session_id, failures=0, window_started_at=now)
                    session.add(throttle)
                elif throttle.window_started_at + timedelta(minutes=15) <= now:
                    throttle.failures, throttle.window_started_at, throttle.blocked_until = 0, now, None
                throttle.failures += 1
                if throttle.failures >= 5:
                    throttle.blocked_until = now + timedelta(minutes=15)
                failure = True
            else:
                if throttle is not None:
                    session.delete(throttle)
                session.execute(delete(MaintenanceGrant).where(MaintenanceGrant.expires_at <= now, MaintenanceGrant.consumed_job_id.is_(None)))
                session.add(MaintenanceGrant(
                    token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(), session_id=principal.session_id,
                    request_hash=request_hash, issued_at=now, expires_at=now + timedelta(seconds=300),
                ))
            session.commit()  # Failed attempts must survive the exception below.
        if failure:
            raise MaintenanceAuthorizationError()
        return token

    def consume(self, token: str, principal: AuthenticatedSession, request_hash: str, job_id: str, now: datetime, *, operation: dict | None = None) -> bool:
        """True permits the first IPC call; False permits only a status lookup."""
        if not isinstance(token, str) or not 1 <= len(token) <= 256 or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise MaintenanceAuthorizationError()
        if not re.fullmatch(r"[0-9a-f]{64}", request_hash) or (operation is not None and operation_hash(operation) != request_hash):
            raise MaintenanceAuthorizationError()
        encoded = canonical_operation(operation or {})
        kind = "cancel" if operation and operation.get("action") == "cancel" else "submit"
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if self.auth.owner_for_session(session, principal, now) is None:
                raise MaintenanceAuthorizationError()
            grant = session.get(MaintenanceGrant, hashlib.sha256(token.encode("utf-8")).hexdigest())
            if grant is None or grant.session_id != principal.session_id or not hmac.compare_digest(grant.request_hash, request_hash):
                raise MaintenanceAuthorizationError()
            if grant.consumed_job_id is not None:
                if grant.consumed_job_id != job_id:
                    raise MaintenanceAuthorizationError()
                return False
            if grant.expires_at <= now or grant.issued_at > now:
                raise MaintenanceAuthorizationError()
            intent = session.get(MaintenanceSubmitIntent, (job_id, kind))
            if intent is not None and (intent.request_hash != request_hash or intent.session_id != principal.session_id or intent.actor != principal.username):
                raise MaintenanceAuthorizationError()
            grant.consumed_job_id = job_id
            if intent is None:
                session.add(MaintenanceSubmitIntent(job_id=job_id, operation_kind=kind, request_hash=request_hash,
                    session_id=principal.session_id, actor=principal.username, request_json=encoded, created_at=now))
            session.commit()
            return intent is None
