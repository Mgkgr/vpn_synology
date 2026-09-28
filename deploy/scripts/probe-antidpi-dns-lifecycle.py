#!/usr/bin/env python3
"""Bounded offline acceptance; no production network, DNS, restart or registration."""
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid


DOCKER = '/usr/local/bin/docker'
STAGING = Path('/volume1/docker/vpn-gateway/.vless-maintenance')
FIXTURE_CODE = r'''
import json, os, socket, struct, subprocess, threading, time
from pathlib import Path
from runtime import configure_dns_filter
def dns_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(('127.0.0.1', 15353))
    while True:
        data, source = server.recvfrom(4096)
        offset, labels = 12, []
        while data[offset]:
            size = data[offset]
            labels.append(data[offset+1:offset+1+size].decode('ascii'))
            offset += size + 1
        offset += 1
        query_type = int.from_bytes(data[offset:offset+2], 'big')
        question = data[12:offset+4]
        host = '.'.join(labels)
        negative = host.startswith('missing-')
        answers = int(query_type == 1 and not negative)
        response = data[:2] + struct.pack('!HHHHH', 0x8183 if negative else 0x8180, 1, answers, 0, 0) + question
        if answers:
            ip = '198.18.0.9' if host.startswith('spoof-') else '127.0.0.1'
            response += b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 1, 4) + socket.inet_aton(ip)
        server.sendto(response, source)
def echo_connection(connection):
    with connection:
        connection.settimeout(20)
        try:
            while True:
                data = connection.recv(65536)
                if not data:
                    return
                connection.sendall(data)
        except OSError:
            pass
def echo_server():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 18080))
    server.listen(16)
    while True:
        threading.Thread(target=echo_connection, args=(server.accept()[0],), daemon=True).start()
threading.Thread(target=dns_server, daemon=True).start()
threading.Thread(target=echo_server, daemon=True).start()
root = Path('/tmp/fixture')
(root / 'rules').mkdir(parents=True)
(root / 'rules/managed-antidpi.txt').write_text('DOMAIN-SUFFIX,antidpi.test\n', encoding='ascii')
config = configure_dns_filter({
    'mode': 'rule', 'log-level': 'silent', 'ipv6': False,
    'dns': {'enable': True, 'listen': '127.0.0.1:53', 'ipv6': False,
            'enhanced-mode': 'fake-ip', 'fake-ip-range': '198.18.0.1/16',
            'fake-ip-filter': ['*.lan', '*.local', 'localhost'],
            'respect-rules': True, 'default-nameserver': ['127.0.0.1'],
            'nameserver': ['127.0.0.1:15353'], 'proxy-server-nameserver': ['127.0.0.1:15353']},
    'rule-providers': {'managed-antidpi': {'type': 'file', 'behavior': 'classical',
                                        'format': 'text', 'path': './rules/managed-antidpi.txt'}},
    'rules': ['MATCH,DIRECT'], 'profile': {'store-fake-ip': False, 'store-selected': False},
})
(root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
child = subprocess.Popen(['/usr/local/bin/mihomo', '-d', str(root), '-f', str(root / 'config.json')],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path('/tmp/dns-fixture.pid').write_text(str(child.pid), encoding='ascii')
while True:
    time.sleep(1)
'''
CLIENT_CODE = r'''
import ipaddress, json, socket, sys, time, uuid
from runtime import read_secret, receive_exact
secret = read_secret()
def connect(host):
    peer = socket.create_connection(('127.0.0.1', 1080), timeout=2)
    peer.settimeout(7)
    try:
        peer.sendall(b'\x05\x01\x02')
        if receive_exact(peer, 2) != b'\x05\x02':
            raise RuntimeError('auth method failed')
        password = secret.encode('ascii')
        peer.sendall(b'\x01\x07gateway' + bytes([len(password)]) + password)
        if receive_exact(peer, 2) != b'\x01\x00':
            raise RuntimeError('authentication failed')
        try:
            destination = b'\x01' + socket.inet_aton(host)
        except OSError:
            encoded = host.encode('ascii')
            destination = b'\x03' + bytes([len(encoded)]) + encoded
        peer.sendall(b'\x05\x01\x00' + destination + (18080).to_bytes(2, 'big'))
        header = receive_exact(peer, 4)
        if header[:2] != b'\x05\x00':
            raise ConnectionRefusedError('destination refused')
        if header[3] not in (1, 4):
            raise RuntimeError('unexpected address type')
        receive_exact(peer, 6 if header[3] == 1 else 18)
        return peer
    except Exception:
        peer.close()
        raise
def exchange(host, count=4):
    with connect(host) as peer:
        payload = bytes(range(256)) * 256
        for _ in range(count):
            peer.sendall(payload)
            if receive_exact(peer, len(payload)) != payload:
                raise RuntimeError('echo mismatch')
        return count * len(payload)
def refused(host):
    started = time.monotonic()
    try:
        exchange(host, 1)
    except OSError:
        return round(time.monotonic() - started, 3)
    raise RuntimeError('negative request unexpectedly carried data')
mode = sys.argv[1]
if mode == 'positive':
    real = socket.gethostbyname('echo.antidpi.test')
    fake = socket.gethostbyname('unselected.example.test')
    if real != '127.0.0.1' or ipaddress.ip_address(fake) not in ipaddress.ip_network('198.18.0.0/15'):
        raise RuntimeError('native scoped FakeIP filter failed')
    result = {'real_ip_for_selected': True, 'fake_ip_for_other': True,
              'domain_echo_bytes': exchange('echo.antidpi.test')}
elif mode == 'negative':
    token = uuid.uuid4().hex
    result = {'fake_literal_refused_s': refused('198.18.0.9'),
              'fake_dns_refused_s': refused('spoof-' + token + '.antidpi.test'),
              'nxdomain_refused_s': refused('missing-' + token + '.antidpi.test')}
elif mode == 'dns_down':
    result = {'dns_failure_refused_s': refused('down-' + uuid.uuid4().hex + '.antidpi.test')}
elif mode == 'no_listener':
    try:
        connection = socket.create_connection(('127.0.0.1', 1080), timeout=2)
    except OSError:
        result = {'dead_namespace_listener_closed': True}
    else:
        connection.close()
        raise RuntimeError('stale listener still accepts clients')
else:
    raise RuntimeError('unsupported test mode')
print(json.dumps(result), flush=True)
'''


