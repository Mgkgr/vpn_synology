"""Once-per-local-day site snapshots, independent of health and routing writes."""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert

from app.models import SiteProbeResult, SiteProbeRun, SiteProbeSchedule
from app.outbounds import build_outbound_registry
from app.site_probe_catalog import SITE_PROBE_ROUTES, SITE_PROBE_TARGETS, SiteProbeObservation


TIMEZONE = "Asia/Yekaterinburg"
ZONE = ZoneInfo(TIMEZONE)
BATCH_SECONDS = 25 * 60
REASONS = frozenset({"probe_group_invalid", "probe_mode_invalid", "controller_unavailable", "dns_preflight_failed",
                     "probe_timeout", "probe_failed", "probe_result_unconfirmed",
                     "http_status_outside_expected", "collector_error", "collector_timeout"})


def _aware(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware datetime required")
    return now.astimezone(UTC)


class SiteProbeService:
    def __init__(self, session_factory, *, enabled=False, targets=None):
        self.sessions, self.enabled = session_factory, enabled
        self.targets = tuple(SITE_PROBE_TARGETS) if targets is None else tuple(targets)
        if not self.targets or len(set(self.targets)) != len(self.targets) or any(key not in SITE_PROBE_TARGETS for key in self.targets):
            raise ValueError("invalid built-in probe catalogue")
        labels = {item.id: item.label for item in build_outbound_registry()}
        self.labels = {key: value.label + (" · " + labels[value.member] if value.member in labels else "")
                       for key, value in SITE_PROBE_ROUTES.items()}

    def claim_day(self, now):
        now = _aware(now)
        if not self.enabled:
            return None
        local = now.astimezone(ZONE)
        day = local.date().isoformat()
        with self.sessions.begin() as session:
            session.execute(update(SiteProbeRun).where(
                SiteProbeRun.state == "running", SiteProbeRun.started_at <= now - timedelta(seconds=BATCH_SECONDS),
            ).values(state="interrupted", completed_at=now))
            if local.time().replace(tzinfo=None) < time(4, 30):
                return None
            # A conditional SQL write, not a SELECT-then-INSERT race. SQLite
            # serializes writers; this watermark is retained across pruning.
            session.execute(insert(SiteProbeSchedule).values(id=1, last_claimed_date="").on_conflict_do_nothing())
            claimed = session.execute(update(SiteProbeSchedule).where(
                SiteProbeSchedule.id == 1, SiteProbeSchedule.last_claimed_date < day,
            ).values(last_claimed_date=day))
            if claimed.rowcount != 1:
                return None
            session.add(SiteProbeRun(day=day, state="running", started_at=now,
                                    expected_count=len(self.targets) * len(SITE_PROBE_ROUTES)))
        return day

    def record(self, day, service_key, route_id, observation, observed_at):
        observed_at = _aware(observed_at)
        if service_key not in self.targets or route_id not in SITE_PROBE_ROUTES:
            raise ValueError("unknown fixed probe")
        state, delay, reason = observation.state, observation.delay_ms, observation.reason
        if state not in {"responded", "http_rejected", "failed", "unknown"} or (reason is not None and reason not in REASONS):
            raise ValueError("invalid safe probe state")
        if state in {"responded", "http_rejected"}:
            if type(delay) is not int or not 0 < delay <= 10000:
                raise ValueError("invalid measured delay")
        elif delay is not None:
            raise ValueError("failed/unknown probe cannot have measured delay")
        if state == "responded" and reason is not None or state != "responded" and reason is None:
            raise ValueError("inconsistent probe reason")
        with self.sessions.begin() as session:
            run = session.get(SiteProbeRun, day)
            if run is None or run.state != "running":
                return False
            written = session.execute(insert(SiteProbeResult).values(
                day=day, service_key=service_key, route_id=route_id, route_label=self.labels[route_id],
                url=SITE_PROBE_TARGETS[service_key].url, observed_at=observed_at,
                state=state, delay_ms=delay, reason=reason,
            ).on_conflict_do_nothing())
            return written.rowcount == 1

    def finish(self, day, state, now):
        now = _aware(now)
        if state not in {"completed", "interrupted"}:
            raise ValueError("invalid run completion")
        with self.sessions.begin() as session:
            run = session.get(SiteProbeRun, day)
            if run is None or run.state != "running":
                return
            count = session.scalar(select(func.count()).select_from(SiteProbeResult).where(SiteProbeResult.day == day))
            run.state = "completed" if state == "completed" and count == run.expected_count else "interrupted"
            run.completed_at = now

    def prune(self, now):
        with self.sessions.begin() as session:
            session.execute(delete(SiteProbeRun).where(SiteProbeRun.started_at < _aware(now) - timedelta(days=30)))

    def snapshot(self, now):
        now = _aware(now)
        with self.sessions() as session:
            run = session.scalar(select(SiteProbeRun).order_by(SiteProbeRun.day.desc()).limit(1))
            watermark = session.get(SiteProbeSchedule, 1)
            records = session.scalars(select(SiteProbeResult).order_by(SiteProbeResult.day.desc())).all()
        latest = {}
        completed = 0
        for row in records:
            latest.setdefault((row.service_key, row.route_id), row)
            completed += bool(run and row.day == run.day)
        services = []
        for key in self.targets:
            target = SITE_PROBE_TARGETS[key]
            routes = []
            for route_id in SITE_PROBE_ROUTES:
                row = latest.get((key, route_id))
                age = (now - row.observed_at).total_seconds() if row else None
                routes.append({
                    "route_id": route_id, "label": row.route_label if row else self.labels[route_id],
                    "state": row.state if row else "unknown", "delay_ms": row.delay_ms if row else None,
                    "reason": row.reason if row else "not_checked", "observed_at": row.observed_at if row else None,
                    "url": row.url if row else target.url,
                    "stale": age is not None and not 0 <= age <= 25 * 3600,
                    "current_run": bool(row and run and row.day == run.day),
                })
            services.append({"key": key, "category": target.category, "url": target.url, "routes": routes})
        local = now.astimezone(ZONE)
        next_day = local.date()
        if watermark and watermark.last_claimed_date:
            next_day = max(next_day, date.fromisoformat(watermark.last_claimed_date) + timedelta(days=1))
        next_run = max(now, datetime.combine(next_day, time(4, 30), ZONE).astimezone(UTC)) if self.enabled else None
        run_data = None
        if run:
            expired = run.state == "running" and now >= run.started_at + timedelta(seconds=BATCH_SECONDS)
            run_data = {"day": run.day, "state": "interrupted" if expired else run.state,
                        "started_at": run.started_at, "completed_at": run.completed_at,
                        "expected_count": run.expected_count, "completed_count": completed}
        return {"enabled": self.enabled, "timezone": TIMEZONE, "next_run_at": next_run,
                "run": run_data, "services": services}


class SiteProbeCollector:
    def __init__(self, mihomo, service, *, now=None):
        self.mihomo, self.service = mihomo, service
        self.now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    async def run_due(self):
        if not self.service.enabled or self._lock.locked():
            return False
        async with self._lock:
            day = await asyncio.to_thread(self.service.claim_day, self.now())
            if day is None:
                return False
            state = "interrupted"
            try:
                async with asyncio.timeout(BATCH_SECONDS):
                    for key in self.service.targets:
                        for route in SITE_PROBE_ROUTES:
                            try:
                                async with asyncio.timeout(25):
                                    observation = await self.mihomo.site_probe(route, key)
                            except TimeoutError:
                                observation = SiteProbeObservation("unknown", reason="collector_timeout")
                            except Exception:
                                observation = SiteProbeObservation("unknown", reason="collector_error")
                            if not await asyncio.to_thread(self.service.record, day, key, route, observation, self.now()):
                                return False
                            await asyncio.sleep(0.05)
                state = "completed"
            except TimeoutError:
                pass  # A bounded, partial run is not evidence that untested sites failed.
            finally:
                await asyncio.shield(asyncio.to_thread(self.service.finish, day, state, self.now()))
                await asyncio.shield(asyncio.to_thread(self.service.prune, self.now()))
            return True


def register_site_probe_jobs(scheduler, collector):
    if collector is None or not collector.service.enabled:
        return
    scheduler.add_job(collector.run_due, "interval", minutes=1, id="daily-service-probes",
                      next_run_time=datetime.now(UTC), max_instances=1, coalesce=True,
                      replace_existing=True, misfire_grace_time=30)
