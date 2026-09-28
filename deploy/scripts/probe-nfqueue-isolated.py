#!/usr/bin/env python3
"""Explicitly approved, bounded NFQUEUE bind probe for this DS923+ only.

No packet capture, firewall writes, external traffic or container mutations.
Linux 4.4 UAPI: include/uapi/linux/netfilter/nfnetlink_queue.h.
This proves queue binding only, NOT complete Zapret support or DPI bypass.
"""

import ctypes
import datetime
import hashlib
import json
import os
import pathlib
import platform
import re
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid


KERNEL = '4.4.302+'
QUEUE = 4242
MODULES = {
    'nfnetlink_queue': '316fd45caf1f9214b24d67639b69583132bc7f5faf12bbd0905107aa26095673',
    'xt_NFQUEUE': '656d9c20c793bf9dafe3c3f7dba4a060788a9daf346f0b31be1b2cda2c93ed6c',
}
CONTAINERS = {
    'vpn-wireguard': ('vpn-gateway', 'wireguard'),
    'vpn-mihomo': ('vpn-gateway', 'mihomo'),
    'vpn-uptime-kuma': ('vpn-gateway', 'uptime-kuma'),
    'vpn-metacubexd': ('vpn-gateway', 'metacubexd'),
    'vpn-dashboard': ('vpn-dashboard', 'dashboard'),
}
STAGING = pathlib.Path('/volume1/docker/vpn-gateway/.vless-maintenance')
SAFE_ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin', 'LC_ALL': 'C'}
DOCKER_FORMAT = (
    '{"id":{{json .Id}},"running":{{json .State.Running}},'
    '"started":{{json .State.StartedAt}},"restarts":{{json .RestartCount}},'
    '"oom":{{json .State.OOMKilled}},"pid":{{json .State.Pid}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"health":{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}}}'
)


def safe_error(error):
    return {'type': type(error).__name__, 'errno': getattr(error, 'errno', None)}


def config_message(seq, port_id, command=None, copy_packet=False):
    if (command is None) == (not copy_packet) or command not in (None, 1, 2):
        raise ValueError('one approved queue operation is required')
    if command is not None:
        value, attr_type = struct.pack('!BBH', command, 0, socket.AF_INET), 1
    else:
        value, attr_type = struct.pack('!IB', 65535, 2), 2
    attr = struct.pack('=HH', 4 + len(value), attr_type) + value
    attr += b'\0' * (-len(attr) % 4)
    body = struct.pack('!BBH', socket.AF_UNSPEC, 0, QUEUE) + attr
    return struct.pack('=IHHII', 16 + len(body), 0x302, 5, seq, port_id) + body


def check_ack(data, address, seq, port_id):
    if address != (0, 0) or len(data) < 36:
        raise ValueError('not a complete kernel acknowledgement')
    length, kind, flags, received_seq, recipient = struct.unpack_from('=IHHII', data)
    if length != len(data) or kind != 2 or flags != 0 or received_seq != seq or recipient != port_id:
        raise ValueError('unexpected acknowledgement envelope')
    code = struct.unpack_from('=i', data, 16)[0]
    original_length, original_type, original_flags, original_seq, original_pid = struct.unpack_from('=IHHII', data, 20)
    if original_length not in (28, 32) or original_type != 0x302 or original_flags != 5 or original_seq != seq or original_pid != port_id:
        raise ValueError('acknowledgement does not match the request')
    if code > 0:
        raise ValueError('invalid netlink error code')
    if code < 0:
        raise OSError(-code, 'NFQUEUE operation rejected by kernel')


