"""Domain objects and persistence operations for collected VPN peer traffic."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from calendar import monthrange
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from app.mihomo import MihomoIntegrationError, ProxyGroup
from app.models import (
    AuditEvent,
    GeoFileMetadata,
    GeoUpdate,
    PeerBaseline,
    PeerSnapshotRecord,
    ProbeEvent,
    RouteEvent,
    TrafficHourly,
    TrafficMonthly,
)
from app.settings import DELAY_TEST_URLS
from app.probe_targets import validate_approved_probe_urls

RAW_SNAPSHOT_RETENTION = timedelta(days=90)
HOURLY_RETENTION_MONTHS = 12


class SnapshotOrderingError(ValueError):
    """Raised when a peer snapshot is not newer than the stored baseline."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PeerSnapshot:
    """A counter reading for one WireGuard peer, without secrets or configuration."""

    peer_name: str
    received_bytes: int
    transmitted_bytes: int
    peer_id: str | None = None
    latest_handshake_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.peer_name.strip():
            raise ValueError("peer_name must not be empty")
        if self.peer_id is not None and not self.peer_id.strip():
            raise ValueError("peer_id must not be empty when supplied")
        if self.received_bytes < 0 or self.transmitted_bytes < 0:
            raise ValueError("peer counters must not be negative")
        if self.latest_handshake_at is not None:
            _utc(self.latest_handshake_at)

    @property
    def peer_key(self) -> str:
        """Use a stable peer identifier when available, otherwise the peer name."""

        return f"id:{self.peer_id}" if self.peer_id else f"name:{self.peer_name}"


@dataclass(frozen=True, order=True, slots=True)
class YearMonth:
    """A validated calendar month used for stored monthly aggregates."""

    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError("month must be between 1 and 12")

    @classmethod
    def parse(cls, value: str) -> "YearMonth":
        try:
            year, month = value.split("-", maxsplit=1)
            parsed = cls(int(year), int(month))
        except (TypeError, ValueError) as error:
            raise ValueError("period must use YYYY-MM") from error
        if str(parsed) != value:
            raise ValueError("period must use YYYY-MM")
        return parsed

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True, slots=True)
class UsageBucket:
    """One peer's accounted usage in a calendar month."""

    period: YearMonth
    peer_name: str
    peer_id: str | None
    received_bytes: int
    transmitted_bytes: int


def _period(value: YearMonth | str) -> YearMonth:
    return value if isinstance(value, YearMonth) else YearMonth.parse(value)


def _counter_delta(current: int, previous: int) -> int:
    """Treat a lower counter as a reset, never as negative traffic."""

    return current if current < previous else current - previous


def _subtract_calendar_months(value: datetime, months: int) -> datetime:
    """Subtract whole calendar months while preserving a valid UTC day/time."""

    target = value.year * 12 + value.month - 1 - months
    target_year, zero_based_month = divmod(target, 12)
    target_month = zero_based_month + 1
    target_day = min(value.day, monthrange(target_year, target_month)[1])
    return value.replace(year=target_year, month=target_month, day=target_day)


def _write_transaction(session: Session) -> SessionTransaction:
    """Use the caller transaction when one exists, otherwise start an atomic one."""

    return session.begin_nested() if session.in_transaction() else session.begin()


def record_peer_snapshot(session: Session, snapshot: PeerSnapshot, observed_at: datetime) -> None:
    """Store a raw reading and atomically roll its delta into hour and month buckets."""

    observed_at = _utc(observed_at)
    hour_start = observed_at.replace(minute=0, second=0, microsecond=0)
    period = str(YearMonth(observed_at.year, observed_at.month))

    with _write_transaction(session):
        previous = session.get(PeerBaseline, snapshot.peer_key)
        if previous is not None and observed_at <= previous.observed_at:
            raise SnapshotOrderingError(
                "observed_at must be strictly later than the latest stored snapshot for this peer"
            )
        received_delta = 0 if previous is None else _counter_delta(snapshot.received_bytes, previous.received_bytes)
        transmitted_delta = (
            0 if previous is None else _counter_delta(snapshot.transmitted_bytes, previous.transmitted_bytes)
        )

        session.add(
            PeerSnapshotRecord(
                peer_key=snapshot.peer_key,
                peer_name=snapshot.peer_name,
                peer_id=snapshot.peer_id,
                received_bytes=snapshot.received_bytes,
                transmitted_bytes=snapshot.transmitted_bytes,
                latest_handshake_at=snapshot.latest_handshake_at,
                observed_at=observed_at,
            )
        )
        _upsert_baseline(session, snapshot, observed_at)
        if previous is not None:
            _upsert_hourly(session, snapshot, hour_start, received_delta, transmitted_delta)
            _upsert_monthly(session, snapshot, period, received_delta, transmitted_delta)


