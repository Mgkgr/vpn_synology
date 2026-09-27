import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure-health-probes.py"
CONFIG = """# preserved configuration
mixed-port: 7890
proxies:
  - name: WG-IMP
    type: vless
    server: example.invalid
  - name: HY2-USA
    type: hysteria2
    password: 'not-a-real-secret'
proxy-groups:
  - name: VPS-FALLBACK
    type: fallback
    proxies:
      - WG-IMP
      - HY2-USA
    interval: 300
rules:
  - RULE-SET,direct-custom,DIRECT
  - MATCH,VPS-FALLBACK
"""


class HealthProbeConfigTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "health probe configurator is not implemented")
        spec = importlib.util.spec_from_file_location("health_probe_config", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.configure = module.configure_health_probes
        self.error = module.HealthProbeConfigError
        self.begin, self.end = module.BEGIN, module.END

    def test_probe_groups_are_single_member_and_preserve_source(self):
        result = self.configure(CONFIG, ("WG-IMP", "HY2-USA"))
        for name in ("WG-IMP", "HY2-USA"):
            self.assertIn(f"  - name: DASH-HEALTH-{name}\n    type: select\n    hidden: true\n    interval: 0\n    empty-fallback: REJECT\n    proxies:\n      - {name}\n", result)
        start, end = result.index(self.begin), result.index(self.end) + len(self.end)
        self.assertEqual(result[:start] + result[end:], CONFIG)
        self.assertEqual(self.configure(result, ("WG-IMP", "HY2-USA")), result)

    def test_missing_member_or_unsupported_structure_fails_closed(self):
        for source in (
            CONFIG.replace("  - name: HY2-USA", "  - name: OTHER"),
            CONFIG.replace("proxy-groups:", "proxy-groups: []"),
            CONFIG.replace("  - name: WG-IMP", "  - name: &alias WG-IMP"),
            CONFIG + "proxy-groups: []\n",
        ):
            with self.subTest(source=source), self.assertRaises(self.error):
                self.configure(source, ("WG-IMP", "HY2-USA"))
        for ids in ((), ("DIRECT",), ("WG-IMP", "WG-IMP"), ("WG-IMP", "ANTIDPI")):
            with self.subTest(ids=ids), self.assertRaises(self.error):
                self.configure(CONFIG, ids)

    def test_conflicting_name_or_modified_owned_block_is_not_overwritten(self):
        conflicting = CONFIG.replace("  - name: VPS-FALLBACK", "  - name: DASH-HEALTH-WG-IMP")
        with self.assertRaises(self.error):
            self.configure(conflicting, ("WG-IMP", "HY2-USA"))
        configured = self.configure(CONFIG, ("WG-IMP", "HY2-USA"))
        with self.assertRaises(self.error):
            self.configure(configured.replace("empty-fallback: REJECT", "empty-fallback: DIRECT"), ("WG-IMP", "HY2-USA"))

    def test_optional_antidpi_and_crlf_preserve_original_format(self):
        config = CONFIG.replace("proxy-groups:", "  - name: ANTIDPI\n    type: socks5\nproxy-groups:")
        first = self.configure(config, ("WG-IMP", "HY2-USA"))
        result = self.configure(first, ("WG-IMP", "HY2-USA", "ANTIDPI"))
        self.assertIn("  - name: DASH-HEALTH-ANTIDPI\n", result)
        windows = self.configure(CONFIG.replace("\n", "\r\n"), ("WG-IMP", "HY2-USA"))
        self.assertNotIn("\n", windows.replace("\r\n", ""))


if __name__ == "__main__":
    unittest.main()
