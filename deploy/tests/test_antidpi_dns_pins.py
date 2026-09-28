import copy
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DnsPinsTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('dns_pins', ROOT / 'antidpi/dns_pins.py')
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.contract = {'version': 2, 'created_at': 1000, 'expires_at': 1300,
                         'source_revision': 'a' * 64,
                         'hosts': {'www.youtube.com': ['142.250.74.206']}}

    def test_short_lease_accepts_only_exact_domains_and_public_ipv4(self):
        result = self.m.validate_pins(self.contract, now=1001)
        self.assertEqual(result, self.contract)
        result['hosts']['www.youtube.com'].append('8.8.8.8')
        self.assertEqual(len(self.contract['hosts']['www.youtube.com']), 1)
        for bad in ('127.0.0.1', '10.1.2.3', '192.168.2.103', '172.24.0.2',
                    '169.254.169.254', '100.64.1.1', '198.18.0.1', '198.19.1.1',
                    '224.0.0.1', '0.0.0.0', '::1', '192.0.2.1', '240.0.0.1'):
            changed = copy.deepcopy(self.contract)
            changed['hosts']['www.youtube.com'] = [bad]
            with self.subTest(address=bad), self.assertRaises(ValueError):
                self.m.validate_pins(changed, now=1001)
        for name in ('*.youtube.com', 'www.youtube.com\n', 'localhost', '8.8.8.8',
                     'a..test', 'www.youtube.com:443', 'www.youtube.com/DIRECT'):
            changed = dict(self.contract, hosts={name: ['8.8.8.8']})
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.m.validate_pins(changed, now=1001)

    def test_expiry_clock_reversal_oversized_or_extra_config_fail_closed(self):
        for now in (999, 1300, 1500):
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.m.validate_pins(self.contract, now=now)
        for changed in (dict(self.contract, expires_at=1601),
                        dict(self.contract, resolver='8.8.8.8'),
                        dict(self.contract, source_revision='wrong'),
                        dict(self.contract, hosts={}),
                        dict(self.contract, version=True),
                        dict(self.contract, hosts={'x.test': ['8.8.8.8'] * 17})):
            with self.assertRaises(ValueError):
                self.m.validate_pins(changed, now=1001)

    def test_native_hosts_config_has_no_dns_lookup_and_rejects_unknown_destinations(self):
        spec = importlib.util.spec_from_file_location('runtime_pins', ROOT / 'antidpi/runtime.py')
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        cfg = self.m.pinned_auth_config(runtime.auth_config('x' * 43), self.contract, now=1001)
        self.assertEqual(cfg['hosts'], self.contract['hosts'])
        self.assertFalse(cfg['dns']['enable'])
        self.assertNotIn('nameserver', str(cfg))
        self.assertNotIn('DIRECT', str(cfg))
        self.assertEqual(cfg['rules'][-1], 'MATCH,REJECT')
        self.assertIn('AND,((DOMAIN,www.youtube.com),(DST-PORT,443)),BYEDPI', cfg['rules'])
        self.assertIn('AND,((IP-CIDR,142.250.74.206/32,no-resolve),(DST-PORT,443)),BYEDPI', cfg['rules'])
        self.assertNotIn('MATCH,BYEDPI', cfg['rules'])
        self.assertIn('--no-domain', runtime.engine_argv())


if __name__ == '__main__':
    unittest.main()
