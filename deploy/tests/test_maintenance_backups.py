import hashlib
import importlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
JOB, REVISION, SNAPSHOT = "a" * 32, "b" * 64, "c" * 64


class MemoryRepository:
    """Archive seam only; real SQLite copy, manifest validation and restore run."""
    def __init__(self):
        self.files = {}
    def backup(self, directory, job_id):
        self.files = {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        return SNAPSHOT
    def listing(self, snapshot_id):
        return [{"path": "/" + name, "type": "file", "size": len(data)} for name, data in self.files.items()]
    def dump(self, snapshot_id, name, destination, limit):
        value = self.files[name]
        if len(value) > limit:
            raise RuntimeError("output limit")
        destination.write_bytes(value)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/backups.py").is_file(), "verified maintenance backup is not implemented")
        self.m = importlib.import_module("maintenance.backups")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "sources"
        self.source.mkdir()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.repository = MemoryRepository()
        self.db = sqlite3.connect(str(self.source / "peers.sqlite3"))
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute("CREATE TABLE peers (id INTEGER PRIMARY KEY, private_key TEXT)")
        self.db.commit()
        self.db.execute("INSERT INTO peers VALUES (1, 'test-only-private-key')")
        self.db.commit()
        self.sources = [self.m.BackupSource("wireguard/identity.sqlite3", self.source / "peers.sqlite3", "sqlite", "wg_identity", "wireguard")]
        for name, role in (("config", "config"), ("key", "dashboard_secrets"), ("worker", "worker_state")):
            path = self.source / name
            path.write_text("test-only-" + name, encoding="utf-8")
            self.sources.append(self.m.BackupSource("common/" + name, path, "file", role, "common"))

    def manager(self, revision=lambda: REVISION):
        return self.m.BackupManager(self.private, self.repository, tuple(self.sources), source_roots=(self.source,),
            revision=revision, image_digests=lambda: {"wireguard": "sha256:" + "d" * 64},
            identity_digest=lambda: "e" * 64, lock_check=lambda job_id: job_id == JOB)

    def test_live_sqlite_wal_latest_commit_is_restorable(self):
        self.assertTrue(Path(str(self.source / "peers.sqlite3") + "-wal").is_file())
        snapshot = self.manager().create(("wireguard",), JOB)
        self.assertEqual(snapshot.snapshot_id, SNAPSHOT)
        self.assertEqual(snapshot.captured_revision, REVISION)
        self.assertIsNotNone(snapshot.verified_at)
        copy = self.root / "restored.sqlite3"
        copy.write_bytes(self.repository.files["wireguard/identity.sqlite3"])
        with closing(sqlite3.connect(str(copy))) as db:
            self.assertEqual(db.execute("SELECT private_key FROM peers").fetchone()[0], "test-only-private-key")
        self.assertEqual(list((self.private / "staging").iterdir()), [])
        self.assertEqual(list((self.private / "verify").iterdir()), [])

    def test_unknown_database_disguised_as_regular_file_is_blocked(self):
        self.sources[0] = self.m.BackupSource("wireguard/identity.sqlite3", self.source / "peers.sqlite3", "file", "wg_identity", "wireguard")
        with self.assertRaises(self.m.BackupError):
            self.manager().create(("wireguard",), JOB)
        self.assertEqual(self.repository.files, {})

    def test_restic_uses_private_password_file_and_closed_commands(self):
        self.assertTrue(hasattr(self.m, "ResticRepository"), "Restic adapter is not implemented")
        password = self.private / "restic-password"
        password.write_text("test-only-restic-password", encoding="utf-8")
        password.chmod(0o600)
        repository = self.private / "repository"
        repository.mkdir(mode=0o700)
        calls = []
        def execute(argv, **kwargs):
            calls.append((argv, kwargs))
            return '{"message_type":"summary","snapshot_id":"' + SNAPSHOT + '"}\n'
        restic = self.m.ResticRepository(repository, password, self.private, executable=Path("/usr/local/bin/restic"), runner=execute)
        with self.m.private_workspace(self.private / "staging", JOB) as directory:
            self.assertEqual(restic.backup(directory, JOB), SNAPSHOT)
        argv, arguments = calls[0]
        self.assertNotIn("test-only-restic-password", str(argv) + str(arguments))
        self.assertEqual(arguments["env"]["RESTIC_PASSWORD_FILE"], str(password))
        self.assertEqual(argv[-1], ".")
        self.assertNotIn("init", argv)
        with self.assertRaises(self.m.BackupError):
            restic.listing("latest")

    def test_unknown_persistence_format_and_missing_identity_are_blocked(self):
        self.sources[0] = self.m.BackupSource("wireguard/identity.sqlite3", self.source / "peers.sqlite3", "unverified-db", "wg_identity", "wireguard")
        with self.assertRaises(self.m.BackupError):
            self.manager().create(("wireguard",), JOB)
        self.sources.pop(0)
        with self.assertRaises(self.m.BackupError):
            self.manager().create(("wireguard",), JOB)

    def test_restore_rejects_traversal_links_and_unlisted_files(self):
        manager = self.manager()
        manager.create(("wireguard",), JOB)
        original = self.repository.listing
        for node in ({"path": "/../escape", "type": "file", "size": 1},
                     {"path": "/symlink", "type": "symlink", "size": 0},
                     {"path": "/unlisted", "type": "file", "size": 1}):
            self.repository.listing = lambda snapshot, node=node: original(snapshot) + [node]
            with self.assertRaises(self.m.BackupError):
                manager.verify(SNAPSHOT)
        self.assertFalse((self.root / "escape").exists())

    def test_corrupt_restore_blocks_verification_and_cleans_plaintext(self):
        manager = self.manager()
        manager.create(("wireguard",), JOB)
        self.repository.files["common/key"] = b"wrong data"
        with self.assertRaises(self.m.BackupError):
            manager.verify(SNAPSHOT)
        self.assertEqual(list((self.private / "verify").iterdir()), [])

    def test_revision_change_during_backup_is_not_verified(self):
        calls = iter((REVISION, "f" * 64))
        with self.assertRaises(self.m.BackupError):
            self.manager(revision=lambda: next(calls)).create(("wireguard",), JOB)
        self.assertEqual(self.repository.files, {})

    def test_sources_outside_trusted_root_cannot_be_copied(self):
        path = self.root / "foreign-secret"
        path.write_text("not part of this project", encoding="utf-8")
        self.sources[1] = self.m.BackupSource("common/config", path, "file", "config", "common")
        with self.assertRaises(self.m.BackupError):
            self.manager().create(("wireguard",), JOB)

    def test_no_lease_no_backup_and_space_is_checked_first(self):
        manager = self.manager()
        manager.lock_check = lambda _job: False
        with self.assertRaises(self.m.BackupError):
            manager.create(("wireguard",), JOB)
        self.assertEqual(self.repository.files, {})
        manager.lock_check = lambda _job: True
        with patch.object(self.m.shutil, "disk_usage", return_value=type("Disk", (), {"free": 1})()):
            with self.assertRaises(self.m.BackupError):
                manager.create(("wireguard",), JOB)
        self.assertEqual(self.repository.files, {})


if __name__ == "__main__":
    unittest.main()