def validate_isolation(parent_namespace, child_namespace, interfaces, links='', routes=None):
    if parent_namespace == child_namespace:
        raise RuntimeError('network isolation has not been proved')
    if set(interfaces) == {'lo'}:
        return
    # Synology creates its unconfigured IPv6-over-IPv4 fallback device per netns.
    # Only that exact DOWN device with no forwarding route; no veth/physical link.
    # The kernel's built-in IPv6 reject defaults are not an egress path.
    reject_default = 'unreachable default dev lo proto kernel metric 4294967295 error -101 pref medium'
    safe_routes = (isinstance(routes, list) and len(routes) == 2 and routes[0] == ''
                   and len(routes[1].splitlines()) <= 2
                   and all(' '.join(line.split()) == reject_default for line in routes[1].splitlines()))
    if set(interfaces) != {'lo', 'sit0'} or not safe_routes:
        raise RuntimeError('unexpected links or routes in new network namespace')
    rows = links.splitlines()
    if len(rows) != 2:
        raise RuntimeError('interface metadata is incomplete')
    observed = set()
    for row in rows:
        match = re.match(r'^\d+: ([^:]+): <([^>]*)> ', row)
        if not match or ' state DOWN ' not in row:
            raise RuntimeError('interface is not confirmed down')
        name, flags = match.groups()
        if name == 'lo' and flags == 'LOOPBACK' and 'link/loopback 00:00:00:00:00:00 ' in row:
            observed.add('lo')
        elif name == 'sit0@NONE' and flags == 'NOARP' and 'link/sit 0.0.0.0 brd 0.0.0.0' in row:
            observed.add('sit0')
        else:
            raise RuntimeError('unexpected configured interface')
    if observed != {'lo', 'sit0'}:
        raise RuntimeError('unexpected interface identities')


def queue_probe(parent_namespace, child_namespace, interfaces, socket_factory=socket.socket,
                links='', routes=None):
    validate_isolation(parent_namespace, child_namespace, interfaces, links, routes)
    result = {'state': 'failed', 'parent_namespace': parent_namespace,
              'child_namespace': child_namespace, 'interfaces': interfaces,
              'queue': QUEUE, 'queue_bound': False, 'queue_unbound': False,
              'packet_rules_created': 0, 'copy_packet_configured': False}
    # A socket is opened only after namespace/links checks, never on the NAS netns.
    with socket_factory(socket.AF_NETLINK, socket.SOCK_RAW, 12) as netlink:
        netlink.settimeout(3)
        netlink.bind((0, 0))
        port_id = netlink.getsockname()[0]
        for seq, label, options in (
            (1, 'queue_bound', {'command': 1}),
            (2, 'copy_packet_configured', {'copy_packet': True}),
            (3, 'queue_unbound', {'command': 2}),
        ):
            result['stage'] = label
            netlink.sendto(config_message(seq, port_id, **options), (0, 0))
            data, address = netlink.recvfrom(8192)
            check_ack(data, address, seq, port_id)
            result[label] = True
    result['state'] = 'passed'
    return result


def validate_module_bytes(data, expected_sha256, kernel, loaded_modules):
    if len(data) < 64 or len(data) > 8388608 or data[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', data, 18)[0] != 62:
        raise ValueError('not an approved x86_64 ELF module')
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError('stock module fingerprint has changed')
    metadata = {}
    for key in ('vermagic', 'depends'):
        values = re.findall(b'\0' + key.encode('ascii') + b'=([^\0]*)\0', data)
        if len(values) != 1:
            raise ValueError('module metadata is absent or ambiguous')
        metadata[key] = values[0].decode('ascii')
    if not metadata['vermagic'].split() or metadata['vermagic'].split()[0] != kernel:
        raise ValueError('module kernel mismatch')
    dependencies = metadata['depends'].split(',') if metadata['depends'] else []
    if not set(dependencies).issubset(loaded_modules):
        raise ValueError('module has unloaded dependencies; do not auto-load')
    return {'sha256': expected_sha256, 'vermagic': metadata['vermagic'], 'depends': dependencies}


def execute(ops, approved=False):
    if not approved:
        raise PermissionError('explicit owner approval is required')
    evidence = ops.preflight()
    baseline = ops.snapshot()
    report = {'state': 'failed', 'evidence': evidence, 'before': baseline, 'loaded_by_probe': []}
    loaded = report['loaded_by_probe']
    try:
        for name in MODULES:
            if not evidence['modules'][name]['loaded']:
                ops.load(name)
                loaded.append(name)
        report['probe'] = ops.isolated_probe()
        report['state'] = 'passed' if report['probe'].get('state') == 'passed' else 'failed'
    except Exception as error:
        report['failure'] = safe_error(error)
    finally:
        try:
            report['cleanup'] = ops.cleanup(loaded)
        except Exception as error:
            report['cleanup'] = {'state': 'unknown', 'error': safe_error(error)}
        try:
            report['after'] = ops.snapshot()
            report['working_state_unchanged'] = baseline == report['after']
        except Exception as error:
            report['postcheck_error'] = safe_error(error)
            report['working_state_unchanged'] = False
        try:
            report['modules_after'] = ops.module_states()
            report['module_state_restored'] = report['modules_after'] == {name: value['loaded'] for name, value in evidence['modules'].items()}
        except Exception as error:
            report['module_postcheck_error'] = safe_error(error)
            report['module_state_restored'] = False
        if report['cleanup']['state'] != 'restored' or not report['working_state_unchanged'] or not report['module_state_restored']:
            report['state'] = 'incomplete'
    return report


def trusted_file(path):
    path = pathlib.Path(path)
    resolved = path.resolve(strict=True)
    for parent in (resolved,) + tuple(resolved.parents):
        meta = parent.stat()
        if meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) & 0o022:
            raise PermissionError('system path is not root-controlled')
    if not resolved.is_file():
        raise ValueError('system path is not a regular file')
    return resolved


