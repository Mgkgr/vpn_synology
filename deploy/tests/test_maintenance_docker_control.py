import copy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class DockerControlTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module('maintenance.docker_control')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name).resolve()
        self.compose = self.private / 'compose.json'
        self.compose.write_text('{}', encoding='utf-8')
        self.rows = {}
        for service, project in [('wireguard', 'vpn-gateway'), ('mihomo', 'vpn-gateway'), ('dashboard', 'vpn-dashboard')]:
            self.rows[service] = dict(id=('a' if service == 'wireguard' else 'b' if service == 'mihomo' else 'c') * 64,
                name='/vpn-' + service, service=service, project=project, running=True,
                image_id='sha256:' + 'd' * 64, network_mode='container:' + 'a' * 64 if service == 'mihomo' else project + '_default',
                runtime={'env': ['SECRET=never-output'], 'privileged': False, 'mounts': [], 'caps': [], 'user': ''})
        self.calls = []
        self.contracts = {service: dict(project=row['project'], image_id=row['image_id'],
            runtime_hash=self.m.runtime_hash(row), files=[{'path': str(self.compose), 'sha256': self.m.file_digest(self.compose)}]) for service, row in self.rows.items()}
        self.docker = self.m.DockerControl(self.contracts, self.private, executable='/usr/local/bin/docker', runner=self.run_command)

    def run_command(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertEqual(kwargs, {'timeout': 60, 'limit': 1048576})
        if argv[1:3] == ['container', 'inspect']:
            target = argv[-1]
            row = next(row for row in self.rows.values() if row['name'].lstrip('/') == target)
            return json.dumps(row)
        return ''

    def test_start_stop_use_inspected_immutable_id(self):
        self.docker.stop('mihomo')
        self.assertEqual(self.calls[-1], ['/usr/local/bin/docker', 'container', 'stop', '--time', '20', 'b' * 64])
        self.docker.start('wireguard')
        starts = [c for c in self.calls if c[1:3] == ['container', 'start']]
        self.assertEqual(starts, [['/usr/local/bin/docker', 'container', 'start', 'a' * 64]])

    def test_foreign_label_mount_capability_or_image_blocks_effect(self):
        original = copy.deepcopy(self.rows['mihomo'])
        for field, value in [('project', 'unrelated'), ('image_id', 'sha256:' + 'f' * 64), ('runtime', {'privileged': True})]:
            self.rows['mihomo'] = {**original, field: value}
            with self.assertRaises(self.m.ControlError):
                self.docker.stop('mihomo')
        self.assertFalse(any(c[2] in ('start', 'stop') for c in self.calls))

    def test_recreate_revalidates_files_and_only_dependent_service(self):
        self.docker.recreate('mihomo')
        command = next(c for c in self.calls if 'compose' in c)
        self.assertEqual(command[-9:], ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', '--', 'mihomo'])
        self.assertIn('--project-name', command)
        self.assertNotIn('down', command)
        self.compose.write_text('{"changed":true}', encoding='utf-8')
        self.calls.clear()
        with self.assertRaises(self.m.ControlError):
            self.docker.recreate('mihomo')
        self.assertFalse(any('compose' in c for c in self.calls))
        with self.assertRaises(self.m.ControlError):
            self.docker.recreate('wireguard')

    def test_namespace_is_checked_again_before_start(self):
        self.rows['mihomo']['network_mode'] = 'container:' + 'e' * 64
        self.assertTrue(self.docker.inventory()['mihomo']['namespace_stale'])
        with self.assertRaises(self.m.ControlError):
            self.docker.start('mihomo')
        self.assertFalse(any(c[1:3] == ['container', 'start'] for c in self.calls))

    def test_same_container_id_with_different_live_namespace_is_stale(self):
        self.rows['wireguard']['pid'], self.rows['mihomo']['pid'] = 41, 42
        self.docker.namespace_inode = lambda pid: pid
        self.assertTrue(self.docker.inventory()['mihomo']['namespace_stale'])

    def test_hash_ignores_generated_hostname_and_compose_location_not_user_labels(self):
        row = copy.deepcopy(self.rows['mihomo'])
        row['runtime'] = {'config': {'Hostname': row['id'][:12], 'Labels': {'com.docker.compose.project.working_dir': '/old', 'user-label': 'one'}}, 'host': {'NetworkMode': row['network_mode']}}
        other = copy.deepcopy(row)
        other['id'] = 'e' * 64
        other['runtime']['config']['Hostname'] = other['id'][:12]
        other['runtime']['config']['Labels']['com.docker.compose.project.working_dir'] = '/private'
        self.assertEqual(self.m.runtime_hash(row), self.m.runtime_hash(other))
        other['runtime']['config']['Labels']['user-label'] = 'two'
        self.assertNotEqual(self.m.runtime_hash(row), self.m.runtime_hash(other))

    def test_arbitrary_service_path_and_unknown_contract_are_denied(self):
        for service in ('other', '../mihomo', 'mihomo;id'):
            with self.assertRaises(self.m.ControlError):
                self.docker.start(service)
        self.contracts['mihomo']['files'][0]['path'] = str(self.private.parent / 'outside.yaml')
        with self.assertRaises(self.m.ControlError):
            self.m.DockerControl(self.contracts, self.private, executable='/usr/local/bin/docker', runner=self.run_command)

    def test_docker_mount_enumeration_order_is_not_configuration_drift(self):
        row=copy.deepcopy(self.rows['dashboard'])
        row['runtime']['mounts']=[{'Destination':'/data','Source':'/private/data','RW':True},
                                   {'Destination':'/run/config','Source':'/private/config','RW':False}]
        other=copy.deepcopy(row); other['runtime']['mounts'].reverse()
        self.assertEqual(self.m.runtime_hash(row),self.m.runtime_hash(other))
        other['runtime']['mounts'][0]['RW']=True
        self.assertNotEqual(self.m.runtime_hash(row),self.m.runtime_hash(other))


if __name__ == '__main__':
    unittest.main()
