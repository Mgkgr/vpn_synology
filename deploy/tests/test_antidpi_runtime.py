import importlib.util
import json
from pathlib import Path
import socket
import threading
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'antidpi' / 'runtime.py'


class AntidpiRuntimeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('antidpi_runtime', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_auth_config_requires_password_and_has_no_direct_fallback(self):
        cfg = self.module.auth_config('x' * 43)
        self.assertEqual(cfg['listeners'][0]['users'], [{'username': 'gateway', 'password': 'x' * 43}])
        self.assertEqual(cfg['listeners'][0]['port'], 1080)
        self.assertFalse(cfg['listeners'][0]['udp'])
        self.assertEqual(cfg['proxies'], [{'name': 'BYEDPI', 'type': 'socks5', 'server': '127.0.0.1', 'port': 1081, 'udp': False}])
        self.assertEqual(cfg['rules'][-1], 'MATCH,BYEDPI')
        self.assertNotIn('DIRECT', str(cfg))
        self.assertIn('IP-CIDR,198.18.0.0/15,REJECT,no-resolve', cfg['rules'])
        self.assertNotIn('tun', cfg)
        self.assertFalse(cfg['dns']['enable'])
        self.assertNotIn('external-controller', cfg)

    def test_missing_weak_or_multiline_secret_is_rejected_before_config(self):
        for secret in ('', 'short', 'x' * 43 + '\n', 'x' * 129, 'test: ' + 'x' * 40):
            with self.subTest(secret_length=len(secret)), self.assertRaises(ValueError):
                self.module.auth_config(secret)

    def test_staging_engine_cannot_use_system_dns_udp_or_bind_publicly(self):
        argv = self.module.engine_argv()
        self.assertEqual(argv[argv.index('--ip') + 1], '127.0.0.1')
        self.assertEqual(argv[argv.index('--port') + 1], '1081')
        self.assertIn('--no-udp', argv)
        self.assertIn('--no-domain', argv)
        self.assertEqual(argv[argv.index('--conn-ip') + 1], '0.0.0.0')

    def test_compose_is_private_and_has_only_immutable_nonroot_images(self):
        cfg = self.module.compose_config('sha256:' + 'a' * 64, 'sha256:' + 'b' * 64)
        self.assertEqual(cfg['services']['socks']['network_mode'], 'service:antidpi')
        for service in cfg['services'].values():
            self.assertEqual(service['cap_drop'], ['ALL'])
            self.assertTrue(service['read_only'])
            self.assertEqual(service['user'], '10002:10002')
            self.assertEqual(service['pull_policy'], 'never')
            self.assertEqual(service['pids_limit'], 64)
            self.assertNotIn('ports', service)
            self.assertNotIn('privileged', service)
            self.assertNotIn('cpus', service)
            self.assertNotIn('docker.sock', str(service))
        for image in ('latest', 'python:latest', 'sha256:abc', 'evil;command'):
            with self.subTest(image=image), self.assertRaises(ValueError):
                self.module.compose_config(image, 'sha256:' + 'b' * 64)

    def test_checked_in_staging_compose_matches_the_tested_private_contract(self):
        root = SCRIPT.parent
        versions = json.loads((root / 'versions.json').read_text(encoding='utf-8'))
        actual = json.loads((root / 'compose.staging.json').read_text(encoding='utf-8'))
        self.assertEqual(actual, self.module.compose_config(versions['engine_image_id'], versions['socks_image_id']))
        self.assertFalse(versions['production_ready'])

    def serve(self, responses, check):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        failures = []
        def server():
            try:
                listener.settimeout(2)
                with listener.accept()[0] as peer:
                    peer.settimeout(2)
                    for expected, answer in responses:
                        actual = bytearray()
                        while len(actual) < len(expected):
                            chunk = peer.recv(len(expected) - len(actual))
                            if not chunk:
                                break
                            actual.extend(chunk)
                        if bytes(actual) != expected:
                            failures.append((bytes(actual), expected))
                        peer.sendall(answer)
            finally:
                listener.close()
        worker = threading.Thread(target=server, daemon=True)
        worker.start()
        try:
            return check(port)
        finally:
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])

    def test_health_accepts_only_completed_socks_auth_exchange(self):
        secret = 'x' * 43
        auth = b'\x01\x07gateway\x2b' + secret.encode()
        result = self.serve([(b'\x05\x01\x02', b'\x05\x02'), (auth, b'\x01\x00')],
                            lambda port: self.module.probe_socks(port, secret))
        self.assertTrue(result)
        result = self.serve([(b'\x05\x01\x02', b'\x05\x02'), (auth, b'\x01\x01')],
                            lambda port: self.module.probe_socks(port, secret))
        self.assertFalse(result)

    def test_health_does_not_accept_open_tcp_port_or_unauthenticated_downgrade(self):
        for response in (b'', b'\x05', b'\x05\x00', b'\x04\x02', b'\x05\xff'):
            with self.subTest(response=response):
                result = self.serve([(b'\x05\x01\x02', response)],
                                    lambda port: self.module.probe_socks(port, 'x' * 43))
                self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
