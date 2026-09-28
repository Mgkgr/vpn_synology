"""Root-only consistent backups from a reviewed discovery manifest; no env paths."""

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from contextlib import closing, contextmanager

from .backups import BackupError, BackupManager, BackupSource, ResticRepository, SOURCE_ROOTS, file_digest, no_symlink, sqlite_schema
from .catalog import DIGEST_RE, read_root_json
from .protocol import COMPONENTS, REVISION, canonical
from .store import JobStore

PRIVATE = Path('/volume1/docker/vpn-dashboard-maintenance/private')
SQL_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')


def load_backup_manifest(private, *, check_live_schema=True):
    try:
        value = read_root_json(private / 'backup-sources.json')
        if (not isinstance(value, dict) or set(value) != {'version', 'repository', 'password_file', 'components', 'images', 'sources', 'revision_files', 'projections', 'identity_source'}
                or value['version'] != 1 or not isinstance(value['components'], list) or not value['components'] or not set(value['components']) <= COMPONENTS
                or not isinstance(value['images'], dict) or set(value['images']) != set(value['components'])
                or any(not isinstance(v, str) or not DIGEST_RE.fullmatch(v) for v in value['images'].values())):
            raise BackupError('backup_discovery_required')
        sources = {}
        for row in value['sources']:
            if not isinstance(row, dict) or set(row) != {'name', 'path', 'kind', 'role', 'component', 'schema'} or row['name'] in sources:
                raise BackupError('backup_discovery_required')
            source = BackupSource(row['name'], Path(row['path']), row['kind'], row['role'], row['component'])
            no_symlink(source.path)
            if not any(root in source.path.parents for root in SOURCE_ROOTS):
                raise BackupError('source_outside_trusted_root')
            if source.kind == 'sqlite':
                if not isinstance(row['schema'], str) or not REVISION.fullmatch(row['schema']) or (check_live_schema and sqlite_schema(source.path) != row['schema']):
                    raise BackupError('discovered_schema_changed')
            elif source.kind != 'file' or row['schema'] is not None:
                raise BackupError('unsupported_backup_source')
            sources[source.name] = source
        identity = sources[value['identity_source']]
        if identity.role != 'wg_identity' or identity.kind != 'file':
            raise BackupError('identity_source_unverified')
        if not value['revision_files'] or any(sources[name].kind != 'file' for name in value['revision_files']):
            raise BackupError('revision_projection_required')
        if not isinstance(value['projections'], dict):
            raise BackupError('revision_projection_required')
        for name, tables in value['projections'].items():
            if sources[name].kind != 'sqlite' or not isinstance(tables, list) or not 1 <= len(tables) <= 32:
                raise BackupError('revision_projection_required')
            for table in tables:
                if (not isinstance(table, dict) or set(table) != {'table', 'columns'} or not SQL_NAME.fullmatch(table['table'])
                        or not isinstance(table['columns'], list) or not 1 <= len(table['columns']) <= 64
                        or any(not isinstance(c, str) or not SQL_NAME.fullmatch(c) for c in table['columns'])):
                    raise BackupError('invalid_revision_projection')
        value['_sources'] = tuple(sources.values())
        return value
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        raise BackupError('backup_discovery_required') from None


def source_revision(manifest):
    sources = {item.name: item for item in manifest['_sources']}
    state = {'files': {name: file_digest(sources[name].path) for name in manifest['revision_files']}, 'projections': {}}
    for name, tables in manifest['projections'].items():
        no_symlink(sources[name].path)
        with closing(sqlite3.connect(sources[name].path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            for table in tables:
                columns = ','.join('"' + c + '"' for c in table['columns'])
                rows = db.execute('SELECT ' + columns + ' FROM "' + table['table'] + '" LIMIT 10001').fetchall()
                if len(rows) > 10000:
                    raise BackupError('revision_projection_too_large')
                state['projections'][name + ':' + table['table']] = sorted(rows, key=canonical)
    encoded = canonical(state)
    if len(encoded) > 2097152:
        raise BackupError('revision_projection_too_large')
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def backup_lease(store):
    lease = store.acquire_writer('dashboard_config')
    stop, lost = threading.Event(), threading.Event()
    def renew():
        while not stop.wait(30):
            try:
                store.renew_writer(lease)
            except Exception:
                lost.set()
                return
    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    def valid(_job):
        lock = store.lock_state()
        return not lost.is_set() and lock.get('token') == lease and lock.get('state') == 'held'
    try:
        yield lease, valid
    finally:
        stop.set()
        thread.join(timeout=6)
        store.release_writer(lease, 'uncertain' if lost.is_set() or thread.is_alive() else 'complete')


def run(action, snapshot_id=None, *, private=PRIVATE):
    if os.name != 'posix' or os.geteuid() != 0:
        raise BackupError('linux_root_required')
    manifest = load_backup_manifest(private, check_live_schema=action == 'backup')
    repository = ResticRepository(manifest['repository'], manifest['password_file'], private)
    identity = next(item.path for item in manifest['_sources'] if item.name == manifest['identity_source'])
    store = JobStore(private / 'jobs.sqlite3')
    with backup_lease(store) as (job_id, held):
        manager = BackupManager(private, repository, manifest['_sources'], revision=lambda: source_revision(manifest),
            image_digests=lambda: manifest['images'], identity_digest=lambda: file_digest(identity), lock_check=held)
        if action == 'backup' and snapshot_id is None:
            result = manager.create(tuple(manifest['components']), job_id)
        elif action == 'verify' and isinstance(snapshot_id, str) and REVISION.fullmatch(snapshot_id):
            result = manager.verify(snapshot_id)
        else:
            raise BackupError('exact_snapshot_required')
        if not held(job_id):
            raise BackupError('backup_lock_lost')
        return {'result': 'verified', 'snapshot_id': result.snapshot_id, 'verified_at': result.verified_at}


if __name__ == '__main__':
    try:
        if not (len(sys.argv) == 2 and sys.argv[1] == 'backup') and not (len(sys.argv) == 3 and sys.argv[1] == 'verify'):
            raise BackupError('usage_backup_or_verify_exact_snapshot')
        print(json.dumps(run(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)))
    except Exception:
        print('BACKUP=unavailable; trusted discovery, encrypted repository and exclusive lease are required', file=sys.stderr)
        sys.exit(1)
