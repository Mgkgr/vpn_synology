import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.models import OutboundControlCycle, SiteProbeResult, SiteProbeRun
from app.site_probe_catalog import SiteProbeObservation
from app.site_probes import SiteProbeCollector, SiteProbeService, register_site_probe_jobs


NOW = datetime(2026, 10, 3, 23, 30, tzinfo=UTC)  # 04:30 in Yekaterinburg


@pytest.fixture
def service(tmp_path):
    engine = create_sqlite_engine(tmp_path / "site.sqlite3")
    create_all(engine)
    value = SiteProbeService(create_session_factory(engine), enabled=True, targets=("github",))
    yield value
    engine.dispose()


class Controller:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.active = self.max_active = 0

    async def site_probe(self, route, target):
        self.calls.append((route, target))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        if self.error:
            raise self.error
        return SiteProbeObservation("responded", 30)


def test_schedule_is_local_once_per_day_and_does_not_replay_history(service):
    assert service.claim_day(NOW - timedelta(seconds=1)) is None
    assert service.claim_day(NOW) == "2026-10-04"
    assert service.claim_day(NOW + timedelta(minutes=10)) is None
    restarted = SiteProbeService(service.sessions, enabled=True, targets=("github",))
    assert restarted.claim_day(NOW + timedelta(hours=1)) is None
    assert restarted.claim_day(NOW - timedelta(days=1)) is None
    assert restarted.claim_day(NOW + timedelta(days=5)) == "2026-10-09"
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SiteProbeRun)) == 2


def test_two_process_like_services_can_claim_only_once(service):
    other = SiteProbeService(service.sessions, enabled=True, targets=("github",))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: value.claim_day(NOW), (service, other)))
    assert results.count("2026-10-04") == 1
    assert results.count(None) == 1


def test_collector_is_sequential_and_saved_get_does_not_probe(service):
    controller = Controller()
    collector = SiteProbeCollector(controller, service, now=lambda: NOW)
    assert asyncio.run(collector.run_due()) is True
    assert asyncio.run(collector.run_due()) is False
    snapshot = service.snapshot(NOW)
    assert controller.max_active == 1 and len(controller.calls) == 3
    assert snapshot["run"]["state"] == "completed"
    assert snapshot["run"]["completed_count"] == 3
    assert snapshot["timezone"] == "Asia/Yekaterinburg"
    assert [row["route_id"] for row in snapshot["services"][0]["routes"]] == ["direct", "primary", "reserve"]
    assert snapshot["services"][0]["routes"][1]["label"] == "Основной · VLESS-NL"
    assert all(row["delay_ms"] == 30 and not row["stale"] for row in snapshot["services"][0]["routes"])
    assert snapshot["next_run_at"] == NOW + timedelta(days=1)
    assert len(controller.calls) == 3


def test_unknown_is_not_outage_and_internal_errors_do_not_escape(service):
    controller = Controller(error=RuntimeError("private upstream password"))
    asyncio.run(SiteProbeCollector(controller, service, now=lambda: NOW).run_due())
    snapshot = service.snapshot(NOW)
    assert all(row["state"] == "unknown" and row["reason"] == "collector_error"
               for row in snapshot["services"][0]["routes"])
    assert "password" not in str(snapshot)
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OutboundControlCycle)) == 0


