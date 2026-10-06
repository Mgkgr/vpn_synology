import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "configure-site-probes.py"
BEFORE = """secret: fixture
proxies:
  - name: WG-IMP
    type: vless
  - name: HY2-USA
    type: hysteria2
proxy-groups:
  - name: VPS-FALLBACK
    type: fallback
    proxies:
      - WG-IMP
      - HY2-USA
rules:
  - MATCH,VPS-FALLBACK
dns:
  enable: true
"""


class SiteProbeConfigTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("site_probe_config", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_only_own_block_changes_and_transform_is_idempotent(self):
        result = self.module.configure_site_probes(BEFORE)
        self.assertEqual(self.module.configure_site_probes(result), result)
        start = result.index(self.module.BEGIN)
        end = result.index(self.module.END) + len(self.module.END)
        self.assertEqual(result[:start] + result[end:], BEFORE)
        for name in ("DIRECT", "WG-IMP", "HY2-USA"):
            self.assertIn("  - name: DASH-SITE-" + name + "\n", result)
        self.assertEqual(result.count("    interval: 0\n"), 3)
        self.assertEqual(result.count("    empty-fallback: REJECT\n"), 3)

    def test_crlf_is_preserved(self):
        result = self.module.configure_site_probes(BEFORE.replace("\n", "\r\n"))
        self.assertNotIn("\n", result.replace("\r\n", ""))

    def test_conflict_missing_proxy_and_changed_owned_block_fail_closed(self):
        result = self.module.configure_site_probes(BEFORE)
        for text in [BEFORE.replace("WG-IMP", "OTHER"),
                     BEFORE.replace("VPS-FALLBACK", "DASH-SITE-DIRECT"),
                     result.replace("    interval: 0", "    interval: 60", 1),
                     BEFORE.replace("proxy-groups:\n", "proxy-groups: []\n"),
                     BEFORE + "  # DASH-SITE-WG-IMP referenced outside the owned block\n"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    self.module.configure_site_probes(text)


if __name__ == "__main__":
    unittest.main()
