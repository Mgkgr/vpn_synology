import copy
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AntidpiDnsTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('antidpi_runtime_dns', ROOT / 'antidpi/runtime.py')
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)
        self.config = {
            'dns': {'enable': True, 'listen': '0.0.0.0:53', 'ipv6': False,
                    'enhanced-mode': 'fake-ip', 'fake-ip-range': '198.18.0.1/16',
                    'fake-ip-filter': ['*.lan', '*.local', 'localhost'],
                    'respect-rules': True, 'nameserver': ['https://dns.example/dns-query'],
                    'proxy-server-nameserver': ['https://dns.example/dns-query']},
            'rule-providers': {'managed-antidpi': {'type': 'file', 'behavior': 'classical',
                                                'format': 'text', 'path': './rules/managed-antidpi.txt'}},
            'rules': ['RULE-SET,managed-direct,DIRECT', 'MATCH,VPS-FALLBACK'],
            'proxy-groups': [{'name': 'VPS-FALLBACK', 'proxies': ['WG-IMP', 'HY2-USA']}],
        }

    def test_dns_filter_adds_only_antidpi_provider_without_mutating_existing_config(self):
        original = copy.deepcopy(self.config)
        self.assertTrue(callable(getattr(self.runtime, 'configure_dns_filter', None)),
                        'native scoped FakeIP filter transform is missing')
        updated = self.runtime.configure_dns_filter(self.config)
        expected = copy.deepcopy(original)
        expected['dns']['fake-ip-filter'].append('rule-set:managed-antidpi')
        self.assertEqual(updated, expected)
        self.assertEqual(self.config, original)
        self.assertEqual(self.runtime.configure_dns_filter(updated), updated)

    def test_unsupported_dns_modes_or_provider_contracts_are_not_silently_rewritten(self):
        self.assertTrue(callable(getattr(self.runtime, 'configure_dns_filter', None)))
        for field, value in (('fake-ip-filter-mode', 'whitelist'), ('fake-ip-filter-mode', 'rule'),
                             ('enable', False), ('ipv6', True), ('enhanced-mode', 'redir-host')):
            changed = copy.deepcopy(self.config)
            changed['dns'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.runtime.configure_dns_filter(changed)
        changed = copy.deepcopy(self.config)
        changed['rule-providers']['managed-antidpi']['behavior'] = 'ipcidr'
        with self.assertRaises(ValueError):
            self.runtime.configure_dns_filter(changed)

    def test_only_one_numeric_private_dns_server_is_permitted(self):
        self.assertTrue(callable(getattr(self.runtime, 'resolver_config', None)))
        self.assertEqual(self.runtime.resolver_config('172.24.0.2'),
                         'nameserver 172.24.0.2\noptions timeout:1 attempts:1 ndots:1\n')
        for value in ('dns.example', '8.8.8.8', '198.18.0.1', '0.0.0.0', '::1',
                      '172.24.0.2\nnameserver 8.8.8.8', '224.0.0.1', '169.254.1.1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.runtime.resolver_config(value)

    def test_domain_mode_requires_matching_resolver_file_and_has_no_fallback(self):
        self.assertTrue(callable(getattr(self.runtime, 'validate_dns_contract', None)))
        contract = {'version': 1, 'resolver': '172.24.0.2'}
        resolver = self.runtime.validate_dns_contract(contract, self.runtime.resolver_config('172.24.0.2'))
        self.assertEqual(resolver, '172.24.0.2')
        cfg = self.runtime.auth_config('x' * 43, resolver=resolver)
        self.assertEqual(cfg['dns']['nameserver'], [resolver])
        self.assertEqual(cfg['dns']['default-nameserver'], [resolver])
        self.assertFalse(cfg['dns']['respect-rules'])
        self.assertNotIn('listen', cfg['dns'])
        self.assertNotIn('fallback', cfg['dns'])
        self.assertIn('IP-CIDR,198.18.0.0/15,REJECT', cfg['rules'])
        self.assertNotIn('--no-domain', self.runtime.engine_argv(resolver=resolver))
        self.assertIn('--no-domain', self.runtime.engine_argv())
        for text in ('nameserver 8.8.8.8\n', self.runtime.resolver_config(resolver) + 'nameserver 1.1.1.1\n'):
            with self.assertRaises(ValueError):
                self.runtime.validate_dns_contract(contract, text)
        with self.assertRaises(ValueError):
            self.runtime.validate_dns_contract(dict(contract, fallback='8.8.8.8'), self.runtime.resolver_config(resolver))


if __name__ == '__main__':
    unittest.main()