def run(argv, timeout=10):
    # Every caller constructs a fixed argument list, never shell text or user input.
    process = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=timeout, env=SAFE_ENV, check=False)
    if process.returncode != 0 or len(process.stdout) > 2097152:
        raise RuntimeError('fixed diagnostic command failed')
    return process.stdout


class HostOperations:
    def module_states(self):
        return {name: pathlib.Path('/sys/module', name).exists() for name in MODULES}

    def preflight(self):
        if sys.platform != 'linux' or os.geteuid() != 0 or platform.release() != KERNEL or platform.machine() != 'x86_64':
            raise PermissionError('this probe is pinned to the approved NAS platform')
        if os.stat('/proc/self/ns/net').st_ino != os.stat('/proc/1/ns/net').st_ino:
            raise PermissionError('parent must be the host network namespace')
        for path in ('/sbin/insmod', '/sbin/rmmod', '/sbin/iptables-save', '/sbin/ip6tables-save', '/sbin/ip', '/usr/local/bin/docker'):
            trusted_file(path)
        loaded = {p.name for p in pathlib.Path('/sys/module').iterdir()}
        result = {'kernel': KERNEL, 'modules': {}}
        for name, digest in MODULES.items():
            path = pathlib.Path('/lib/modules', name + '.ko')
            real = trusted_file(path)
            if real != pathlib.Path('/usr/lib/modules', name + '.ko'):
                raise ValueError('unexpected module location')
            data = path.read_bytes()
            metadata = validate_module_bytes(data, digest, KERNEL, loaded)
            metadata.update({'path': str(real), 'loaded': name in loaded})
            result['modules'][name] = metadata
        return result

    def snapshot(self):
        containers = {}
        for name, identity in CONTAINERS.items():
            value = json.loads(run(['/usr/local/bin/docker', 'inspect', '--format', DOCKER_FORMAT, name]))
            if (value['project'], value['service']) != identity or not value['running'] or value['oom']:
                raise RuntimeError('working container baseline is not acceptable')
            if value['health'] not in (None, 'healthy'):
                raise RuntimeError('working container is not healthy')
            value['namespace'] = os.stat('/proc/{}/ns/net'.format(int(value.pop('pid')))).st_ino
            containers[name] = value
        if containers['vpn-wireguard']['namespace'] != containers['vpn-mihomo']['namespace']:
            raise RuntimeError('working VPN namespace mismatch')
        signatures = {}
        for label, argv in (
            ('firewall4', ['/sbin/iptables-save']),
            ('firewall6', ['/sbin/ip6tables-save']),
            ('routes4', ['/sbin/ip', '-4', 'route', 'show', 'table', 'all']),
            ('routes6', ['/sbin/ip', '-6', 'route', 'show', 'table', 'all']),
            ('policy4', ['/sbin/ip', '-4', 'rule', 'show']),
            ('policy6', ['/sbin/ip', '-6', 'rule', 'show']),
        ):
            lines = run(argv).decode('ascii').splitlines()
            # Comments contain capture time; chain counters change under normal load.
            lines = [re.sub(r'\[\d+:\d+\]', '[counters]', line) for line in lines if not line.startswith('#')]
            signatures[label] = hashlib.sha256('\n'.join(lines).encode('ascii')).hexdigest()
        return {'containers': containers, 'network_signatures': signatures,
                'namespace': os.stat('/proc/self/ns/net').st_ino}

    def load(self, name):
        if name not in MODULES or pathlib.Path('/sys/module', name).exists():
            raise RuntimeError('module state changed since preflight')
        # Revalidate immediately before loading; only this exact stock file is accepted.
        path = pathlib.Path('/lib/modules', name + '.ko')
        trusted_file(path)
        validate_module_bytes(path.read_bytes(), MODULES[name], KERNEL,
                              {p.name for p in pathlib.Path('/sys/module').iterdir()})
        run(['/sbin/insmod', str(path)])

    def isolated_probe(self):
        return json.loads(run(['/usr/bin/python3', '-B', str(pathlib.Path(__file__).resolve()), '--isolated-child'], timeout=15))

    def cleanup(self, loaded):
        result = {'state': 'restored', 'removed': [], 'left_loaded': []}
        for name in reversed(loaded):
            if name not in MODULES:
                raise ValueError('refusing to remove an unrelated module')
            module_path = pathlib.Path('/sys/module', name)
            deadline = time.monotonic() + 3
            while module_path.exists():
                try:
                    users = int((module_path / 'refcnt').read_text(encoding='ascii').strip())
                    if users == 0:
                        run(['/sbin/rmmod', name], timeout=5)
                        break
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if module_path.exists():
                result['state'] = 'left_loaded'
                result['left_loaded'].append(name)
            else:
                result['removed'].append(name)
        return result


