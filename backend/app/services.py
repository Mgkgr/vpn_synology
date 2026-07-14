"""Read-only HTTP health probes for dashboard-adjacent services."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from time import perf_counter

import httpx


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    name: str
    healthy: bool
    reason: str
    latency_ms: int
    status_code: int | None


class ServiceProbe:
    """Check configured HTTP endpoints without Docker, controller writes, or redirects."""

    def __init__(
        self,
        *,
        wgeasy_url: str,
        mihomo_url: str,
        metacubexd_url: str,
        kuma_url: str,
        mihomo_secret: str | None = None,
        redirect_statuses: Collection[int] = (),
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._wgeasy_url = wgeasy_url
        self._mihomo_version_url = f"{mihomo_url.rstrip('/')}/version"
        self._metacubexd_url = metacubexd_url
        self._kuma_url = kuma_url
        self._mihomo_secret = mihomo_secret.strip() if mihomo_secret and mihomo_secret.strip() else None
        self._redirect_statuses = frozenset(
            status for status in redirect_statuses if isinstance(status, int) and not isinstance(status, bool) and 300 <= status < 400
        )
        self._timeout = timeout
        self._transport = transport

    async def check_all(self) -> list[ServiceStatus]:
        """Return one result per configured endpoint in stable UI display order."""

        return [
            # wg-easy deliberately sends unauthenticated root requests to its
            # login screen.  That redirect proves its UI process is alive.
            await self._check("wg-easy", self._wgeasy_url, accepted_redirects={302}),
            await self._check("mihomo", self._mihomo_version_url, mihomo=True),
            await self._check("metacubexd", self._metacubexd_url),
            await self._check("kuma", self._kuma_url),
        ]

    async def _check(
        self,
        name: str,
        url: str,
        *,
        mihomo: bool = False,
        accepted_redirects: Collection[int] = (),
    ) -> ServiceStatus:
        headers = {"Authorization": f"Bearer {self._mihomo_secret}"} if mihomo and self._mihomo_secret else {}
        started_at = perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            return ServiceStatus(name, False, "request timed out", self._latency_ms(started_at), None)
        except httpx.HTTPError:
            return ServiceStatus(name, False, "request failed", self._latency_ms(started_at), None)

        status_code = response.status_code
        latency_ms = self._latency_ms(started_at)
        if mihomo and status_code == 401:
            reason = "authentication rejected" if self._mihomo_secret else "authentication required but no Mihomo secret is configured"
            return ServiceStatus(name, False, reason, latency_ms, status_code)
        if 200 <= status_code < 300:
            return ServiceStatus(name, True, f"HTTP {status_code}", latency_ms, status_code)
        if status_code in self._redirect_statuses or status_code in accepted_redirects:
            return ServiceStatus(name, True, f"accepted redirect HTTP {status_code}", latency_ms, status_code)
        return ServiceStatus(name, False, f"unexpected HTTP status {status_code}", latency_ms, status_code)

    @staticmethod
    def _latency_ms(started_at: float) -> int:
        return max(0, round((perf_counter() - started_at) * 1000))
