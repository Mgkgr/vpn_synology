"""Private Kuma Push API, bounded retries and honest delivery metadata.

Push API has no idempotency key. A lost response can cause a rare duplicate
notification; successful HTTP acknowledgement cannot prove Telegram delivery.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
from stat import S_IMODE
from time import monotonic
from typing import Literal

import httpx
from sqlalchemy import select, text

from app.models import NotificationDeliveryState, OutboundIncident
from app.outbound_health import HealthService


MIN_PUSH_INTERVAL_SECONDS = 60
_TOKEN = re.compile(r"[A-Za-z0-9_-]{10,128}\Z")
_SECRET_REQUEST = ContextVar("kuma_secret_request", default=False)


class _HidePushTransportLogs(logging.Filter):
    def filter(self, record):
        return not _SECRET_REQUEST.get()


_LOG_FILTER = _HidePushTransportLogs()


class KumaPushError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in {"request_failed", "not_accepted", "invalid_response", "invalid_request"} else "request_failed"
        super().__init__(self.code)


def load_push_tokens(path: Path | None, outbound_ids: tuple[str, ...]) -> tuple[dict[str, str], str | None]:
    if path is None:
        return {}, None
    try:
        metadata = path.stat()
        if path.is_symlink() or not path.is_file() or metadata.st_size > 8192 or (os.name != "nt" and S_IMODE(metadata.st_mode) != 0o600):
            return {}, "tokens_invalid"
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not set(outbound_ids) | {"collector"} <= set(value) or not set(value) <= {"WG-IMP", "HY2-USA", "ANTIDPI", "collector"}:
            return {}, "tokens_invalid"
        if any(not isinstance(token, str) or not _TOKEN.fullmatch(token) for token in value.values()) or len(set(value.values())) != len(value):
            return {}, "tokens_invalid"
        return value, None
    except (OSError, UnicodeError, ValueError):
        return {}, "tokens_unavailable"


class KumaPushClient:
    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        try:
            url = httpx.URL(base_url)
            if url.scheme not in ("http", "https") or not url.host or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
                raise ValueError
        except (ValueError, TypeError):
            raise KumaPushError("invalid_request") from None
        self._url, self._transport = url, transport
        # HTTPX INFO logs contain the token in the URL, and httpcore DEBUG can
        # include a reflected Location header. Suppression is task-local, not a
        # process-wide log level change; other requests retain their diagnostics.
        for name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
            logger = logging.getLogger(name)
            if _LOG_FILTER not in logger.filters:
                logger.addFilter(_LOG_FILTER)

    async def publish(self, token: str, *, status: Literal["up", "down"], message: str) -> None:
        if not _TOKEN.fullmatch(token) or status not in ("up", "down") or len(message) > 1024:
            raise KumaPushError("invalid_request")
        scope = _SECRET_REQUEST.set(True)
        try:
            async with asyncio.timeout(10), httpx.AsyncClient(
                timeout=5.0, trust_env=False, verify=True, follow_redirects=False, transport=self._transport,
            ) as client:
                async with client.stream("GET", self._url.copy_with(path="/api/push/" + token), params={"status": status, "msg": message}) as response:
                    if response.status_code != 200:
                        raise KumaPushError("not_accepted")
                    body = bytearray()
                    async for part in response.aiter_bytes():
                        body.extend(part)
                        if len(body) > 8192:
                            raise KumaPushError("invalid_response")
                    try:
                        accepted = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise KumaPushError("invalid_response") from None
                    if not isinstance(accepted, dict) or accepted.get("ok") is not True:
                        raise KumaPushError("not_accepted")
        except (httpx.HTTPError, TimeoutError):
            # Never retain a request/response, URL, or chained raw exception.
            raise KumaPushError("request_failed") from None
        finally:
            _SECRET_REQUEST.reset(scope)


class KumaPublisher:
    def __init__(self, sessions, health: HealthService, client: KumaPushClient, tokens: dict[str, str], configuration_error: str | None = None, *, monotonic_clock=monotonic):
        self.sessions, self.health, self.client = sessions, health, client
        self._tokens = dict(tokens)
        self.configuration_error = configuration_error
        self.enabled = bool(tokens) and {entry.id for entry in health.registry} | {"collector"} <= set(tokens)
        self._lock = asyncio.Lock()
        self._monotonic = monotonic_clock

    async def run_once(self, now: datetime | None = None) -> None:
        if not self.enabled or self._lock.locked():
            return
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("publisher time must be aware")
        async with self._lock:
            started = self._monotonic()
            current_time = lambda: now + timedelta(seconds=max(0.0, self._monotonic() - started))
            snapshots = await asyncio.to_thread(self.health.snapshot, now)
            known = all(item.state != "unknown" for item in snapshots)
            collector = ("up", "collector:ok", "OBSERVATION_RESUMED collector") if known else ("down", "collector:unknown", "MONITORING_UNKNOWN collector")
            await self._publish_one("collector", collector, current_time)
            labels = {entry.id: entry.label for entry in self.health.registry}
            for snapshot in snapshots:
                desired = await asyncio.to_thread(self._desired, snapshot, labels, now)
                if desired is not None:
                    await self._publish_one(snapshot.outbound, desired, current_time)

    def _desired(self, snapshot, labels, now):
        label = labels[snapshot.outbound]
        fallback = labels.get(snapshot.selected_fallback, "unknown")
        if snapshot.incident_id is not None:
            duration = max(0, int((now - snapshot.incident_started_at).total_seconds()))
            code = "MONITORING_UNKNOWN" if snapshot.state == "unknown" else "INCIDENT_DOWN"
            message = f"{code} {label}; first_failed_at={snapshot.incident_started_at.isoformat()}; duration_seconds={duration}; fallback={fallback}"
            return "down", f"down:{snapshot.incident_id}", message
        if snapshot.state == "unknown" or snapshot.last_success_at is None:
            # Do not fabricate an initial UP or recovery. Collector monitor
            # carries the missing-observation signal; this monitor may time out.
            return None
        with self.sessions() as session:
            incident = session.scalar(select(OutboundIncident).where(
                OutboundIncident.outbound == snapshot.outbound, OutboundIncident.recovered_at.is_not(None),
            ).order_by(OutboundIncident.recovered_at.desc()).limit(1))
            delivery = session.get(NotificationDeliveryState, snapshot.outbound)
            revision = f"recovered:{incident.id}" if incident is not None else "observation:ok"
            if incident is not None and (delivery is None or delivery.accepted_revision != revision):
                duration = int((incident.recovered_at - incident.first_failed_at).total_seconds())
                message = f"INCIDENT_RECOVERED {label}; duration_seconds={duration}; recovered_at={incident.recovered_at.isoformat()}; fallback={fallback}"
            else:
                message = f"OBSERVATION_OK {label}; state={snapshot.state}; fallback={fallback}"
        return "up", revision, message

    def _claim(self, key, revision, now):
        with self.sessions() as session, session.begin():
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(NotificationDeliveryState, key)
            if row is None:
                row = NotificationDeliveryState(monitor_key=key, failures=0)
                session.add(row)
            row.pending_revision = revision
            if row.next_attempt_at is not None and now < row.next_attempt_at:
                return False
            row.last_attempt_at = now
            row.next_attempt_at = now + timedelta(seconds=MIN_PUSH_INTERVAL_SECONDS)
            return True

    async def _publish_one(self, key, desired, current_time):
        status, revision, message = desired
        now = current_time()
        if not await asyncio.to_thread(self._claim, key, revision, now):
            return
        error_code = None
        try:
            await self.client.publish(self._tokens[key], status=status, message=message)
        except KumaPushError as error:
            error_code = error.code
        except Exception:
            error_code = "request_failed"
        await asyncio.to_thread(self._finish, key, revision, current_time(), error_code)

    def _finish(self, key, revision, now, error_code):
        with self.sessions() as session, session.begin():
            row = session.get(NotificationDeliveryState, key)
            row.last_error_code = error_code
            if error_code is None:
                row.accepted_revision = revision
                row.last_accepted_at = now
                row.failures = 0
                row.next_attempt_at = now + timedelta(seconds=MIN_PUSH_INTERVAL_SECONDS)
            else:
                row.failures += 1
                delay = (60, 120, 300)[min(row.failures - 1, 2)]
                row.next_attempt_at = now + timedelta(seconds=delay)


def register_kuma_jobs(scheduler, publisher: KumaPublisher | None) -> None:
    if publisher is not None and publisher.enabled:
        scheduler.add_job(publisher.run_once, "cron", second=55, id="outbound-health-publisher", max_instances=1, coalesce=True, replace_existing=True, misfire_grace_time=5)