def test_cancellation_keeps_interrupted_run_and_prevents_same_day_retry(service):
    controller = Controller(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(SiteProbeCollector(controller, service, now=lambda: NOW).run_due())
    snapshot = service.snapshot(NOW)
    assert snapshot["run"]["state"] == "interrupted"
    assert snapshot["run"]["completed_count"] == 0
    assert service.claim_day(NOW) is None


def test_crashed_run_expires_without_claiming_twice(service):
    service.claim_day(NOW)
    assert service.claim_day(NOW + timedelta(minutes=26)) is None
    assert service.snapshot(NOW + timedelta(minutes=26))["run"]["state"] == "interrupted"


def test_partial_run_previous_result_age_and_snapshot_labels(service):
    first = service.claim_day(NOW)
    service.record(first, "github", "primary", SiteProbeObservation("responded", 34), NOW)
    service.finish(first, "interrupted", NOW)
    second = service.claim_day(NOW + timedelta(days=1))
    assert second
    snapshot = service.snapshot(NOW + timedelta(hours=26))
    row = snapshot["services"][0]["routes"][1]
    assert row["stale"] and not row["current_run"]
    assert row["observed_at"] == NOW and row["label"] == "Основной · VLESS-NL"
    assert snapshot["services"][0]["routes"][0]["observed_at"] is None


def test_retention_preserves_schedule_watermark_for_backward_clock(service):
    day = service.claim_day(NOW)
    service.record(day, "github", "direct", SiteProbeObservation("responded", 12), NOW)
    service.finish(day, "interrupted", NOW)
    service.prune(NOW + timedelta(days=31))
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SiteProbeResult)) == 0
        assert session.scalar(select(func.count()).select_from(SiteProbeRun)) == 0
    assert service.claim_day(NOW) is None


def test_incomplete_run_cannot_be_finished_as_completed(service):
    day = service.claim_day(NOW)
    service.record(day, "github", "direct", SiteProbeObservation("failed", reason="probe_timeout"), NOW)
    service.finish(day, "completed", NOW)
    assert service.snapshot(NOW)["run"]["state"] == "interrupted"


def test_invalid_observation_cannot_persist_arbitrary_error_or_latency(service):
    day = service.claim_day(NOW)
    for observation in [SiteProbeObservation("unknown", reason="secret text"),
                        SiteProbeObservation("responded", -1), SiteProbeObservation("failed", 11, "probe_failed")]:
        with pytest.raises(ValueError):
            service.record(day, "github", "direct", observation, NOW)


def test_disabled_collector_and_scheduler_do_not_send_probes(service):
    disabled = SiteProbeService(service.sessions, enabled=False, targets=("github",))
    controller = Controller()
    assert asyncio.run(SiteProbeCollector(controller, disabled, now=lambda: NOW).run_due()) is False
    assert not controller.calls
    assert disabled.snapshot(NOW)["next_run_at"] is None
    class Scheduler:
        def add_job(self, *args, **kwargs):
            raise AssertionError("disabled collector should not be scheduled")
    register_site_probe_jobs(Scheduler(), None)


def test_batch_deadline_keeps_unperformed_checks_unknown(service, monkeypatch):
    monkeypatch.setattr("app.site_probes.BATCH_SECONDS", 0.02)
    class Slow:
        async def site_probe(self, route, key):
            await asyncio.sleep(1)
            raise AssertionError("batch deadline did not cancel the request")
    asyncio.run(SiteProbeCollector(Slow(), service, now=lambda: NOW).run_due())
    snapshot = service.snapshot(NOW)
    assert snapshot["run"]["state"] == "interrupted"
    assert snapshot["run"]["completed_count"] == 0
    assert all(item["reason"] == "not_checked" for item in snapshot["services"][0]["routes"])


def test_duplicate_result_cannot_replace_original_observation(service):
    day = service.claim_day(NOW)
    assert service.record(day, "github", "direct", SiteProbeObservation("responded", 12), NOW)
    assert not service.record(day, "github", "direct", SiteProbeObservation("failed", reason="probe_timeout"), NOW)
    assert service.snapshot(NOW)["services"][0]["routes"][0]["delay_ms"] == 12


def test_enabled_scheduler_runs_one_periodic_due_check_with_startup_catchup(service):
    jobs = []
    class Scheduler:
        def add_job(self, *args, **kwargs):
            jobs.append((args, kwargs))
    collector = SiteProbeCollector(Controller(), service)
    register_site_probe_jobs(Scheduler(), collector)
    assert len(jobs) == 1
    args, options = jobs[0]
    assert args == (collector.run_due, "interval")
    assert options["minutes"] == 1 and options["max_instances"] == 1 and options["coalesce"] is True
    assert options["next_run_time"].tzinfo is not None
