import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class DashboardMaintenanceBindingTests(unittest.TestCase):
    def test_dashboard_exposes_only_readonly_scoped_ipc_not_host_privileges(self):
        service = yaml.safe_load((ROOT / 'compose.yaml').read_text(encoding='utf-8'))['services']['dashboard']
        mounts = service['volumes']
        scoped = [value for value in mounts if '/run/vpn-maintenance' in str(value)]
        self.assertEqual(scoped, ['/run/vpn-maintenance:/run/vpn-maintenance:ro'])
        for value in mounts:
            self.assertNotIn('docker.sock', str(value))
            self.assertNotIn('vpn-dashboard-maintenance/private', str(value))
        self.assertTrue(service['read_only'])
        self.assertEqual(service['cap_drop'], ['ALL'])
        settings = dict(line.split('=', 1) for line in (ROOT / 'deploy/dashboard.env.example').read_text(encoding='utf-8').splitlines()
                        if line and not line.startswith('#'))
        self.assertEqual(settings.get('MAINTENANCE_ENABLED'), 'false')

    def load_helper(self):
        path = ROOT / 'deploy/scripts/prepare-maintenance-socket.py'
        self.assertTrue(path.is_file(), 'secure directory preparation is missing')
        spec = importlib.util.spec_from_file_location('prepare_maintenance_socket', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_directory_is_prepared_without_replacing_live_socket_parent(self):
        helper = self.load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / 'vpn-maintenance'
            helper.prepare(parent)
            marker = parent / 'control.sock'
            marker.write_bytes(b'existing-socket-placeholder')
            inode = parent.stat().st_ino
            helper.prepare(parent)
            self.assertEqual(parent.stat().st_ino, inode)
            self.assertEqual(marker.read_bytes(), b'existing-socket-placeholder')
            if os.name == 'posix':
                self.assertEqual(parent.stat().st_mode & 0o777, 0o750)
                self.assertEqual((parent.stat().st_uid, parent.stat().st_gid), (0, 10001))

    def test_non_directory_is_rejected_without_modification(self):
        helper = self.load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'vpn-maintenance'
            path.write_bytes(b'keep')
            with self.assertRaises(ValueError):
                helper.prepare(path)
            self.assertEqual(path.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
