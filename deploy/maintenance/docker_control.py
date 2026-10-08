"""Scoped Docker mutations from an installer-owned, hash-checked contract.

The public socket never accepts paths/Compose/models. Installer activation must
first verify and copy resolved Compose files into its private root-owned tree.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .backups import file_digest, no_symlink
from .catalog import DIGEST_RE, IDENTITIES
from .docker_adapter import DOCKER_PATHS, bounded_run
from .protocol import REVISION, canonical

SERVICES = {service: (project, 'vpn-antidpi-socks' if service == 'socks' else 'vpn-' + service)
    for project, services, _, _ in IDENTITIES.values() for service in services}
OWNERS = {'mihomo': 'wireguard', 'socks': 'antidpi'}
CONTAINER_ID = re.compile(r'[0-9a-f]{64}\Z')
CONTROL_FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"running":{{json .State.Running}},"pid":{{json .State.Pid}},'
    '"image_id":{{json .Image}},"network_mode":{{json .HostConfig.NetworkMode}},'
    '"runtime":{"config":{{json .Config}},"host":{{json .HostConfig}},"mounts":{{json .Mounts}}}}')


class ControlError(RuntimeError):
    pass


def runtime_hash(row):
    # Secret environment values enter only a hash in memory, never status/logs.
    value = copy.deepcopy(row['runtime'])
    if isinstance(value.get('mounts'), list):
        # Docker derives Mounts from a Go map; enumeration order is not stable.
        # Preserve every field (including RW/source), canonicalize only ordering.
        value['mounts'] = sorted(value['mounts'], key=canonical)
    config = value.get('config')
    if isinstance(config, dict):
        if config.get('Hostname') == row.get('id', '')[:12]:
            config['Hostname'] = '<container-id>'
        labels = config.get('Labels')
        if isinstance(labels, dict):
            config['Labels'] = {key: item for key, item in labels.items() if not key.startswith('com.docker.compose.')}
        if 'Image' in config:
            config['Image'] = row['image_id']
    if row['service'] in OWNERS:
        if isinstance(value.get('host'), dict):
            value['host']['NetworkMode'] = 'service:' + OWNERS[row['service']]
    return hashlib.sha256(canonical(value)).hexdigest()


def _namespace_inode(pid):
    if type(pid) is not int or pid <= 0:
        raise ControlError('namespace_unavailable')
    try:
        return os.stat('/proc/' + str(pid) + '/ns/net').st_ino
    except OSError:
        raise ControlError('namespace_unavailable') from None


class DockerControl:
    def __init__(self, contracts, private_path, *, executable=None, runner=bounded_run, namespace_inode=_namespace_inode):
        self.private = Path(private_path)
        no_symlink(self.private)
        self.contracts = copy.deepcopy(contracts)
        self.runner, self.namespace_inode = runner, namespace_inode
        self.executable = executable or next((p for p in DOCKER_PATHS if Path(p).is_file()), None)
        if self.executable not in DOCKER_PATHS or not isinstance(contracts, dict) or not contracts or not set(contracts) <= set(SERVICES):
            raise ControlError('runtime_contract_unavailable')
        for service in contracts:
            self._validate_contract(service)

    def _validate_contract(self, service):
        if service not in SERVICES or service not in self.contracts:
            raise ControlError('service_not_installed')
        record = self.contracts[service]
        if (not isinstance(record, dict) or set(record) != {'project', 'image_id', 'runtime_hash', 'files'}
                or record['project'] != SERVICES[service][0] or not isinstance(record['image_id'], str)
                or not DIGEST_RE.fullmatch(record['image_id']) or not isinstance(record['runtime_hash'], str)
                or not REVISION.fullmatch(record['runtime_hash']) or not isinstance(record['files'], list)
                or not 1 <= len(record['files']) <= 3):
            raise ControlError('runtime_contract_invalid')
        for item in record['files']:
            if not isinstance(item, dict) or set(item) != {'path', 'sha256'} or not isinstance(item['path'], str) or not isinstance(item['sha256'], str) or not REVISION.fullmatch(item['sha256']):
                raise ControlError('compose_contract_invalid')
            path = Path(item['path'])
            no_symlink(path)
            if self.private not in path.parents or not path.is_file() or path.stat().st_size > 1048576:
                raise ControlError('compose_outside_private_root')
            if os.name == 'posix':
                for candidate in (path, *path.parents):
                    if candidate == self.private.parent:
                        break
                    info = candidate.stat()
                    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                        raise ControlError('compose_not_root_owned')
            if file_digest(path) != item['sha256']:
                raise ControlError('compose_revision_changed')
        return record

    def _run(self, *args):
        return self.runner([self.executable, *args], timeout=60, limit=1048576)

    def _inspect(self, service):
        contract = self._validate_contract(service)
        try:
            row = json.loads(self._run('container', 'inspect', '--format', CONTROL_FORMAT, SERVICES[service][1]))
            if (row['project'] != contract['project'] or row['service'] != service or row['name'] != '/' + SERVICES[service][1]
                    or type(row['running']) is not bool or not CONTAINER_ID.fullmatch(row['id'])
                    or row['image_id'] != contract['image_id'] or runtime_hash(row) != contract['runtime_hash']):
                raise ControlError('container_contract_mismatch')
            if service in OWNERS and not re.fullmatch(r'container:[A-Za-z0-9_.-]{1,128}', row['network_mode']):
                raise ControlError('dependent_namespace_mismatch')
            return row
        except (TypeError, KeyError, ValueError):
            raise ControlError('container_contract_unavailable') from None

    def _stale(self, service, row, owner):
        if row['network_mode'] not in ('container:' + owner['id'], 'container:' + SERVICES[OWNERS[service]][1]):
            return True
        if row['running'] and owner['running'] and 'pid' in row and 'pid' in owner:
            return self.namespace_inode(row['pid']) != self.namespace_inode(owner['pid'])
        return False

    def inventory(self):
        rows = {service: self._inspect(service) for service in self.contracts}
        result = {}
        for service, row in rows.items():
            owner = OWNERS.get(service)
            if owner and owner not in rows:
                raise ControlError('namespace_owner_not_installed')
            result[service] = {'running': row['running'], 'namespace_stale': self._stale(service, row, rows[owner]) if owner else False}
        return result

    def stop(self, service):
        row = self._inspect(service)
        self._run('container', 'stop', '--time', '20', row['id'])

    def start(self, service):
        row = self._inspect(service)
        if service in OWNERS:
            owner = self._inspect(OWNERS[service])
            if not owner['running'] or self._stale(service, row, owner):
                raise ControlError('dependent_requires_recreate')
        self._run('container', 'start', row['id'])
        after = self._inspect(service)
        if not after['running']:
            raise ControlError('container_not_running')
        if service in OWNERS and self._stale(service, after, self._inspect(OWNERS[service])):
            raise ControlError('dependent_requires_recreate')

    def recreate(self, service):
        if service not in OWNERS:
            raise ControlError('recreate_only_namespace_dependent')
        self._inspect(service)  # Includes rechecking mounts, capabilities, image and source hashes.
        if not self._inspect(OWNERS[service])['running']:
            raise ControlError('namespace_owner_not_running')
        contract = self._validate_contract(service)
        args = ['compose', '--project-name', contract['project'], '--project-directory', str(self.private)]
        for item in contract['files']:
            args += ['-f', item['path']]
        args += ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', '--', service]
        self._run(*args)
        after = self._inspect(service)
        if not after['running'] or self._stale(service, after, self._inspect(OWNERS[service])):
            raise ControlError('dependent_namespace_not_ready')
