"""Authenticated health reads from SQLite only; never wait for network probes."""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.auth import require_admin
from app.models import NotificationDeliveryState
from app.outbound_health import HealthService
from app.schemas import OutboundHealthStatusResponse


def _read(runtime, now):
    service = runtime.outbound_health or HealthService(runtime.session_factory)
    snapshots = service.snapshot(now)
    entries = {entry.id: entry for entry in service.registry}
    enabled = runtime.outbound_health_enabled
    outbounds = []
    for snapshot in snapshots:
        values = asdict(snapshot)
        values.pop("last_cycle_id")
        values["id"] = values.pop("outbound")
        values.update(label=entries[snapshot.outbound].label, engine=entries[snapshot.outbound].engine, total=3)
        if not enabled:
            values.update(state="unknown", pending_since=None, reasons=["monitoring_disabled"])
        outbounds.append(values)
    timestamps = [item.observed_at for item in snapshots if item.observed_at is not None]
    collector_state = "disabled" if not enabled else "healthy" if all(item.state != "unknown" for item in snapshots) else "unknown"
    publisher = runtime.kuma_publisher
    delivery = {"state": "disabled", "monitors": [], "error_code": None}
    if publisher is not None and publisher.configuration_error:
        delivery.update(state="error", error_code=publisher.configuration_error)
    elif publisher is not None and publisher.enabled and enabled:
        with runtime.session_factory() as session:
            rows = {row.monitor_key: row for row in session.scalars(select(NotificationDeliveryState))}
        for key in (*entries, "collector"):
            row = rows.get(key)
            state = "pending" if row is None else "error" if row.last_error_code else "accepted" if row.last_accepted_at and row.accepted_revision == row.pending_revision else "pending"
            delivery["monitors"].append({
                "key": key, "state": state, "last_attempt_at": row.last_attempt_at if row else None,
                "last_accepted_at": row.last_accepted_at if row else None, "error_code": row.last_error_code if row else None,
            })
        states = {item["state"] for item in delivery["monitors"]}
        delivery["state"] = "error" if "error" in states else "pending" if "pending" in states else "accepted"
    return {"enabled": enabled, "observed_at": min(timestamps) if timestamps else None, "collector_state": collector_state, "outbounds": outbounds, "delivery": delivery}


def build_health_router() -> APIRouter:
    router = APIRouter(prefix="/api/health", dependencies=[Depends(require_admin)])

    @router.get("/outbounds", response_model=OutboundHealthStatusResponse)
    async def read_health(request: Request):
        return await asyncio.to_thread(_read, request.app.state.runtime, datetime.now(UTC))

    return router
