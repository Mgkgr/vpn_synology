#!/usr/bin/env python3
"""Offline, short-lived staging test; never joins a production network namespace."""
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import select
import subprocess
import sys
import tempfile
import time
import uuid


DOCKER = '/usr/local/bin/docker'
STAGING = Path('/volume1/docker/vpn-gateway/.vless-maintenance')
SCOPE = 'antidpi-offline-probe'
PEER_CODE = r'''
import json, socket, threading, time
from runtime import read_secret, receive_exact
secret = read_secret()
def auth(value=None, method=2):
    connection = socket.create_connection(('127.0.0.1', 1080), timeout=3)
    connection.sendall(bytes([5, 1, method]))
    reply = receive_exact(connection, 2)
    if method == 0:
        connection.close()
        if reply == b'\x05\x00':
            raise RuntimeError('unauthenticated access accepted')
        return
    if reply != b'\x05\x02':
        raise RuntimeError('password method required')
    password = value.encode('ascii')
    connection.sendall(b'\x01\x07gateway' + bytes([len(password)]) + password)
    reply = receive_exact(connection, 2)
    if value != secret:
        connection.close()
        if reply == b'\x01\x00':
            raise RuntimeError('wrong password accepted')
        return
    if reply != b'\x01\x00':
        raise RuntimeError('correct password rejected')
    return connection
auth(method=0)
auth(value='wrong-' + secret)
listener = socket.socket()
listener.bind(('127.0.0.1', 18080))
listener.listen(1)
def echo():
    listener.settimeout(10)
    with listener.accept()[0] as connection:
        connection.settimeout(25)
        while True:
            block = connection.recv(65536)
            if not block:
                return
            connection.sendall(block)
threading.Thread(target=echo, daemon=True).start()
connection = auth(value=secret)
connection.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01' + (18080).to_bytes(2, 'big'))
header = receive_exact(connection, 4)
if header[:2] != b'\x05\x00':
    raise RuntimeError('IPv4 connect failed')
if header[3] == 1:
    receive_exact(connection, 6)
elif header[3] == 4:
    receive_exact(connection, 18)
else:
    raise RuntimeError('unexpected address type')
payload = bytes(range(256)) * 256
total = 0
for _ in range(4):
    connection.sendall(payload)
    if receive_exact(connection, len(payload)) != payload:
        raise RuntimeError('payload mismatch')
    total += len(payload)
print(json.dumps({'stage':'stream_ready','auth_required':True,'wrong_password_rejected':True,'echo_bytes':total}), flush=True)
deadline = time.monotonic() + 25
closed = False
while time.monotonic() < deadline:
    try:
        connection.sendall(b'ping')
        if receive_exact(connection, 4) != b'ping':
            closed = True
            break
    except (ConnectionError, OSError):
        closed = True
        break
    time.sleep(0.2)
connection.close()
listener.close()
print(json.dumps({'stage':'after_handler_stop','stream_closed':closed}), flush=True)
raise SystemExit(0 if closed else 1)
'''


def create_args(run_id, role, image, owner_id=None):
    if not re.fullmatch(r'[a-f0-9]{32}', run_id) or role not in ('engine', 'socks', 'peer'):
        raise ValueError('invalid fixed staging identity')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('immutable verified image ID required')
    if role == 'engine':
        if owner_id is not None:
            raise ValueError('engine always owns a new isolated namespace')
        network = 'none'
    else:
        if not isinstance(owner_id, str) or not re.fullmatch(r'[a-f0-9]{64}', owner_id):
            raise ValueError('only a verified staging container ID may own the namespace')
        network = 'container:' + owner_id
    return [DOCKER, 'create', '--name', 'vpn-antidpi-check-' + run_id[:12] + '-' + role,
            '--label', 'vpn.dashboard.scope=' + SCOPE, '--label', 'vpn.dashboard.run=' + run_id,
            '--label', 'vpn.dashboard.role=' + role,
            '--network', network, '--user', '10002:10002', '--cap-drop', 'ALL', '--read-only',
            '--security-opt', 'no-new-privileges:true', '--restart', 'no', '--pids-limit', '64',
            '--memory', '256m' if role == 'engine' else '128m',
            '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777',
            '--log-opt', 'max-size=1m', '--log-opt', 'max-file=2', image]


