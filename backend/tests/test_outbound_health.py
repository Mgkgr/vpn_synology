from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db import create_all, create_session_factory, create_sqlite_engine


START = datetime(2026, 9, 27, 8, tzinfo=UTC)


@pytest.fixture
def health(tmp_path):
    from app.outbound_health import HealthService

    engine = create_sqlite_engine(tmp_path / "health.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    yield HealthService(sessions), sessions
    engine.dispose()


def cycle(minute, result="failed", *, seconds=20, outbound="WG-IMP"):
    from app.outbound_health import ControlCycle

    slot = START + timedelta(minutes=minute)
    return ControlCycle(
        outbound, slot.isoformat(), slot + timedelta(seconds=seconds), result,
        {"healthy": 3, "degraded": 2, "failed": 0, "unknown": 0}[result],
        "HY2-USA", ("probe_failed",) if result == "failed" else (),
    )


@pytest.mark.parametrize("seconds,confirmed", [(19, False), (20, True)])
def test_incident_599_600_and_two_recoveries(health, seconds, confirmed):
    from app.models import OutboundIncident

    service, sessions = health
    for minute in range(10):
        assert service.record_cycle(cycle(minute)).state == "pending"
    snapshot = service.record_cycle(cycle(10, seconds=seconds))
    assert (snapshot.incident_id is not None) == confirmed
    if not confirmed:
        return
    assert snapshot.state == "down"
    assert snapshot.incident_started_at == START + timedelta(seconds=20)
    first = service.record_cycle(cycle(11, "degraded"))
    assert first.incident_id == snapshot.incident_id
    assert first.recovery_streak == 1
    assert first.state == "down"
    second = service.record_cycle(cycle(12, "healthy"))
    assert second.incident_id is None
    assert second.state == "healthy"
    with sessions() as session:
        incident = session.get(OutboundIncident, snapshot.incident_id)
        assert incident.recovered_at == cycle(12).completed_at
        assert session.scalar(select(func.count()).select_from(OutboundIncident)) == 1


def test_degraded_duplicate_and_restart_are_persistent(health):
    from app.models import OutboundControlCycle, OutboundIncident
    from app.outbound_health import HealthService

    service, sessions = health
    result = service.record_cycle(cycle(0, "degraded"))
    assert result.state == "degraded"
    assert service.record_cycle(cycle(0, "failed")) == result
    for minute in range(1, 12):
        service.record_cycle(cycle(minute))
    restored = HealthService(sessions)
    result = restored.record_cycle(cycle(11))
    assert result.state == "down"
    assert restored.snapshot(cycle(11).completed_at)[0] == result
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboundIncident)) == 1
        assert session.scalar(select(func.count()).select_from(OutboundControlCycle)) == 12


@pytest.mark.parametrize("gap", [-1, 2, 120])
def test_gap_and_clock_jump_are_unknown(health, gap):
    service, _ = health
    service.record_cycle(cycle(0))
    interrupted = service.record_cycle(cycle(gap))
    assert interrupted.state == "unknown"
    assert interrupted.pending_since is None
    assert interrupted.recovery_streak == 0
    assert "observation_gap" in interrupted.reasons
    resumed = service.record_cycle(cycle(gap + 1))
    # For clock reversal the repeated minute is a replay; it cannot accumulate failure time.
    assert resumed.incident_id is None


def test_unknown_and_stale_keep_open_incident_and_require_consecutive_recovery(health):
    service, _ = health
    for minute in range(11):
        opened = service.record_cycle(cycle(minute))
    service.record_cycle(cycle(11, "healthy"))
    unknown = service.record_cycle(cycle(12, "unknown"))
    assert unknown.state == "unknown"
    assert unknown.incident_id == opened.incident_id
    assert unknown.recovery_streak == 0
    assert service.record_cycle(cycle(13, "healthy")).incident_id == opened.incident_id
    stale = service.snapshot(cycle(13).completed_at + timedelta(seconds=181))[0]
    assert stale.state == "unknown"
    assert stale.incident_id == opened.incident_id
    assert stale.recovery_streak == 0
    assert "stale" in stale.reasons
    backwards = service.snapshot(START)[0]
    assert backwards.state == "unknown"
    assert service.record_cycle(cycle(17, "healthy")).state == "unknown"
    assert service.record_cycle(cycle(18, "healthy")).incident_id == opened.incident_id
    assert service.record_cycle(cycle(19, "healthy")).incident_id is None


def test_no_data_invalid_cycle_or_secret_reason_cannot_be_healthy(health):
    from dataclasses import replace

    service, _ = health
    assert [item.state for item in service.snapshot(START)] == ["unknown", "unknown"]
    with pytest.raises(ValueError):
        service.record_cycle(replace(cycle(0), outbound="DIRECT"))
    with pytest.raises(ValueError):
        service.record_cycle(replace(cycle(0), result="healthy", successes=1))
    with pytest.raises(ValueError) as error:
        service.record_cycle(replace(cycle(0), reasons=("secret=do-not-log",)))
    assert "do-not-log" not in str(error.value)
    with pytest.raises(ValueError):
        service.record_cycle(replace(cycle(0), completed_at=START.replace(tzinfo=None)))


def test_retention_keeps_open_incident_and_current_state(health):
    from app.models import OutboundControlCycle, OutboundIncident

    service, sessions = health
    for minute in range(11):
        snapshot = service.record_cycle(cycle(minute))
    service.prune(START + timedelta(days=400))
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboundControlCycle)) == 0
        assert session.get(OutboundIncident, snapshot.incident_id).recovered_at is None
    assert service.snapshot(START + timedelta(days=400))[0].incident_id == snapshot.incident_id
