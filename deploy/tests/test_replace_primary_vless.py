import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).parents[1] / "scripts" / "replace-primary-vless.py"
URI = (
    "vless://11111111-2222-4333-8444-555555555555@vpn.example:8443"
    "?type=tcp&security=reality&encryption=none&flow=xtls-rprx-vision"
    "&sni=dl.google.com&fp=firefox&pbk=" + "A" * 43 + "&sid=f5a481999553cb89&spx=%2Fexample"
)
HY2_URI = (
    "hy2://test-auth-secret@reserve.example:443/"
    "?obfs=salamander&obfs-password=test-obfs-secret&sni=reserve.example"
)
BEFORE = """secret: private-controller-secret
dns:
  enable: true
proxies:
  - name: WG-IMP
    type: wireguard
    private-key: private-wireguard-secret
    server: old.example
    port: 12345
  - name: HY2-USA
    type: hysteria2
    password: reserve-secret
proxy-groups:
  - name: VPS-FALLBACK
    type: fallback
    proxies:
      - WG-IMP
      - HY2-USA
    interval: 300
rules:
  - RULE-SET,managed-wg-imp,WG-IMP
  - MATCH,VPS-FALLBACK
"""


class VlessReplacementTests(unittest.TestCase):
    def module(self):
        self.assertTrue(MODULE.is_file(), "replacement module has not been implemented")
        spec = importlib.util.spec_from_file_location("replace_primary_vless", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_profile_keeps_reality_vision_fingerprint_and_port(self):
        module = self.module()
        profile = module.parse_profile(URI.replace("://", "\\://").replace("@", "\\@") + "&#x20;")
        self.assertEqual(profile["port"], 8443)
        self.assertEqual(profile["servername"], "dl.google.com")
        self.assertEqual(profile["client-fingerprint"], "firefox")
        self.assertEqual(profile["flow"], "xtls-rprx-vision")
        self.assertEqual(profile["reality-opts"]["short-id"], "f5a481999553cb89")
        self.assertTrue(profile["tls"])
        self.assertFalse(profile["skip-cert-verify"])

    def test_rejects_unsafe_or_ambiguous_profile_without_echoing_credentials(self):
        module = self.module()
        for bad in (URI.replace("security=reality", "security=none"), URI + "&fp=chrome", URI.replace("sid=f5a481999553cb89", "sid=ZZ"), URI.replace("8443", "70000")):
            with self.subTest(bad=bad.replace("11111111-2222-4333-8444-555555555555", "redacted")):
                with self.assertRaises(ValueError) as caught:
                    module.parse_profile(bad)
                self.assertNotIn("11111111-2222-4333-8444-555555555555", str(caught.exception))

    def test_replacement_preserves_reserve_rules_dns_and_internal_identifier(self):
        module = self.module()
        result = module.replace_outbound(BEFORE, module.parse_profile(URI))
        self.assertEqual(result.split("proxies:", 1)[0], BEFORE.split("proxies:", 1)[0])
        self.assertEqual(result.split("  - name: HY2-USA", 1)[1], BEFORE.split("  - name: HY2-USA", 1)[1])
        self.assertIn("  - name: WG-IMP\n    type: vless\n", result)
        self.assertNotIn("private-wireguard-secret", result)
        self.assertEqual(module.replace_outbound(result, module.parse_profile(URI)), result)

    def test_missing_or_duplicate_primary_is_not_modified(self):
        module = self.module()
        for bad in (BEFORE.replace("name: WG-IMP", "name: OTHER"), BEFORE.replace("  - name: HY2-USA", "  - name: WG-IMP\n    type: wireguard\n  - name: HY2-USA")):
            with self.assertRaises(ValueError):
                module.replace_outbound(bad, module.parse_profile(URI))

    def test_success_persists_only_after_runtime_verified(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_bytes(BEFORE.encode("utf-8"))
            candidate = module.replace_outbound(BEFORE, module.parse_profile(URI))
            actions = []
            def apply(text):
                self.assertEqual(path.read_text(encoding="utf-8"), BEFORE)
                actions.append(text)
            def verify():
                self.assertEqual(path.read_text(encoding="utf-8"), BEFORE)
                actions.append("verified")
            module.commit_candidate(path, BEFORE, candidate, apply, verify)
            self.assertEqual(path.read_text(encoding="utf-8"), candidate)
            self.assertEqual(actions, [candidate, "verified"])

    def test_failed_runtime_verification_restores_old_runtime_and_keeps_file(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_bytes(BEFORE.encode("utf-8"))
            calls = []
            def fail():
                raise RuntimeError("probe failed")
            with self.assertRaises(RuntimeError):
                module.commit_candidate(path, BEFORE, "candidate", calls.append, fail)
            self.assertEqual(path.read_text(encoding="utf-8"), BEFORE)
            self.assertEqual(calls, ["candidate", BEFORE])

    def test_concurrent_file_change_is_never_overwritten(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_bytes(BEFORE.encode("utf-8"))
            calls = []
            def changed():
                path.write_text("changed by administrator", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                module.commit_candidate(path, BEFORE, "candidate", calls.append, changed)
            self.assertEqual(path.read_text(encoding="utf-8"), "changed by administrator")
            self.assertEqual(calls[-1], "changed by administrator")

    def test_isolated_preflight_never_enables_routes_dns_or_public_listener(self):
        module = self.module()
        config = module.isolated_config(module.parse_profile(URI), 34567)
        self.assertEqual(config["bind-address"], "127.0.0.1")
        self.assertFalse(config["allow-lan"])
        self.assertFalse(config["tun"]["enable"])
        self.assertFalse(config["dns"]["enable"])
        self.assertNotIn("external-controller", config)
        self.assertEqual(config["rules"], ["MATCH,WG-IMP"])
        self.assertEqual(len(config["proxies"]), 1)

    def test_probe_gate_requires_repeated_success_at_multiple_destinations(self):
        module = self.module()
        self.assertTrue(module.probes_acceptable([
            {"target": name, "ok": True} for name in ("cloudflare", "google", "github") for _ in range(3)
        ]))
        self.assertFalse(module.probes_acceptable([
            {"target": "cloudflare", "ok": True} for _ in range(9)
        ]))
        self.assertFalse(module.probes_acceptable([
            {"target": name, "ok": name == "google"} for name in ("cloudflare", "google", "github") for _ in range(3)
        ]))

    def test_hysteria_profile_without_mport_uses_only_supplied_udp_port(self):
        module = self.module()
        profile = module.parse_profile(HY2_URI)
        self.assertEqual(profile, {
            "name": "HY2-USA", "type": "hysteria2", "server": "reserve.example",
            "port": 443, "password": "test-auth-secret", "obfs": "salamander",
            "obfs-password": "test-obfs-secret", "sni": "reserve.example",
            "skip-cert-verify": False, "udp": True,
        })

    def test_hysteria_port_hopping_is_present_only_when_explicitly_supplied(self):
        module = self.module()
        self.assertEqual(module.parse_profile(HY2_URI + "&mport=20000-50000")["ports"], "20000-50000")

    def test_hysteria_rejects_unsafe_or_ambiguous_input_without_echoing_secrets(self):
        module = self.module()
        for bad in (HY2_URI + "&insecure=1", HY2_URI + "&sni=other.example",
                    HY2_URI.replace("obfs=salamander", "obfs=unknown"),
                    HY2_URI.replace("obfs-password=test-obfs-secret", "obfs-password="),
                    HY2_URI + "&mport=65536-70000", HY2_URI + "&mport=50000-20000",
                    HY2_URI.replace("test-auth-secret@", "@")):
            with self.assertRaises(ValueError) as caught:
                module.parse_profile(bad)
            self.assertNotIn("test-auth-secret", str(caught.exception))
            self.assertNotIn("test-obfs-secret", str(caught.exception))

    def test_reserve_replacement_removes_old_ports_and_preserves_primary_and_rules(self):
        module = self.module()
        before = BEFORE.replace("    password: reserve-secret", "    ports: 20000-50000\n    password: reserve-secret")
        result = module.replace_outbound(before, module.parse_profile(HY2_URI))
        self.assertEqual(result.split("  - name: HY2-USA", 1)[0], before.split("  - name: HY2-USA", 1)[0])
        self.assertEqual(result.split("proxy-groups:", 1)[1], before.split("proxy-groups:", 1)[1])
        self.assertNotIn("    ports:", result)
        self.assertNotIn("password: reserve-secret", result)
        self.assertIn('    server: "reserve.example"', result)
        self.assertEqual(module.replace_outbound(result, module.parse_profile(HY2_URI)), result)

    def test_missing_or_duplicate_reserve_refuses_to_touch_primary(self):
        module = self.module()
        profile = module.parse_profile(HY2_URI)
        for bad in (BEFORE.replace("name: HY2-USA", "name: OTHER"),
                    BEFORE.replace("  - name: WG-IMP", "  - name: HY2-USA")):
            with self.assertRaises(ValueError):
                module.replace_outbound(bad, profile)

    def test_isolated_reserve_check_routes_only_to_reserve_without_public_listener(self):
        module = self.module()
        config = module.isolated_config(module.parse_profile(HY2_URI), 34567)
        self.assertEqual(config["rules"], ["MATCH,HY2-USA"])
        self.assertEqual([p["name"] for p in config["proxies"]], ["HY2-USA"])
        self.assertEqual(config["bind-address"], "127.0.0.1")
        self.assertFalse(config["allow-lan"])
        self.assertFalse(config["tun"]["enable"])
        self.assertFalse(config["dns"]["enable"])

    def test_reserve_transaction_gates_on_reserve_not_primary_and_persists(self):
        self.check_reserve_transaction(reserve_ok=True)

    def test_failed_reserve_probe_restores_runtime_and_preserves_file(self):
        self.check_reserve_transaction(reserve_ok=False)

    def check_reserve_transaction(self, reserve_ok):
        module = self.module()
        before = BEFORE.replace("type: wireguard", "type: vless")

        class Controller:
            runtime = before

            def inspect(self):
                return {"primary_type": "Vless", "reserve_type": "Hysteria2",
                        "fallback_order": ["WG-IMP", "HY2-USA"],
                        "fallback_selected": "WG-IMP", "rule_count": 2}

            def apply(self, configuration):
                self.runtime = configuration

            def probes(self, name, rounds=2):
                return [{"target": target, "ok": reserve_ok and name == "HY2-USA"}
                        for target in ("cloudflare", "google", "github") for _ in range(rounds)]

        controller = Controller()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(before, encoding="utf-8", newline="")
            job = Path(directory) / "job"
            job.mkdir()
            with patch.object(module, "CONFIG", config), patch.object(module, "Controller", return_value=controller), \
                    patch.object(module, "preflight"), patch.object(module, "encrypted_backup"):
                if reserve_ok:
                    module.run(module.parse_profile(HY2_URI), job, True)
                    status = json.loads((job / "status.json").read_text(encoding="utf-8"))
                    self.assertEqual(status["state"], "success")
                    self.assertTrue(status["production_changed"])
                    self.assertIn('server: "reserve.example"', config.read_text(encoding="utf-8"))
                    self.assertEqual(config.read_text(encoding="utf-8"), controller.runtime)
                    self.assertEqual(controller.runtime.split("  - name: HY2-USA", 1)[0], before.split("  - name: HY2-USA", 1)[0])
                else:
                    with self.assertRaises(RuntimeError):
                        module.run(module.parse_profile(HY2_URI), job, True)
                    self.assertEqual(config.read_text(encoding="utf-8"), before)
                    self.assertEqual(controller.runtime, before)


if __name__ == "__main__":
    unittest.main()
