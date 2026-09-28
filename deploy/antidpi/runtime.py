"""Staging-only ByeDPI transport; production registration is a separate gate."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import time


SECRET_PATH = Path('/run/secrets/socks_password')
DNS_CONTRACT_PATH = Path('/run/config/dns.json')
DNS_PINS_PATH = Path('/run/config/dns-pins.json')
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


def configure_dns_filter(config):
    """Pure candidate transform. Never writes or reloads the gateway config."""
    updated = copy.deepcopy(config)
    dns = updated.get('dns', {})
    provider = updated.get('rule-providers', {}).get('managed-antidpi', {})
    if (dns.get('enable') is not True or dns.get('ipv6', False) is not False
            or dns.get('enhanced-mode') != 'fake-ip'
            or dns.get('fake-ip-filter-mode', 'blacklist') != 'blacklist'
            or provider != {'type': 'file', 'behavior': 'classical', 'format': 'text',
                            'path': './rules/managed-antidpi.txt'}):
        raise ValueError('unsupported scoped DNS/provider contract')
    filters = dns.setdefault('fake-ip-filter', [])
    if not isinstance(filters, list) or any(not isinstance(value, str) for value in filters):
        raise ValueError('invalid existing fake-ip-filter')
    if 'rule-set:managed-antidpi' not in filters:
        filters.append('rule-set:managed-antidpi')
    return updated


def resolver_config(resolver):
    address = ipaddress.IPv4Address(resolver)
    # Explicit RFC1918/loopback only; never a hostname, provider DNS or FakeIP.
    if not any(address in ipaddress.IPv4Network(cidr) for cidr in
               ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8')):
        raise ValueError('numeric gateway DNS resolver required')
    return 'nameserver {}\noptions timeout:1 attempts:1 ndots:1\n'.format(address)


def validate_dns_contract(contract, resolv_conf):
    if (not isinstance(contract, dict) or set(contract) != {'version', 'resolver'}
            or type(contract['version']) is not int or contract['version'] != 1
            or not isinstance(contract['resolver'], str)
            or resolv_conf != resolver_config(contract['resolver'])):
        raise ValueError('DNS contract and resolver file must match exactly')
    return contract['resolver']


def read_dns_contract():
    try:
        fd = os.open(str(DNS_CONTRACT_PATH), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None  # Default remains IP-only; never inherit Docker/system DNS.
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        contract = json.loads(stream.read(1024))
    return validate_dns_contract(contract, Path('/etc/resolv.conf').read_text(encoding='ascii'))


def read_dns_pins():
    from dns_pins import validate_pins
    if DNS_CONTRACT_PATH.exists():
        # v1 was an offline fixed-answer experiment, not safe for Internet use.
        raise ValueError('legacy dynamic DNS contract is not permitted')
    try:
        fd = os.open(str(DNS_PINS_PATH), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384 or info.st_mode & 0o077:
            raise ValueError('DNS snapshot must be a private regular file')
        return validate_pins(json.loads(stream.read(16385)))


def auth_config(secret, resolver=None):
    config = {
        'mode': 'rule', 'log-level': 'silent', 'ipv6': False, 'allow-lan': True,
        'dns': {'enable': False}, 'profile': {'store-selected': False, 'store-fake-ip': False},
        'listeners': [{'name': 'gateway-auth', 'type': 'socks', 'listen': '0.0.0.0',
                       'port': 1080, 'udp': False,
                       'users': [{'username': 'gateway', 'password': validate_secret(secret)}]}],
        'proxies': [{'name': 'BYEDPI', 'type': 'socks5', 'server': '127.0.0.1', 'port': 1081, 'udp': False}],
        'rules': ['NETWORK,udp,REJECT', 'IP-CIDR,198.18.0.0/15,REJECT,no-resolve',
                  'IP-CIDR6,::/0,REJECT,no-resolve', 'MATCH,BYEDPI'],
    }
    if resolver is not None:
        resolver_config(resolver)
        config['dns'] = {'enable': True, 'ipv6': False, 'enhanced-mode': 'redir-host',
                         'use-hosts': False, 'use-system-hosts': False,
                         'respect-rules': False, 'nameserver': [resolver],
                         'default-nameserver': [resolver]}
        # Resolve before forwarding domains too, so a FakeIP DNS answer is rejected.
        config['rules'][1] = 'IP-CIDR,198.18.0.0/15,REJECT'
    return config


def engine_argv(resolver=None):
    argv = ['/usr/local/bin/ciadpi', '--ip', '127.0.0.1', '--port', '1081',
            '--conn-ip', '0.0.0.0', '--no-udp', '--no-domain', '--max-conn', '128',
            '--timeout', '10', '--split', '1']
    if resolver is not None:
        resolver_config(resolver)
        argv.remove('--no-domain')
    return argv


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
    pins = read_dns_pins()
    directory = Path('/tmp/mihomo')
    directory.mkdir(mode=0o700, exist_ok=True)
    config_path = directory / 'config.yaml'
    fd = os.open(str(config_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        config = auth_config(secret)
        if pins is not None:
            from dns_pins import pinned_auth_config
            config = pinned_auth_config(config, pins)
        json.dump(config, stream)
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
            try:
                if read_dns_pins() != pins:
                    return 4
            except Exception:
                return 4  # Expiry/drift closes this test listener and its connections.
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
        read_dns_pins()  # Reject the old DNS mode; never enable a second lookup.
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