def isolated_child():
    if sys.platform != 'linux' or os.geteuid() != 0 or any(not pathlib.Path('/sys/module', name).exists() for name in MODULES):
        raise PermissionError('required approved modules are not already loaded')
    parent = os.stat('/proc/self/ns/net').st_ino
    libc = ctypes.CDLL(None, use_errno=True)
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int
    if libc.unshare(0x40000000) != 0:  # CLONE_NEWNET, no other namespaces/flags.
        raise OSError(ctypes.get_errno(), 'isolated network namespace failed')
    child = os.stat('/proc/self/ns/net').st_ino
    interfaces = [line.split(':', 1)[0].strip() for line in pathlib.Path('/proc/net/dev').read_text(encoding='ascii').splitlines() if ':' in line]
    evidence = {'state': 'failed', 'parent_namespace': parent, 'child_namespace': child, 'interfaces': interfaces}
    try:
        if parent == child:
            raise RuntimeError('network namespace did not change')
        links = run(['/sbin/ip', '-o', 'link', 'show'], timeout=2).decode('ascii')
        routes = [run(['/sbin/ip', family, 'route', 'show', 'table', 'all'], timeout=2).decode('ascii') for family in ('-4', '-6')]
        evidence['links'] = links
        evidence['routes_empty'] = routes == ['', '']
        evidence.update(queue_probe(parent, child, interfaces, links=links, routes=routes))
    except Exception as error:
        evidence['failure'] = safe_error(error)
    return evidence


def main():
    if sys.argv[1:] == ['--isolated-child']:
        try:
            result = isolated_child()
        except Exception as error:
            result = {'state': 'failed', 'failure': safe_error(error)}
        print(json.dumps(result, sort_keys=True))
        return 0
    if sys.argv[1:] != ['--approved']:
        sys.exit('No changes made. Requires explicit approval for this isolated stock-module probe.')
    import pwd  # NAS only; keeps the pure protocol tests importable on Windows.
    owner = pwd.getpwnam('prometei')
    name = 'nfqueue-isolated-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8] + '.json'
    report_path = STAGING / name
    descriptor = os.open(str(report_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        try:
            report = execute(HostOperations(), approved=True)
        except Exception as error:
            report = {'state': 'failed_preflight', 'failure': safe_error(error)}
        report['observed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report['scope'] = 'isolated_queue_bind_only_no_packet_routing'
        json.dump(report, output, sort_keys=True, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
        os.fchown(output.fileno(), owner.pw_uid, owner.pw_gid)
    print('NFQUEUE_PROBE=' + report['state'])
    print('REPORT=' + str(report_path))
    return 0 if report['state'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