def load_builder():
    spec = importlib.util.spec_from_file_location('antidpi_build', Path(__file__).with_name('build-antidpi-staging.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_network_state(value, kind):
    if not kind.startswith('firewall'):
        return value
    # iptables-save includes timestamps and live chain counters, not configuration.
    # Preserve every actual rule, chain name, policy and rule order exactly.
    lines = [re.sub(r'^(:\S+\s+\S+\s+)\[\d+:\d+\]$', r'\1[counters]', line)
             for line in value.splitlines() if not line.startswith('#')]
    return '\n'.join(lines)


def main():
    if len(sys.argv) != 3 or sys.argv[1] != '--approved' or not re.fullmatch(r'[a-f0-9]{32}', sys.argv[2]):
        sys.exit('Only --approved <reviewed-build-id> is accepted.')
    if os.geteuid() != 0:
        sys.exit('Existing NAS root console is required.')
    builder = load_builder()
    command = builder.command
    run_id = uuid.uuid4().hex
    report = {'run_id': run_id, 'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'state': 'failed', 'production_ready': False, 'scope': 'offline_transport_only',
              'containers': {}, 'cleanup': {}, 'stage': 'preflight',
              'namespaces': {'host': os.stat('/proc/self/ns/net').st_ino}}
    created = []
    attached = None
    def snapshot():
        state = {'containers': builder.production_snapshot(), 'network': {}}
        for key, argv in (
            ('firewall4', ['/sbin/iptables-save']), ('firewall6', ['/sbin/ip6tables-save']),
            ('routes4', ['/sbin/ip', '-4', 'route', 'show', 'table', 'all']),
            ('routes6', ['/sbin/ip', '-6', 'route', 'show', 'table', 'all']),
            ('rules4', ['/sbin/ip', '-4', 'rule', 'show']), ('rules6', ['/sbin/ip', '-6', 'rule', 'show'])):
            state['network'][key] = hashlib.sha256(normalize_network_state(command(argv), key).encode()).hexdigest()
        return state
    def identity(container_id):
        labels = json.loads(command([DOCKER, 'inspect', '--format', '{{json .Config.Labels}}', container_id]))
        if labels.get('vpn.dashboard.scope') != SCOPE or labels.get('vpn.dashboard.run') != run_id:
            raise ValueError('staging container identity changed')
    before = snapshot()
    report['before'] = before
    with tempfile.TemporaryDirectory(prefix='vpn-antidpi-probe-', dir='/var/tmp') as directory:
        try:
            build = json.loads((STAGING / ('antidpi-build-' + sys.argv[2] + '.json')).read_text())
            if (build.get('state') != 'success' or build.get('run_id') != sys.argv[2]
                    or not build.get('production_unchanged') or build.get('context_sha256') != builder.CONTEXT_SHA256):
                raise ValueError('build report is not accepted')
            images = {role: build['images'][role]['verified']['id'] for role in ('engine', 'socks')}
            for image in images.values():
                labels = json.loads(command([DOCKER, 'image', 'inspect', '--format', '{{json .Config.Labels}}', image]))
                if labels.get('vpn.dashboard.run') != sys.argv[2] or labels.get('vpn.dashboard.scope') != 'antidpi-staging':
                    raise ValueError('built image label mismatch')
            secret_path = Path(directory) / 'socks_password'
            fd = os.open(str(secret_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='ascii') as stream:
                stream.write(secrets.token_urlsafe(32))
                stream.flush()
                os.fchown(stream.fileno(), 10002, 10002)
            for role in ('engine', 'socks', 'peer'):
                report['stage'] = 'start_' + role
                args = create_args(run_id, role, images['socks' if role == 'socks' else 'engine'],
                                   report['containers'].get('engine') if role != 'engine' else None)
                extra = []
                tail = []
                if role != 'engine':
                    extra += ['--mount', 'type=bind,src=' + str(secret_path) + ',dst=/run/secrets/socks_password,readonly']
                if role == 'peer':
                    extra += ['--entrypoint', 'python3']
                    tail = ['-B', '-u', '-c', PEER_CODE]
                container_id = command(args[:-1] + extra + args[-1:] + tail).strip()
                if not re.fullmatch(r'[a-f0-9]{64}', container_id):
                    raise ValueError('unexpected container identity')
                created.append(container_id)
                report['containers'][role] = container_id
                identity(container_id)
                if role == 'peer':
                    attached = subprocess.Popen([DOCKER, 'start', '--attach', container_id],
                                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, bufsize=0)
                    break
                command([DOCKER, 'start', container_id])
                healthy = False
                for _ in range(12):
                    result = subprocess.run([DOCKER, 'exec', container_id, 'python3', '-B', '/app/healthcheck.py', role],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    if result.returncode == 0:
                        healthy = True
                        break
                    time.sleep(0.5)
                if not healthy:
                    report['container_log'] = command([DOCKER, 'logs', '--tail', '10', container_id])[-1000:]
                    raise RuntimeError('staging health failed')
                pid = int(command([DOCKER, 'inspect', '--format', '{{.State.Pid}}', container_id]))
                namespace = os.stat('/proc/{}/ns/net'.format(pid)).st_ino
                report['namespaces'][role] = namespace
                if namespace == report['namespaces']['host']:
                    raise RuntimeError('staging namespace is not isolated')
                if role == 'socks' and namespace != report['namespaces']['engine']:
                    raise RuntimeError('auth wrapper is in a different namespace')
                if role == 'engine':
                    routes = command([DOCKER, 'exec', container_id, 'cat', '/proc/net/route'])
                    report['ipv4_route_entries'] = len(routes.strip().splitlines()) - 1
                    if report['ipv4_route_entries'] != 0:
                        raise RuntimeError('offline staging has an IPv4 route')
            report['stage'] = 'auth_and_echo'
            ready, _, _ = select.select([attached.stdout], [], [], 20)
            if not ready:
                raise TimeoutError('offline peer did not publish readiness')
            line = attached.stdout.readline()
            first = json.loads(line)
            if first.get('stage') != 'stream_ready' or first.get('echo_bytes') != 262144:
                raise ValueError('offline transfer did not complete')
            report['peer'] = first
            report['stage'] = 'stop_test_handler'
            identity(report['containers']['engine'])
            stopped_at = time.monotonic()
            command([DOCKER, 'stop', '--time', '2', report['containers']['engine']], timeout=10)
            stdout, stderr = attached.communicate(timeout=30)
            after = json.loads(stdout.strip())
            if attached.returncode != 0 or after.get('stream_closed') is not True:
                raise RuntimeError('existing stream did not fail closed')
            report['handler_failure'] = after
            for _ in range(12):
                raw = command([DOCKER, 'inspect', '--format', '{{json .State.Running}}|{{json .State.ExitCode}}',
                               report['containers']['socks']]).strip()
                running, code = map(json.loads, raw.split('|'))
                if not running:
                    break
                time.sleep(0.5)
            report['auth_proxy_after_failure'] = {'running': running, 'exit_code': code,
                                                  'observed_seconds': round(time.monotonic() - stopped_at, 3)}
            if running or code != 3:
                raise RuntimeError('auth proxy did not stop on dead handler')
            report['state'] = 'success'
        except Exception as error:
            report['error'] = type(error).__name__
        finally:
            if attached is not None and attached.poll() is None:
                attached.terminate()
                try:
                    attached.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    attached.kill()
                    attached.wait(timeout=5)
            for container_id in reversed(created):
                try:
                    identity(container_id)
                    command([DOCKER, 'rm', '--force', container_id], timeout=20)
                    report['cleanup'][container_id] = 'removed'
                except Exception as error:
                    report['cleanup'][container_id] = type(error).__name__
                    report['state'] = 'failed'
            report['after'] = snapshot()
            report['production_unchanged'] = before == report['after']
            if not report['production_unchanged']:
                report['state'] = 'failed'
            destination = STAGING / ('antidpi-probe-' + run_id + '.json')
            builder.publish_report(report, destination)
            print('ANTIDPI_PROBE=' + report['state'], flush=True)
            print('REPORT=' + str(destination), flush=True)
    return 0 if report['state'] == 'success' else 1


if __name__ == '__main__':
    sys.exit(main())
