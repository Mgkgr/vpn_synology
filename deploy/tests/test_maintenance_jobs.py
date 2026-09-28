import dataclasses
import importlib
from pathlib import Path
import sys
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
JOB = "a" * 32
SECOND = "c" * 32
REVISION = "b" * 64


class JobTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/store.py").is_file(), "durable maintenance store is not implemented")
        self.m = importlib.import_module("maintenance.store")
        self.p = importlib.import_module("maintenance.protocol")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "jobs.sqlite3"
        self.store = self.m.JobStore(self.path)
        self.request = self.p.parse_request(dict(action="restart", components=["mihomo"], expected_revision=REVISION, release_ids={}, enable_stopped=[], snapshot_id=None, accept_data_loss=False))

    def test_submit_is_durable_and_idempotent(self):
        first = self.store.submit(JOB, self.request, "admin", now=100)
        reopened = self.m.JobStore(self.path)
        self.assertEqual(reopened.submit(JOB, self.request, "admin", now=101), first)
        self.assertEqual(len(reopened.jobs()), 1)
        with self.assertRaises(self.m.JobConflict):
            reopened.submit(JOB, dataclasses.replace(self.request, components=("wireguard",)), "admin", now=101)
        with self.assertRaises(self.m.JobConflict):
            reopened.submit(JOB, self.request, "another-admin", now=101)
        self.store.set_phase(JOB, "preflight", now=102)
        self.store.set_phase(JOB, "apply", now=103)
        calls = []
        if self.store.begin_effect(JOB, "restart.mihomo", now=103):
            calls.append("restart")
            self.store.finish_effect(JOB, "restart.mihomo", now=104)
        if self.store.begin_effect(JOB, "restart.mihomo", now=105):
            calls.append("restart")
        self.assertEqual(calls, ["restart"])

    def test_reboot_requires_reconcile(self):
        self.store.submit(JOB, self.request, "admin", now=100)
        self.store.set_phase(JOB, "preflight", now=101)
        self.store.set_phase(JOB, "apply", now=102)
        self.store.begin_effect(JOB, "restart.mihomo", now=102)
        self.store.recover_after_restart(now=103)
        self.assertEqual(self.store.get_job(JOB).phase, "needs_reconcile")
        with self.assertRaises(self.m.JobBusy):
            self.store.submit(SECOND, self.request, "admin", now=104)
        with self.assertRaises(self.m.JobConflict):
            self.store.begin_effect(JOB, "restart.mihomo", now=104)

    def test_write_guard_blocks_maintenance_race(self):
        lease = self.store.acquire_writer("rules", now=100)
        with self.assertRaises(self.m.JobBusy):
            self.store.submit(JOB, self.request, "admin", now=101)
        self.store.renew_writer(lease, now=115)
        self.store.release_writer(lease, outcome="complete", now=116)
        self.store.submit(JOB, self.request, "admin", now=117)
        with self.assertRaises(self.m.JobBusy):
            self.store.acquire_writer("clients", now=118)

    def test_expired_writer_is_not_blindly_unlocked(self):
        lease = self.store.acquire_writer("clients", now=100)
        with self.assertRaises(self.m.JobBusy):
            self.store.submit(JOB, self.request, "admin", now=190)
        self.assertEqual(self.store.lock_state(now=190)["state"], "needs_reconcile")
        with self.assertRaises(self.m.JobConflict):
            self.store.renew_writer(lease, now=191)
        self.store.release_writer(lease, outcome="complete", now=192)
        self.assertEqual(self.store.lock_state(now=192)["state"], "needs_reconcile")

    def test_cancel_never_unlocks_an_active_apply(self):
        self.store.submit(JOB, self.request, "admin", now=100)
        self.assertTrue(self.store.get_job(JOB).cancel_allowed)
        self.assertEqual(self.store.cancel(JOB, now=101).phase, "cancelled")
        self.store.submit(SECOND, self.request, "admin", now=102)
        self.store.set_phase(SECOND, "preflight", now=103)
        self.store.set_phase(SECOND, "apply", now=104)
        self.assertFalse(self.store.get_job(SECOND).cancel_allowed)
        with self.assertRaises(self.m.JobConflict):
            self.store.cancel(SECOND, now=105)

    def test_uncommitted_disk_failure_has_no_accepted_job(self):
        commit = self.store._commit
        def fail_job_commit(db):
            if db.execute("SELECT 1 FROM jobs").fetchone():
                raise OSError("disk full")
            commit(db)
        with patch.object(self.store, "_commit", side_effect=fail_job_commit):
            with self.assertRaises(OSError):
                self.store.submit(JOB, self.request, "admin", now=100)
        self.assertEqual(self.store.jobs(), ())

    def test_concurrent_submits_admit_only_one_mutation(self):
        barrier = threading.Barrier(2)
        def submit(job_id):
            barrier.wait(timeout=3)
            try:
                return self.store.submit(job_id, self.request, "admin", now=100).phase
            except self.m.JobBusy:
                return "busy"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (JOB, SECOND)))
        self.assertCountEqual(results, ["queued", "busy"])
        self.assertEqual(len(self.store.jobs()), 1)

    def test_sqlite_checkpoint_may_remove_wal_during_path_validation(self):
        wal = Path(str(self.path) + '-wal')
        original_exists, original_stat = Path.exists, Path.stat
        def stale_exists(path):
            return True if path == wal else original_exists(path)
        def after_checkpoint(path, *args, **kwargs):
            if path == wal:
                raise FileNotFoundError(2, 'SQLite checkpoint removed WAL')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'exists', stale_exists), patch.object(Path, 'stat', after_checkpoint):
            self.assertEqual(self.store.submit(JOB, self.request, 'admin', now=100).phase, 'queued')
        self.assertEqual(len(self.store.jobs()), 1)

    def test_missing_main_database_is_not_recreated_during_connection(self):
        self.path.unlink()
        with self.assertRaises(self.m.JobConflict):
            self.store.jobs()
        self.assertFalse(self.path.exists())

    def test_expired_release_does_not_claim_success(self):
        lease = self.store.acquire_writer("rules", now=100)
        self.assertFalse(self.store.release_writer(lease, outcome="complete", now=191))
        self.assertEqual(self.store.lock_state(now=191)["state"], "needs_reconcile")

    def test_successful_release_and_preapply_cancellation_cleanup(self):
        lease = self.store.acquire_writer("rules", now=100)
        self.assertTrue(self.store.release_writer(lease, outcome="complete", now=101))
        self.store.submit(JOB, self.request, "admin", now=102)
        self.store.set_phase(JOB, "preflight", now=103)
        self.assertTrue(self.store.cancel(JOB, now=104).cancel_requested)
        with self.assertRaises(self.m.JobConflict):
            self.store.set_phase(JOB, "apply", now=105)
        with self.assertRaises(self.m.JobBusy):
            self.store.acquire_writer("rules", now=106)
        self.store.set_phase(JOB, "cancelled", now=107)  # runner acknowledges staging cleanup
        self.assertEqual(self.store.lock_state(now=108)["state"], "free")


if __name__ == "__main__":
    unittest.main()
