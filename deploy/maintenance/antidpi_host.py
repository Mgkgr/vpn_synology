"""Trusted host boundary for the isolated four-service Anti-DPI runtime."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
import re

from antidpi.production import HOSTS, CONTROL_HOST, canonical, validate_config, validate_lease


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_private(path, limit=65536):
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('unsafe_runtime_path')
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('invalid_runtime_file')
        return stream.read(limit + 1)


def atomic_private(path, data, *, owner=None):
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('unsafe_runtime_path')
    candidate = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    fd = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
            if owner is not None and os.name == 'posix': os.fchown(stream.fileno(), owner, owner)
        os.replace(str(candidate), str(path))
        if os.name == 'posix':
            parent = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(parent)
            finally: os.close(parent)
    finally:
        if candidate.exists(): candidate.unlink()


def replace_config(path, value, expected_hash):
    value = validate_config(value)
    if digest(read_private(path)) != expected_hash:
        raise ValueError('runtime_revision_changed')
    atomic_private(path, canonical(value), owner=10002)


def renewed_lease(answers, source_revision, now):
    if not isinstance(answers, dict) or set(answers) != set(HOSTS.values()) | {CONTROL_HOST}:
        raise ValueError('incomplete_dns_answer')
    hosts, ttls = {}, []
    for host, response in answers.items():
        if not isinstance(response, dict) or response.get('Status') != 0:
            raise ValueError('dns_query_failed')
        rows = response.get('Answer')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise ValueError('invalid_dns_answer')
        addresses = set()
        for row in rows:
            if not isinstance(row, dict): raise ValueError('invalid_dns_answer')
            ttl = row.get('TTL')
            if type(ttl) is not int or ttl <= 0: raise ValueError('invalid_dns_ttl')
            ttls.append(ttl)
            if row.get('type') != 1: continue
            address = ipaddress.IPv4Address(row.get('data'))
            if (not address.is_global or address.is_reserved or address.is_multicast
                    or address in ipaddress.IPv4Network('192.0.0.0/24')):
                raise ValueError('nonpublic_dns_answer')
            addresses.add(str(address))
        if not 1 <= len(addresses) <= 16: raise ValueError('no_ipv4_answer')
        hosts[host] = [sorted(addresses)[0]]
    return validate_lease(dict(version=2, created_at=now, expires_at=now + min(600, min(ttls)),
                               source_revision=source_revision, hosts=hosts), now)


def capabilities(evidence, identity, *, runtime_ok, now):
    names = ('runtime', 'dns_renewal', 'namespace_recovery', 'service_isolation', 'production_input', 'backup')
    valid = (isinstance(evidence, dict) and evidence.get('identity') == identity
             and type(evidence.get('verified_at')) in (int, float)
             and 0 <= now - evidence['verified_at'] <= 30 * 86400
             and isinstance(evidence.get('checks'), dict))
    checks = evidence['checks'] if valid else {}
    blockers = [name + ('_unavailable' if name == 'backup' else '_unverified')
                for name in names if checks.get(name) is not True]
    if not runtime_ok and 'runtime_unverified' not in blockers:
        blockers.insert(0, 'runtime_unverified')
    return dict(probe_ready=runtime_ok is True, apply_ready=not blockers, blockers=blockers)


def compose_runtime(code_path, runtime_path, engine_image, socks_image):
    if (code_path != '/volume1/docker/vpn-dashboard-maintenance/app'
            or runtime_path != '/volume1/docker/vpn-antidpi/runtime'
            or any(not isinstance(image,str) or not re.fullmatch(r'sha256:[a-f0-9]{64}',image)
                   for image in (engine_image,socks_image))):
        raise ValueError('invalid_installation_contract')
    services={}
    for role,image,memory in (('antidpi',engine_image,'256m'),('socks',socks_image,'128m')):
        entry='engine' if role=='antidpi' else 'socks'
        services[role]={
            'image':image,'pull_policy':'never','container_name':'vpn-antidpi' if role=='antidpi' else 'vpn-antidpi-socks',
            'user':'10002:10002','restart':'unless-stopped','read_only':True,'cap_drop':['ALL'],
            'security_opt':['no-new-privileges:true'],'mem_limit':memory,
            'environment':{'PYTHONPATH':'/opt/vpn','PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'},
            'entrypoint':['python3','-B','-m','antidpi.runtime_service',entry],
            'tmpfs':['/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777'],
            'logging':{'driver':'json-file','options':{'max-size':'1m','max-file':'2'}},
            'volumes':[{'type':'bind','source':code_path,'target':'/opt/vpn','read_only':True},
                       {'type':'bind','source':runtime_path,'target':'/run/antidpi','read_only':True}],
        }
    services['antidpi']['network_mode']='bridge'
    services['socks'].update(network_mode='service:antidpi',depends_on=['antidpi'])
    return {'name':'vpn-antidpi','services':services}
