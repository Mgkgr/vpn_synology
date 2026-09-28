import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'probe-antidpi-staging.py'


class StagingProbeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('staging_probe', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_container_arguments_have_no_host_egress_or_root_capabilities(self):
        args = self.module.create_args('a' * 32, 'engine', 'sha256:' + 'b' * 64)
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertEqual(args[args.index('--user') + 1], '10002:10002')
        self.assertEqual(args[args.index('--cap-drop') + 1], 'ALL')
        self.assertIn('--read-only', args)
        self.assertEqual(args[args.index('--restart') + 1], 'no')
        for prohibited in ('--privileged', '--publish', '-p', '--cpus', 'docker.sock', '--pid=host'):
            self.assertNotIn(prohibited, ' '.join(args) if '.' in prohibited else args)

    def test_shared_namespace_accepts_only_a_verified_container_id(self):
        for bad in ('host', 'vpn-wireguard', '', '../other', 'a' * 63):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.module.create_args('a' * 32, 'socks', 'sha256:' + 'b' * 64, bad)
        args = self.module.create_args('a' * 32, 'socks', 'sha256:' + 'b' * 64, 'c' * 64)
        self.assertEqual(args[args.index('--network') + 1], 'container:' + 'c' * 64)

    def test_in_container_probe_parses_without_exposing_or_embedding_credentials(self):
        compile(self.module.PEER_CODE, '<isolated-peer>', 'exec')
        self.assertIn('read_secret()', self.module.PEER_CODE)
        self.assertNotIn('print(secret', self.module.PEER_CODE)
        self.assertNotIn('http', self.module.PEER_CODE)

    def test_firewall_comparison_ignores_only_capture_time_and_chain_counters(self):
        first = '# Generated at 10:00\n*filter\n:INPUT ACCEPT [1:20]\n-A INPUT -s 10.0.0.1 -j ACCEPT\nCOMMIT\n'
        later = first.replace('10:00', '10:01').replace('[1:20]', '[50:400]')
        changed_rule = later.replace('-s 10.0.0.1', '-s 10.0.0.2')
        normalize = self.module.normalize_network_state
        self.assertEqual(normalize(first, 'firewall4'), normalize(later, 'firewall4'))
        self.assertNotEqual(normalize(first, 'firewall4'), normalize(changed_rule, 'firewall4'))
        self.assertNotEqual(normalize(first, 'routes4'), normalize(later, 'routes4'))


if __name__ == '__main__':
    unittest.main()
