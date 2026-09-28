"""Async client for the scoped worker; never connects to Docker or a TCP listener."""

import asyncio
import json
import os
import re
import stat
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

MAX_FRAME = 65536
RESOURCES = frozenset(("rules", "geodata", "clients", "strategies", "dashboard_config"))
DEFAULT_SOCKET = Path("/run/vpn-maintenance/control.sock")
_mutation_progress: ContextVar[list[bool] | None] = ContextVar("maintenance_mutation_progress", default=None)


def mark_mutation():
    """Call immediately before the first persistent write/upstream mutation."""
    progress = _mutation_progress.get()
    if progress is not None:
        progress[0] = True


def guarded_write(resource: str):
    def decorate(method):
        @wraps(method)
        async def guarded(self, *args, **kwargs):
            client = getattr(self, "_maintenance_client", None)
            if client is None:
                return await method(self, *args, **kwargs)
            async with client.write_guard(resource, track_mutations=True):
                return await method(self, *args, **kwargs)
        return guarded
    return decorate


async def acquire_thread_lock(lock):
    # A cancelled to_thread(lock.acquire) can acquire later and orphan the lock.
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.05)


class MaintenanceUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Исполнитель обслуживания недоступен; состояние операции нужно уточнить.")


class MaintenanceConflict(RuntimeError):
    pass


class MaintenanceBusy(RuntimeError):
    def __init__(self, job_id: str | None):
        super().__init__("Выполняется обслуживание или другое изменение. Повторите после завершения.")
        self.job_id = job_id if isinstance(job_id, str) and re.fullmatch(r"[0-9a-f]{32}", job_id) else None


class MaintenanceClient:
    def __init__(self, *, enabled: bool = False, socket_path: Path = DEFAULT_SOCKET, transport: Callable[[dict], Awaitable[dict]] | None = None, renew_seconds: float = 15):
        self.enabled, self.socket_path = enabled, socket_path
        self._transport = transport or self._unix_request
        self.renew_seconds = renew_seconds

    async def _unix_request(self, frame: dict) -> dict:
        path = self.socket_path
        if path != DEFAULT_SOCKET or any(item.is_symlink() for item in (path, *path.parents)):
            raise MaintenanceUnavailable()
        info = path.stat()
        if os.name != "posix" or not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_gid != 10001 or stat.S_IMODE(info.st_mode) != 0o660:
            raise MaintenanceUnavailable()
        parent = path.parent.stat()
        if parent.st_uid != 0 or parent.st_gid != 10001 or stat.S_IMODE(parent.st_mode) != 0o750:
            raise MaintenanceUnavailable()
        payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        if len(payload) > MAX_FRAME:
            raise MaintenanceUnavailable()
        reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_FRAME)
        try:
            writer.write(payload)
            await writer.drain()
            raw = await reader.readuntil(b"\n")
            if len(raw) > MAX_FRAME:
                raise MaintenanceUnavailable()
            return json.loads(raw)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def request(self, method: str, **params) -> Any:
        if not self.enabled:
            raise MaintenanceUnavailable()
        try:
            async with asyncio.timeout(3):
                response = await self._transport({"version": 1, "method": method, "params": params})
        except (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            raise MaintenanceUnavailable() from None
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            raise MaintenanceUnavailable()
        if response["ok"]:
            return response.get("result")
        if response.get("error") == "busy":
            raise MaintenanceBusy(response.get("job_id"))
        if response.get("error") in {"conflict", "invalid_request"} or (response.get("error") == "not_configured" and method == "submit"):
            raise MaintenanceConflict("Операция не соответствует текущему состоянию.")
        raise MaintenanceUnavailable()

    async def submit(self, job_id: str, request: dict, actor: str):
        return await self.request("submit", job_id=job_id, request=request, actor=actor)

    async def get_job(self, job_id: str):
        return await self.request("job", job_id=job_id)

    async def cancel(self, job_id: str, actor: str):
        return await self.request("cancel", job_id=job_id, actor=actor)

    @asynccontextmanager
    async def write_guard(self, resource: str, *, track_mutations: bool = False):
        if resource not in RESOURCES:
            raise ValueError("unknown write resource")
        if not self.enabled:
            yield
            return
        result = await self.request("writer_acquire", resource=resource)
        lease = result.get("lease_id") if isinstance(result, dict) else None
        if not isinstance(lease, str) or not re.fullmatch(r"[0-9a-f]{32}", lease):
            raise MaintenanceUnavailable()
        owner = asyncio.current_task()
        lost = False
        complete = False
        progress = [not track_mutations]
        progress_token = _mutation_progress.set(progress)

        async def renew():
            nonlocal lost
            try:
                while True:
                    await asyncio.sleep(self.renew_seconds)
                    await self.request("writer_renew", lease_id=lease)
            except (MaintenanceUnavailable, MaintenanceConflict, MaintenanceBusy):
                lost = True
                owner.cancel()

        renewal = asyncio.create_task(renew())
        try:
            yield
            if lost:
                owner.uncancel()
                raise MaintenanceUnavailable()
            complete = True
        except asyncio.CancelledError:
            if lost:
                owner.uncancel()  # Consume only the cancellation initiated by our renewal task.
                raise MaintenanceUnavailable() from None
            raise
        finally:
            _mutation_progress.reset(progress_token)
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
            if not lost:
                # A partial/failed writer is reconciled, not optimistically unlocked.
                clean = complete or not progress[0]
                released = await self.request("writer_release", lease_id=lease, outcome="complete" if clean else "uncertain")
                if clean and (not isinstance(released, dict) or released.get("released") is not True):
                    raise MaintenanceUnavailable()
