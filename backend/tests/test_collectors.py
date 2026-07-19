import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient

from app.collectors import (
    Collector,
    PeerSnapshot,
    SnapshotOrderingError,
    YearMonth,
    monthly_usage,
    record_peer_snapshot,
    register_collector_jobs,
    run_daily_retention,
)
from app.main import CollectorRuntime, create_app
from app.db import create_all, create_session_factory, create_sqlite_engine
from app.mihomo import ControllerWriteResult, ProxyDelay, ProxyGroup
from app.models import (
    AuditEvent,
    GeoFileMetadata,
    GeoUpdate,
    GatewayTrafficSample,
    PeerBaseline,
    PeerSnapshotRecord,
    ProbeEvent,
    RouteEvent,
    TrafficHourly,
    TrafficMonthly,
)
from app.mihomo import Traffic
from app.services import ServiceStatus
from app.wgeasy import ContractStatus, WireGuardClient


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def peer(name: str, *, rx: int, tx: int) -> PeerSnapshot:
    return PeerSnapshot(peer_name=name, received_bytes=rx, transmitted_bytes=tx)


@pytest.fixture
def session(tmp_path):
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    session = create_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_counter_reset_never_creates_negative_usage(session) -> None:
    record_peer_snapshot(session, peer("desktop", rx=100, tx=40), at("2026-07-01T00:00:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=20, tx=10), at("2026-07-01T01:00:00Z"))
    usage = monthly_usage(session, "2026-07")

    assert usage[0].received_bytes == 20
    assert usage[0].transmitted_bytes == 10


def test_snapshot_deltas_are_aggregated_in_one_hourly_bucket(session) -> None:
    record_peer_snapshot(session, peer("desktop", rx=100, tx=10), at("2026-07-01T00:00:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=150, tx=30), at("2026-07-01T00:20:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=160, tx=50), at("2026-07-01T00:40:00Z"))

    hourly = session.scalars(select(TrafficHourly)).one()
    usage = monthly_usage(session, YearMonth(2026, 7))

    assert (hourly.received_bytes, hourly.transmitted_bytes) == (60, 40)
    assert (usage[0].received_bytes, usage[0].transmitted_bytes) == (60, 40)


def test_monthly_usage_is_separated_by_peer_and_period(session) -> None:
    record_peer_snapshot(session, peer("desktop", rx=10, tx=1), at("2026-07-31T23:50:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=30, tx=5), at("2026-08-01T00:10:00Z"))
    record_peer_snapshot(session, peer("phone", rx=5, tx=2), at("2027-07-01T00:00:00Z"))
    record_peer_snapshot(session, peer("phone", rx=9, tx=8), at("2027-07-01T01:00:00Z"))

    july_2026 = monthly_usage(session, "2026-07")
    august_2026 = monthly_usage(session, "2026-08")
    july_2027 = monthly_usage(session, "2027-07")

    assert july_2026 == []
    assert [(bucket.peer_name, bucket.received_bytes, bucket.transmitted_bytes) for bucket in august_2026] == [
        ("desktop", 20, 4)
    ]
    assert [(bucket.peer_name, bucket.received_bytes, bucket.transmitted_bytes) for bucket in july_2027] == [
        ("phone", 4, 6)
    ]


def test_retention_removes_only_snapshots_older_than_ninety_days(session) -> None:
    record_peer_snapshot(session, peer("desktop", rx=100, tx=10), at("2026-04-01T00:00:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=150, tx=30), at("2026-04-02T00:00:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=170, tx=40), at("2026-07-01T00:00:00Z"))

    removed = run_daily_retention(session, at("2026-07-02T00:00:00Z"))
    snapshots = session.scalars(select(PeerSnapshotRecord).order_by(PeerSnapshotRecord.observed_at)).all()

    assert removed == 2
    assert [snapshot.observed_at for snapshot in snapshots] == [at("2026-07-01T00:00:00Z")]
    assert [(bucket.received_bytes, bucket.transmitted_bytes) for bucket in monthly_usage(session, "2026-04")] == [
        (50, 20)
    ]


def test_retention_removes_hourly_data_older_than_twelve_months_but_keeps_monthly_data(session) -> None:
    record_peer_snapshot(session, peer("desktop", rx=100, tx=10), at("2025-07-12T00:00:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=110, tx=20), at("2025-07-12T01:00:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=120, tx=30), at("2025-07-13T00:00:00Z"))

    run_daily_retention(session, at("2026-07-13T00:00:00Z"))

    hourly = session.scalars(select(TrafficHourly).order_by(TrafficHourly.hour_start)).all()
    monthly = session.scalars(select(TrafficMonthly).order_by(TrafficMonthly.period)).all()

    assert [bucket.hour_start for bucket in hourly] == [at("2025-07-13T00:00:00Z")]
    assert [(bucket.period, bucket.received_bytes, bucket.transmitted_bytes) for bucket in monthly] == [
        ("2025-07", 20, 20)
    ]


def test_retention_keeps_only_a_bounded_week_of_gateway_throughput_samples(session) -> None:
    now = at("2026-07-13T12:00:00Z")
    session.add_all(
        [
            GatewayTrafficSample(observed_at=now - timedelta(days=7, seconds=1), up_bps=10, down_bps=20),
            GatewayTrafficSample(observed_at=now - timedelta(days=6), up_bps=30, down_bps=40),
        ]
    )
    session.commit()

    removed = run_daily_retention(session, now)

    remaining = session.scalars(select(GatewayTrafficSample)).all()
    assert removed == 1
    assert [(row.up_bps, row.down_bps) for row in remaining] == [(30, 40)]


def test_retention_deletes_all_old_raw_snapshots_but_keeps_a_separate_peer_baseline(session) -> None:
    record_peer_snapshot(session, peer("desktop", rx=100, tx=10), at("2026-01-01T00:00:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=120, tx=20), at("2026-01-02T00:00:00Z"))
    record_peer_snapshot(session, peer("phone", rx=50, tx=5), at("2026-01-03T00:00:00Z"))

    run_daily_retention(session, at("2026-04-15T00:00:00Z"))
    record_peer_snapshot(session, peer("desktop", rx=150, tx=30), at("2026-05-01T00:00:00Z"))

    snapshots = session.scalars(
        select(PeerSnapshotRecord).order_by(PeerSnapshotRecord.peer_name, PeerSnapshotRecord.observed_at)
    ).all()
    baselines = session.scalars(select(PeerBaseline).order_by(PeerBaseline.peer_name)).all()

    assert [(snapshot.peer_name, snapshot.received_bytes) for snapshot in snapshots] == [
        ("desktop", 150),
    ]
    assert [
        (baseline.peer_name, baseline.received_bytes, baseline.transmitted_bytes, baseline.observed_at)
        for baseline in baselines
    ] == [
        ("desktop", 150, 30, at("2026-05-01T00:00:00Z")),
        ("phone", 50, 5, at("2026-01-03T00:00:00Z")),
    ]
    assert [(bucket.received_bytes, bucket.transmitted_bytes) for bucket in monthly_usage(session, "2026-05")] == [
        (30, 10)
    ]


def test_snapshot_recording_can_follow_a_usage_read_in_the_same_session(session) -> None:
    record_peer_snapshot(session, peer("desktop", rx=100, tx=10), at("2026-07-01T00:00:00Z"))
    assert monthly_usage(session, "2026-07") == []

    record_peer_snapshot(session, peer("desktop", rx=120, tx=20), at("2026-07-01T01:00:00Z"))

    assert [(bucket.received_bytes, bucket.transmitted_bytes) for bucket in monthly_usage(session, "2026-07")] == [
        (20, 10)
    ]


@pytest.mark.parametrize("observed_at", ["2026-07-01T01:00:00Z", "2026-07-01T00:30:00Z"])
def test_snapshot_rejects_a_timestamp_that_is_not_strictly_later_than_the_latest_peer_snapshot(
    session, observed_at: str
) -> None:
    record_peer_snapshot(session, peer("desktop", rx=100, tx=10), at("2026-07-01T01:00:00Z"))

    with pytest.raises(SnapshotOrderingError, match="strictly later"):
        record_peer_snapshot(session, peer("desktop", rx=120, tx=20), at(observed_at))

    snapshots = session.scalars(select(PeerSnapshotRecord)).all()
    assert [(snapshot.received_bytes, snapshot.transmitted_bytes) for snapshot in snapshots] == [(100, 10)]


class StubWgEasy:
    def __init__(self, status: ContractStatus) -> None:
        self.status = status

    async def verify_contract(self) -> ContractStatus:
        return self.status


class StubMihomo:
    def __init__(self, selections: list[str] | None = None) -> None:
        self.selections = list(selections or ["WG-IMP"])
        self.delay_calls: list[tuple[str, str]] = []
        self.geo_upgrade_calls = 0

    async def groups(self) -> list[ProxyGroup]:
        selected = self.selections.pop(0) if self.selections else "WG-IMP"
        return [ProxyGroup("VPS-FALLBACK", "fallback", ("WG-IMP", "HY2-NL"), selected)]

    async def traffic(self) -> Traffic:
        return Traffic(up=1_500, down=2_500)

    async def proxy_delay(self, name: str, url: str) -> ProxyDelay:
        self.delay_calls.append((name, url))
        return ProxyDelay(37)

    async def geo_upgrade(self) -> ControllerWriteResult:
        self.geo_upgrade_calls += 1
        return ControllerWriteResult("geo_upgrade", 204)


class StubServices:
    async def check_all(self) -> list[ServiceStatus]:
        return [
            ServiceStatus("wg-easy", True, "HTTP 200", 4, 200),
            ServiceStatus("mihomo", True, "HTTP 200", 3, 200),
        ]


def run(awaitable):
    return asyncio.run(awaitable)


def make_collector(session_factory, *, wgeasy, mihomo, geodata_dir, now=None):
    return Collector(
        session_factory,
        wgeasy=wgeasy,
        mihomo=mihomo,
        service_probe=StubServices(),
        geodata_dir=geodata_dir,
        now=now or (lambda: at("2026-07-13T12:00:00Z")),
    )


def test_minute_records_normalized_peers_services_and_fallback_state(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    try:
        wgeasy = StubWgEasy(
            ContractStatus(
                True,
                None,
                (
                    WireGuardClient(
                        id=8,
                        name="laptop",
                        enabled=True,
                        ipv4_address="10.8.0.8",
                        latest_handshake_at="2026-07-13T11:59:00Z",
                        received_bytes=123,
                        transmitted_bytes=45,
                    ),
                ),
            )
        )
        collector = make_collector(factory, wgeasy=wgeasy, mihomo=StubMihomo(), geodata_dir=tmp_path / "geodata")

        run(collector.run_minute())

        with factory() as session:
            snapshot = session.scalars(select(PeerSnapshotRecord)).one()
            traffic = session.scalars(select(GatewayTrafficSample)).one()
            events = session.scalars(select(ProbeEvent).order_by(ProbeEvent.target)).all()
        assert (snapshot.peer_id, snapshot.received_bytes, snapshot.transmitted_bytes) == ("8", 123, 45)
        assert snapshot.latest_handshake_at == at("2026-07-13T11:59:00Z")
        assert (traffic.observed_at, traffic.up_bps, traffic.down_bps) == (
            at("2026-07-13T12:00:00Z"),
            1_500,
            2_500,
        )
        assert [(event.target, event.succeeded, event.outbound) for event in events] == [
            ("fallback:VPS-FALLBACK", True, "WG-IMP"),
            ("route-state:VPS-FALLBACK", True, "WG-IMP"),
            ("service:mihomo", True, None),
            ("service:wg-easy", True, None),
        ]
    finally:
        engine.dispose()


def test_minute_keeps_service_collection_when_wgeasy_contract_is_not_ready(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    try:
        collector = make_collector(
            factory,
            wgeasy=StubWgEasy(ContractStatus(False, "client-list contract mismatch")),
            mihomo=StubMihomo(),
            geodata_dir=tmp_path / "geodata",
        )

        run(collector.run_minute())

        with factory() as session:
            assert session.scalars(select(PeerSnapshotRecord)).all() == []
            audit = session.scalars(select(AuditEvent)).one()
            services = session.scalars(select(ProbeEvent).where(ProbeEvent.target.like("service:%"))).all()
        assert (audit.action, audit.succeeded, audit.error_text) == (
            "wgeasy_contract_unavailable",
            False,
            "client-list contract mismatch",
        )
        assert len(services) == 2
    finally:
        engine.dispose()


def test_probe_cycle_records_each_approved_endpoint_and_fallback_switch(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    try:
        mihomo = StubMihomo(["WG-IMP", "HY2-NL"])
        collector = make_collector(
            factory,
            wgeasy=StubWgEasy(ContractStatus(False, "not used")),
            mihomo=mihomo,
            geodata_dir=tmp_path / "geodata",
        )

        run(collector.run_probe_cycle())
        run(collector.run_probe_cycle())

        with factory() as session:
            probes = session.scalars(select(ProbeEvent).where(ProbeEvent.target.in_(("WG-IMP", "HY2-NL")))).all()
            routes = session.scalars(select(RouteEvent)).all()
        assert len(probes) == 12
        assert {(event.target, event.endpoint, event.succeeded, event.latency_ms) for event in probes} == {
            (target, endpoint, True, 37)
            for target in ("WG-IMP", "HY2-NL")
            for endpoint in Collector.DEFAULT_PROBE_URLS
        }
        assert [(event.route, event.previous_outbound, event.new_outbound) for event in routes] == [
            ("VPS-FALLBACK", "WG-IMP", "HY2-NL")
        ]
        assert all("->" not in (event.detail or "") for event in routes)
    finally:
        engine.dispose()


def test_probe_cycle_establishes_a_baseline_without_a_false_switch(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    try:
        collector = make_collector(
            factory,
            wgeasy=StubWgEasy(ContractStatus(False, "not used")),
            mihomo=StubMihomo(["WG-IMP", "WG-IMP"]),
            geodata_dir=tmp_path / "geodata",
        )

        run(collector.run_probe_cycle())
        run(collector.run_probe_cycle())

        with factory() as session:
            assert session.scalars(select(RouteEvent)).all() == []
    finally:
        engine.dispose()


def test_minute_observations_record_two_exit_changes_before_the_next_probe_cycle(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    try:
        collector = make_collector(
            factory,
            wgeasy=StubWgEasy(ContractStatus(False, "not used")),
            mihomo=StubMihomo(["WG-IMP", "HY2-NL", "WG-IMP", "WG-IMP"]),
            geodata_dir=tmp_path / "geodata",
        )

        run(collector.run_minute())
        run(collector.run_minute())
        run(collector.run_minute())
        run(collector.run_probe_cycle())

        with factory() as session:
            routes = session.scalars(select(RouteEvent).order_by(RouteEvent.id)).all()
            states = session.scalars(
                select(ProbeEvent)
                .where(ProbeEvent.target == "route-state:VPS-FALLBACK")
                .order_by(ProbeEvent.id)
            ).all()
        assert [(event.previous_outbound, event.new_outbound) for event in routes] == [
            ("WG-IMP", "HY2-NL"),
            ("HY2-NL", "WG-IMP"),
        ]
        assert [event.outbound for event in states] == ["WG-IMP", "HY2-NL", "WG-IMP", "WG-IMP"]
        assert all(event.new_outbound in {"WG-IMP", "HY2-NL"} for event in routes)
    finally:
        engine.dispose()


def test_collector_lifespan_registers_and_stops_scheduler_only_when_explicitly_entered() -> None:
    class Scheduler:
        def __init__(self) -> None:
            self.jobs = []
            self.started = False
            self.stopped = False

        def add_job(self, func, trigger, **kwargs) -> None:
            self.jobs.append((func, trigger, kwargs))

        def start(self) -> None:
            self.started = True

        def shutdown(self, *, wait: bool = True) -> None:
            self.stopped = True

    class CollectorStub:
        async def run_minute(self) -> None:
            pass

        async def run_probe_cycle(self) -> None:
            pass

        def run_daily(self) -> None:
            pass

        async def scheduled_geo_upgrade(self) -> None:
            pass

        async def catch_up_geo_upgrade(self) -> bool:
            return False

    scheduler = Scheduler()
    app = create_app(runtime_factory=lambda: CollectorRuntime(CollectorStub(), scheduler), trusted_hosts=("testserver",))
    unstarted_client = TestClient(app)
    try:
        assert unstarted_client.get("/api/healthz").json() == {"status": "ok"}
        assert scheduler.jobs == []
        assert scheduler.started is False
    finally:
        unstarted_client.close()

    with TestClient(app) as client:
        assert client.get("/api/healthz").json() == {"status": "ok"}
        assert scheduler.started is True
        assert [options["id"] for _func, _trigger, options in scheduler.jobs] == [
            "collector-minute",
            "collector-probe",
                "collector-daily",
                "collector-geo-upgrade",
                "collector-geo-catchup",
            ]
    assert scheduler.stopped is True


def test_daily_captures_geodata_file_metadata_and_hash(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    geodata_dir = tmp_path / "geodata"
    geodata_dir.mkdir()
    geoip = geodata_dir / "geoip.dat"
    geosite = geodata_dir / "geosite.dat"
    geoip.write_bytes(b"geoip-data")
    geosite.write_bytes(b"geosite-data")
    try:
        collector = make_collector(
            factory,
            wgeasy=StubWgEasy(ContractStatus(False, "not used")),
            mihomo=StubMihomo(),
            geodata_dir=geodata_dir,
        )

        collector.run_daily()

        with factory() as session:
            update = session.scalars(select(GeoUpdate)).one()
            metadata = session.scalars(select(GeoFileMetadata).order_by(GeoFileMetadata.filename)).all()
        assert (update.source, update.operation, update.succeeded) == ("daily", "metadata_snapshot", True)
        assert [(item.filename, item.size_bytes, item.sha256) for item in metadata] == [
            ("geoip.dat", 10, hashlib.sha256(b"geoip-data").hexdigest()),
            ("geosite.dat", 12, hashlib.sha256(b"geosite-data").hexdigest()),
        ]
    finally:
        engine.dispose()


def test_manual_geo_upgrade_records_before_and_after_without_controller_payload(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    geodata_dir = tmp_path / "geodata"
    geodata_dir.mkdir()
    (geodata_dir / "geoip.dat").write_bytes(b"before")
    mihomo = StubMihomo()
    try:
        collector = make_collector(
            factory,
            wgeasy=StubWgEasy(ContractStatus(False, "not used")),
            mihomo=mihomo,
            geodata_dir=geodata_dir,
        )

        run(collector.manual_geo_upgrade("admin"))

        with factory() as session:
            update = session.scalars(select(GeoUpdate)).one()
            metadata = session.scalars(select(GeoFileMetadata).order_by(GeoFileMetadata.phase)).all()
            audit = session.scalars(select(AuditEvent).where(AuditEvent.action == "geo_upgrade")).one()
        assert mihomo.geo_upgrade_calls == 1
        assert (update.source, update.operation, update.succeeded, update.status_code) == (
            "manual",
            "geo_upgrade",
            True,
            204,
        )
        assert [item.phase for item in metadata] == ["after", "before"]
        assert (audit.actor, audit.succeeded, audit.status_code, audit.detail) == ("admin", True, 204, None)
    finally:
        engine.dispose()


def test_scheduled_geo_upgrade_is_audited_separately_from_a_manual_request(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    mihomo = StubMihomo()
    try:
        collector = make_collector(
            factory,
            wgeasy=StubWgEasy(ContractStatus(False, "not used")),
            mihomo=mihomo,
            geodata_dir=tmp_path / "geodata",
        )

        run(collector.scheduled_geo_upgrade())

        with factory() as session:
            update = session.scalars(select(GeoUpdate)).one()
            audit = session.scalars(select(AuditEvent).where(AuditEvent.action == "geo_upgrade")).one()
        assert mihomo.geo_upgrade_calls == 1
        assert (update.source, update.operation, update.succeeded) == ("scheduled", "geo_upgrade", True)
        assert audit.actor == "scheduler"
    finally:
        engine.dispose()


def test_geo_upgrade_catchup_runs_when_no_success_exists_for_26_hours(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    mihomo = StubMihomo()
    now = datetime(2026, 7, 15, 5, tzinfo=UTC)
    try:
        collector = make_collector(
            factory,
            wgeasy=StubWgEasy(ContractStatus(False, "not used")),
            mihomo=mihomo,
            geodata_dir=tmp_path / "geodata",
            now=lambda: now,
        )

        assert run(collector.catch_up_geo_upgrade()) is True
        assert mihomo.geo_upgrade_calls == 1
    finally:
        engine.dispose()


def test_register_collector_jobs_runs_geo_update_every_day_at_four_yekaterinburg_time(tmp_path) -> None:
    class Scheduler:
        def __init__(self) -> None:
            self.jobs = []
            self.started = False

        def add_job(self, func, trigger, **kwargs) -> None:
            self.jobs.append((func, trigger, kwargs))

        def start(self) -> None:
            self.started = True

    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    try:
        collector = make_collector(
            factory,
            wgeasy=StubWgEasy(ContractStatus(False, "not used")),
            mihomo=StubMihomo(),
            geodata_dir=tmp_path / "geodata",
        )
        scheduler = Scheduler()

        register_collector_jobs(scheduler, collector)

        assert [(trigger, options["id"], options.get("minutes")) for _func, trigger, options in scheduler.jobs] == [
            ("interval", "collector-minute", 1),
            ("interval", "collector-probe", 5),
                ("interval", "collector-daily", None),
                ("cron", "collector-geo-upgrade", None),
                ("date", "collector-geo-catchup", None),
            ]
        assert scheduler.jobs[2][2]["hours"] == 24
        assert scheduler.jobs[3][2]["hour"] == 4
        assert scheduler.jobs[3][2]["minute"] == 0
        assert scheduler.jobs[3][2]["timezone"] == "Asia/Yekaterinburg"
        assert scheduler.started is False
    finally:
        engine.dispose()
