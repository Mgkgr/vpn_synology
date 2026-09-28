import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class BackupCliTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module('maintenance.backup_cli')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name).resolve()

    def test_missing_or_incomplete_manifest_blocks_backup(self):
        with self.assertRaises(self.m.BackupError):
            self.m.load_backup_manifest(self.private)
        (self.private / 'backup-sources.json').write_text('{"sources":[]}', encoding='utf-8')
        with self.assertRaises(self.m.BackupError):
            self.m.load_backup_manifest(self.private)

    def test_manifest_requires_recorded_database_schema_and_identity_source(self):
        manifest = {'version': 1, 'repository': str(self.private / 'restic'), 'password_file': str(self.private / 'password'),
            'components': ['wireguard'], 'images': {'wireguard': 'sha256:' + 'a' * 64}, 'sources': [
                {'name': 'wg.sqlite', 'path': '/volume1/docker/vpn-gateway/wg-easy/wg.sqlite', 'kind': 'sqlite', 'role': 'wg_identity', 'component': 'wireguard', 'schema': None}], 'revision_files': [], 'identity_source': 'wg.sqlite'}
        (self.private / 'backup-sources.json').write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(self.m.BackupError):
            self.m.load_backup_manifest(self.private)

    def test_legacy_wrappers_do_not_delete_environment_targets_or_copy_live_databases(self):
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        for name in ('backup-dashboard.sh', 'restore-verify.sh'):
            source = (scripts / name).read_text(encoding='utf-8')
            self.assertNotIn('rm -rf', source)
            self.assertNotIn('restic restore latest', source)
            self.assertNotIn('restic init', source)
            self.assertNotIn('tar -C', source)
            self.assertIn('maintenance.backup_cli', source)
        deploy = (scripts / 'run-dashboard-deploy.sh').read_text(encoding='utf-8')
        self.assertLess(deploy.index('compose.versions.yaml'), deploy.index('if /usr/local/sbin/vpn-dashboard-deploy'))

    def test_verification_discovery_does_not_require_working_live_database(self):
        root = self.private.as_posix() + '/'
        manifest = {'version': 1, 'repository': str(self.private / 'restic'), 'password_file': str(self.private / 'password'),
            'components': ['wireguard'], 'images': {'wireguard': 'sha256:' + 'a' * 64}, 'sources': [
                {'name': 'wg.conf', 'path': root + 'wg-easy/wg0.conf', 'kind': 'file', 'role': 'wg_identity', 'component': 'wireguard', 'schema': None},
                {'name': 'wg.sqlite', 'path': root + 'wg-easy/wg.sqlite', 'kind': 'sqlite', 'role': 'database', 'component': 'wireguard', 'schema': 'b' * 64}],
            'revision_files': ['wg.conf'], 'identity_source': 'wg.conf', 'projections': {}}
        (self.private / 'backup-sources.json').write_text(json.dumps(manifest), encoding='utf-8')
        with patch.object(self.m, 'SOURCE_ROOTS', (self.private,)), patch.object(self.m, 'sqlite_schema', side_effect=OSError('live database unavailable')) as schema:
            result = self.m.load_backup_manifest(self.private, check_live_schema=False)
            self.assertEqual(len(result['_sources']), 2)
            schema.assert_not_called()

    def test_backup_uses_shared_lease_and_releases_it(self):
        store = self.m.JobStore(self.private / 'private/jobs.sqlite3')
        with self.m.backup_lease(store) as (token, held):
            self.assertTrue(held(token))
            self.assertEqual(store.lock_state()['kind'], 'writer')
        self.assertEqual(store.lock_state()['state'], 'free')


if __name__ == '__main__':
    unittest.main()
