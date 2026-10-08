"""Closed four-service runtime. Only trusted host files select engine arguments.

No native ByeDPI auto-switcher, DNS resolver, public port or DIRECT fallback.
DNS leases are renewed by the host worker, not by the public dashboard.
"""
import copy
import hashlib
import json
import re
import subprocess

from .dns_pins import validate_pins
from .runtime import validate_secret

HOSTS = {'youtube': 'www.youtube.com', 'discord': 'discord.com',
         'telegram': 'web.telegram.org', 'instagram': 'www.instagram.com'}
CONTROL_HOST = 'www.wikipedia.org'
PORTS = {sid: 1081 + index for index, sid in enumerate(HOSTS)}
ARGUMENTS = {
    'tlsrec-sni': ('--tlsrec', '1+s'), 'disorder-1': ('--disorder', '1'),
    'disorder-sni': ('--disorder', '1+s'), 'split-1': ('--split', '1'),
    'split-sni': ('--split', '1+s'), 'oob-sni': ('--oob', '1+s'),
    'disoob-sni': ('--disoob', '1+s'), 'fake-md5': ('--fake', '-1', '--md5sig'),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def validate_config(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'identity', 'generation', 'selections'}
            or type(value['version']) is not int or value['version'] != 1
            or any(not isinstance(value[key], str) or not re.fullmatch('[a-f0-9]{64}', value[key])
                   for key in ('identity', 'generation'))
            or not isinstance(value['selections'], dict) or set(value['selections']) != set(HOSTS)
            or any(not isinstance(item, str) or item not in ARGUMENTS for item in value['selections'].values())):
        raise ValueError('invalid_runtime_configuration')
    return copy.deepcopy(value)


def validate_lease(value, now=None):
    value = validate_pins(value, now=now)
    if set(value['hosts']) != set(HOSTS.values()) | {CONTROL_HOST}:
        raise ValueError('unexpected_dns_hosts')
    if any(len(addresses) != 1 for addresses in value['hosts'].values()):
        raise ValueError('one_comparison_address_required')
    return value


def engine_argv(sid, strategy):
    if sid not in HOSTS or strategy not in ARGUMENTS:
        raise ValueError('unreviewed_strategy')
    return ['/usr/local/bin/ciadpi', '--ip', '127.0.0.1', '--port', str(PORTS[sid]),
            '--conn-ip', '0.0.0.0', '--no-udp', '--no-domain', '--max-conn', '128',
            '--timeout', '10'] + list(ARGUMENTS[strategy])


def _start(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def stop_child(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


class EngineSupervisor:
    def __init__(self, start=_start):
        self.start, self.children = start, {}

    def reconcile(self, config):
        config = validate_config(config)  # Validate the entire set before any mutation.
        for sid, strategy in config['selections'].items():
            previous = self.children.get(sid)
            if previous and previous[0] == strategy and previous[1].poll() is None:
                continue
            if previous:
                stop_child(previous[1])  # Existing sockets of this strategy close too.
            self.children[sid] = (strategy, self.start(engine_argv(sid, strategy)))

    def stop(self):
        for _, child in self.children.values():
            stop_child(child)
        self.children.clear()


def auth_config(secret, config, lease, now=None):
    validate_config(config)
    lease = validate_lease(lease, now)
    # Never infer a service from a shared CDN IP. Domain-form SOCKS remains exact.
    by_ip = {}
    for sid, host in HOSTS.items():
        by_ip.setdefault(lease['hosts'][host][0], []).append(sid)
    rules = ['NETWORK,udp,REJECT', 'IP-CIDR6,::/0,REJECT,no-resolve']
    for sid, host in HOSTS.items():
        rules.append('AND,((DOMAIN,{}),(DST-PORT,443)),{}'.format(host, sid))
    for address, services in sorted(by_ip.items()):
        if len(services) == 1:
            rules.append('AND,((IP-CIDR,{}/32,no-resolve),(DST-PORT,443)),{}'.format(address, services[0]))
    return {
        'mode': 'rule', 'log-level': 'silent', 'ipv6': False, 'allow-lan': True,
        'dns': {'enable': False}, 'profile': {'store-selected': False, 'store-fake-ip': False},
        'hosts': {host: lease['hosts'][host] for host in HOSTS.values()},
        'listeners': [{'name': 'gateway-auth', 'type': 'socks', 'listen': '0.0.0.0',
                       'port': 1080, 'udp': False,
                       'users': [{'username': 'gateway', 'password': validate_secret(secret)}]}],
        'proxies': [{'name': sid, 'type': 'socks5', 'server': '127.0.0.1', 'port': PORTS[sid], 'udp': False}
                    for sid in HOSTS],
        'rules': rules + ['MATCH,REJECT'],
    }


def auth_fingerprint(config, lease, now=None):
    # A lease extension or strategy change does not restart other auth sessions.
    # Host changes require a new auth process and closure of old DNS-bound sockets.
    value = auth_config('x' * 48, config, lease, now)
    return hashlib.sha256(canonical(value)).hexdigest()
