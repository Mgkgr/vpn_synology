"""Private consistent snapshots; restore only into a fresh, bounded workspace.

Sources are supplied by root-verified discovery, never by an HTTP request. An
unknown database format is a stop condition, not permission to copy a live DB.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Optional, Tuple

from .protocol import COMPONENTS, REVISION, canonical, require_id

MAX_FILES = 2000
MAX_BYTES = 4 * 1024 ** 3
MAX_MANIFEST = 2 * 1024 ** 2
SOURCE_ROOTS = tuple(Path(item) for item in (
    "/volume1/docker/vpn-gateway", "/volume1/docker/vpn-dashboard/deploy",
    "/volume1/docker/vpn-antidpi", "/volume1/docker/vpn-dashboard-maintenance/private",
))
REQUIRED_ROLES = frozenset(("wg_identity", "config", "dashboard_secrets", "worker_state"))


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupSource:
    name: str
    path: Path
    kind: str  # file or sqlite, assigned only after root discovery.
    role: str
    component: str


@dataclass(frozen=True)
class BackupSet:
    snapshot_id: str
    component_ids: Tuple[str, ...]
    captured_revision: str
    image_digests: dict
    database_schemas: dict
    verified_at: Optional[float]


def safe_name(name):
    if not isinstance(name, str) or not 1 <= len(name) <= 240 or not re.fullmatch(r"[A-Za-z0-9_./-]+", name):
        raise BackupError("invalid_archive_member")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in (".", "..", "") for part in name.split("/")) or path.as_posix() != name:
        raise BackupError("invalid_archive_member")
    return name


def no_symlink(path):
    if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
        raise BackupError("unsafe_backup_path")


def private_directory(path):
    no_symlink(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700)):
        raise BackupError("backup_directory_not_private")


@contextmanager
def private_workspace(parent, prefix):
    private_directory(parent)
    directory = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=str(parent)))
    try:
        yield directory
    finally:
        # Only the directory we created, never a request/env-supplied target.
        no_symlink(parent)
        if directory.parent != parent or not directory.name.startswith(prefix + "-"):
            raise BackupError("unsafe_cleanup_target")
        if directory.is_symlink():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(str(directory))


def _private_file(path):
    no_symlink(path.parent)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_schema(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("sqlite_integrity_failed")
        rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        return hashlib.sha256(canonical(rows)).hexdigest()


def online_sqlite_backup(source, destination, limit=MAX_BYTES):
    no_symlink(source)
    os.close(_private_file(destination))
    deadline = time.monotonic() + 60
    def progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise BackupError("sqlite_backup_timeout")
        if destination.stat().st_size > limit:
            raise BackupError("backup_size_limit")
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as origin, closing(sqlite3.connect(str(destination))) as target:
            origin.backup(target, pages=128, progress=progress, sleep=0.05)
            target.execute("PRAGMA journal_mode=DELETE")
        return sqlite_schema(destination)
    except sqlite3.Error:
        raise BackupError("sqlite_backup_failed") from None


def _copy_regular(source, destination, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(str(source), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink != 1:
            raise BackupError("unsupported_backup_source")
        with os.fdopen(fd, "rb") as original:
            fd = None
            header = original.read(16)
            if header == b"SQLite format 3\x00":
                raise BackupError("sqlite_requires_online_backup")
            total = len(header)
            with os.fdopen(_private_file(destination), "wb") as target:
                target.write(header)
                for chunk in iter(lambda: original.read(65536), b""):
                    total += len(chunk)
                    if total > limit:
                        raise BackupError("backup_size_limit")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
    finally:
        if fd is not None:
            os.close(fd)


class BackupManager:
    def __init__(self, private_path, repository, sources, *, source_roots=SOURCE_ROOTS, revision, image_digests, identity_digest, lock_check):
        self.private, self.repository, self.sources = Path(private_path), repository, tuple(sources)
        self.source_roots = tuple(Path(root) for root in source_roots)
        self.revision, self.image_digests, self.identity_digest, self.lock_check = revision, image_digests, identity_digest, lock_check

    def _sources(self, components):
        if not components or len(set(components)) != len(components) or not set(components) <= COMPONENTS:
            raise BackupError("invalid_backup_selection")
        sources = tuple(item for item in self.sources if item.component in (*components, "common") or item.role in REQUIRED_ROLES)
        if not sources or len(sources) > MAX_FILES or not REQUIRED_ROLES <= {item.role for item in sources}:
            raise BackupError("backup_sources_incomplete")
        names = set()
        for item in sources:
            name = safe_name(item.name)
            if name == "manifest.json" or name in names or item.kind not in ("file", "sqlite") or item.component not in COMPONENTS | {"common"}:
                raise BackupError("unsupported_backup_source")
            names.add(name)
            no_symlink(item.path)
            for root in self.source_roots:
                no_symlink(root)
            if not any(root in item.path.parents for root in self.source_roots) or not item.path.is_file():
                raise BackupError("source_outside_trusted_root")
        return sources

    def create(self, components, job_id):
        require_id(job_id)
        if not self.lock_check(job_id):
            raise BackupError("backup_lock_required")
        private_directory(self.private)
        sources = self._sources(components)
        total = sum(item.path.stat().st_size for item in sources)
        if total > MAX_BYTES or shutil.disk_usage(str(self.private)).free < total * 3 + 64 * 1024 ** 2:
            raise BackupError("insufficient_backup_space")
        before = self.revision()
        identity = self.identity_digest()
        if not isinstance(before, str) or not REVISION.fullmatch(before) or not isinstance(identity, str) or not REVISION.fullmatch(identity):
            raise BackupError("revision_or_identity_unavailable")
        with private_workspace(self.private / "staging", job_id) as staging:
            files, written = [], 0
            for item in sources:
                destination = staging / item.name
                schema = None
                if item.kind == "sqlite":
                    schema = online_sqlite_backup(item.path, destination, MAX_BYTES - written)
                else:
                    _copy_regular(item.path, destination, MAX_BYTES - written)
                size = destination.stat().st_size
                written += size
                if written > MAX_BYTES:
                    raise BackupError("backup_size_limit")
                files.append({"name": item.name, "size": size, "sha256": file_digest(destination), "kind": item.kind,
                    "schema": schema, "role": item.role, "component": item.component})
            if self.revision() != before or self.identity_digest() != identity or not self.lock_check(job_id):
                raise BackupError("backup_revision_changed")
            manifest = {"format": 1, "components": list(components), "revision": before, "images": self.image_digests(),
                "identity_digest": identity, "files": files}
            encoded = canonical(manifest)
            if len(encoded) > MAX_MANIFEST:
                raise BackupError("backup_manifest_too_large")
            with os.fdopen(_private_file(staging / "manifest.json"), "wb") as target:
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            snapshot_id = self.repository.backup(staging, job_id)
        return self.verify(snapshot_id)

    def verify(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not REVISION.fullmatch(snapshot_id):
            raise BackupError("invalid_snapshot_id")
        private_directory(self.private)
        try:
            with private_workspace(self.private / "verify", snapshot_id) as directory:
                listing = self.repository.listing(snapshot_id)
                if not isinstance(listing, list) or len(listing) > MAX_FILES * 2 + 1:
                    raise BackupError("backup_file_limit")
                names, total = {}, 0
                for node in listing:
                    if node.get("path") == "/" and node.get("type") == "dir":
                        continue
                    path = node.get("path", "")
                    if not isinstance(path, str) or not path.startswith("/"):
                        raise BackupError("invalid_archive_member")
                    name = safe_name(path[1:])
                    if name in names or node.get("type") not in ("file", "dir"):
                        raise BackupError("unsupported_archive_member")
                    if node["type"] == "file":
                        size = node.get("size")
                        if type(size) is not int or size < 0:
                            raise BackupError("invalid_archive_size")
                        names[name] = size
                        total += size
                if total > MAX_BYTES + MAX_MANIFEST or names.get("manifest.json", MAX_MANIFEST + 1) > MAX_MANIFEST:
                    raise BackupError("backup_size_limit")
                if shutil.disk_usage(str(self.private)).free < total + 64 * 1024 ** 2:
                    raise BackupError("insufficient_restore_space")
                manifest_path = directory / "manifest.json"
                self.repository.dump(snapshot_id, "manifest.json", manifest_path, MAX_MANIFEST)
                os.chmod(manifest_path, 0o600)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                backup = self._manifest(snapshot_id, manifest, names)
                for entry in manifest["files"]:
                    path = directory / entry["name"]
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    self.repository.dump(snapshot_id, entry["name"], path, entry["size"])
                    no_symlink(path)
                    os.chmod(path, 0o600)
                    if not path.is_file() or path.stat().st_size != entry["size"] or file_digest(path) != entry["sha256"]:
                        raise BackupError("restored_file_mismatch")
                    if entry["kind"] == "sqlite" and sqlite_schema(path) != entry["schema"]:
                        raise BackupError("restored_schema_mismatch")
                # Only encrypted repository data and this metadata survive cleanup.
                private_directory(self.private / "backup-index")
                index = self.private / "backup-index" / (snapshot_id + ".json")
                with private_workspace(self.private / "staging", snapshot_id) as stage:
                    temporary = stage / "verified.json"
                    with os.fdopen(_private_file(temporary), "wb") as out:
                        out.write(canonical(asdict(backup)))
                        out.flush()
                        os.fsync(out.fileno())
                    no_symlink(index)
                    os.replace(str(temporary), str(index))
                return backup
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            raise BackupError("backup_verification_failed") from None

    def _manifest(self, snapshot_id, manifest, names):
        if not isinstance(manifest, dict) or set(manifest) != {"format", "components", "revision", "images", "identity_digest", "files"} or type(manifest["format"]) is not int or manifest["format"] != 1:
            raise BackupError("invalid_backup_manifest")
        components = manifest["components"]
        if not isinstance(components, list) or not components or len(set(components)) != len(components) or not set(components) <= COMPONENTS:
            raise BackupError("invalid_backup_components")
        if not REVISION.fullmatch(manifest["revision"]) or not REVISION.fullmatch(manifest["identity_digest"]):
            raise BackupError("invalid_backup_revision")
        images, files = manifest["images"], manifest["files"]
        if not isinstance(images, dict) or not set(components) <= set(images) or any(key not in COMPONENTS | {"socks"} or not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for key, value in images.items()):
            raise BackupError("invalid_backup_images")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES or not REQUIRED_ROLES <= {item["role"] for item in files}:
            raise BackupError("backup_sources_incomplete")
        seen, schemas = {"manifest.json"}, {}
        for item in files:
            if set(item) != {"name", "size", "sha256", "kind", "schema", "role", "component"}:
                raise BackupError("invalid_backup_member")
            name = safe_name(item["name"])
            if name in seen or names.get(name) != item["size"] or type(item["size"]) is not int or not REVISION.fullmatch(item["sha256"]):
                raise BackupError("invalid_backup_member")
            seen.add(name)
            if item["kind"] == "sqlite" and isinstance(item["schema"], str) and REVISION.fullmatch(item["schema"]):
                schemas[name] = item["schema"]
            elif item["kind"] != "file" or item["schema"] is not None:
                raise BackupError("invalid_database_format")
        if seen != set(names):
            raise BackupError("unlisted_backup_member")
        return BackupSet(snapshot_id, tuple(components), manifest["revision"], images, schemas, time.time())


def _run_restic(argv, *, env, cwd, destination=None, limit=MAX_MANIFEST, timeout=900):
    """Drain bounded output; stderr and exceptions never expose repository data."""
    process = None
    target = None
    chunks, failures = [], []
    try:
        if destination is not None:
            target = os.fdopen(_private_file(destination), "wb")
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd=str(cwd), shell=False)
        def drain():
            size = 0
            try:
                for chunk in iter(lambda: process.stdout.read(65536), b""):
                    size += len(chunk)
                    if size > limit:
                        raise BackupError("restic_output_limit")
                    if target is None:
                        chunks.append(chunk)
                    else:
                        target.write(chunk)
            except (OSError, BackupError):
                failures.append(True)
                process.kill()
        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise BackupError("restic_timeout") from None
        finally:
            thread.join(timeout=5)
        if thread.is_alive() or failures or process.returncode != 0:
            raise BackupError("restic_operation_failed")
        if target is not None:
            target.flush()
            os.fsync(target.fileno())
            return None
        return b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeError):
        raise BackupError("restic_operation_failed") from None
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if target is not None:
            target.close()


class ResticRepository:
    """A pre-existing local encrypted repository, fixed by the root installer.

    Never initializes, prunes, selects 'latest', or restores an archive wholesale.
    Individual verified regular files are streamed into exclusive private files.
    """

    EXECUTABLES = ("/usr/local/bin/restic", "/opt/bin/restic", "/usr/bin/restic")

    def __init__(self, repository, password_file, private_path, *, executable=None, runner=_run_restic):
        self.repository, self.password_file, self.private = Path(repository), Path(password_file), Path(private_path)
        self.runner = runner
        if executable is None:
            executable = next((Path(item) for item in self.EXECUTABLES if Path(item).is_file()), None)
        if executable is None or str(executable).replace("\\", "/") not in self.EXECUTABLES:
            raise BackupError("restic_not_installed")
        self.executable = str(executable)
        self._validate()

    def _validate(self):
        private_directory(self.private)
        for path in (self.repository, self.password_file):
            no_symlink(path)
        secret = self.password_file.stat()
        if not self.repository.is_dir() or not stat.S_ISREG(secret.st_mode) or not 1 <= secret.st_size <= 4096:
            raise BackupError("restic_repository_unavailable")
        if os.name == "posix" and (secret.st_uid != 0 or stat.S_IMODE(secret.st_mode) != 0o600):
            raise BackupError("restic_password_not_private")
        for name in ("cache", "tmp"):
            private_directory(self.private / name)

    def _call(self, arguments, cwd=None, destination=None, limit=MAX_MANIFEST):
        self._validate()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.private), "RESTIC_REPOSITORY": str(self.repository),
            "RESTIC_PASSWORD_FILE": str(self.password_file), "RESTIC_CACHE_DIR": str(self.private / "cache"), "TMPDIR": str(self.private / "tmp")}
        return self.runner([self.executable, *arguments], env=env, cwd=cwd or self.private, destination=destination, limit=limit, timeout=900)

    def backup(self, directory, job_id):
        require_id(job_id)
        no_symlink(directory)
        if directory.parent != self.private / "staging" or not directory.name.startswith(job_id + "-"):
            raise BackupError("invalid_restic_staging")
        raw = self._call(["backup", "--json", "--tag", "vpn-maintenance", "--tag", job_id, "--", "."], cwd=directory)
        try:
            summaries = [value for value in (json.loads(line) for line in raw.splitlines()) if value.get("message_type") == "summary"]
            snapshot_id = summaries[0]["snapshot_id"] if len(summaries) == 1 else None
            if not isinstance(snapshot_id, str) or not REVISION.fullmatch(snapshot_id):
                raise BackupError("restic_snapshot_unconfirmed")
            return snapshot_id
        except (ValueError, KeyError, TypeError):
            raise BackupError("restic_snapshot_unconfirmed") from None

    def listing(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not REVISION.fullmatch(snapshot_id):
            raise BackupError("invalid_snapshot_id")
        try:
            output = []
            for line in self._call(["ls", "--json", snapshot_id]).splitlines():
                node = json.loads(line)
                if node.get("struct_type") == "snapshot":
                    if node.get("id") != snapshot_id:
                        raise BackupError("restic_snapshot_mismatch")
                elif node.get("struct_type") == "node":
                    output.append({"path": node.get("path"), "type": node.get("type"), "size": node.get("size", 0)})
                else:
                    raise BackupError("restic_listing_unrecognized")
            return output
        except (ValueError, TypeError):
            raise BackupError("restic_listing_invalid") from None

    def dump(self, snapshot_id, name, destination, limit):
        if not isinstance(snapshot_id, str) or not REVISION.fullmatch(snapshot_id) or not 0 <= limit <= MAX_BYTES:
            raise BackupError("invalid_snapshot_id")
        safe_name(name)
        no_symlink(destination)
        if self.private / "verify" not in destination.parents:
            raise BackupError("invalid_restore_target")
        self._call(["dump", snapshot_id, "/" + name], destination=destination, limit=limit)
