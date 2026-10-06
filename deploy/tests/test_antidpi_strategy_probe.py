import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import types


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/antidpi-strategy-probe.py'


class StrategyProbeTests(unittest.TestCase):
    def module(self):
        self.assertTrue(SCRIPT.is_file(), 'bounded strategy probe is not implemented')
        spec = importlib.util.spec_from_file_location('strategy_probe', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_only_reviewed_ids_can_launch_engine_without_widening_network_access(self):
        m = self.module()
        self.assertEqual(m.engine_argv('disorder-sni')[-2:], ['--disorder', '1+s'])
        for strategy in m.CANDIDATES:
            argv = m.engine_argv(strategy)
            self.assertEqual(argv[:5], ['/usr/local/bin/ciadpi', '--ip', '127.0.0.1', '--port', '1081'])
            self.assertIn('--no-udp', argv)
            self.assertIn('--no-domain', argv)
            self.assertNotIn('--auto', argv)
        for invalid in ('--daemon', 'split-1; id', '/tmp/strategy', 'unknown', None):
            with self.assertRaises(ValueError):
                m.engine_argv(invalid)

    def run_fake(self, *, limit=60, cancel=None, advance=0):
        m = self.module()
        clock, seen, stopped = [0.0], [], []
        def start(strategy):
            return strategy
        def measure(handle, host, mode):
            seen.append((handle, host, mode))
            clock[0] += advance
            return {'ok': True, 'tls_ok': True, 'stage': 'complete', 'status': 200, 'elapsed_ms': 50}
        result = m.run_matrix('www.youtube.com', start, measure, stopped.append,
                              now=lambda: clock[0], max_attempts=limit, cancelled=cancel)
        return m, result, seen, stopped

    def test_real_matrix_repeats_candidates_and_control_without_applying_any_result(self):
        m, result, seen, stopped = self.run_fake()
        self.assertEqual(result['stop_reason'], 'completed')
        self.assertEqual(len(seen), 30)
        self.assertEqual(sum(mode == 'direct_control' for _, _, mode in seen), 6)
        self.assertEqual(len(stopped), 10)
        self.assertEqual(result['accepted_candidates'], list(m.CANDIDATES))
        self.assertFalse(result['applied'])

    def test_attempt_and_time_bounds_preserve_partial_result_and_cleanup(self):
        _, result, seen, stopped = self.run_fake(limit=4)
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(stopped), 2)
        self.assertEqual(result['stop_reason'], 'attempt_limit')
        self.assertEqual(result['accepted_candidates'], [])
        _, result, seen, stopped = self.run_fake(advance=160)
        self.assertEqual(len(seen), 3)
        self.assertEqual(result['stop_reason'], 'time_limit')
        self.assertEqual(len(stopped), 1)

    def test_cancellation_and_failed_measurement_do_not_leave_runtime_or_apply_partial(self):
        _, result, seen, stopped = self.run_fake(cancel=lambda: True)
        self.assertEqual(result['stop_reason'], 'cancelled')
        self.assertEqual(seen, [])
        self.assertEqual(stopped, [])
        m = self.module()
        removed = []
        def broken(*args):
            raise RuntimeError('fixture failure')
        with self.assertRaises(RuntimeError):
            m.run_matrix('discord.com', lambda strategy: 'test-runtime', broken, removed.append)
        self.assertEqual(removed, ['test-runtime'])

    def test_target_and_limits_are_validated_before_creating_containers(self):
        m = self.module()
        def forbidden(*args):
            self.fail('side effect before validation')
        for host in ('127.0.0.1', 'example.org', 'https://discord.com', 'discord.com/path', '*.discord.com'):
            with self.assertRaises(ValueError):
                m.run_matrix(host, forbidden, forbidden, forbidden)
        for limit in (0, 61, True, 1.5):
            with self.assertRaises(ValueError):
                m.run_matrix('discord.com', forbidden, forbidden, forbidden, max_attempts=limit)

    def test_telegram_and_instagram_are_explicit_targets_not_arbitrary_urls(self):
        m = self.module()
        seen = []
        def measure(handle, host, mode):
            seen.append(host)
            return {'ok': True, 'tls_ok': True, 'status': 200, 'elapsed_ms': 10}
        for host in ('web.telegram.org', 'www.instagram.com'):
            result = m.run_matrix(host, lambda _: None, measure, lambda _: None)
            self.assertEqual(result['stop_reason'], 'completed')
        self.assertEqual(seen.count('web.telegram.org'), 30)
        self.assertEqual(seen.count('www.instagram.com'), 30)

    def test_confirmation_only_retests_reviewed_candidate_with_both_controls(self):
        m = self.module()
        seen = []
        def measure(handle, host, mode):
            seen.append((handle, mode))
            return {'ok': True, 'tls_ok': True, 'status': 200, 'elapsed_ms': 10}
        result = m.run_matrix('web.telegram.org', lambda name: name, measure, lambda _: None,
                              candidates=('tlsrec-sni',))
        self.assertEqual(len(seen), 9)
        self.assertEqual(sum(mode == 'byedpi' for _, mode in seen), 3)
        self.assertEqual(result['accepted_candidates'], ['tlsrec-sni'])
        for candidates in ((), ('unreviewed',), ('tlsrec-sni','tlsrec-sni'), '--tlsrec'):
            with self.assertRaises(ValueError):
                m.run_matrix('discord.com', lambda _: self.fail('created'), measure, lambda _: None,
                             candidates=candidates)

    def test_http_denial_is_distinct_from_tls_timeout_and_not_accepted(self):
        m = self.module()
        def measure(handle, host, mode):
            return {'ok': False, 'tls_ok': True, 'stage': 'complete', 'status': 403, 'elapsed_ms': 50}
        result = m.run_matrix('discord.com', lambda strategy: strategy, measure, lambda _: None)
        self.assertEqual(result['accepted_candidates'], [])
        self.assertTrue(all(row['tls_ok'] for row in result['measurements']))
        self.assertTrue(all(row['status'] == 403 for row in result['measurements']))

    def test_https_probe_uses_verified_tls_and_reports_http_denial_separately(self):
        m = self.module()
        self.assertTrue(callable(getattr(m, 'probe_https', None)), 'HTTPS probe is missing')
        class Peer:
            closed = False
            def settimeout(self, value):
                self.timeout = value
            def sendall(self, data):
                self.request = data
            def recv(self, size):
                return b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n'
            def version(self):
                return 'TLSv1.3'
            def close(self):
                self.closed = True
        peer = Peer()
        class Context:
            def wrap_socket(self, connection, server_hostname):
                if server_hostname != 'discord.com':
                    raise ValueError('wrong SNI')
                return connection
        runtime = types.SimpleNamespace(read_dns_pins=lambda: {'hosts': {'discord.com': ['162.159.128.233']}})
        with patch.dict('sys.modules', {'runtime': runtime}), \
                patch('socket.create_connection', return_value=peer), \
                patch('ssl.create_default_context', return_value=Context()):
            row = m.probe_https('discord.com', 'direct_control')
        self.assertEqual(row['status'], 403)
        self.assertTrue(row['tls_ok'])
        self.assertFalse(row['ok'])
        self.assertTrue(peer.closed)
        self.assertLessEqual(peer.timeout, 10)
        self.assertIn(b'Host: discord.com\r\n', peer.request)

    def test_https_error_is_bounded_and_does_not_disclose_raw_exception(self):
        m = self.module()
        self.assertTrue(callable(getattr(m, 'probe_https', None)), 'HTTPS probe is missing')
        runtime = types.SimpleNamespace(read_dns_pins=lambda: {'hosts': {'discord.com': ['162.159.128.233']}})
        with patch.dict('sys.modules', {'runtime': runtime}), \
                patch('socket.create_connection', side_effect=TimeoutError('private-value')):
            row = m.probe_https('discord.com', 'direct_control')
        self.assertEqual(row['error_type'], 'TimeoutError')
        self.assertEqual(row['stage'], 'connect')
        self.assertFalse(row['tls_ok'])
        self.assertNotIn('private-value', str(row))


if __name__ == '__main__':
    unittest.main()
