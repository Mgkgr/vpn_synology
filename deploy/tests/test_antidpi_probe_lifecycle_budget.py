"""NAS startup time is not the 10-second HTTPS network budget."""
import base64
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance.antidpi_docker import CODE, DockerRuntime


class ProbeLifecycleBudgetTests(unittest.TestCase):
    def test_create_start_and_cleanup_have_separate_bounded_budgets(self):
        calls = []
        observation = dict(verdict='transport_error', reason='timeout', latency_ms=None, http_status=None)

        def run(argv, timeout, limit):
            calls.append((argv, timeout))
            if argv[1] == 'create':
                return 'a' * 64
            if argv[1] == 'start':
                return json.dumps(observation)
            if argv[1] == 'rm':
                return ''
            self.fail('probe combined startup and network in one CLI deadline')

        runtime = DockerRuntime('sha256:' + '1' * 64, CODE, None, runner=run)
        self.assertEqual(runtime.probe('discord.com', '162.159.135.232', 'tlsrec-sni', 'candidate'), observation)
        self.assertEqual([argv[1] for argv, _ in calls], ['create', 'start', 'rm'])
        self.assertEqual([timeout for _, timeout in calls], [25, 30, 15])
        create, start, remove = [argv for argv, _ in calls]
        name = create[create.index('--name') + 1]
        self.assertEqual(start[2:], ['-a', name])
        self.assertEqual(remove[2:], ['-f', name])
        self.assertIn('--rm', create)
        self.assertIn('--pull=never', create)
        self.assertNotIn('--publish', create)
        payload = json.loads(base64.b64decode(create[-1]))
        self.assertEqual(payload, dict(host='discord.com', address='162.159.135.232', strategy='tlsrec-sni', mode='candidate'))

    def test_production_probe_does_not_create_or_restart_any_container(self):
        calls = []

        def run(argv, timeout, limit):
            calls.append((argv, timeout))
            return '{"verdict":"success","reason":"verified","latency_ms":140,"http_status":200}'

        runtime = DockerRuntime('sha256:' + '1' * 64, CODE, None, runner=run)
        result = runtime.probe('discord.com', '162.159.135.232', 'tlsrec-sni', 'production')
        self.assertEqual(result['latency_ms'], 140)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1:3], ['exec', 'vpn-antidpi-socks'])
        self.assertEqual(calls[0][1], 15)


if __name__ == '__main__':
    unittest.main()
