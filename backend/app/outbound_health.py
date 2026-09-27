"""Durable health based only on consecutive independent control observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from typing import Literal

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import OutboundControlCycle, OutboundHealthState, OutboundIncident
from app.outbounds import FALLBACK_MEMBERS, FallbackOutboundId, OutboundDefinition, OutboundId, build_outbound_registry


CycleResult = Literal["healthy", "degraded", "failed", "unknown"]
HealthState = Literal["healthy", "degraded", "pending", "down", "unknown"]
REASON_CODES = frozenset({
    "probe_failed", "probe_timeout", "controller_unavailable", "probe_group_invalid",
    "collector_error", "observation_gap", "stale", "no_data", "clock_jump",
})


@dataclass(frozen=True)
class ControlCycle:
    outbound: OutboundId
    cycle_id: str
    completed_at: datetime
    result: CycleResult
    successes: int
    selected_fallback: FallbackOutboundId | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HealthSnapshot:
    outbound: OutboundId
    state: HealthState = "unknown"
    observed_at: datetime | None = None
    pending_since: datetime | None = None
    incident_id: int | None = None
    incident_started_at: datetime | None = None
    recovery_streak: int = 0
    last_success_at: datetime | None = None
    last_cycle_id: str | None = None
    successes: int = 0
    selected_fallback: FallbackOutboundId | None = None
    reasons: tuple[str, ...] = ("no_data",)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("health timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _slot(value: str) -> datetime:
    try:
        parsed = _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        if parsed.second or parsed.microsecond:
            raise ValueError
        return parsed
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid control minute") from error


class HealthService:
    def __init__(self, sessions: sessionmaker[Session], registry: tuple[OutboundDefinition, ...] | None = None):
        self.sessions = sessions
        self.registry = build_outbound_registry() if registry is None else registry
        self._ids = {entry.id for entry in self.registry}

    def record_cycle(self, cycle: ControlCycle) -> HealthSnapshot:
        slot = _slot(cycle.cycle_id)
        completed = _utc(cycle.completed_at)
        cycle_id = slot.isoformat().replace("+00:00", "Z")
        if (
            cycle.outbound not in self._ids or cycle.result not in ("healthy", "degraded", "failed", "unknown")
            or type(cycle.successes) is not int or not 0 <= cycle.successes <= 3
            or (cycle.result == "healthy" and cycle.successes != 3)
            or (cycle.result == "degraded" and cycle.successes != 2)
            or (cycle.result == "failed" and cycle.successes >= 2)
            or cycle.selected_fallback not in (*FALLBACK_MEMBERS, None)
            or any(reason not in REASON_CODES for reason in cycle.reasons)
        ):
            raise ValueError("invalid control observation")

        # Serialize before reading the state: deduplication and transition commit
        # together, including two processes sharing the same WAL database.
        with self.sessions() as session, session.begin():
            session.execute(text("BEGIN IMMEDIATE"))
            state = session.get(OutboundHealthState, cycle.outbound)
            previous = session.scalar(select(OutboundControlCycle.id).where(
                OutboundControlCycle.outbound == cycle.outbound,
                OutboundControlCycle.cycle_id == cycle_id,
            ))
            if previous is not None or (state is not None and state.last_cycle_id == cycle_id):
                return self._snapshot(state, cycle.outbound)
            if state is None:
                state = OutboundHealthState(outbound=cycle.outbound, state="unknown", recovery_streak=0)
                session.add(state)

            result, reasons = cycle.result, cycle.reasons
            if not slot <= completed < slot + timedelta(minutes=1):
                result, reasons = "unknown", ("clock_jump",)
            elif state.last_cycle_id is not None and (
                slot - _slot(state.last_cycle_id) != timedelta(minutes=1)
                or state.observed_at is None or completed <= state.observed_at
            ):
                result, reasons = "unknown", ("observation_gap",)
            if result == "unknown":
                state.state = "unknown"
                state.pending_since = None
                state.recovery_streak = 0
            elif result == "failed":
                state.recovery_streak = 0
                if state.incident_id is None:
                    if state.pending_since is None:
                        state.pending_since = completed
                    if completed - state.pending_since >= timedelta(seconds=600):
                        incident = OutboundIncident(outbound=cycle.outbound, first_failed_at=state.pending_since, confirmed_at=completed)
                        session.add(incident)
                        session.flush()
                        state.incident_id = incident.id
                        state.incident_started_at = state.pending_since
                        state.pending_since = None
                state.state = "down" if state.incident_id is not None else "pending"
            else:
                state.last_success_at = completed
                state.pending_since = None
                if state.incident_id is not None:
                    state.recovery_streak += 1
                    if state.recovery_streak >= 2:
                        session.get(OutboundIncident, state.incident_id).recovered_at = completed
                        state.incident_id = None
                        state.incident_started_at = None
                        state.recovery_streak = 0
                else:
                    state.recovery_streak = 0
                state.state = "down" if state.incident_id is not None else result

            state.observed_at = completed
            state.last_cycle_id = cycle_id
            state.successes = cycle.successes
            state.selected_fallback = cycle.selected_fallback
            state.reasons_json = json.dumps(reasons)
            session.add(OutboundControlCycle(
                outbound=cycle.outbound, cycle_id=cycle_id, completed_at=completed,
                result=result, successes=cycle.successes, selected_fallback=cycle.selected_fallback,
                reasons_json=state.reasons_json,
            ))
            session.flush()
            return self._snapshot(state, cycle.outbound)

    def snapshot(self, now: datetime) -> tuple[HealthSnapshot, ...]:
        now = _utc(now)
        with self.sessions() as session:
            return tuple(self._snapshot(session.get(OutboundHealthState, entry.id), entry.id, now) for entry in self.registry)

    @staticmethod
    def _snapshot(row: OutboundHealthState | None, outbound: OutboundId, now: datetime | None = None) -> HealthSnapshot:
        if row is None:
            return HealthSnapshot(outbound)
        state, reasons = row.state, tuple(json.loads(row.reasons_json or "[]"))
        if now is not None and row.observed_at is not None:
            age = (now - row.observed_at).total_seconds()
            if age > 180 or age < 0:
                state, reasons = "unknown", ("stale" if age > 180 else "clock_jump",)
        return HealthSnapshot(
            outbound=outbound, state=state, observed_at=row.observed_at,
            pending_since=row.pending_since if state != "unknown" else None,
            incident_id=row.incident_id, incident_started_at=row.incident_started_at,
            recovery_streak=row.recovery_streak if state != "unknown" else 0,
            last_success_at=row.last_success_at, last_cycle_id=row.last_cycle_id,
            successes=row.successes, selected_fallback=row.selected_fallback, reasons=reasons,
        )

    def prune(self, now: datetime) -> None:
        now = _utc(now)
        with self.sessions() as session, session.begin():
            session.execute(delete(OutboundControlCycle).where(OutboundControlCycle.completed_at < now - timedelta(days=30)))
            session.execute(delete(OutboundIncident).where(OutboundIncident.recovered_at < now - timedelta(days=365)))
