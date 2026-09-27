import dataclasses
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


class FakeDocker:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def inspect_component(self, component):
        return self.rows.get(component.id, ())


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/catalog.py").is_file(), "trusted catalogue is not implemented")
        self.m = importlib.import_module("maintenance.catalog")
        self.catalog = self.m.load_catalog(ROOT / "maintenance/components.json")

    def row(self, project="vpn-gateway", **changes):
        return dict(service="mihomo", project=project, container="vpn-mihomo", running=True,
                    version="v1.19.28", digest=DIGEST, image_id=OTHER,
                    expected_digest=DIGEST, **changes)

    def test_discovery_checks_project_labels(self):
        result = self.m.discover_components(FakeDocker({"mihomo": (self.row("foreign"),)}), self.catalog)
        snapshot = next(item for item in result if item.component == "mihomo")
        self.assertEqual(snapshot.freshness_error, "project_mismatch")
        self.assertFalse(snapshot.running)
        antidpi = next(item for item in result if item.component == "antidpi")
        self.assertFalse(antidpi.installed)

    def test_digest_and_config_drift(self):
        row = self.row()
        row["expected_digest"] = OTHER
        snapshot = self.m.discover_components(FakeDocker({"mihomo": (row,)}), self.catalog)[0]
        self.assertTrue(snapshot.installed)
        self.assertNotEqual(snapshot.actual_digest, snapshot.expected_digest)
        self.assertTrue(snapshot.drift)
        self.assertEqual(snapshot.artifacts[0].purpose, "mihomo")

    def test_missing_metadata_and_partial_antidpi_are_not_healthy(self):
        row = self.row()
        row["digest"] = None
        snapshot = self.m.discover_components(FakeDocker({"mihomo": (row,)}), self.catalog)[0]
        self.assertIsNone(snapshot.actual_digest)
        self.assertIsNone(snapshot.drift)
        self.assertEqual(snapshot.freshness_error, "digest_unavailable")
        anti = dict(row, service="antidpi", project="vpn-antidpi", container="vpn-antidpi")
        snapshot = self.m.discover_components(FakeDocker({"antidpi": (anti,)}), self.catalog)[-1]
        self.assertTrue(snapshot.installed)
        self.assertFalse(snapshot.running)
        self.assertEqual(snapshot.freshness_error, "incomplete_component")

    def test_catalog_rejects_unknown_paths_sources_and_extra_keys(self):
        raw = json.loads((ROOT / "maintenance/components.json").read_text(encoding="utf-8"))
        for change in ({"repository": "attacker/mihomo"}, {"services": ["../docker"]}, {"command": "sh"}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                changed = json.loads(json.dumps(raw))
                changed["components"][0].update(change)
                path = Path(temp) / "catalog.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(self.m.CatalogError):
                    self.m.load_catalog(path)

    def test_docker_adapter_returns_no_env_or_unbounded_inspect(self):
        adapter_module = importlib.import_module("maintenance.docker_adapter")
        calls = []
        def run(argv, timeout, limit):
            calls.append(argv)
            if "ls" in argv:
                return "vpn-mihomo\n"
            if "container" in argv:
                return json.dumps({"service": "mihomo", "project": "vpn-gateway", "container": "/vpn-mihomo", "running": True, "image_id": OTHER, "image": "metacubex/mihomo@" + DIGEST})
            return json.dumps({"digests": ["metacubex/mihomo@" + DIGEST], "version": "v1.19.28", "os": "linux", "arch": "amd64"})
        adapter = adapter_module.DockerAdapter(self.catalog, executable=Path("/usr/local/bin/docker"), runner=run)
        self.assertEqual(adapter.inspect_component(self.catalog[0])[0]["digest"], DIGEST)
        self.assertTrue(all("--format" in call for call in calls))
        self.assertNotIn(".Env", repr(calls))
        with self.assertRaises(adapter_module.DockerError):
            adapter.inspect_component(dataclasses.replace(self.catalog[0], compose_project="foreign"))


if __name__ == "__main__":
    unittest.main()
