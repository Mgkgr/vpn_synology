import importlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class BackupBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module('maintenance.bootstrap_backup')

    def test_projection_covers_all_wg_identity_columns_not_traffic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wg.sqlite'
            with closing(sqlite3.connect(str(path))) as db:
                for name in self.m.TABLES['wireguard']:
                    db.execute('CREATE TABLE "' + name + '" (id INTEGER, private_key TEXT, new_setting TEXT)')
            tables = self.m.projections(path, 'wireguard')
            self.assertEqual({p['table'] for p in tables}, set(self.m.TABLES['wireguard']))
            self.assertTrue(all(p['columns'] == ['id', 'private_key', 'new_setting'] for p in tables))

    def test_unknown_configuration_schema_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wg.sqlite'
            with closing(sqlite3.connect(str(path))) as db:
                db.execute('CREATE TABLE clients_table (id INTEGER)')
            with self.assertRaises(self.m.BackupError):
                self.m.projections(path, 'wireguard')

    def test_new_unreviewed_table_cannot_escape_revision_tracking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wg.sqlite'
            with closing(sqlite3.connect(str(path))) as db:
                for name in self.m.TABLES['wireguard']:
                    db.execute('CREATE TABLE "' + name + '" (id INTEGER)')
                db.execute('CREATE TABLE new_client_keys (id INTEGER)')
            with self.assertRaises(self.m.BackupError):
                self.m.projections(path, 'wireguard')

    def test_source_inventory_keeps_server_identity_and_encryption_keys(self):
        names = {item[0] for item in self.m.FIXED_SOURCES}
        self.assertTrue({'wireguard/wg-easy.db', 'wireguard/wg0.conf',
                         'dashboard/dashboard.sqlite3', 'dashboard/dashboard.env',
                         'dashboard/secrets/dashboard_encryption_key',
                         'dashboard/secrets/mihomo_api_secret',
                         'kuma/kuma.db', 'mihomo/config.yaml',
                         'gateway/compose.yaml', 'dashboard/compose.yaml'} <= names)
        self.assertNotIn('restic-password', ' '.join(names))

    def test_reviewed_additive_dashboard_state_is_copied_but_not_configuration_revision(self):
        telemetry={'maintenance_grants','maintenance_stepup_throttles','maintenance_submission_receipts',
            'maintenance_submit_intents','notification_delivery_states','outbound_control_cycles',
            'outbound_health_states','outbound_incidents','site_probe_results','site_probe_runs','site_probe_schedule'}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'dashboard.sqlite'
            with closing(sqlite3.connect(str(path))) as db:
                for name in set(self.m.TABLES['dashboard'])|telemetry:
                    db.execute('CREATE TABLE "'+name+'" (id INTEGER)')
            result=self.m.projections(path,'dashboard')
            self.assertEqual({row['table'] for row in result},set(self.m.TABLES['dashboard']))


if __name__ == '__main__':
    unittest.main()
