"""Authenticated SQLite-only service check snapshots; no network side effects."""

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from app.auth import require_admin
from app.site_probes import SiteProbeService


def build_site_probe_router():
    router = APIRouter(prefix="/api/rules", dependencies=[Depends(require_admin)])

    @router.get("/service-checks")
    async def read_checks(request: Request):
        runtime = request.app.state.runtime
        service = runtime.site_probes or SiteProbeService(runtime.session_factory)
        try:
            return await asyncio.to_thread(service.snapshot, datetime.now(UTC))
        except (SQLAlchemyError, OSError):
            raise HTTPException(503, "Service probe snapshot unavailable") from None

    return router
