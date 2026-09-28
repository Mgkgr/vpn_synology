import importlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
JOB, REVISION, SNAPSHOT = "a" * 32, "b" * 64, "c" * 64
DIGEST = "sha256:" + "d" * 64


class Backups:
    def __init__(self, trace):
        self.trace, self.verified, self.revision = trace, True, REVISION
    def create(self, components, job_id):
        self.trace.append("backup")
        return self.verify(SNAPSHOT)
    def verify(self, snapshot_id):
        return SimpleNamespace(snapshot_id=snapshot_id, component_ids=("metacubexd", "dashboard"), captured_revision=self.revision,
            image_digests={"metacubexd": DIGEST, "dashboard": DIGEST}, database_schemas={}, verified_at=1 if self.verified else None)


class Driver:
    def __init__(self, trace):
        self.trace, self.current_revision = trace, REVISION
        self.release_verified, self.schema_verified, self.resources = True, True, True
        self.fail, self.fail_rollback, self.changed = None, False, False
    def revision(self): return self.current_revision
    def preflight(self, _request): self.trace.append("preflight")
    def resources_ok(self, _components): return self.resources
    def resolve(self, component, release_id):
        return SimpleNamespace(component=component, release_id=release_id, compatibility="approved" if self.release_verified else "unverified", digest=DIGEST)
    def rollback_plan(self, component, release, snapshot):
        from maintenance.updates import RollbackPlan
        return RollbackPlan(component, snapshot.snapshot_id, self.schema_verified, DIGEST, DIGEST, {}, {}, "unchanged", "test-only-validation")
    def download(self, release): self.trace.append("download:" + release.component)
    def validate_candidate(self, release, snapshot):
        self.trace.append("candidate:" + release.component)
        if self.changed: self.current_revision = "e" * 64
        return True
    def apply(self, release, snapshot, job_id, effect):
        def mutate():
            self.trace.append("apply:" + release.component)
            if self.fail == release.component: raise RuntimeError("test injected apply failure")
        effect("replace", mutate)
    def verify(self, component): self.trace.append("verify:" + component)
    def commit(self, release): self.trace.append("commit:" + release.component)
    def restore(self, plan, snapshot, job_id, effect):
        def mutate():
            self.trace.append("restore:" + plan.component)
            if self.fail_rollback: raise RuntimeError("test injected restore failure")
        effect("restore", mutate)
    def prove_rollback(self, plan, snapshot): return not self.fail_rollback


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/updates.py").is_file(), "verified update runner is not implemented")
        self.m = importlib.import_module("maintenance.updates")
        self.p = importlib.import_module("maintenance.protocol")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = importlib.import_module("maintenance.store").JobStore(Path(self.temp.name) / "private/jobs.sqlite3")
        self.trace = []
        self.backups, self.driver = Backups(self.trace), Driver(self.trace)
        self.runner = self.m.UpdateRunner(self.store, self.backups, self.driver)

    def request(self, action="update", components=("metacubexd",), loss=False):
        request = self.p.parse_request(dict(action=action, components=list(components), expected_revision=REVISION,
            release_ids={c: "release-1" for c in components} if action == "update" else {}, enable_stopped=[],
            snapshot_id=SNAPSHOT if action == "rollback" else None, accept_data_loss=loss))
        self.store.submit(JOB, request, "owner")
        return request

    def test_backup_candidate_and_revision_checks_precede_apply(self):
        result = self.runner.run(self.request(), JOB)
        self.assertEqual(result.phase, "completed")
        self.assertEqual(self.trace, ["preflight", "backup", "download:metacubexd", "candidate:metacubexd", "apply:metacubexd", "verify:metacubexd", "commit:metacubexd"])

    def test_release_catalog_approved_contract_is_accepted(self):
        from datetime import datetime, timezone
        from maintenance.releases import ReleaseCandidate
        candidate = ReleaseCandidate("metacubexd", "release-1", "v1.0", datetime.now(timezone.utc), DIGEST, "approved", None,
            "ghcr.io/metacubex/metacubexd@" + DIGEST)
        self.driver.resolve = lambda component, release_id: candidate
        self.assertEqual(self.runner.run(self.request(), JOB).phase, "completed")

    def test_no_verified_backup_no_update(self):
        self.backups.verified = False
        self.assertEqual(self.runner.run(self.request(), JOB).phase, "failed")
        self.assertFalse(any(item.startswith("apply:") for item in self.trace))

    def test_unknown_schema_migration_blocks_upgrade(self):
        self.driver.schema_verified = False
        self.assertEqual(self.runner.run(self.request(), JOB).phase, "failed")
        self.assertFalse(any(item.startswith("apply:") for item in self.trace))

    def test_unverified_release_and_insufficient_resources_do_not_stop_anything(self):
        self.driver.release_verified = False
        self.assertEqual(self.runner.run(self.request(), JOB).phase, "failed")
        self.assertEqual(self.trace, ["preflight"])

    def test_insufficient_resources_blocks_before_backup(self):
        self.driver.resources = False
        self.assertEqual(self.runner.run(self.request(), JOB).phase, "failed")
        self.assertEqual(self.trace, ["preflight"])

    def test_revision_change_after_candidate_blocks_apply(self):
        self.driver.changed = True
        result = self.runner.run(self.request(), JOB)
        self.assertEqual(result.phase, "failed")
        self.assertEqual(result.error_code, "revision_changed")
        self.assertFalse(any(item.startswith("apply:") for item in self.trace))

    def test_apply_failure_rolls_back_once_and_stops_batch(self):
        self.driver.fail = "metacubexd"
        result = self.runner.run(self.request(components=("metacubexd", "dashboard")), JOB)
        self.assertEqual(result.phase, "failed")
        self.assertEqual(result.error_code, "update_reverted")
        self.assertEqual(self.trace.count("restore:metacubexd"), 1)
        self.assertNotIn("apply:dashboard", self.trace)
        self.assertEqual(self.store.lock_state()["state"], "free")

    def test_failed_rollback_holds_lock_for_reconciliation(self):
        self.driver.fail, self.driver.fail_rollback = "metacubexd", True
        result = self.runner.run(self.request(), JOB)
        self.assertEqual(result.phase, "needs_reconcile")
        self.assertNotEqual(self.store.lock_state()["state"], "free")

    def test_rollback_refuses_new_client_data(self):
        self.backups.revision = "f" * 64
        result = self.runner.run(self.request(action="rollback"), JOB)
        self.assertEqual(result.error_code, "rollback_data_loss_confirmation_required")
        self.assertFalse(any(item.startswith("restore:") for item in self.trace))

    def test_fresh_loss_confirmation_allows_only_selected_verified_snapshot(self):
        self.backups.revision = "f" * 64
        result = self.runner.run(self.request(action="rollback", loss=True), JOB)
        self.assertEqual(result.phase, "completed")
        self.assertEqual(self.trace.count("restore:metacubexd"), 1)

    def test_manual_rollback_also_keeps_dashboard_last(self):
        result = self.runner.run(self.request(action='rollback', components=('dashboard', 'metacubexd'), loss=True), JOB)
        self.assertEqual(result.phase, 'completed')
        self.assertEqual([step for step in self.trace if step.startswith('restore:')], ['restore:metacubexd', 'restore:dashboard'])


if __name__ == "__main__": unittest.main()
