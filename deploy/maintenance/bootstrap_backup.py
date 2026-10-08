"""Manual NAS bootstrap of a verified encrypted snapshot; never restores live data.

No service mutations, timers, socket server or grants are installed. Production
writers are not paused: all configuration projections must remain unchanged
through capture, and each SQLite database uses the online backup API.
"""
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
from contextlib import closing

from . import backup_cli
from .backups import BackupError, ResticRepository, file_digest, no_symlink, private_directory, sqlite_schema
from .protocol import canonical

PRIVATE = backup_cli.PRIVATE
GATEWAY = Path('/volume1/docker/vpn-gateway')
DASHBOARD = Path('/volume1/docker/vpn-dashboard')
DOCKER = '/usr/local/bin/docker'
RESTIC_SHA = '20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639'
COMPONENTS = ('wireguard', 'mihomo', 'uptime-kuma', 'metacubexd', 'dashboard')
FIXED_SOURCES = (
    ('wireguard/wg-easy.db', GATEWAY / 'wireguard/data/wg-easy.db', 'sqlite', 'database', 'wireguard'),
    ('wireguard/wg0.conf', GATEWAY / 'wireguard/data/wg0.conf', 'file', 'wg_identity', 'wireguard'),
    ('kuma/kuma.db', GATEWAY / 'uptime-kuma/kuma.db', 'sqlite', 'database', 'uptime-kuma'),
    ('kuma/db-config.json', GATEWAY / 'uptime-kuma/db-config.json', 'file', 'config', 'uptime-kuma'),
    ('dashboard/dashboard.sqlite3', DASHBOARD / 'deploy/data/dashboard.sqlite3', 'sqlite', 'database', 'dashboard'),
    ('dashboard/dashboard.env', DASHBOARD / 'deploy/dashboard.env', 'file', 'dashboard_secrets', 'dashboard'),
    ('dashboard/secrets/dashboard_encryption_key', DASHBOARD / 'deploy/secrets/dashboard_encryption_key', 'file', 'dashboard_secrets', 'dashboard'),
    ('dashboard/secrets/mihomo_api_secret', DASHBOARD / 'deploy/secrets/mihomo_api_secret', 'file', 'dashboard_secrets', 'dashboard'),
    ('dashboard/compose.yaml', DASHBOARD / 'compose.yaml', 'file', 'config', 'dashboard'),
    ('gateway/compose.yaml', GATEWAY / 'compose.yaml', 'file', 'config', 'common'),
    ('mihomo/config.yaml', GATEWAY / 'mihomo/config.yaml', 'file', 'config', 'mihomo'),
    ('mihomo/GeoIP.dat', GATEWAY / 'mihomo/GeoIP.dat', 'file', 'geodata', 'mihomo'),
    ('mihomo/GeoSite.dat', GATEWAY / 'mihomo/GeoSite.dat', 'file', 'geodata', 'mihomo'),
)
TABLES = {
    'wireguard': ('__drizzle_migrations', 'clients_table', 'general_table', 'hooks_table',
                  'interfaces_table', 'one_time_links_table', 'user_configs_table', 'users_table'),
    'dashboard': ('dashboard_admins', 'dashboard_owners', 'managed_rule_policies', 'probe_targets', 'wgeasy_credentials'),
    'uptime-kuma': ('api_key', 'docker_host', 'group', 'incident', 'knex_migrations', 'maintenance',
                   'maintenance_status_page', 'monitor', 'monitor_group', 'monitor_maintenance',
                   'monitor_notification', 'monitor_tag', 'notification', 'proxy', 'remote_browser',
                   'setting', 'status_page', 'status_page_cname', 'tag', 'user'),
}
VOLATILE_TABLES = {
    'wireguard': (),
    'dashboard': ('audit_events', 'dashboard_sessions', 'gateway_traffic_samples', 'geo_file_metadata',
                  'geo_updates', 'login_throttle_records', 'peer_baselines', 'peer_snapshots',
                  'probe_events', 'route_events', 'traffic_hourly', 'traffic_monthly',
                  'maintenance_grants', 'maintenance_stepup_throttles', 'maintenance_submission_receipts',
                  'maintenance_submit_intents', 'notification_delivery_states', 'outbound_control_cycles',
                  'outbound_health_states', 'outbound_incidents', 'site_probe_results', 'site_probe_runs',
                  'site_probe_schedule'),
    'uptime-kuma': ('domain_expiry', 'heartbeat', 'knex_migrations_lock', 'monitor_tls_info',
                   'notification_sent_history', 'stat_daily', 'stat_hourly', 'stat_minutely'),
}


def projections(path, component):
    no_symlink(path)
    result = []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.execute('PRAGMA query_only=ON')
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if names - set(TABLES[component]) - set(VOLATILE_TABLES[component]):
            raise BackupError('unreviewed_configuration_table')
        for table in TABLES[component]:
            columns = [row[1] for row in db.execute('PRAGMA table_info("' + table + '")')]
            # dns_last_result is live probe output, not configuration.
            if component == 'uptime-kuma' and table == 'monitor':
                columns = [name for name in columns if name != 'dns_last_result']
            if (not columns or len(columns) > 256
                    or any(not backup_cli.SQL_NAME.fullmatch(c) for c in columns)):
                raise BackupError('reviewed_configuration_schema_missing')
            result.append({'table': table, 'columns': columns})
    return result


