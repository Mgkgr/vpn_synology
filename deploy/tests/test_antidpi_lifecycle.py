import copy
import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/antidpi-lifecycle.py'


class AntidpiLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'scoped namespace recovery planner is missing')
        spec = importlib.util.spec_from_file_location('antidpi_lifecycle', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.owner = {'id': 'a' * 64, 'image': 'sha256:' + '1' * 64, 'running': True,
                      'namespace': 20, 'healthy': True, 'network': 'none',
                      'labels': {'vpn.dashboard.scope': 'antidpi-offline-probe',
                                 'vpn.dashboard.run': 'b' * 32, 'vpn.dashboard.role': 'engine'}}
        self.auth = {'id': 'c' * 64, 'image': 'sha256:' + '2' * 64, 'running': True,
                     'namespace': 20, 'healthy': True, 'network': 'container:' + 'a' * 64,
                     'labels': dict(self.owner['labels'], **{'vpn.dashboard.role': 'socks'})}

    def plan(self, last_repair=None, now=1000):
        return self.module.recovery_plan(self.owner, self.auth,
                                        {'engine': 'sha256:' + '1' * 64, 'socks': 'sha256:' + '2' * 64},
                                        'b' * 32, host_namespace=1, last_repair=last_repair, now=now)

    def test_healthy_shared_namespace_requires_no_operation(self):
        self.assertEqual(self.plan(), [])

    def test_recreated_owner_requires_auth_recreation_not_restart(self):
        self.owner.update(id='d' * 64, namespace=30)
        self.assertEqual(self.plan(), ['stop_socks', 'recreate_socks'])

    def test_same_owner_id_with_changed_namespace_still_requires_recreation(self):
        self.owner['namespace'] = 30
        self.assertEqual(self.plan(), ['stop_socks', 'recreate_socks'])

    def test_dead_owner_never_causes_an_auth_only_restart(self):
        self.owner.update(running=False, namespace=None, healthy=False)
        self.assertEqual(self.plan(), ['stop_socks', 'wait_engine'])

    def test_stopped_auth_is_recreated_to_avoid_retaining_old_namespace(self):
        self.auth.update(running=False, namespace=None, healthy=False)
        self.assertEqual(self.plan(), ['recreate_socks'])

    def test_cooldown_still_stops_stale_listener_without_restart_loop(self):
        self.owner['namespace'] = 30
        self.assertEqual(self.plan(last_repair=950), ['stop_socks', 'cooldown'])
        self.assertEqual(self.plan(last_repair=700), ['stop_socks', 'recreate_socks'])

    def test_foreign_labels_images_and_host_namespace_are_rejected(self):
        original = copy.deepcopy(self.auth)
        for field, value in (('image', 'sha256:' + '3' * 64), ('namespace', 1),
                             ('network', 'host'), ('id', 'vpn-dashboard'),
                             ('labels', {'vpn.dashboard.scope': 'other'})):
            self.auth = copy.deepcopy(original)
            self.auth[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.plan()


if __name__ == '__main__':
    unittest.main()
