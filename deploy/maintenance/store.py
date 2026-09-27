"""Durable SQLite intents and a shared mutation lock. No Docker operations here."""

import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .protocol import RESOURCES, MaintenanceRequest, canonical, parse_request, request_hash, require_id

TERMINAL = frozenset(("completed", "failed", "cancelled"))
CANCELLABLE = frozenset(("queued", "preflight", "backup", "download"))
TRANSITIONS = {"queued": {"preflight", "cancelled", "failed"}, "preflight": {"backup", "download", "apply", "failed", "cancelled"}, "backup": {"download", "apply", "failed", "cancelled"}, "download": {"apply", "failed", "cancelled"}, "apply": {"verify", "rollback", "failed"}, "verify": {"completed", "rollback", "failed"}, "rollback": {"verify", "failed"}, "needs_reconcile": set()}


class JobConflict(RuntimeError):
    pass


class JobBusy(RuntimeError):
    def __init__(self, job_id=None):
        super().__init__("busy")
        self.job_id = job_id


@dataclass(frozen=True)
class JobView:
    job_id: str
    request_hash: str
    actor: str
    phase: str
    component: Optional[str]
    created_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    revision_before: str
    revision_after: Optional[str]
    maintenance_until: Optional[float]
    error_code: Optional[str]
    cancel_allowed: bool
    cancel_requested: bool


def _view(row):
    data = dict(row)
    data.pop("request_json")
    data["cancel_requested"] = bool(data["cancel_requested"])
    data["cancel_allowed"] = data["phase"] in CANCELLABLE and not data["cancel_requested"]
    return JobView(**data)


