"""Short-lived, exact-host DNS snapshots for the isolated Anti-DPI runtime.

Mihomo's native hosts path sends an IPv4 SOCKS target. ByeDPI remains --no-domain:
there is no second lookup, DNS fallback, or DNS path back through this proxy.
The independent host refresher supplies leases. A snapshot never authorizes
client routing or bypasses production acceptance gates.
"""
import copy
import ipaddress
import math
import re
import time


FIELDS = {'version', 'created_at', 'expires_at', 'source_revision', 'hosts'}
HOST = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}\Z')


def validate_pins(value, *, now=None):
    now = time.time() if now is None else now
    if not isinstance(value, dict) or set(value) != FIELDS or type(value['version']) is not int or value['version'] != 2:
        raise ValueError('invalid DNS snapshot')
    created, expiry = value['created_at'], value['expires_at']
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (created, expiry, now))
            or not 0 < expiry - created <= 600 or not created <= now < expiry
            or not isinstance(value['source_revision'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', value['source_revision'])):
        raise ValueError('expired or invalid DNS snapshot lease')
    hosts = value['hosts']
    if not isinstance(hosts, dict) or not 1 <= len(hosts) <= 32:
        raise ValueError('bounded exact-host DNS snapshot required')
    for host, addresses in hosts.items():
        if (not isinstance(host, str) or len(host) > 253 or not HOST.fullmatch(host)
                or not isinstance(addresses, list) or not 1 <= len(addresses) <= 16
                or any(not isinstance(a, str) for a in addresses)
                or len(set(addresses)) != len(addresses)):
            raise ValueError('invalid DNS snapshot host')
        for value_ip in addresses:
            address = ipaddress.IPv4Address(value_ip)
            if (str(address) != value_ip or not address.is_global or address.is_multicast
                    or address.is_reserved or address in ipaddress.IPv4Network('192.0.0.0/24')):
                raise ValueError('non-public DNS snapshot destination')
    return copy.deepcopy(value)


def pinned_auth_config(base, contract, *, now=None):
    contract = validate_pins(contract, now=now)
    config = copy.deepcopy(base)
    if config.get('dns') != {'enable': False} or len(config.get('proxies', [])) != 1:
        raise ValueError('only the IP-only isolated auth config may be pinned')
    config['hosts'] = contract['hosts']
    rules = ['NETWORK,udp,REJECT', 'IP-CIDR6,::/0,REJECT,no-resolve']
    for host in sorted(contract['hosts']):
        rules.append('AND,((DOMAIN,{}),(DST-PORT,443)),BYEDPI'.format(host))
    for address in sorted({a for addresses in contract['hosts'].values() for a in addresses}):
        rules.append('AND,((IP-CIDR,{}/32,no-resolve),(DST-PORT,443)),BYEDPI'.format(address))
    config['rules'] = rules + ['MATCH,REJECT']
    return config
