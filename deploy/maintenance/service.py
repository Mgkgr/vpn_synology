"""Private Unix control socket; jobs are committed before a response is sent."""

import os
import socket
import sqlite3
import stat
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .protocol import MAX_FRAME, ProtocolError, canonical, decode_frame, parse_request
from .store import JobBusy, JobConflict
from .strategy_catalog import StrategyRequest
from .strategy_runner import StrategyUnavailable
from .strategy_state import StrategyConflict


class ServiceError(RuntimeError):
    pass


def verify_socket_parent(path: Path):
    if path.name != "control.sock" or any(item.is_symlink() for item in (path, *path.parents)):
        raise ServiceError("unsafe_socket_path")
    info = path.parent.stat()
    if os.name != "posix" or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 10001 or stat.S_IMODE(info.st_mode) != 0o750:
        raise ServiceError("unsafe_socket_directory")
    if path.exists():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
            raise ServiceError("unsafe_socket_file")


@contextmanager
def process_lock(private_path: Path):
    import fcntl  # Linux only; the Windows tests use the frame handler.
    path = private_path / "worker.lock"
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ServiceError("unsafe_worker_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ServiceError("worker_already_running") from None
        yield
    finally:
        os.close(fd)


class MaintenanceService:
    def __init__(self, store, inventory=None, queue_release_check=None, executor_ready=None, strategies=None):
        self.store = store
        self.inventory = inventory
        self.queue_release_check = queue_release_check
        self.executor_ready = executor_ready or (lambda: False)
        self.strategies = strategies

    def handle(self, raw, peer_uid):
        try:
            frame = decode_frame(raw, peer_uid)
            method, params = frame["method"], frame["params"]
            if method == "submit":
                parsed = parse_request(params['request'])
                if isinstance(parsed, StrategyRequest):
                    if self.strategies is None:
                        return {'ok':False,'error':'not_configured'}
                    return {'ok':True,'result':self.strategies.submit(params['job_id'],parsed,params['actor'])}
                if self.executor_ready() is not True:
                    return {"ok": False, "error": "not_configured"}
                result = asdict(self.store.submit(params["job_id"], parse_request(params["request"]), params["actor"]))
            elif method == "cancel":
                result = asdict(self.store.cancel(params["job_id"]))
            elif method == "job":
                result = self.strategies.job(params['job_id']) if self.strategies else asdict(self.store.get_job(params["job_id"]))
            elif method == 'strategy_snapshot' and self.strategies:
                result = self.strategies.snapshot()
            elif method == "jobs":
                result = [asdict(job) for job in self.store.jobs()]
            elif method == "writer_acquire":
                result = {"lease_id": self.store.acquire_writer(params["resource"])}
            elif method == "writer_renew":
                self.store.renew_writer(params["lease_id"])
                result = {"renewed": True}
            elif method == "writer_release":
                result = {"released": self.store.release_writer(params["lease_id"], params["outcome"])}
            elif method == "components" and self.inventory:
                result = self.inventory()  # Cached bounded safe snapshot, never Docker inspect on this connection.
                result = {**result, "execution_ready": self.executor_ready() is True}
            elif method == "check_releases" and self.queue_release_check:
                result = self.queue_release_check()  # Enqueue only; HTTP checks have their own bounded worker.
            else:
                return {"ok": False, "error": "not_configured"}
            return {"ok": True, "result": result}
        except JobBusy as error:
            return {"ok": False, "error": "busy", "job_id": error.job_id}
        except (JobConflict, StrategyConflict):
            return {"ok": False, "error": "conflict"}
        except StrategyUnavailable:
            return {'ok':False,'error':'not_configured'}
        except (ProtocolError, ValueError, TypeError, KeyError, RecursionError):
            return {"ok": False, "error": "invalid_request"}
        except (OSError, sqlite3.Error):
            return {"ok": False, "error": "storage_unavailable"}


class UnixServer:
    def __init__(self, service: MaintenanceService, socket_path: Path):
        self.service, self.path = service, socket_path

    def _connection(self, connection):
        with connection:
            deadline = time.monotonic() + 3
            try:
                uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
                if uid not in (0, 10001):
                    return
                raw = bytearray()
                while b"\n" not in raw and len(raw) <= MAX_FRAME:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    connection.settimeout(remaining)
                    chunk = connection.recv(min(8192, MAX_FRAME + 1 - len(raw)))
                    if not chunk:
                        return
                    raw.extend(chunk)
                response = canonical(self.service.handle(bytes(raw), uid)) + b"\n"
                if len(response) > MAX_FRAME:
                    response = b'{"ok":false,"error":"response_too_large"}\n'
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    connection.settimeout(remaining)
                    connection.sendall(response)
            except (OSError, ValueError, TypeError):
                return  # Never log request bodies, paths, SQLite or peer errors.

    def serve(self, stop: threading.Event, ready=None):
        if os.name != "posix" or not hasattr(socket, "SO_PEERCRED") or os.geteuid() != 0:
            raise ServiceError("linux_root_required")
        verify_socket_parent(self.path)
        with process_lock(self.service.store.path.parent):
            self.service.store.recover_after_restart()
            if self.path.exists():
                self.path.unlink()  # Only the validated stale socket, after acquiring the process lock.
            capacity = threading.BoundedSemaphore(8)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener, ThreadPoolExecutor(max_workers=4) as pool:
                listener.bind(str(self.path))
                os.chown(self.path, 0, 10001)
                os.chmod(self.path, 0o660)
                inode = self.path.stat().st_ino
                listener.listen(8)
                listener.settimeout(0.5)
                if ready:
                    ready.set()
                try:
                    while not stop.is_set():
                        try:
                            connection, _ = listener.accept()
                        except socket.timeout:
                            continue
                        if not capacity.acquire(blocking=False):
                            connection.close()
                            continue
                        future = pool.submit(self._connection, connection)
                        future.add_done_callback(lambda _: capacity.release())
                finally:
                    if self.path.exists() and not self.path.is_symlink() and self.path.stat().st_ino == inode:
                        self.path.unlink()