class JobStore:
    def __init__(self, path: Path):
        self.path = path
        for candidate in (path, *path.parents):
            if candidate.is_symlink():
                raise JobConflict("unsafe_state_path")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            info = path.parent.stat()
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
                raise JobConflict("unsafe_private_directory")
        if not path.exists():
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, actor TEXT NOT NULL,
                    request_json TEXT NOT NULL, phase TEXT NOT NULL, component TEXT,
                    created_at REAL NOT NULL, started_at REAL, finished_at REAL,
                    revision_before TEXT NOT NULL, revision_after TEXT, maintenance_until REAL,
                    error_code TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS mutation_lock (
                    slot INTEGER PRIMARY KEY CHECK(slot=1), kind TEXT NOT NULL, token TEXT NOT NULL,
                    resource TEXT, state TEXT NOT NULL, expires REAL, observed REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effects (
                    job_id TEXT NOT NULL, step TEXT NOT NULL, state TEXT NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, PRIMARY KEY(job_id, step), FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    phase TEXT NOT NULL, observed_at REAL NOT NULL, code TEXT
                );
            """)

    def _connect(self):
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if path.is_symlink():
                raise JobConflict("unsafe_state_file")
            if path.exists():
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o077)):
                    raise JobConflict("unsafe_state_file")
        db = sqlite3.connect(str(self.path), timeout=0.5, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            return db
        except BaseException:
            db.close()
            raise

    def _commit(self, db):
        db.commit()

    @contextmanager
    def _write(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            self._commit(db)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _lock(self, db, now):
        row = db.execute("SELECT * FROM mutation_lock WHERE slot=1").fetchone()
        if row and row["kind"] == "writer" and (now >= row["expires"] or now < row["observed"]):
            db.execute("UPDATE mutation_lock SET state='needs_reconcile' WHERE slot=1")
            row = db.execute("SELECT * FROM mutation_lock WHERE slot=1").fetchone()
        return row

    def lock_state(self, now=None):
        now = time.time() if now is None else now
        with self._write() as db:
            row = self._lock(db, now)
            return dict(row) if row else {"state": "free"}

    def submit(self, job_id, request: MaintenanceRequest, actor, now=None):
        require_id(job_id)
        request = parse_request(asdict(request))
        digest = request_hash(request)
        now = time.time() if now is None else now
        # Persist expired lease state independently of a later busy exception.
        self.lock_state(now)
        with self._write() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row:
                if row["request_hash"] != digest or row["actor"] != actor:
                    raise JobConflict("job_id_conflict")
                return _view(row)
            lock = self._lock(db, now)
            if lock:
                raise JobBusy(lock["token"] if lock["kind"] == "job" else None)
            db.execute("INSERT INTO jobs(job_id,request_hash,actor,request_json,phase,created_at,revision_before) VALUES(?,?,?,?,?,?,?)", (job_id, digest, actor, canonical(asdict(request)).decode("utf-8"), "queued", now, request.expected_revision))
            db.execute("INSERT INTO mutation_lock(slot,kind,token,state,observed) VALUES(1,'job',?,'held',?)", (job_id, now))
            self._event(db, job_id, "queued", now)
            return _view(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def get_job(self, job_id):
        require_id(job_id)
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise JobConflict("job_not_found")
            return _view(row)
        finally:
            db.close()

    def jobs(self):
        db = self._connect()
        try:
            return tuple(_view(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC, job_id LIMIT 50"))
        finally:
            db.close()

    def get_request(self, job_id):
        require_id(job_id)
        db = self._connect()
        try:
            row = db.execute("SELECT request_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise JobConflict("job_not_found")
            return parse_request(json.loads(row[0]))
        finally:
            db.close()

    def _event(self, db, job_id, phase, now, code=None):
        db.execute("INSERT INTO events(job_id,phase,observed_at,code) VALUES(?,?,?,?)", (job_id, phase, now, code))

    def set_phase(self, job_id, phase, now=None, error_code=None, revision_after=None):
        now = time.time() if now is None else now
        with self._write() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or phase not in TRANSITIONS.get(row["phase"], set()):
                raise JobConflict("invalid_phase_transition")
            if phase == "apply" and row["cancel_requested"]:
                raise JobConflict("cancel_requested")
            if phase in TERMINAL and db.execute("SELECT 1 FROM effects WHERE job_id=? AND state='intent'", (job_id,)).fetchone():
                raise JobConflict("unfinished_effect")
            db.execute("UPDATE jobs SET phase=?,error_code=?,revision_after=COALESCE(?,revision_after),started_at=COALESCE(started_at,?),finished_at=? WHERE job_id=?", (phase, error_code, revision_after, now, now if phase in TERMINAL else None, job_id))
            self._event(db, job_id, phase, now, error_code)
            if phase in TERMINAL:
                db.execute("DELETE FROM mutation_lock WHERE kind='job' AND token=?", (job_id,))
        return self.get_job(job_id)

    def begin_effect(self, job_id, step, now=None):
        now = time.time() if now is None else now
        with self._write() as db:
            job = db.execute("SELECT phase FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job or job["phase"] not in ("apply", "rollback"):
                raise JobConflict("effect_not_allowed")
            row = db.execute("SELECT state FROM effects WHERE job_id=? AND step=?", (job_id, step)).fetchone()
            if row:
                if row[0] != "complete":
                    raise JobConflict("effect_needs_reconcile")
                return False
            db.execute("INSERT INTO effects(job_id,step,state,started_at) VALUES(?,?,'intent',?)", (job_id, step, now))
            return True

    def finish_effect(self, job_id, step, now=None):
        now = time.time() if now is None else now
        with self._write() as db:
            changed = db.execute("UPDATE effects SET state='complete',finished_at=? WHERE job_id=? AND step=? AND state='intent'", (now, job_id, step)).rowcount
            if not changed:
                raise JobConflict("effect_not_pending")

    def recover_after_restart(self, now=None):
        now = time.time() if now is None else now
        with self._write() as db:
            rows = db.execute("SELECT job_id FROM jobs WHERE phase NOT IN ('queued','completed','failed','cancelled','needs_reconcile')").fetchall()
            for row in rows:
                db.execute("UPDATE jobs SET phase='needs_reconcile',error_code='worker_interrupted' WHERE job_id=?", (row[0],))
                self._event(db, row[0], "needs_reconcile", now, "worker_interrupted")
            db.execute("UPDATE mutation_lock SET state='needs_reconcile' WHERE kind='writer' OR token IN (SELECT job_id FROM jobs WHERE phase='needs_reconcile')")

    def cancel(self, job_id, now=None):
        now = time.time() if now is None else now
        with self._write() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["phase"] not in CANCELLABLE:
                raise JobConflict("cancel_not_allowed")
            if row["phase"] == "queued":
                db.execute("UPDATE jobs SET phase='cancelled',finished_at=?,cancel_requested=1 WHERE job_id=?", (now, job_id))
                db.execute("DELETE FROM mutation_lock WHERE kind='job' AND token=?", (job_id,))
                self._event(db, job_id, "cancelled", now)
            else:
                db.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (job_id,))
        return self.get_job(job_id)

    def acquire_writer(self, resource, now=None):
        if resource not in RESOURCES:
            raise JobConflict("invalid_resource")
        now = time.time() if now is None else now
        self.lock_state(now)
        with self._write() as db:
            lock = self._lock(db, now)
            if lock:
                raise JobBusy(lock["token"] if lock["kind"] == "job" else None)
            lease = uuid.uuid4().hex
            db.execute("INSERT INTO mutation_lock VALUES(1,'writer',?,?,'held',?,?)", (lease, resource, now + 90, now))
            return lease

    def renew_writer(self, lease_id, now=None):
        require_id(lease_id)
        now = time.time() if now is None else now
        self.lock_state(now)
        with self._write() as db:
            lock = self._lock(db, now)
            if not lock or lock["kind"] != "writer" or lock["token"] != lease_id or lock["state"] != "held":
                raise JobConflict("lease_lost")
            db.execute("UPDATE mutation_lock SET expires=?,observed=? WHERE slot=1", (now + 90, now))

    def release_writer(self, lease_id, outcome, now=None):
        require_id(lease_id)
        now = time.time() if now is None else now
        with self._write() as db:
            lock = self._lock(db, now)
            if not lock or lock["kind"] != "writer" or lock["token"] != lease_id:
                raise JobConflict("lease_lost")
            if outcome == "complete" and lock["state"] == "held":
                db.execute("DELETE FROM mutation_lock WHERE slot=1")
                return True
            else:
                db.execute("UPDATE mutation_lock SET state='needs_reconcile' WHERE slot=1")
                return False

    def prune(self, now=None):
        now = time.time() if now is None else now
        with self._write() as db:
            args = (now - 365 * 86400,)
            query = "SELECT job_id FROM jobs WHERE phase IN ('completed','failed','cancelled') AND finished_at < ?"
            for table in ("events", "effects"):
                db.execute("DELETE FROM " + table + " WHERE job_id IN (" + query + ")", args)
            db.execute("DELETE FROM jobs WHERE job_id IN (" + query + ")", args)
