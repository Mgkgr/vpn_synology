import importlib.util
import pathlib
import unittest
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'classify-antidpi-support.py'


class AntidpiSupportTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('antidpi_support', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def evidence(self):
        return {
            'architecture': 'x86_64', 'kernel': '4.4.302+',
            'docker': {'state': 'ok', 'kernel': '4.4.302+', 'architecture': 'x86_64', 'version': '24.0.2'},
            'net_namespace': True,
            'config': {name: 'y' for name, _ in self.module.FEATURES.values()},
            'modules': {}, 'errors': [],
        }

    def test_unloaded_module_is_not_unsupported(self):
        evidence = self.evidence()
        evidence['config']['CONFIG_NETFILTER_NETLINK_QUEUE'] = 'm'
        evidence['modules']['nfnetlink_queue'] = {'loaded': False, 'file_present': True, 'vermagic': '4.4.302+ SMP mod_unload'}
        result = self.module.classify_support(evidence)
        self.assertEqual(result['state'], 'candidate')
        self.assertIn('isolated_queue_bind_not_tested', result['reasons'])
        evidence['modules']['nfnetlink_queue'] = {'loaded': False, 'file_present': True, 'vermagic': None}
        self.assertEqual(self.module.classify_support(evidence)['state'], 'unknown')

    def test_incomplete_probe_is_unknown(self):
        for change in ({'errors': ['ssh_timeout']}, {'config': {}}, {'docker': {'state': 'unavailable'}}, {'errors': ['permission_denied']}):
            with self.subTest(change=change):
                evidence = self.evidence()
                evidence.update(change)
                self.assertEqual(self.module.classify_support(evidence)['state'], 'unknown')

    def test_only_explicit_missing_feature_is_unsupported(self):
        evidence = self.evidence()
        evidence['config']['CONFIG_NETFILTER_NETLINK_QUEUE'] = 'n'
        self.assertEqual(self.module.classify_support(evidence)['state'], 'unsupported')
        evidence['modules']['nfnetlink_queue'] = {'loaded': True}
        self.assertEqual(self.module.classify_support(evidence)['state'], 'unknown')

    def test_wrong_vermagic_or_foreign_docker_kernel_is_not_candidate(self):
        evidence = self.evidence()
        evidence['config']['CONFIG_NETFILTER_NETLINK_QUEUE'] = 'm'
        evidence['modules']['nfnetlink_queue'] = {'file_present': True, 'vermagic': '5.15.0 SMP'}
        self.assertEqual(self.module.classify_support(evidence)['state'], 'unknown')
        evidence = self.evidence()
        evidence['docker']['kernel'] = '6.8.0'
        self.assertEqual(self.module.classify_support(evidence)['state'], 'unknown')

    def test_invalid_evidence_and_unknown_architecture_fail_closed(self):
        for evidence in (None, [], {}, {'config': None}, {'architecture': 'arm64'}):
            with self.subTest(evidence=evidence):
                self.assertEqual(self.module.classify_support(evidence)['state'], 'unknown')

    def test_config_parser_only_collects_allowlisted_options(self):
        result = self.module.parse_config('CONFIG_NETFILTER_NETLINK_QUEUE=m\n# CONFIG_NF_CONNTRACK_MARK is not set\nSECRET=example\n')
        self.assertEqual(result, {'CONFIG_NETFILTER_NETLINK_QUEUE': 'm', 'CONFIG_NF_CONNTRACK_MARK': 'n'})

    def test_collector_runs_only_read_only_docker_info(self):
        commands = []
        def run(argv):
            commands.append(argv)
            return {'ServerVersion': '24.0.2', 'KernelVersion': '4.4.302+', 'Architecture': 'x86_64', 'OSType': 'linux'}
        with patch.object(self.module, 'command_json', side_effect=run):
            report = self.module.collect_evidence()
        self.assertTrue(commands)
        for argv in commands:
            self.assertEqual(argv[1:3], ['info', '--format'])
            self.assertIn(argv[0], self.module.DOCKER_PATHS)
        self.assertEqual(report['docker']['version'], '24.0.2')
        self.assertNotIn('secrets', report)


if __name__ == '__main__':
    unittest.main()
