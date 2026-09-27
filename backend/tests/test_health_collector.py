import asyncio
from datetime import UTC, datetime
import json

import pytest
from sqlalchemy import select

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.mihomo import MihomoIntegrationError, ProxyDelay, ProxyGroup
from app.models import OutboundControlCycle
from app.outbound_health import HealthService
from app.outbounds import build_outbound_registry


NOW = datetime(2026, 9, 27, 8, 0, 20, tzinfo=UTC)


class Controller:
    def __init__(self):
        self.calls = []
        self.active = self.peak = self.group_calls = 0
        self.unavailable = False
        self.invalid = False
        self.failure = None
        self.wait = None

    async def groups(self):
        self.group_calls += 1
        if self.unavailable:
            raise MihomoIntegrationError("groups", "controller request failed")
        entries = build_outbound_registry()
        return [ProxyGroup("VPS-FALLBACK", "Fallback", ("WG-IMP", "HY2-USA"), "HY2-USA")] + [
            ProxyGroup(entry.probe_name, "Selector", ("DIRECT",) if self.invalid else (entry.id,), entry.id)
            for entry in entries
        ]

    async def control_delay(self, name, key):
        self.calls.append((name, key))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.wait:
                await self.wait.wait()
            await asyncio.sleep(0)
            if self.failure:
                raise self.failure
            return ProxyDelay(42)
        finally:
            self.active -= 1


@pytest.fixture
def setup(tmp_path):
    from app.health_collector import HealthCollector

    engine = create_sqlite_engine(tmp_path / "health.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    health = HealthService(sessions)
    controller = Controller()
    collector = HealthCollector(controller, health, now=lambda: NOW)
    yield collector, controller, health, sessions
    engine.dispose()


def test_control_cycle_is_bounded_and_independent(setup):
    collector, controller, health, sessions = setup
    assert asyncio.run(collector.run_cycle()) is True
    assert controller.peak == 3
    assert set(controller.calls) == {(entry.probe_name, key) for entry in health.registry for key in ("cloudflare", "google", "github")}
    assert [item.state for item in health.snapshot(NOW)] == ["healthy", "healthy"]
    with sessions() as session:
        cycles = list(session.scalars(select(OutboundControlCycle)))
        assert len(cycles) == 2
        assert all(len(json.loads(row.checks_json)) == 3 for row in cycles)
        assert all(row.selected_fallback == "HY2-USA" for row in cycles)


@pytest.mark.parametrize("unavailable,invalid", [(True, False), (False, True)])
def test_controller_failure_is_unknown(setup, unavailable, invalid):
    collector, controller, health, _ = setup
    controller.unavailable, controller.invalid = unavailable, invalid
    asyncio.run(collector.run_cycle())
    assert controller.calls == []
    assert [item.state for item in health.snapshot(NOW)] == ["unknown", "unknown"]


@pytest.mark.parametrize("code,expected", [(503, "pending"), (504, "pending"), (401, "unknown"), (None, "unknown")])
def test_errors_recheck_controller_and_do_not_invent_failures(setup, code, expected):
    collector, controller, health, _ = setup
    controller.failure = MihomoIntegrationError("control_delay", "controller request failed", code)
    asyncio.run(collector.run_cycle())
    assert controller.group_calls == 2
    assert [item.state for item in health.snapshot(NOW)] == [expected, expected]


def test_duplicate_jobs_do_not_overlap_and_cancellation_does_not_record_failure(setup):
    collector, controller, health, sessions = setup

    async def scenario():
        controller.wait = asyncio.Event()
        running = asyncio.create_task(collector.run_cycle())
        for _ in range(10):
            await asyncio.sleep(0)
        assert controller.calls
        assert await collector.run_cycle() is False
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        with sessions() as session:
            assert list(session.scalars(select(OutboundControlCycle))) == []
        controller.wait.set()
        assert await collector.run_cycle() is True
    asyncio.run(scenario())


def test_health_jobs_are_separate_coalesced_and_opt_in():
    from app.health_collector import register_health_jobs

    class Scheduler:
        def __init__(self):
            self.jobs = []
        def add_job(self, func, trigger, **kwargs):
            self.jobs.append((func, trigger, kwargs))
    scheduler = Scheduler()
    register_health_jobs(scheduler, None)
    assert scheduler.jobs == []
    collector = type("Collector", (), {"run_cycle": lambda: None, "prune": lambda: None})()
    register_health_jobs(scheduler, collector)
    _, trigger, options = scheduler.jobs[0]
    assert trigger == "cron" and options["second"] == 0
    assert options["max_instances"] == 1 and options["coalesce"] is True
    assert options["misfire_grace_time"] <= 5
