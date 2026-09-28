"""Staging-only ByeDPI transport. No DNS forwarding or production routing yet."""
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time


SECRET_PATH = Path('/run/secrets/socks_password')
MIHOMO = '/usr/local/bin/mihomo'


def validate_secret(secret):
    if not isinstance(secret, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', secret):
        raise ValueError('a private generated SOCKS secret is required')
    return secret


def read_secret():
    fd = os.open(str(SECRET_PATH), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r', encoding='ascii') as stream:
        if os.fstat(stream.fileno()).st_mode & 0o077:
            raise PermissionError('SOCKS secret must not be group/world readable')
        return validate_secret(stream.read(129))


def auth_config(secret):
    return {
        'mode': 'rule', 'log-level': 'silent', 'ipv6': False, 'allow-lan': True,
        'dns': {'enable': False}, 'profile': {'store-selected': False, 'store-fake-ip': False},
        'listeners': [{'name': 'gateway-auth', 'type': 'socks', 'listen': '0.0.0.0',
                       'port': 1080, 'udp': False,
                       'users': [{'username': 'gateway', 'password': validate_secret(secret)}]}],
        'proxies': [{'name': 'BYEDPI', 'type': 'socks5', 'server': '127.0.0.1', 'port': 1081, 'udp': False}],
        'rules': ['NETWORK,udp,REJECT', 'IP-CIDR,198.18.0.0/15,REJECT,no-resolve',
                  'IP-CIDR6,::/0,REJECT,no-resolve', 'MATCH,BYEDPI'],
    }


def engine_argv():
    # Domain requests fail closed until a reviewed non-FakeIP DNS path is wired.
    return ['/usr/local/bin/ciadpi', '--ip', '127.0.0.1', '--port', '1081',
            '--conn-ip', '0.0.0.0', '--no-udp', '--no-domain', '--max-conn', '128',
            '--timeout', '10', '--split', '1']


def compose_config(engine_image, socks_image):
    for image in (engine_image, socks_image):
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
            raise ValueError('verified immutable local image ID required')
    def service(image, memory, role):
        return {'image': image, 'pull_policy': 'never', 'user': '10002:10002',
                'restart': 'no', 'read_only': True, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'], 'pids_limit': 64,
                'mem_limit': memory, 'stop_grace_period': '10s',
                'tmpfs': ['/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777'],
                'logging': {'driver': 'json-file', 'options': {'max-size': '1m', 'max-file': '2'}},
                'healthcheck': {'test': ['CMD', 'python3', '-B', '/app/healthcheck.py', role],
                                'interval': '10s', 'timeout': '4s', 'retries': 2, 'start_period': '5s'}}
    engine = service(engine_image, '256m', 'engine')
    engine['network_mode'] = 'none'
    socks = service(socks_image, '128m', 'socks')
    socks.update({'network_mode': 'service:antidpi',
                  'depends_on': {'antidpi': {'condition': 'service_healthy'}},
                  'volumes': [{'type': 'bind', 'source': './secrets/socks_password',
                               'target': '/run/secrets/socks_password', 'read_only': True}]})
    return {'name': 'vpn-antidpi-staging', 'services': {'antidpi': engine, 'socks': socks}}


def receive_exact(connection, size):
    result = bytearray()
    while len(result) < size:
        value = connection.recv(size - len(result))
        if not value:
            raise OSError('SOCKS connection closed before reply')
        result.extend(value)
    return bytes(result)


def probe_socks(port, secret=None):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.8) as connection:
            if secret is None:
                connection.sendall(b'\x05\x01\x00')
                return receive_exact(connection, 2) == b'\x05\x00'
            password = validate_secret(secret).encode('ascii')
            connection.sendall(b'\x05\x01\x02')
            if receive_exact(connection, 2) != b'\x05\x02':
                return False
            connection.sendall(b'\x01\x07gateway' + bytes([len(password)]) + password)
            return receive_exact(connection, 2) == b'\x01\x00'
    except (OSError, ValueError):
        return False


def run_socks():
    secret = read_secret()
    directory = Path('/tmp/mihomo')
    directory.mkdir(mode=0o700, exist_ok=True)
    config_path = directory / 'config.yaml'
    fd = os.open(str(config_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(auth_config(secret), stream)
    argv = [MIHOMO, '-d', str(directory), '-f', str(config_path)]
    validation = subprocess.run(argv + ['-t'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
    if validation.returncode != 0 or not probe_socks(1081):
        raise RuntimeError('private handler/config is not ready')
    stopping = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.append(True))
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while not stopping and child.poll() is None:
            if not probe_socks(1081):
                return 3  # Never keep an accepting auth proxy on a stale/dead namespace.
            time.sleep(1)
        return 0 if stopping else (child.returncode or 1)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def main():
    if sys.argv[1:] == ['engine']:
        argv = engine_argv()
        os.execv(argv[0], argv)
    if sys.argv[1:] == ['socks']:
        try:
            return run_socks()
        except Exception as error:
            print('ANTIDPI_SOCKS=unavailable; type=' + type(error).__name__, flush=True)
            return 1
    return 2


if __name__ == '__main__':
    sys.exit(main())