def _upsert_baseline(session: Session, snapshot: PeerSnapshot, observed_at: datetime) -> None:
    statement = insert(PeerBaseline).values(
        peer_key=snapshot.peer_key,
        peer_name=snapshot.peer_name,
        peer_id=snapshot.peer_id,
        received_bytes=snapshot.received_bytes,
        transmitted_bytes=snapshot.transmitted_bytes,
        observed_at=observed_at,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=(PeerBaseline.peer_key,),
            set_={
                "peer_name": snapshot.peer_name,
                "peer_id": snapshot.peer_id,
                "received_bytes": snapshot.received_bytes,
                "transmitted_bytes": snapshot.transmitted_bytes,
                "observed_at": observed_at,
            },
        )
    )


def _upsert_hourly(
    session: Session,
    snapshot: PeerSnapshot,
    hour_start: datetime,
    received_delta: int,
    transmitted_delta: int,
) -> None:
    statement = insert(TrafficHourly).values(
        peer_key=snapshot.peer_key,
        peer_name=snapshot.peer_name,
        peer_id=snapshot.peer_id,
        hour_start=hour_start,
        received_bytes=received_delta,
        transmitted_bytes=transmitted_delta,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=(TrafficHourly.peer_key, TrafficHourly.hour_start),
            set_={
                "peer_name": snapshot.peer_name,
                "peer_id": snapshot.peer_id,
                "received_bytes": TrafficHourly.received_bytes + received_delta,
                "transmitted_bytes": TrafficHourly.transmitted_bytes + transmitted_delta,
            },
        )
    )


def _upsert_monthly(
    session: Session,
    snapshot: PeerSnapshot,
    period: str,
    received_delta: int,
    transmitted_delta: int,
) -> None:
    statement = insert(TrafficMonthly).values(
        peer_key=snapshot.peer_key,
        peer_name=snapshot.peer_name,
        peer_id=snapshot.peer_id,
        period=period,
        received_bytes=received_delta,
        transmitted_bytes=transmitted_delta,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=(TrafficMonthly.peer_key, TrafficMonthly.period),
            set_={
                "peer_name": snapshot.peer_name,
                "peer_id": snapshot.peer_id,
                "received_bytes": TrafficMonthly.received_bytes + received_delta,
                "transmitted_bytes": TrafficMonthly.transmitted_bytes + transmitted_delta,
            },
        )
    )


def monthly_usage(session: Session, period: YearMonth | str) -> list[UsageBucket]:
    """Return the aggregated deltas for each peer in the requested UTC month."""

    parsed_period = _period(period)
    records = session.scalars(
        select(TrafficMonthly)
        .where(TrafficMonthly.period == str(parsed_period))
        .order_by(TrafficMonthly.peer_name, TrafficMonthly.peer_key)
    )
    return [
        UsageBucket(
            period=parsed_period,
            peer_name=record.peer_name,
            peer_id=record.peer_id,
            received_bytes=record.received_bytes,
            transmitted_bytes=record.transmitted_bytes,
        )
        for record in records
    ]


