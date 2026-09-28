import importlib.util
from pathlib import Path
import unittest


class PinnedAcceptanceTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/probe-antidpi-pinned-sites.py'
        spec = importlib.util.spec_from_file_location('pinned_acceptance', path)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_site_probes_are_small_and_fixed_https_only(self):
        self.assertEqual(set(self.m.SITES), {'www.youtube.com', 'discord.com', 'www.wikipedia.org'})
        self.assertLessEqual(self.m.READ_LIMIT, 65536)
        self.assertLessEqual(self.m.ATTEMPTS, 3)
        self.assertIn('ssl.create_default_context()', self.m.SITE_CLIENT)
        self.assertNotIn('CERT_NONE', self.m.SITE_CLIENT)
        self.assertNotIn('urlopen', self.m.SITE_CLIENT)

    def test_external_diagnostics_always_use_a_new_namespace_without_published_ports(self):
        args = self.m.engine_network_args('bridge')
        self.assertEqual(args, ['--network', 'bridge'])
        for network in ('host', 'container:vpn-wireguard', 'vpn-gateway_default'):
            with self.assertRaises(ValueError):
                self.m.engine_network_args(network)

    def test_answer_projection_rejects_fakeip_or_private_and_bounds_cnames(self):
        answer = {'Answer': [{'type': 5, 'data': 'edge.example.'}, {'type': 1, 'data': '142.250.74.206'}]}
        self.assertEqual(self.m.answer_ipv4(answer), ['142.250.74.206'])
        for bad in ('198.18.0.1', '127.0.0.1', '192.168.2.103', '10.66.0.1'):
            with self.assertRaises(ValueError):
                self.m.answer_ipv4({'Answer': [{'type': 1, 'data': bad}]})
        with self.assertRaises(ValueError):
            self.m.answer_ipv4({'Answer': []})

    def test_direct_and_proxy_compare_the_same_dns_destination(self):
        answers = {host: ['142.250.74.206', '142.250.74.207'] for host in self.m.SITES}
        pinned = self.m.comparison_hosts(answers)
        self.assertEqual(pinned, {host: ['142.250.74.206'] for host in self.m.SITES})
        self.assertEqual(len(answers['www.youtube.com']), 2)

    def test_completed_measurements_do_not_exit_success_when_sites_fail(self):
        self.assertEqual(self.m.acceptance_exit({'state':'success','mode':'sites','site_acceptance':False}), 2)
        self.assertEqual(self.m.acceptance_exit({'state':'success','mode':'sites','site_acceptance':True}), 0)
        self.assertEqual(self.m.acceptance_exit({'state':'success','mode':'offline'}), 0)
        self.assertEqual(self.m.acceptance_exit({'state':'failed','mode':'sites'}), 1)


if __name__ == '__main__':
    unittest.main()
