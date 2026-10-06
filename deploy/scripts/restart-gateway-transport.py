"""Explicitly approved, ordered restart and bounded read-only observation.

Runs detached so loss of the VPN-carried SSH session cannot interrupt recovery.
No images, configuration, clients, ports or unrelated containers are changed.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

WG = 'vpn-wireguard'
MIHOMO = 'vpn-mihomo'
JOB = Path('/volume1/docker/vpn-gateway/.vless-maintenance')
REPORT = JOB / 'udp-restart-status.json'
OBSERVATIONS = JOB / 'udp-post-restart-observations.jsonl'


def restart_gateway(docker):
    container_id = docker.container_id(WG)
    if docker.network_mode(MIHOMO) not in ['container:' + container_id, 'container:' + WG]:
        raise RuntimeError('unexpected Mihomo network namespace')
    identity = docker.client_digest()
    try:
        docker.stop(MIHOMO)
        docker.stop(WG)
        docker.start(WG)
        docker.wait_wireguard()
        docker.start(MIHOMO)
        docker.wait_controller()
        if docker.client_digest() != identity:
            raise RuntimeError('WireGuard client identity changed during restart')
    except Exception:
        # Best-effort recovery, even when the SSH client has disconnected.
        try:
            docker.start(WG)
            docker.wait_wireguard()
        finally:
            docker.start(MIHOMO)
        raise
    return identity


CONTROLLER_CHECK = r'''
import json, os
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
url = os.environ.get('MIHOMO_CONTROLLER_URL') or os.environ.get('MIHOMO_URL')
assert url
filename = os.environ.get('MIHOMO_API_SECRET_FILE')
secret = Path(filename).read_text().strip() if filename else os.environ.get('MIHOMO_API_SECRET', '')
op = build_opener(ProxyHandler({}))
def read(path):
 with op.open(Request(url.rstrip('/') + path, headers={'Authorization': 'Bearer ' + secret}), timeout=5) as r:
  return json.load(r)
version = read('/version').get('version')
group = read('/proxies/VPS-FALLBACK')
primary = read('/proxies/WG-IMP')
reserve = read('/proxies/HY2-USA')
print(json.dumps(dict(version=version, selected=group.get('now'), fixed=group.get('fixed'),
                     primary_type=primary.get('type'), primary_alive=primary.get('alive'),
                     reserve_type=reserve.get('type'), reserve_alive=reserve.get('alive'))))
'''


class Docker:
    def call(self, args, input=None, timeout=30):
        r = subprocess.run(['/usr/local/bin/docker'] + args, input=input,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if r.returncode:
            raise RuntimeError('Docker operation failed: ' + args[0])
        return r.stdout.decode().strip()

    def container_id(self, name):
        return self.call(['inspect', '-f', '{{.Id}}', name])

    def network_mode(self, name):
        return self.call(['inspect', '-f', '{{.HostConfig.NetworkMode}}', name])

    def client_digest(self):
        # Private keys are only hashed in memory; never written or printed.
        lines = self.call(['exec', WG, 'wg', 'show', 'wg0', 'dump']).splitlines()
        if not lines:
            raise RuntimeError('WireGuard identity is unavailable')
        identity = [lines[0].split('\t')[:3]]
        peers = [line.split('\t') for line in lines[1:]]
        identity.extend(sorted([p[0], p[1], p[3]] for p in peers if len(p) >= 8))
        if len(identity) != len(lines):
            raise RuntimeError('WireGuard dump format is unexpected')
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def stop(self, name):
        self.call(['stop', '-t', '10', name], timeout=30)

    def start(self, name):
        self.call(['start', name], timeout=45)

    def wait(self, operation):
        deadline = time.monotonic() + 75
        while True:
            try:
                return operation()
            except (RuntimeError, subprocess.TimeoutExpired, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('gateway readiness timed out') from None
                time.sleep(1)

    def wait_wireguard(self):
        return self.wait(self.client_digest)

    def controller(self):
        return json.loads(self.call(['exec', '-i', 'vpn-dashboard', 'python', '-'],
                                    input=CONTROLLER_CHECK.encode(), timeout=30))

    def wait_controller(self):
        return self.wait(self.controller)


def publish(payload):
    payload['at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    fd, name = tempfile.mkstemp(prefix='.udp-status-', dir=str(JOB))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(name, 0o600)
    os.chown(name, 1026, 100)
    os.replace(name, str(REPORT))


def udp_counters(path):
    lines = path.read_text().splitlines()
    for header, values in zip(lines[::2], lines[1::2]):
        if header.startswith('Udp:'):
            return dict(zip(header.split()[1:], map(int, values.split()[1:])))
    return {}


def sample(docker):
    pid = docker.call(['inspect', '-f', '{{.State.Pid}}', MIHOMO])
    health_path = Path('/volume1/docker/vpn-dashboard/deploy/data/host-health.json')
    health = json.loads(health_path.read_text())
    sockets = []
    for line in Path('/proc/net/udp').read_text().splitlines()[1:]:
        f = line.split()
        if f[1].endswith(':CA6C') or f[2].endswith(':CA6C'):
            sockets.append(dict(local=f[1], remote=f[2], inode=f[9], drops=int(f[-1]),
                                rx_queue=int(f[4].split(':')[1], 16)))
    return dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                host_udp=udp_counters(Path('/proc/net/snmp')),
                gateway_udp=udp_counters(Path('/proc/' + pid + '/net/snmp')),
                gateway_pid=int(pid), udp_sockets=sockets,
                health_observed_at=health.get('observed_at'),
                cpu=health.get('cpu_usage_percent'), memory=health.get('memory'),
                controller=docker.controller())


def job(duration):
    docker = Docker()
    try:
        publish(dict(phase='restarting'))
        began = time.monotonic()
        identity = restart_gateway(docker)
        ready = time.monotonic()
        status = dict(phase='monitoring', restart_seconds=round(ready - began, 2),
                      client_keys_unchanged=True, controller=docker.controller())
        publish(status)
        # Exclusive creation prevents accidentally mixing two test runs.
        fd = os.open(str(OBSERVATIONS), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchown(fd, 1026, 100)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            while True:
                try:
                    row = sample(docker)
                except Exception as e:
                    row = dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(), error=type(e).__name__)
                f.write(json.dumps(row) + '\n')
                f.flush()
                if time.monotonic() - ready >= duration:
                    break
                time.sleep(15)
        if docker.client_digest() != identity:
            raise RuntimeError('client identity changed during observation')
        status['phase'] = 'complete'
        publish(status)
    except Exception as e:
        publish(dict(phase='failed', error=type(e).__name__, reason=str(e)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--observe-seconds', type=int, default=660)
    args = parser.parse_args()
    if not 60 <= args.observe_seconds <= 1800:
        raise RuntimeError('observation duration is outside safe bounds')
    if os.geteuid() != 0:
        raise RuntimeError('run as root in the authorized DSM maintenance session')
    if OBSERVATIONS.exists():
        raise RuntimeError('observation report already exists; do not repeat restart')
    import fcntl
    lock = open('/var/run/vpn-gateway-transport-restart.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pid = os.fork()
    if pid:
        print('DETACHED_RESTART_PID=' + str(pid), flush=True)
        return
    os.setsid()
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    fd = os.open('/dev/null', os.O_RDWR)
    for stream in [0, 1, 2]:
        os.dup2(fd, stream)
    if fd > 2:
        os.close(fd)
    job(args.observe_seconds)
    lock.close()
    os._exit(0)


if __name__ == '__main__':
    main()