def run_daily_retention(session: Session, now: datetime) -> int:
    """Apply raw/hourly retention without automatically deleting monthly summaries."""

    now = _utc(now)
    raw_cutoff = now - RAW_SNAPSHOT_RETENTION
    hourly_cutoff = _subtract_calendar_months(now, HOURLY_RETENTION_MONTHS)

    with _write_transaction(session):
        raw_result = session.execute(delete(PeerSnapshotRecord).where(PeerSnapshotRecord.observed_at < raw_cutoff))
        hourly_result = session.execute(delete(TrafficHourly).where(TrafficHourly.hour_start < hourly_cutoff))
    return (raw_result.rowcount or 0) + (hourly_result.rowcount or 0)


@dataclass(frozen=True, slots=True)
class GeoFileSnapshot:
    """Read-only metadata for an individual mounted GeoData file."""

    filename: str
    size_bytes: int
    modified_at: datetime
    sha256: str


@dataclass(frozen=True, slots=True)
class ProbeDiagnosticStep:
    """One deliberately bounded stage of a manual route diagnosis."""

    succeeded: bool | None
    latency_ms: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ProbeDiagnosticDns(ProbeDiagnosticStep):
    hostname: str
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProbeDiagnosis:
    observed_at: datetime
    outbound: str
    endpoint: str
    conclusion: str
    conclusion_text: str
    controller: ProbeDiagnosticStep
    dns: ProbeDiagnosticDns
    exit: ProbeDiagnosticStep


class GeoFileStore(Protocol):
    """Injectable read-only view of the GeoData mount for offline tests."""

    def metadata(self, directory: Path) -> Collection[GeoFileSnapshot]: ...


class LocalGeoFileStore:
    """Read mounted GeoIP/GeoSite files without modifying them."""

    def metadata(self, directory: Path) -> tuple[GeoFileSnapshot, ...]:
        if not directory.is_dir():
            raise OSError("GeoData directory is unavailable")
        snapshots: list[GeoFileSnapshot] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file() or not _is_geodata_filename(path.name):
                continue
            stat = path.stat()
            snapshots.append(
                GeoFileSnapshot(
                    filename=path.name,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                    sha256=_sha256_file(path),
                )
            )
        return tuple(snapshots)


