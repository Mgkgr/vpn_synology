import importlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
JOB, REVISION = "a" * 32, "b" * 64


class Docker:
    def __init__(self, trace):
        self.trace = trace
        self.running = {s: True for s in ("wireguard", "mihomo", "antidpi", "socks", "uptime-kuma", "metacubexd", "dashboard")}
        self.absent, self.stale, self.fail = set(), set(), None
        self.foreign = False
    def inventory(self):
        if self.foreign:
            raise RuntimeError("foreign compose identity")
        return {s: {"running": value, "namespace_stale": s in self.stale} for s, value in self.running.items() if s not in self.absent}
    def _run(self, op, service):
        self.trace.append((op, service))
        if self.fail == (op, service):
            raise RuntimeError("test failure")
        self.running[service] = op != "stop"
    def stop(self, service): self._run("stop", service)
    def start(self, service): self._run("start", service)
    def recreate(self, service): self._run("recreate", service)


class Readiness:
    def __init__(self, trace): self.trace = trace
    def preflight(self, _components): pass
    def wait(self, component): self.trace.append(("verify", component))


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/restart.py").is_file(), "maintenance restart runner is not implemented")
        self.m = importlib.import_module("maintenance.restart")
        self.p = importlib.import_module("maintenance.protocol")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = importlib.import_module("maintenance.store").JobStore(Path(self.temp.name) / "private/jobs.sqlite3")
        self.trace = []
        self.docker = Docker(self.trace)
        self.runner = self.m.RestartRunner(self.store, self.docker, Readiness(self.trace), revision=lambda: REVISION, identity_digest=lambda: "c" * 64)

    def request(self, components, enable=()):
        request = self.p.parse_request(dict(action="restart", components=list(components), expected_revision=REVISION,
            release_ids={}, enable_stopped=list(enable), snapshot_id=None, accept_data_loss=False))
        self.store.submit(JOB, request, "owner")
        return request

    def test_restart_dependency_order_and_dashboard_last(self):
        request = self.request(("dashboard", "metacubexd", "uptime-kuma", "mihomo", "wireguard", "antidpi"))
        result = self.runner.run(request, JOB)
        self.assertEqual(result.phase, "completed")
        self.assertEqual(self.trace, [("stop", "mihomo"), ("stop", "wireguard"), ("start", "wireguard"), ("verify", "wireguard"),
            ("stop", "socks"), ("stop", "antidpi"), ("start", "antidpi"), ("start", "socks"), ("verify", "antidpi"),
            ("start", "mihomo"), ("verify", "mihomo"), ("stop", "uptime-kuma"), ("start", "uptime-kuma"), ("verify", "uptime-kuma"),
            ("stop", "metacubexd"), ("start", "metacubexd"), ("verify", "metacubexd"),
            ("stop", "dashboard"), ("start", "dashboard"), ("verify", "dashboard")])
        before = list(self.trace)
        self.runner.run(request, JOB)
        self.assertEqual(self.trace, before)

    def test_absent_antidpi_is_not_installed_by_restart(self):
        self.docker.absent = {"antidpi", "socks"}
        result = self.runner.run(self.request(("mihomo", "wireguard")), JOB)
        self.assertEqual(result.phase, "completed")
        self.assertFalse(any(service in {"antidpi", "socks"} for _, service in self.trace))

    def test_stopped_dependency_needs_explicit_choice(self):
        self.docker.running["wireguard"] = False
        result = self.runner.run(self.request(("mihomo",)), JOB)
        self.assertEqual(result.phase, "failed")
        self.assertEqual(result.error_code, "stopped_dependency_needs_choice")
        self.assertEqual(self.trace, [])

    def test_stopped_service_is_not_implicitly_enabled(self):
        self.docker.running["uptime-kuma"] = False
        result = self.runner.run(self.request(("uptime-kuma",)), JOB)
        self.assertEqual(result.phase, "failed")
        self.assertEqual(self.trace, [])

    def test_recovery_attempt_is_once_and_failure_does_not_loop(self):
        self.docker.fail = ("start", "wireguard")
        result = self.runner.run(self.request(("wireguard",)), JOB)
        self.assertEqual(result.phase, "needs_reconcile")
        self.assertEqual(self.trace.count(("start", "wireguard")), 2)  # apply + one recovery.
        before = list(self.trace)
        self.runner.run(self.store.get_request(JOB), JOB)
        self.assertEqual(self.trace, before)
        self.assertNotEqual(self.store.lock_state()["state"], "free")

    def test_stale_namespace_is_recreated_only_for_dependents(self):
        self.docker.stale = {"mihomo"}
        result = self.runner.run(self.request(("mihomo",)), JOB)
        self.assertEqual(result.phase, "completed")
        self.assertEqual([entry for entry in self.trace if entry[0] == "recreate"], [("recreate", "mihomo")])
        self.assertNotIn(("stop", "wireguard"), self.trace)

    def test_revision_or_identity_mismatch_prevents_effects(self):
        self.runner.revision = lambda: "d" * 64
        self.assertEqual(self.runner.run(self.request(("mihomo",)), JOB).phase, "failed")
        self.assertEqual(self.trace, [])

    def test_foreign_compose_labels_prevent_effects(self):
        self.docker.foreign = True
        self.assertEqual(self.runner.run(self.request(("mihomo",)), JOB).phase, "failed")
        self.assertEqual(self.trace, [])

    def test_two_runners_cannot_claim_the_same_job(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from unittest.mock import patch
        request = self.request(("uptime-kuma",))
        original = self.store.get_job
        barrier, local = threading.Barrier(2), threading.local()
        def read(job_id):
            row = original(job_id)
            if not getattr(local, "read", False):
                local.read = True
                barrier.wait(timeout=2)
            return row
        with patch.object(self.store, "get_job", side_effect=read), ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.runner.run(request, JOB), range(2)))
        self.assertEqual(self.trace.count(("stop", "uptime-kuma")), 1)
        self.assertEqual(self.store.get_job(JOB).phase, "completed")


if __name__ == "__main__": unittest.main()
