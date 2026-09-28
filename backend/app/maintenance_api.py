"""Owner-only maintenance commands, with durable at-most-once IPC submission."""

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import select

from app.auth import AuthenticatedSession, get_auth_service, require_admin, require_csrf
from app.maintenance_auth import MaintenanceAuthorizationError, MaintenanceGrantService, MaintenanceStepUpThrottled, operation_hash
from app.maintenance_client import MaintenanceBusy, MaintenanceClient, MaintenanceConflict, MaintenanceUnavailable
from app.models import MaintenanceSubmitIntent

Component = Literal["mihomo", "wireguard", "uptime-kuma", "metacubexd", "dashboard", "antidpi"]
JobId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
ReleaseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", max_length=128)]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class MaintenanceOperation(ClosedModel):
    action: Literal["restart", "update", "rollback"]
    components: Annotated[list[Component], Field(min_length=1, max_length=6)]
    expected_revision: Digest
    release_ids: dict[Component, ReleaseId]
    enable_stopped: Annotated[list[Component], Field(max_length=6)]
    snapshot_id: Digest | None
    accept_data_loss: bool

    @model_validator(mode="after")
    def closed_selection(self):
        if len(set(self.components)) != len(self.components) or len(set(self.enable_stopped)) != len(self.enable_stopped):
            raise ValueError("duplicate component")
        if not set(self.enable_stopped) <= set(self.components):
            raise ValueError("invalid stopped selection")
        if set(self.release_ids) != (set(self.components) if self.action == "update" else set()):
            raise ValueError("invalid release selection")
        if (self.action == "rollback") != (self.snapshot_id is not None) or (self.accept_data_loss and self.action != "rollback"):
            raise ValueError("invalid rollback confirmation")
        return self


class CancelOperation(ClosedModel):
    action: Literal["cancel"]
    job_id: JobId


class AuthorizeRequest(ClosedModel):
    operation: Annotated[MaintenanceOperation | CancelOperation, Field(discriminator="action")]
    password: Annotated[SecretStr, Field(min_length=1, max_length=1024)]


class SubmitRequest(ClosedModel):
    job_id: JobId
    operation: MaintenanceOperation
    grant: Annotated[SecretStr, Field(min_length=1, max_length=256)]


class CancelBody(ClosedModel):
    grant: Annotated[SecretStr, Field(min_length=1, max_length=256)]


class EmptyBody(ClosedModel):
    pass


def _client(request: Request) -> MaintenanceClient:
    client = getattr(request.app.state.runtime, "maintenance_client", None)
    if client is None or not client.enabled:
        raise MaintenanceUnavailable()
    return client


def _grants(request: Request) -> MaintenanceGrantService:
    return MaintenanceGrantService(request.app.state.runtime.session_factory, get_auth_service(request))


def _owner_write(request: Request, principal: AuthenticatedSession = Depends(require_admin)) -> AuthenticatedSession:
    require_csrf(request, principal)
    if not get_auth_service(request).is_owner(principal):
        raise MaintenanceAuthorizationError()
    _client(request)
    return principal


def _intent_exists(request: Request, job_id: str) -> bool:
    with request.app.state.runtime.session_factory() as session:
        return session.scalar(select(MaintenanceSubmitIntent.job_id).where(MaintenanceSubmitIntent.job_id == job_id).limit(1)) is not None


def _unknown(job_id: str) -> dict:
    return {"job_id": job_id, "phase": "unknown", "cancel_allowed": False,
        "message": "Ответ исполнителя не подтверждён. Проверяется прежняя операция; повторный запуск не выполняется."}


async def _status(request: Request, job_id: str) -> dict:
    try:
        return await _client(request).get_job(job_id)
    except (MaintenanceUnavailable, MaintenanceConflict):
        if _intent_exists(request, job_id):
            return _unknown(job_id)
        raise


