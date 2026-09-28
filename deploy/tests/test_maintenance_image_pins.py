import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REVISION = "a" * 64
IMAGE = "metacubex/mihomo@sha256:" + "b" * 64


class ImagePinTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/image_pins.py").is_file(), "root image overlays are not implemented")
        self.m = importlib.import_module("maintenance.image_pins")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = {key: self.root / key for key in ("vpn-gateway", "vpn-dashboard", "vpn-antidpi")}
        for path in self.projects.values(): path.mkdir()
        self.pins = self.m.ImageOverrides(self.root / "private", projects=self.projects)

    def test_overlay_contains_only_fixed_service_image_pins(self):
        path = self.pins.write("vpn-gateway", {"mihomo": IMAGE}, REVISION)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"services": {"mihomo": {"image": IMAGE}}})
        self.assertEqual(self.pins.verify_deploy("vpn-gateway", REVISION), path)

    def test_arbitrary_service_image_or_mutable_tag_is_rejected(self):
        for pins in ({"foreign": IMAGE}, {"mihomo": "attacker/image@sha256:" + "b" * 64}, {"mihomo": "metacubex/mihomo:latest"}):
            with self.assertRaises(self.m.ImagePinError): self.pins.write("vpn-gateway", pins, REVISION)
        self.assertEqual(list(self.projects["vpn-gateway"].iterdir()), [])

    def test_local_dashboard_pin_is_image_id_not_registry_manifest(self):
        path = self.pins.write("vpn-dashboard", {"dashboard": "sha256:" + "b" * 64}, REVISION)
        self.assertIn("sha256:", path.read_text(encoding="utf-8"))

    def test_deploy_cannot_silently_ignore_override_or_new_source(self):
        path = self.pins.write("vpn-gateway", {"mihomo": IMAGE}, REVISION)
        with self.assertRaises(self.m.ImagePinError): self.pins.verify_deploy("vpn-gateway", "c" * 64)
        path.write_text('{"services":{"mihomo":{"volumes":["/:/host"]}}}', encoding="utf-8")
        with self.assertRaises(self.m.ImagePinError): self.pins.verify_deploy("vpn-gateway", REVISION)


if __name__ == "__main__": unittest.main()
