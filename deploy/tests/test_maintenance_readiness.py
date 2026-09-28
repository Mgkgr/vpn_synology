import importlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/readiness.py").is_file(), "host-side readiness is not implemented")
        self.m = importlib.import_module("maintenance.readiness")

    def test_missing_host_probe_is_unknown_and_blocks_preflight(self):
        readiness = self.m.Readiness({})
        self.assertTrue(all(item.state == "unknown" for item in readiness.check("mihomo")))
        with self.assertRaises(self.m.ReadinessError):
            readiness.preflight(("mihomo",))

    def test_sleeping_clients_are_not_a_readiness_failure(self):
        checks = {name: lambda timeout: True for name in self.m.REQUIRED["wireguard"]}
        self.assertFalse(any("handshake" in name for name in checks))
        readiness = self.m.Readiness({"wireguard": checks})
        readiness.preflight(("wireguard",))
        self.assertTrue(all(item.state == "pass" for item in readiness.wait("wireguard")))

    def test_readiness_deadline_is_bounded_without_real_sleep(self):
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        checks = {name: lambda timeout: False for name in self.m.REQUIRED["mihomo"]}
        readiness = self.m.Readiness({"mihomo": checks}, monotonic=lambda: clock[0], sleep=sleep)
        with self.assertRaises(self.m.ReadinessError):
            readiness.wait("mihomo")
        self.assertLessEqual(clock[0], 120)

    def test_probe_error_text_cannot_leak_into_result(self):
        def broken(timeout): raise RuntimeError("Authorization: secret test credential")
        checks = {name: broken for name in self.m.REQUIRED["dashboard"]}
        result = self.m.Readiness({"dashboard": checks}).check("dashboard")
        self.assertNotIn("credential", str(result))
        self.assertTrue(all(item.state == "unknown" for item in result))


if __name__ == "__main__": unittest.main()
