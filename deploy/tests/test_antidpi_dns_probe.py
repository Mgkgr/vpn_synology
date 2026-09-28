import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/probe-antidpi-dns-lifecycle.py'


class DnsProbeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'isolated DNS/lifecycle acceptance harness is missing')
        spec = importlib.util.spec_from_file_location('antidpi_dns_probe', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_dns_fixture_alone_may_bind_privileged_port_in_private_namespace(self):
        options = self.module.fixture_options()
        self.assertEqual(options, ['--user', '0:0', '--cap-add', 'NET_BIND_SERVICE',
                                   '--entrypoint', 'python3'])
        self.assertNotIn('NET_ADMIN', options)

    def test_synthetic_dns_uses_no_real_domains_or_external_fallback(self):
        self.assertIn('antidpi.test', self.module.FIXTURE_CODE)
        self.assertIn('127.0.0.1:15353', self.module.FIXTURE_CODE)
        self.assertIn('configure_dns_filter', self.module.FIXTURE_CODE)
        self.assertNotIn('8.8.8.8', self.module.FIXTURE_CODE)
        self.assertNotIn('https://', self.module.FIXTURE_CODE)
        compile(self.module.FIXTURE_CODE, '<dns-fixture>', 'exec')
        compile(self.module.CLIENT_CODE, '<domain-peer>', 'exec')

    def test_recovery_verifies_real_container_id_and_namespace(self):
        metadata = {'Id': 'a' * 64, 'Image': 'sha256:' + 'b' * 64,
                    'State': {'Running': True, 'Pid': 123},
                    'Config': {'Labels': {'vpn.dashboard.role': 'engine'}},
                    'HostConfig': {'NetworkMode': 'none'}}
        result = self.module.inspected_state(metadata, 42, True)
        self.assertEqual(result['namespace'], 42)
        self.assertEqual(result['id'], 'a' * 64)
        self.assertEqual(result['image'], 'sha256:' + 'b' * 64)
        self.assertTrue(result['healthy'])


if __name__ == '__main__':
    unittest.main()
