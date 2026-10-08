import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'deploy/scripts/enable-outbound-health.py'


class HealthDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'health activation is not implemented')
        spec = importlib.util.spec_from_file_location('health_activation', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_flag_preserves_all_other_environment_settings(self):
        source = b'# reviewed\r\nOTHER=value\r\nOUTBOUND_HEALTH_ENABLED=false\r\n'
        self.assertEqual(self.module.enable_flag(source), source.replace(b'=false', b'=true'))
        result = self.module.enable_flag(b'OTHER=value\n')
        self.assertEqual(result, b'OTHER=value\nOUTBOUND_HEALTH_ENABLED=true\n')
        self.assertEqual(self.module.enable_flag(result), result)
        for bad in (b'OUTBOUND_HEALTH_ENABLED=false\nOUTBOUND_HEALTH_ENABLED=true\n',
                    b' OUTBOUND_HEALTH_ENABLED=false\n', b'OUTBOUND_HEALTH_ENABLED=maybe\n'):
            with self.assertRaises(self.module.ActivationError):
                self.module.enable_flag(bad)

    def make_actions(self):
        calls, statuses = [], []
        files = {'config': b'original', 'env': b'OTHER=value\n'}
        error = self.module.ActivationError

        class Actions:
            def read(self):
                return dict(files)

            def backup(self):
                calls.append('backup')
                return {'result': 'verified', 'snapshot_id': 'a' * 64}

            def validate(self, content):
                calls.append('validate')

            def write(self, key, expected, content):
                if files[key] != expected:
                    raise error('concurrent_change')
                files[key] = content
                calls.append('write_' + key)

            def reload(self):
                calls.append('reload')

            def restart_dashboard(self):
                calls.append('restart_dashboard')

            def verify(self):
                calls.append('verify')
                return True

            def publish(self, status):
                statuses.append(dict(status))

        return Actions(), files, calls, statuses

    def test_verified_order_requires_backup_and_preserves_unrelated_settings(self):
        actions, files, calls, statuses = self.make_actions()
        result = self.module.activate(actions, lambda config: config + b'\nprobe-groups')
        self.assertEqual(result['state'], 'verified')
        self.assertEqual(calls, ['backup', 'validate', 'write_config', 'reload', 'write_env', 'restart_dashboard', 'verify'])
        self.assertEqual(files['env'], b'OTHER=value\nOUTBOUND_HEALTH_ENABLED=true\n')
        self.assertEqual([row['state'] for row in statuses], ['not_started', 'validated', 'applied', 'verified'])

    def test_concurrent_config_change_aborts_before_writes(self):
        actions, files, calls, statuses = self.make_actions()
        original = actions.backup

        def changed():
            receipt = original()
            files['config'] = b'changed-by-owner'
            return receipt

        actions.backup = changed
        with self.assertRaises(self.module.ActivationError):
            self.module.activate(actions, lambda config: config + b'\nprobe-groups')
        self.assertEqual(calls, ['backup'])
        self.assertEqual(files['config'], b'changed-by-owner')
        self.assertEqual(statuses[-1]['state'], 'not_started')

    def test_unverified_backup_and_invalid_config_never_write(self):
        for step in ('backup', 'validate'):
            actions, files, calls, statuses = self.make_actions()

            def fail(*_):
                raise RuntimeError('sensitive internal details')

            setattr(actions, step, fail)
            with self.assertRaises(self.module.ActivationError) as error:
                self.module.activate(actions, lambda config: config + b'\nprobe-groups')
            self.assertNotIn('sensitive', str(error.exception))
            self.assertEqual(files['config'], b'original')
            self.assertEqual(statuses[-1]['state'], 'not_started')

    def test_disconnect_is_unknown_not_success_or_claimed_rollback(self):
        actions, files, calls, statuses = self.make_actions()

        def disconnect():
            raise ConnectionError('transport lost')

        actions.reload = disconnect
        with self.assertRaises(self.module.ActivationError):
            self.module.activate(actions, lambda config: config + b'\nprobe-groups')
        self.assertEqual(statuses[-1]['state'], 'unknown')
        self.assertIn(b'probe-groups', files['config'])
        self.assertNotIn('restart_dashboard', calls)

    def test_artifact_contains_health_runtime_modules(self):
        required = ('outbounds.py', 'outbound_health.py', 'health_collector.py', 'health_api.py', 'kuma_push.py')
        for name in required:
            self.assertTrue((ROOT / 'backend/app' / name).is_file(), name)
        self.assertTrue((ROOT / 'frontend/src/components/OutboundHealthPanel.tsx').is_file())


if __name__ == '__main__':
    unittest.main()
