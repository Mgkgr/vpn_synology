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

    def test_strategy_request_is_one_fixed_domain_and_never_automatic_application(self):
        self.assertTrue(callable(getattr(self.m, 'parse_request', None)), 'strategy request validation is missing')
        self.assertEqual(self.m.parse_request(['--approved', 'a'*32, 'strategies', 'discord.com']),
                         ('a'*32, 'strategies', 'discord.com'))
        self.assertEqual(self.m.parse_request(['--approved', 'a'*32, 'sites']), ('a'*32, 'sites', None))
        for args in (['--approved', 'a'*32, 'strategies'],
                     ['--approved', 'a'*32, 'strategies', '127.0.0.1'],
                     ['--approved', 'a'*32, 'strategies', 'discord.com', '--apply']):
            with self.assertRaises(ValueError):
                self.m.parse_request(args)

    def test_strategy_failure_or_incomplete_result_is_not_successful_acceptance(self):
        self.assertEqual(self.m.acceptance_exit({'state':'success', 'mode':'strategies',
                                                'matrix':{'stop_reason':'completed','accepted_candidates':[]}}), 2)
        self.assertEqual(self.m.acceptance_exit({'state':'success', 'mode':'strategies',
                                                'matrix':{'stop_reason':'time_limit','accepted_candidates':['split-1']}}), 2)
        self.assertEqual(self.m.acceptance_exit({'state':'success', 'mode':'strategies',
                                                'matrix':{'stop_reason':'completed','accepted_candidates':['split-1']}}), 0)

    def test_strategy_dns_query_only_requests_the_chosen_service(self):
        self.assertTrue(callable(getattr(self.m, 'dns_query', None)), 'scoped DNS query is missing')
        for host in ('web.telegram.org', 'www.instagram.com'):
            self.assertEqual(self.m.parse_request(['--approved','a'*32,'strategies',host])[-1], host)
            source = self.m.dns_query((host,))
            # Execute the query with a fake HTTP boundary; ensure no unrelated name is queried.
            import sys
            import types
            from unittest.mock import patch
            from urllib.parse import urlparse, parse_qs
            observed = []
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, size): return b'{"Answer":[]}'
            class Opener:
                def open(self, request, timeout):
                    observed.append(parse_qs(urlparse(request.full_url).query)['name'][0])
                    return Response()
            settings = types.SimpleNamespace(mihomo_url='http://controller',
                mihomo_api_secret=types.SimpleNamespace(get_secret_value=lambda: 'fixture-token'))
            stub = types.SimpleNamespace(Settings=types.SimpleNamespace(from_env=lambda: settings))
            with patch.dict(sys.modules, {'app.settings':stub}), \
                    patch('urllib.request.build_opener', return_value=Opener()), patch('builtins.print'):
                exec(source, {})
            self.assertEqual(observed, [host])
        with self.assertRaises(ValueError):
            self.m.dns_query(('localhost',))

    def test_confirmation_mode_retains_fixed_target_and_negative_exit_code(self):
        self.assertEqual(self.m.parse_request(['--approved','a'*32,'confirm','www.instagram.com']),
                         ('a'*32,'confirm','www.instagram.com'))
        self.assertEqual(self.m.acceptance_exit({'state':'success','mode':'confirm',
            'matrix':{'stop_reason':'completed','accepted_candidates':[]}}), 2)


if __name__ == '__main__':
    unittest.main()