def fixture_options():
    # Only synthetic DNS needs port 53. Real engine/auth remain UID 10002, cap-drop ALL.
    return ['--user', '0:0', '--cap-add', 'NET_BIND_SERVICE', '--entrypoint', 'python3']


def inspected_state(metadata, namespace, healthy):
    return {'id': metadata['Id'], 'image': metadata['Image'],
            'running': metadata['State']['Running'], 'namespace': namespace,
            'healthy': healthy, 'network': metadata['HostConfig']['NetworkMode'],
            'labels': metadata['Config'].get('Labels') or {}}


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if len(sys.argv) != 3 or sys.argv[1] != '--approved' or not re.fullmatch(r'[a-f0-9]{32}', sys.argv[2]):
        sys.exit('Only --approved <reviewed-build-id> is accepted.')
    if os.geteuid() != 0:
        sys.exit('Existing NAS root console required.')
    import fcntl
    lock = os.open(str(STAGING / 'antidpi-acceptance.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    builder = load('build-antidpi-staging')
    probe = load('probe-antidpi-staging')
    lifecycle = load('antidpi-lifecycle')
    command = builder.command
    run_id = uuid.uuid4().hex
    host_ns = os.stat('/proc/self/ns/net').st_ino
    report = {'run_id': run_id, 'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'state': 'failed', 'production_ready': False, 'scope': 'offline_dns_lifecycle',
              'stage': 'preflight', 'cleanup': {}, 'checks': {}, 'generations': []}
    created = []

    def stage(value):
        report['stage'] = value
        print('ANTIDPI_ACCEPTANCE_STAGE=' + value, flush=True)

    def snapshot():
        result = {'containers': builder.production_snapshot(), 'network': {}}
        for key, argv in (
            ('firewall4', ['/sbin/iptables-save']), ('firewall6', ['/sbin/ip6tables-save']),
            ('routes4', ['/sbin/ip', '-4', 'route', 'show', 'table', 'all']),
            ('routes6', ['/sbin/ip', '-6', 'route', 'show', 'table', 'all']),
            ('rules4', ['/sbin/ip', '-4', 'rule', 'show']), ('rules6', ['/sbin/ip', '-6', 'rule', 'show'])):
            result['network'][key] = hashlib.sha256(probe.normalize_network_state(command(argv), key).encode()).hexdigest()
        return result

    def inspect(container_id):
        value = json.loads(command([DOCKER, 'inspect', container_id]))[0]
        labels = value['Config'].get('Labels') or {}
        if (container_id not in created or value['Id'] != container_id
                or labels.get('vpn.dashboard.scope') != probe.SCOPE
                or labels.get('vpn.dashboard.run') != run_id):
            raise ValueError('container is outside this exact isolated probe')
        return value

    def remove(container_id):
        inspect(container_id)
        command([DOCKER, 'rm', '--force', container_id], timeout=20)
        created.remove(container_id)
        report['cleanup'][container_id] = 'removed'

    def healthy(container_id, role):
        return subprocess.run([DOCKER, 'exec', container_id, 'python3', '-B', '/app/healthcheck.py', role],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0

    def state(container_id, role):
        data = inspect(container_id)
        running = data['State']['Running']
        namespace = os.stat('/proc/{}/ns/net'.format(data['State']['Pid'])).st_ino if running else None
        return inspected_state(data, namespace, healthy(container_id, role) if running else False)

    def wait_health(container_id, role):
        for _ in range(20):
            if healthy(container_id, role):
                return
            time.sleep(0.5)
        raise RuntimeError('isolated service did not become ready')

    def client(fixture, mode):
        inspect(fixture)
        return json.loads(command([DOCKER, 'exec', '--user', '10002:10002', fixture,
                                   'python3', '-B', '-c', CLIENT_CODE, mode], timeout=25))

    before = snapshot()
    report['before'] = before
    try:
        build = json.loads((STAGING / ('antidpi-build-' + sys.argv[2] + '.json')).read_text())
        if (build.get('state') != 'success' or build.get('run_id') != sys.argv[2]
                or not build.get('production_unchanged') or build.get('context_sha256') != builder.CONTEXT_SHA256):
            raise ValueError('build not accepted')
        images = {role: build['images'][role]['verified']['id'] for role in ('engine', 'socks')}
        for image in images.values():
            labels = json.loads(command([DOCKER, 'image', 'inspect', '--format', '{{json .Config.Labels}}', image]))
            if labels.get('vpn.dashboard.run') != sys.argv[2] or labels.get('vpn.dashboard.scope') != 'antidpi-staging':
                raise ValueError('image provenance mismatch')
        report['build_id'] = sys.argv[2]
        with tempfile.TemporaryDirectory(prefix='vpn-antidpi-accept-', dir='/var/tmp') as directory:
            root = Path(directory)
            for name, content, mode in (
                ('socks_password', secrets.token_urlsafe(32), 0o600),
                ('dns.json', json.dumps({'version': 1, 'resolver': '127.0.0.1'}), 0o644),
                ('resolv.conf', 'nameserver 127.0.0.1\noptions timeout:1 attempts:1 ndots:1\n', 0o644)):
                fd = os.open(str(root / name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
                with os.fdopen(fd, 'w', encoding='ascii') as stream:
                    stream.write(content)
                    os.fchown(stream.fileno(), 10002, 10002)
                    os.fchmod(stream.fileno(), mode)

            def create(role, owner=None, generation=0):
                image = images['engine' if role == 'engine' else 'socks']
                base_role = 'peer' if role == 'fixture' else role
                args = probe.create_args(run_id, base_role, image, owner)
                if role == 'fixture':
                    args[args.index('--name') + 1] += '-g' + str(generation)
                extra, tail = [], []
                for name, target in (('dns.json', '/run/config/dns.json'), ('resolv.conf', '/etc/resolv.conf')):
                    extra += ['--mount', 'type=bind,src=' + str(root / name) + ',dst=' + target + ',readonly']
                if role != 'engine':
                    extra += ['--mount', 'type=bind,src=' + str(root / 'socks_password') + ',dst=/run/secrets/socks_password,readonly']
                if role == 'fixture':
                    extra += fixture_options()
                    tail = ['-B', '-u', '-c', FIXTURE_CODE]
                container_id = command(args[:-1] + extra + args[-1:] + tail).strip()
                if not re.fullmatch(r'[a-f0-9]{64}', container_id):
                    raise ValueError('invalid created identity')
                created.append(container_id)
                inspect(container_id)
                command([DOCKER, 'start', container_id])
                if role != 'fixture':
                    wait_health(container_id, role)
                else:
                    for _ in range(20):
                        result = subprocess.run([DOCKER, 'exec', container_id, 'python3', '-c',
                                                 "import socket; assert socket.gethostbyname('echo.antidpi.test') == '127.0.0.1'"],
                                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, timeout=4)
                        if result.returncode == 0:
                            break
                        time.sleep(0.5)
                    else:
                        raise RuntimeError('synthetic gateway DNS not ready')
                return container_id

            stage('initial_domain_transport')
            engine = create('engine')
            fixture = create('fixture', engine)
            socks = create('socks', engine)
            owner_state, auth_state = state(engine, 'engine'), state(socks, 'socks')
            if lifecycle.recovery_plan(owner_state, auth_state, images, run_id, host_ns, None, time.monotonic()):
                raise RuntimeError('initial namespace pairing is unhealthy')
            routes = command([DOCKER, 'exec', engine, 'cat', '/proc/net/route'])
            if len(routes.strip().splitlines()) != 1:
                raise RuntimeError('offline namespace has egress routes')
            report['generations'].append({'owner': engine, 'socks': socks, 'namespace': owner_state['namespace']})
            report['checks']['initial'] = client(fixture, 'positive')
            report['checks']['negative'] = client(fixture, 'negative')
            stage('dns_failure')
            command([DOCKER, 'exec', fixture, 'python3', '-c',
                     "import os,signal; from pathlib import Path; os.kill(int(Path('/tmp/dns-fixture.pid').read_text()), signal.SIGTERM)"])
            report['checks']['dns_down'] = client(fixture, 'dns_down')

            for generation, operation in ((1, 'recreate'), (2, 'restart')):
                stage('owner_' + operation)
                old_namespace = state(engine, 'engine')['namespace']
                inspect(engine)
                command([DOCKER, 'stop', '--time', '2', engine], timeout=10)
                for _ in range(16):
                    if not inspect(socks)['State']['Running']:
                        break
                    time.sleep(0.5)
                if inspect(socks)['State']['Running'] or inspect(socks)['State']['ExitCode'] != 3:
                    raise RuntimeError('auth supervisor did not fail closed')
                report['checks']['stopped_' + operation] = client(fixture, 'no_listener')
                if operation == 'recreate':
                    remove(engine)
                    engine = create('engine')
                else:
                    command([DOCKER, 'start', engine])
                    wait_health(engine, 'engine')
                new_fixture = create('fixture', engine, generation)
                owner_state, auth_state = state(engine, 'engine'), state(socks, 'socks')
                plan = lifecycle.recovery_plan(owner_state, auth_state, images, run_id, host_ns, None, time.monotonic())
                if plan != ['recreate_socks']:
                    raise RuntimeError('recovery planner did not request scoped recreation')
                remove(socks)
                socks = create('socks', engine)
                owner_state, auth_state = state(engine, 'engine'), state(socks, 'socks')
                if lifecycle.recovery_plan(owner_state, auth_state, images, run_id, host_ns, None, time.monotonic()):
                    raise RuntimeError('recovery did not converge')
                if operation == 'recreate' and owner_state['namespace'] == old_namespace:
                    raise RuntimeError('recreated owner retained the pinned old namespace')
                report['generations'].append({'owner': engine, 'socks': socks, 'namespace': owner_state['namespace'],
                                              'previous_namespace': old_namespace, 'operation': operation})
                report['checks']['after_' + operation] = client(new_fixture, 'positive')
                remove(fixture)
                fixture = new_fixture
            report['state'] = 'success'
    except Exception as error:
        report['error'] = type(error).__name__
    finally:
        for container_id in list(reversed(created)):
            try:
                remove(container_id)
            except Exception as error:
                report['cleanup'][container_id] = type(error).__name__
                report['state'] = 'failed'
        report['after'] = snapshot()
        report['production_unchanged'] = before == report['after']
        if not report['production_unchanged']:
            report['state'] = 'failed'
        destination = STAGING / ('antidpi-dns-lifecycle-' + run_id + '.json')
        builder.publish_report(report, destination)
        os.close(lock)
        print('ANTIDPI_DNS_LIFECYCLE=' + report['state'], flush=True)
        print('REPORT=' + str(destination), flush=True)
    return 0 if report['state'] == 'success' else 1


if __name__ == '__main__':
    sys.exit(main())
