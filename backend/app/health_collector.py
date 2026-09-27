"""Bounded independent control checks, separate from the editable diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from app.mihomo import CONTROL_ENDPOINTS, MihomoClient, MihomoIntegrationError, ProxyGroup
from app.outbound_health import ControlCheck, ControlCycle, HealthService
from app.outbounds import FALLBACK_MEMBERS, OutboundDefinition


class HealthCollector:
    def __init__(self, mihomo: MihomoClient, service: HealthService, *, now: Callable[[], datetime] | None = None):
        self.mihomo, self.service = mihomo, service
        self.now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    async def run_cycle(self) -> bool:
        if self._lock.locked():
            return False
        async with self._lock:
            started = self.now()
            slot = started.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
            try:
                async with asyncio.timeout(50):
                    results, selected = await self._observe()
            except asyncio.CancelledError:
                # Shutdown isn't evidence of an outage. No partial cycle is saved.
                raise
            except Exception:
                results, selected = {entry.id: self._unknown("collector_error") for entry in self.service.registry}, None
            completed = self.now()
            for entry in self.service.registry:
                checks = results[entry.id]
                successes = sum(check.succeeded is True for check in checks)
                if any(check.succeeded is None for check in checks):
                    result = "unknown"
                else:
                    result = "healthy" if successes == 3 else "degraded" if successes == 2 else "failed"
                cycle = ControlCycle(
                    entry.id, slot, completed, result, successes, selected,
                    tuple(sorted({check.reason for check in checks if check.reason})), checks,
                )
                await asyncio.to_thread(self.service.record_cycle, cycle)
            return True

    async def _observe(self):
        try:
            groups = await self.mihomo.groups()
        except MihomoIntegrationError:
            return {entry.id: self._unknown("controller_unavailable") for entry in self.service.registry}, None
        selected = next((group.now for group in groups if group.name == "VPS-FALLBACK" and group.now in FALLBACK_MEMBERS), None)
        semaphore = asyncio.Semaphore(3)
        results = {}

        async def observe(entry):
            if not self._valid_group(groups, entry):
                results[entry.id] = self._unknown("probe_group_invalid")
                return
            results[entry.id] = tuple(await asyncio.gather(*(
                self._check(entry.probe_name, key, semaphore) for key in CONTROL_ENDPOINTS
            )))

        await asyncio.gather(*(observe(entry) for entry in self.service.registry))
        if any(check.succeeded is not True for checks in results.values() for check in checks):
            # A failed controller transaction must not condemn every exit. Repeat
            # one bounded read after failures, never PUT or group-members /delay.
            try:
                after = await self.mihomo.groups()
            except MihomoIntegrationError:
                return {entry.id: self._unknown("controller_unavailable") for entry in self.service.registry}, selected
            for entry in self.service.registry:
                if not self._valid_group(after, entry):
                    results[entry.id] = self._unknown("probe_group_invalid")
        return results, selected

    async def _check(self, probe_name, key, semaphore):
        async with semaphore:
            try:
                delay = await self.mihomo.control_delay(probe_name, key)
                return ControlCheck(key, True, delay.delay_ms)
            except MihomoIntegrationError as error:
                # Pinned Mihomo returns 503/504 specifically for a completed or
                # timed-out outbound test. HTTP/auth/transport errors are unknown.
                if error.status_code in (503, 504):
                    return ControlCheck(key, False, reason="probe_timeout" if error.status_code == 504 else "probe_failed")
                return ControlCheck(key, None, reason="controller_unavailable")

    @staticmethod
    def _valid_group(groups: list[ProxyGroup], entry: OutboundDefinition) -> bool:
        matches = [group for group in groups if group.name == entry.probe_name]
        return len(matches) == 1 and matches[0].type == "Selector" and matches[0].proxies == (entry.id,) and matches[0].now == entry.id

    @staticmethod
    def _unknown(reason: str) -> tuple[ControlCheck, ...]:
        return tuple(ControlCheck(key, None, reason=reason) for key in CONTROL_ENDPOINTS)

    async def prune(self) -> None:
        await asyncio.to_thread(self.service.prune, self.now())


def register_health_jobs(scheduler, collector: HealthCollector | None) -> None:
    if collector is None:
        return
    scheduler.add_job(
        collector.run_cycle, "cron", second=0, id="outbound-health-control",
        max_instances=1, coalesce=True, replace_existing=True, misfire_grace_time=5,
    )
    scheduler.add_job(
        collector.prune, "interval", hours=24, id="outbound-health-retention",
        max_instances=1, coalesce=True, replace_existing=True,
    )
