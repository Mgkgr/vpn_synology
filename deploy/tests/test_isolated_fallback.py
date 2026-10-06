import copy
import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).parents[1] / "scripts" / "verify-isolated-fallback.py"
PRIMARY = {"name": "WG-IMP", "type": "vless", "server": "primary.example", "port": 443}
RESERVE = {"name": "HY2-USA", "type": "hysteria2", "server": "reserve.example", "port": 443}


class IsolatedFallbackTests(unittest.TestCase):
    def module(self):
        self.assertTrue(MODULE.is_file(), "isolated verification is not implemented")
        spec = importlib.util.spec_from_file_location("isolated_fallback", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_baseline_is_local_only_and_cannot_take_over_client_routes(self):
        config = self.module().build_config(PRIMARY, RESERVE, 32001, 32002, "test-secret", 32003)
        self.assertEqual(config["bind-address"], "127.0.0.1")
        self.assertEqual(config["external-controller"], "127.0.0.1:32002")
        self.assertFalse(config["allow-lan"])
        self.assertFalse(config["tun"]["enable"])
        self.assertFalse(config["dns"]["enable"])
        self.assertEqual(config["proxy-groups"][0]["proxies"], ["WG-IMP", "HY2-USA"])
        self.assertEqual(config["proxy-groups"][0]["type"], "fallback")
        self.assertEqual(config["rules"], ["MATCH,MAINT-FALLBACK"])
        self.assertEqual(config["proxies"], [PRIMARY, RESERVE])

    def test_failure_injection_only_changes_isolated_copy_of_primary(self):
        before = copy.deepcopy((PRIMARY, RESERVE))
        config = self.module().build_config(PRIMARY, RESERVE, 32001, 32002, "test-secret", 32003, primary_down=True)
        self.assertEqual(config["proxies"][0]["server"], "127.0.0.1")
        self.assertEqual(config["proxies"][0]["port"], 32003)
        self.assertEqual(config["proxies"][1], RESERVE)
        self.assertEqual((PRIMARY, RESERVE), before)

    def test_ports_and_known_identifiers_are_required(self):
        module = self.module()
        with self.assertRaises(ValueError):
            module.build_config(PRIMARY, RESERVE, 32001, 32001, "test-secret", 32003)
        with self.assertRaises(ValueError):
            module.build_config(dict(PRIMARY, name="unexpected"), RESERVE, 32001, 32002, "test-secret", 32003)


if __name__ == "__main__":
    unittest.main()