def build_maintenance_router() -> APIRouter:
    router = APIRouter(prefix="/api/maintenance")

    @router.get("/components")
    async def components(request: Request, principal: AuthenticatedSession = Depends(require_admin)):
        try:
            inventory = await _client(request).request("components")
        except MaintenanceUnavailable:
            return {"available": False, "can_maintain": False, "components": [], "revision": None}
        if not isinstance(inventory, dict):
            raise MaintenanceUnavailable()
        return {**inventory, "available": True, "can_maintain": get_auth_service(request).is_owner(principal)}

    @router.post("/check", status_code=202)
    async def check(payload: EmptyBody, request: Request, _: AuthenticatedSession = Depends(_owner_write)):
        return await _client(request).request("check_releases")

    @router.get("/jobs")
    async def jobs(request: Request, _: AuthenticatedSession = Depends(require_admin)):
        available = True
        try:
            records = await _client(request).request("jobs")
        except MaintenanceUnavailable:
            records, available = [], False
        with request.app.state.runtime.session_factory() as session:
            intents = session.scalars(select(MaintenanceSubmitIntent).where(MaintenanceSubmitIntent.operation_kind == "submit")
                .order_by(MaintenanceSubmitIntent.created_at.desc()).limit(100)).all()
        known = {row["job_id"] for row in records}
        return {"available": available, "jobs": [*records, *(_unknown(row.job_id) for row in intents if row.job_id not in known)]}

    @router.get("/jobs/{job_id}")
    async def job(request: Request, job_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)], _: AuthenticatedSession = Depends(require_admin)):
        return await _status(request, job_id)

    @router.post("/authorize")
    async def authorize(payload: AuthorizeRequest, request: Request, principal: AuthenticatedSession = Depends(_owner_write)):
        token = await asyncio.to_thread(_grants(request).issue, principal, operation_hash(payload.operation.model_dump()),
            payload.password.get_secret_value(), datetime.now(UTC))
        return JSONResponse({"grant": token, "expires_in": 300}, headers={"Cache-Control": "no-store"})

    @router.post("/jobs", status_code=202)
    async def submit(payload: SubmitRequest, request: Request, principal: AuthenticatedSession = Depends(_owner_write)):
        operation = payload.operation.model_dump()
        first = await asyncio.to_thread(_grants(request).consume, payload.grant.get_secret_value(), principal,
            operation_hash(operation), payload.job_id, datetime.now(UTC), operation=operation)
        if not first:
            return await _status(request, payload.job_id)
        try:
            return await _client(request).submit(payload.job_id, operation, principal.username)
        except MaintenanceUnavailable:
            return _unknown(payload.job_id)

    @router.post("/jobs/{job_id}/cancel", status_code=202)
    async def cancel(payload: CancelBody, request: Request, job_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)], principal: AuthenticatedSession = Depends(_owner_write)):
        operation = {"action": "cancel", "job_id": job_id}
        first = await asyncio.to_thread(_grants(request).consume, payload.grant.get_secret_value(), principal,
            operation_hash(operation), job_id, datetime.now(UTC), operation=operation)
        if not first:
            return await _status(request, job_id)
        try:
            return await _client(request).cancel(job_id, principal.username)
        except MaintenanceUnavailable:
            return _unknown(job_id)

    return router


def install_maintenance_handlers(app):
    @app.exception_handler(MaintenanceAuthorizationError)
    async def authorization_error(_request, error):
        code = 429 if isinstance(error, MaintenanceStepUpThrottled) else 403
        return JSONResponse({"detail": str(error)}, status_code=code, headers={"Cache-Control": "no-store"})

    @app.exception_handler(MaintenanceBusy)
    async def busy(_request, error):
        return JSONResponse({"detail": str(error), "job_id": error.job_id}, status_code=409)

    @app.exception_handler(MaintenanceConflict)
    async def conflict(_request, _error):
        return JSONResponse({"detail": "Состояние изменилось. Обновите данные перед новой операцией."}, status_code=409)

    @app.exception_handler(MaintenanceUnavailable)
    async def unavailable(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=503)