def production_state():
    result = {}
    for component in COMPONENTS:
        output = subprocess.run([DOCKER, 'inspect', '--format',
            '{{json .Id}}|{{json .Image}}|{{json .Config.Labels}}|{{json .State.StartedAt}}|{{json .RestartCount}}|{{json .State.Running}}',
            'vpn-' + component], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=10, check=True).stdout.decode('utf-8')
        container, image, labels, started, restarts, running = map(json.loads, output.strip().split('|'))
        project = 'vpn-dashboard' if component == 'dashboard' else 'vpn-gateway'
        if (labels.get('com.docker.compose.project') != project
                or labels.get('com.docker.compose.service') != component or running is not True
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', image)):
            raise BackupError('unexpected_production_identity')
        result[component] = {'container': container, 'image': image, 'started': started, 'restarts': restarts}
    return result


def write_private(path, data):
    no_symlink(path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def initialize_repository(private):
    binary = Path('/usr/local/bin/restic')
    no_symlink(binary)
    info = binary.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or file_digest(binary) != RESTIC_SHA:
        raise BackupError('pinned_restic_binary_changed')
    password = private / 'restic-password'
    repository = private / 'repository'
    if not password.exists():
        if repository.exists():
            raise BackupError('repository_exists_without_password')
        write_private(password, secrets.token_urlsafe(48).encode('ascii'))
    private_directory(repository)
    restic = ResticRepository(repository, password, private, executable=binary)
    if not (repository / 'config').exists():
        if any(repository.iterdir()):
            raise BackupError('partial_repository_requires_inspection')
        restic._call(['init', '--repository-version', '2'])
    return restic


def discover(private, state):
    sources = list(FIXED_SOURCES)
    rules = GATEWAY / 'mihomo/rules'
    no_symlink(rules)
    for path in sorted(rules.iterdir()):
        if not re.fullmatch(r'[a-z0-9-]+\.txt', path.name) or not path.is_file():
            raise BackupError('unexpected_rules_inventory')
        sources.append(('mihomo/rules/' + path.name, path, 'file', 'config', 'mihomo'))
    # Include project .env and installed immutable-image overlays if present.
    for prefix, root, component in (('gateway', GATEWAY, 'common'), ('dashboard', DASHBOARD, 'dashboard')):
        for name in ('.env', 'compose.versions.yaml'):
            path = root / name
            if path.exists():
                sources.append((prefix + '/' + name, path, 'file', 'config', component))
    inventory, projection = [], {}
    for name, path, kind, role, component in sources:
        no_symlink(path)
        meta = path.stat()
        if not stat.S_ISREG(meta.st_mode):
            raise BackupError('unexpected_source_type')
        inventory.append({'name': name, 'path': str(path), 'kind': kind, 'role': role,
                          'component': component, 'schema': sqlite_schema(path) if kind == 'sqlite' else None})
        if kind == 'sqlite':
            projection[name] = projections(path, component)
    manifest_path = private / 'backup-sources.json'
    inventory.append({'name': 'maintenance/backup-sources.json', 'path': str(manifest_path), 'kind': 'file',
                      'role': 'worker_state', 'component': 'common', 'schema': None})
    return {'version': 1, 'repository': str(private / 'repository'), 'password_file': str(private / 'restic-password'),
            'components': list(COMPONENTS), 'images': {k: v['image'] for k, v in state.items()},
            'sources': inventory, 'revision_files': [i['name'] for i in inventory if i['kind'] == 'file'],
            'projections': projection, 'identity_source': 'wireguard/wg0.conf'}


def run():
    if os.name != 'posix' or os.geteuid() != 0:
        raise BackupError('linux_root_required')
    private_directory(PRIVATE.parent)
    private_directory(PRIVATE)
    before = production_state()
    manifest = discover(PRIVATE, before)
    path = PRIVATE / 'backup-sources.json'
    if path.exists():
        if backup_cli.read_root_json(path) != manifest:
            raise BackupError('backup_manifest_drift_requires_review')
    else:
        write_private(path, canonical(manifest))
    repository = initialize_repository(PRIVATE)
    result = backup_cli.run('backup')
    # Independent encrypted-data read check after the file-by-file restore validation.
    repository._call(['check', '--read-data'], limit=2097152)
    after = production_state()
    if before != after:
        raise BackupError('production_state_changed_during_backup')
    if any(any((PRIVATE / name).iterdir()) for name in ('staging', 'verify')):
        raise BackupError('plaintext_workspace_cleanup_incomplete')
    return dict(result, production_unchanged=True, restored_files=len(manifest['sources']),
                sqlite_databases=sum(i['kind'] == 'sqlite' for i in manifest['sources']),
                repository_checked=True, plaintext_workspaces_clean=True,
                worker_installed=False, off_host_copy=False)
