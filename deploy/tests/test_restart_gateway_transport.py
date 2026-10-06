import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'restart-gateway-transport.py'


class Docker:
    def __init__(self, namespace='container:wireguard-id'):
        self.namespace = namespace
        self.calls = []
        self.digest = 'same-client-keys'

    def container_id(self, name):
        return 'wireguard-id'

    def network_mode(self, name):
        return self.namespace

    def client_digest(self):
        return self.digest

    def stop(self, name):
        self.calls.append(('stop', name))

    def start(self, name):
        self.calls.append(('start', name))

    def wait_wireguard(self):
        self.calls.append(('ready', 'wireguard'))

    def wait_controller(self):
        self.calls.append(('ready', 'mihomo'))


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'ordered transport restart has not been implemented')
        spec = importlib.util.spec_from_file_location('restart', SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_restarts_only_the_namespace_pair_in_dependency_order(self):
        docker = Docker()
        self.m.restart_gateway(docker)
        self.assertEqual(docker.calls, [('stop', 'vpn-mihomo'), ('stop', 'vpn-wireguard'),
                                       ('start', 'vpn-wireguard'), ('ready', 'wireguard'),
                                       ('start', 'vpn-mihomo'), ('ready', 'mihomo')])

    def test_wrong_namespace_is_rejected_before_stopping_anything(self):
        docker = Docker('bridge')
        with self.assertRaisesRegex(RuntimeError, 'namespace'):
            self.m.restart_gateway(docker)
        self.assertEqual(docker.calls, [])

    def test_changed_client_keys_are_reported_as_failure(self):
        docker = Docker()
        def changed():
            docker.digest = 'different-client-keys'
        docker.wait_controller = changed
        with self.assertRaisesRegex(RuntimeError, 'client'):
            self.m.restart_gateway(docker)

    def test_start_failure_attempts_to_bring_back_both_containers(self):
        docker = Docker()
        first = True
        def fail_once():
            nonlocal first
            if first:
                first = False
                raise RuntimeError('not ready')
        docker.wait_wireguard = fail_once
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            self.m.restart_gateway(docker)
        self.assertIn(('start', 'vpn-mihomo'), docker.calls)
        self.assertEqual(docker.calls.count(('start', 'vpn-wireguard')), 2)


if __name__ == '__main__':
    unittest.main()