class Collector:
    """Persist scheduled, browser-safe dashboard observations.

    Network adapters, time and the GeoData reader are injected.  The collector
    never starts jobs itself and never persists credentials, controller bodies
    or WireGuard configuration.
    """

    DEFAULT_PROBE_URLS = DELAY_TEST_URLS
    _FALLBACK_PREFIX = "fallback:"
    _ROUTE_STATE_PREFIX = "route-state:"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        wgeasy: object,
        mihomo: object,
        service_probe: object,
        geodata_dir: Path,
        probe_urls: Collection[str] = DELAY_TEST_URLS,
        probe_url_supplier: Callable[[], Collection[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
        now: Callable[[], datetime] | None = None,
        files: GeoFileStore | None = None,
    ) -> None:
        if clock is not None and now is not None:
            raise ValueError("provide either clock or now")
        urls = validate_approved_probe_urls(probe_urls)
        self._session_factory = session_factory
        self._wgeasy = wgeasy
        self._mihomo = mihomo
        self._service_probe = service_probe
        self._geodata_dir = Path(geodata_dir)
        self._probe_urls = urls
        self._probe_url_supplier = probe_url_supplier
        self._probe_active = False
        self._geo_upgrade_lock = asyncio.Lock()
        self._clock = clock or now or (lambda: datetime.now(UTC))
        self._files = files or LocalGeoFileStore()

    async def run_minute(self) -> None:
        """Collect peer counters, HTTP service status and the selected fallback exit."""

        observed_at = _utc(self._clock())
        await self._collect_peers(observed_at)
        await self._collect_service_state(observed_at)
        selected = await self._fallback_selection(observed_at)
        if selected is not None:
            group, outbound = selected
            self._persist_selected_exit(observed_at, group, outbound)
            self._record_probe_events(
                [
                    ProbeEvent(
                        observed_at=observed_at,
                        target=f"{self._FALLBACK_PREFIX}{group}",
                        succeeded=True,
                        latency_ms=None,
                        endpoint=None,
                        outbound=outbound,
                        status="selected",
                        status_code=None,
                        error_text=None,
                        detail=None,
                    )
                ]
            )

    def start_probe_cycle(self) -> bool:
        """Schedule one immediate run without allowing overlapping controller probes."""

        if self._probe_active:
            return False
        self._probe_active = True
        asyncio.create_task(self._run_probe_cycle_active())
        return True

    async def run_probe_cycle(self) -> bool:
        """Probe both independent exits and record only a real fallback selection change."""

        if self._probe_active:
            return False
        self._probe_active = True
        return await self._run_probe_cycle_active()

    async def _run_probe_cycle_active(self) -> bool:
        """Run after the caller has atomically reserved the single probe slot."""

        try:
            observed_at = _utc(self._clock())
            selected = await self._fallback_selection(observed_at)
            if selected is not None:
                self._persist_selected_exit(observed_at, *selected)

            outbound = selected[1] if selected is not None else None
            urls = self._current_probe_urls()
            events = await asyncio.gather(
                *(self._probe_one(observed_at, target, endpoint, outbound) for target in ("WG-IMP", "HY2-NL") for endpoint in urls)
            )
            self._record_probe_events(events)
            return True
        finally:
            self._probe_active = False

    def _current_probe_urls(self) -> tuple[str, ...]:
        if self._probe_url_supplier is not None:
            # The production supplier is ProbeTargetService, which validates
            # custom DNS targets immediately before exposing them here.
            return tuple(self._probe_url_supplier())
        return validate_approved_probe_urls(self._probe_urls)

    async def _probe_one(
        self,
        observed_at: datetime,
        target: str,
        endpoint: str,
        outbound: str | None,
    ) -> ProbeEvent:
        try:
            result = await _await_result(self._mihomo.proxy_delay(target, endpoint))
            return ProbeEvent(
                observed_at=observed_at,
                target=target,
                succeeded=True,
                latency_ms=result.delay_ms,
                endpoint=endpoint,
                outbound=outbound,
                status="ok",
                status_code=None,
                error_text=None,
                detail=None,
            )
        except BaseException as error:
            reason, status_code = _safe_mihomo_error(error, "delay probe failed")
            return ProbeEvent(
                observed_at=observed_at,
                target=target,
                succeeded=False,
                latency_ms=None,
                endpoint=endpoint,
                outbound=outbound,
                status="failed",
                status_code=status_code,
                error_text=reason,
                detail=None,
            )

    async def diagnose_probe(self, outbound: str, endpoint: str) -> ProbeDiagnosis:
        """Locate a failed delay check without exposing controller internals.

        The stages are intentionally sequential: a failed controller request
        makes later results meaningless, and an unresolved hostname makes a
        tunnel delay check ambiguous.
        """

        observed_at = _utc(self._clock())
        controller = await self._diagnostic_step(
            lambda: self._mihomo.version(), "Mihomo controller is unavailable"
        )
        if controller.succeeded is not True:
            return ProbeDiagnosis(
                observed_at, outbound, endpoint,
                "controller_unavailable",
                "Контроллер Mihomo не ответил. Это сбой связи панели с Mihomo, а не результат проверки сайта или VPN-выхода.",
                controller,
                ProbeDiagnosticDns(None, None, "Не запускалась: контроллер недоступен.", "", ()),
                ProbeDiagnosticStep(None, None, "Не запускалась: контроллер недоступен."),
            )

        dns_started = perf_counter()
        try:
            dns_result = await _await_result(self._mihomo.dns_lookup(endpoint))
            dns = ProbeDiagnosticDns(
                True,
                _elapsed_ms(dns_started),
                None,
                _short_safe_text(getattr(dns_result, "hostname", None), ""),
                tuple(_short_safe_text(item, "") for item in getattr(dns_result, "addresses", ()) if _short_safe_text(item, "")),
            )
        except BaseException as error:
            reason, _status_code = _safe_mihomo_error(error, "Mihomo DNS request failed")
            return ProbeDiagnosis(
                observed_at, outbound, endpoint,
                "dns_failure",
                "Контроллер Mihomo отвечает, но DNS внутри Mihomo не вернул ответ для сайта. Проверьте DNS-настройки и доступность DNS-резолверов.",
                controller,
                ProbeDiagnosticDns(False, _elapsed_ms(dns_started), reason, "", ()),
                ProbeDiagnosticStep(None, None, "Не запускалась: DNS-проверка не прошла."),
            )

        if not dns.addresses:
            return ProbeDiagnosis(
                observed_at, outbound, endpoint,
                "dns_no_address",
                "Mihomo ответил на DNS-запрос, но не получил публичный A-запись. Проверка выхода не запускалась, чтобы не смешивать причины.",
                controller,
                dns,
                ProbeDiagnosticStep(None, None, "Не запускалась: DNS не вернул публичный IPv4-адрес."),
            )

        exit_step = await self._diagnostic_step(
            lambda: self._mihomo.proxy_delay(outbound, endpoint), "Проверка через выход не завершилась"
        )
        if exit_step.succeeded:
            return ProbeDiagnosis(
                observed_at, outbound, endpoint,
                "ok",
                "Контроллер, DNS Mihomo и выбранный выход ответили. Маршрут до сайта работает в момент проверки.",
                controller, dns, exit_step,
            )
        return ProbeDiagnosis(
            observed_at, outbound, endpoint,
            "exit_failure",
            "Контроллер и DNS отвечают; проверка через выбранный выход не завершилась. Причина находится после DNS: у выхода, по пути до сайта или на самом сайте.",
            controller, dns, exit_step,
        )

    async def _diagnostic_step(self, operation: Callable[[], object], fallback: str) -> ProbeDiagnosticStep:
        started = perf_counter()
        try:
            result = await _await_result(operation())
        except BaseException as error:
            reason, _status_code = _safe_mihomo_error(error, fallback)
            return ProbeDiagnosticStep(False, _elapsed_ms(started), reason)
        delay = getattr(result, "delay_ms", None)
        return ProbeDiagnosticStep(True, delay if isinstance(delay, int) and delay >= 0 else _elapsed_ms(started), None)

    def run_daily(self) -> None:
        """Run retention and store a read-only GeoData metadata audit."""

        observed_at = _utc(self._clock())
        try:
            metadata = tuple(self._files.metadata(self._geodata_dir))
            succeeded = True
            error_text = None
        except OSError:
            metadata = ()
            succeeded = False
            error_text = "GeoData metadata is unavailable"

        with self._session_factory.begin() as session:
            run_daily_retention(session, observed_at)
            update = GeoUpdate(
                observed_at=observed_at,
                source="daily",
                operation="metadata_snapshot",
                succeeded=succeeded,
                status_code=None,
                error_text=error_text,
                version=None,
                detail=None,
            )
            session.add(update)
            session.flush()
            self._store_geo_metadata(session, update.id, "daily", metadata)
            session.add(
                AuditEvent(
                    observed_at=observed_at,
                    actor="backend",
                    action="geo_metadata_snapshot",
                    succeeded=succeeded,
                    status_code=None,
                    error_text=error_text,
                    detail=None,
                )
            )

    async def manual_geo_upgrade(self, actor: str) -> None:
        """Explicitly request Mihomo's GeoData upgrade and audit local metadata around it."""

        await self._geo_upgrade(actor, source="manual")

    async def scheduled_geo_upgrade(self) -> None:
        """Run the daily GeoData refresh owned by the dashboard scheduler."""

        await self._geo_upgrade("scheduler", source="scheduled")

    async def catch_up_geo_upgrade(self) -> bool:
        """Refresh GeoData after a missed daily window, without duplicating fresh updates."""

        now = _utc(self._clock())
        with self._session_factory() as session:
            last_success = session.scalar(
                select(GeoUpdate.observed_at)
                .where(GeoUpdate.operation == "geo_upgrade", GeoUpdate.succeeded.is_(True))
                .order_by(GeoUpdate.observed_at.desc())
                .limit(1)
            )
        if last_success is not None and last_success >= now - timedelta(hours=26):
            return False
        await self._geo_upgrade("scheduler", source="catchup")
        return True

    async def _geo_upgrade(self, actor: str, *, source: str) -> None:
        """Serialize manual and scheduled upgrades to keep their audit trail unambiguous."""

        actor = _safe_actor(actor)
        async with self._geo_upgrade_lock:
            observed_at = _utc(self._clock())
            before, before_error = self._read_geo_metadata()
            with self._session_factory.begin() as session:
                update = GeoUpdate(
                    observed_at=observed_at,
                    source=source,
                    operation="geo_upgrade",
                    succeeded=None,
                    status_code=None,
                    error_text=before_error,
                    version=None,
                    detail=None,
                )
                session.add(update)
                session.flush()
                update_id = update.id
                self._store_geo_metadata(session, update_id, "before", before)

            try:
                result = await _await_result(self._mihomo.geo_upgrade())
                succeeded = True
                status_code = result.status_code
                error_text = before_error
            except BaseException as error:
                succeeded = False
                error_text, status_code = _safe_mihomo_error(error, "Geo upgrade failed")

            after, after_error = self._read_geo_metadata()
            if after_error is not None and error_text is None:
                error_text = after_error
            with self._session_factory.begin() as session:
                update = session.get(GeoUpdate, update_id)
                if update is None:
                    raise RuntimeError("Geo upgrade audit record disappeared")
                update.succeeded = succeeded
                update.status_code = status_code
                update.error_text = error_text
                self._store_geo_metadata(session, update_id, "after", after)
                session.add(
                    AuditEvent(
                        observed_at=_utc(self._clock()),
                        actor=actor,
                        action="geo_upgrade",
                        succeeded=succeeded,
                        status_code=status_code,
                        error_text=error_text,
                        detail=None,
                    )
                )

    async def _collect_peers(self, observed_at: datetime) -> None:
        try:
            contract = await _await_result(self._wgeasy.verify_contract())
        except BaseException:
            self._record_audit(observed_at, "wgeasy_contract_unavailable", "wg-easy contract is unavailable")
            return
        if not contract.ready:
            self._record_audit(
                observed_at,
                "wgeasy_contract_unavailable",
                _short_safe_text(contract.reason, "wg-easy contract is unavailable"),
            )
            return

        with self._session_factory.begin() as session:
            for client in contract.clients:
                try:
                    record_peer_snapshot(
                        session,
                        PeerSnapshot(
                            peer_name=client.name,
                            peer_id=str(client.id),
                            received_bytes=client.received_bytes,
                            transmitted_bytes=client.transmitted_bytes,
                            latest_handshake_at=_parse_handshake(client.latest_handshake_at),
                        ),
                        observed_at,
                    )
                except (SnapshotOrderingError, ValueError):
                    continue

    async def _collect_service_state(self, observed_at: datetime) -> None:
        try:
            statuses = await _await_result(self._service_probe.check_all())
        except BaseException:
            self._record_audit(observed_at, "service_probe_unavailable", "service probe is unavailable")
            return
        self._record_probe_events(
            [
                ProbeEvent(
                    observed_at=observed_at,
                    target=f"service:{status.name}",
                    succeeded=status.healthy,
                    latency_ms=status.latency_ms,
                    endpoint=None,
                    outbound=None,
                    status=_short_safe_text(status.reason, "service status unavailable"),
                    status_code=status.status_code,
                    error_text=None if status.healthy else _short_safe_text(status.reason, "service status unavailable"),
                    detail=None,
                )
                for status in statuses
            ]
        )

    async def _fallback_selection(self, observed_at: datetime) -> tuple[str, str] | None:
        try:
            groups = await _await_result(self._mihomo.groups())
        except BaseException as error:
            reason, _status_code = _safe_mihomo_error(error, "fallback group is unavailable")
            self._record_audit(observed_at, "mihomo_fallback_unavailable", reason)
            return None
        for group in groups:
            if _is_selected_fallback(group):
                return group.name, group.now  # type: ignore[return-value]
        return None

    def _persist_selected_exit(self, observed_at: datetime, group: str, outbound: str) -> None:
        """Persist a selected independent exit and emit exactly one real transition."""

        target = f"{self._ROUTE_STATE_PREFIX}{group}"
        with self._session_factory.begin() as session:
            previous = session.scalars(
                select(ProbeEvent)
                .where(ProbeEvent.target == target)
                .order_by(ProbeEvent.id.desc())
            ).first()
            session.add(
                ProbeEvent(
                    observed_at=observed_at,
                    target=target,
                    succeeded=True,
                    latency_ms=None,
                    endpoint=None,
                    outbound=outbound,
                    status="selected",
                    status_code=None,
                    error_text=None,
                    detail=None,
                )
            )
            if previous is not None and previous.outbound is not None and previous.outbound != outbound:
                session.add(
                    RouteEvent(
                        observed_at=observed_at,
                        route=group,
                        action="selected_outbound_changed",
                        previous_outbound=previous.outbound,
                        new_outbound=outbound,
                        detail="fallback selection changed",
                    )
                )

    def _record_probe_events(self, events: Collection[ProbeEvent]) -> None:
        if not events:
            return
        with self._session_factory.begin() as session:
            session.add_all(events)

    def _record_audit(self, observed_at: datetime, action: str, error_text: str) -> None:
        with self._session_factory.begin() as session:
            session.add(
                AuditEvent(
                    observed_at=observed_at,
                    actor="backend",
                    action=action,
                    succeeded=False,
                    status_code=None,
                    error_text=_short_safe_text(error_text, "integration failure"),
                    detail=None,
                )
            )

    def _read_geo_metadata(self) -> tuple[tuple[GeoFileSnapshot, ...], str | None]:
        try:
            return tuple(self._files.metadata(self._geodata_dir)), None
        except OSError:
            return (), "GeoData metadata is unavailable"

    def current_geo_metadata(self) -> tuple[tuple[GeoFileSnapshot, ...], str | None]:
        """Return the mounted GeoData state without creating an audit record."""

        return self._read_geo_metadata()

    @staticmethod
    def _store_geo_metadata(
        session: Session,
        geo_update_id: int,
        phase: str,
        metadata: Collection[GeoFileSnapshot],
    ) -> None:
        session.add_all(
            [
                GeoFileMetadata(
                    geo_update_id=geo_update_id,
                    phase=phase,
                    filename=item.filename,
                    size_bytes=item.size_bytes,
                    modified_at=_utc(item.modified_at),
                    sha256=item.sha256,
                )
                for item in metadata
            ]
        )


def register_collector_jobs(scheduler: object, collector: Collector) -> None:
    """Register, but never start, the jobs owned by the application lifespan."""

    scheduler.add_job(
        collector.run_minute,
        "interval",
        minutes=1,
        id="collector-minute",
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        collector.run_probe_cycle,
        "interval",
        minutes=5,
        id="collector-probe",
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        collector.run_daily,
        "interval",
        hours=24,
        id="collector-daily",
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        collector.scheduled_geo_upgrade,
        "cron",
        hour=4,
        minute=0,
        timezone="Asia/Yekaterinburg",
        id="collector-geo-upgrade",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        collector.catch_up_geo_upgrade,
        "date",
        id="collector-geo-catchup",
        replace_existing=True,
    )


async def _await_result(value: object):
    if inspect.isawaitable(value):
        return await value
    return value


def _parse_handshake(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def _is_selected_fallback(group: ProxyGroup) -> bool:
    return (
        group.type is not None
        and group.type.casefold() == "fallback"
        and group.now is not None
        and group.now in {"WG-IMP", "HY2-NL"}
        and {"WG-IMP", "HY2-NL"}.issubset(group.proxies)
    )


def _safe_mihomo_error(error: BaseException, fallback: str) -> tuple[str, int | None]:
    if isinstance(error, MihomoIntegrationError):
        return _short_safe_text(error.reason, fallback), error.status_code
    return fallback, None


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _short_safe_text(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = value.strip()
    return normalized[:255] if normalized else fallback


def _safe_actor(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("actor must be text")
    actor = value.strip()
    if not actor or len(actor) > 255:
        raise ValueError("actor is invalid")
    return actor


def _is_geodata_filename(filename: str) -> bool:
    normalized = filename.casefold()
    return "geoip" in normalized or "geosite" in normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
