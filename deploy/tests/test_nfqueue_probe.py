import errno
import hashlib
import importlib.util
import pathlib
import struct
import unittest
from unittest.mock import Mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'probe-nfqueue-isolated.py'


class NfqueueProbeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('nfqueue_probe', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def ack(self, seq=1, error=0, peer=321):
        request = struct.pack('=IHHII', 28, 0x302, 5, seq, peer)
        return struct.pack('=IHHIIi', 36, 2, 0, seq, peer, error) + request

    def test_bind_message_matches_linux44_wire_format(self):
        message = self.module.config_message(1, 321, command=1)
        expected = bytes.fromhex(
            '1c000000 0203 0500 01000000 41010000 '
            '0000 1092 0800 0100 0100 0002')
        self.assertEqual(message, expected)

    def test_copy_params_include_packed_five_byte_payload_and_padding(self):
        message = self.module.config_message(2, 321, copy_packet=True)
        self.assertEqual(message[16:], bytes.fromhex('0000 1092 0900 0200 0000ffff 02 000000'))
        self.assertEqual(struct.unpack_from('=I', message)[0], len(message))

    def test_kernel_ack_success_and_negative_errno_are_distinct(self):
        self.module.check_ack(self.ack(), (0, 0), 1, 321)
        with self.assertRaises(OSError) as failure:
            self.module.check_ack(self.ack(error=-errno.EPERM), (0, 0), 1, 321)
        self.assertEqual(failure.exception.errno, errno.EPERM)

    def test_ack_rejects_foreign_sequence_sender_truncation_and_echo(self):
        for data, address in (
            (self.ack(seq=3), (0, 0)),
            (self.ack(), (123, 0)),
            (self.ack()[:-1], (0, 0)),
            (self.ack()[:20] + b'\0' * 16, (0, 0)),
            (self.ack() + b'garbage', (0, 0)),
        ):
            with self.subTest(data=data, address=address), self.assertRaises(ValueError):
                self.module.check_ack(data, address, 1, 321)

    def module_bytes(self, version='4.4.302+', dependency='nfnetlink'):
        elf = bytearray(64)
        elf[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', elf, 18, 62)
        return bytes(elf) + ('\0vermagic=' + version + ' SMP mod_unload \0depends=' + dependency + '\0').encode('ascii')

    def test_module_requires_pinned_digest_matching_kernel_and_loaded_dependencies(self):
        data = self.module_bytes()
        digest = hashlib.sha256(data).hexdigest()
        metadata = self.module.validate_module_bytes(data, digest, '4.4.302+', {'nfnetlink'})
        self.assertEqual(metadata['depends'], ['nfnetlink'])
        for sha, kernel, loaded in ((digest, '5.0.0', {'nfnetlink'}), (digest, '4.4.302+', set()), ('0' * 64, '4.4.302+', {'nfnetlink'})):
            with self.subTest(sha=sha, kernel=kernel, loaded=loaded), self.assertRaises(ValueError):
                self.module.validate_module_bytes(data, sha, kernel, loaded)

    def test_namespace_is_checked_before_opening_any_socket(self):
        create_socket = Mock()
        with self.assertRaises(RuntimeError):
            self.module.queue_probe(123, 123, ['lo'], create_socket)
        with self.assertRaises(RuntimeError):
            self.module.queue_probe(123, 456, ['lo', 'eth0'], create_socket)
        create_socket.assert_not_called()

    def test_synology_automatic_sit0_is_allowed_only_down_without_routes(self):
        links = ('1: lo: <LOOPBACK> mtu 65536 qdisc noop state DOWN mode DEFAULT group default qlen 1\\    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n'
                 '2: sit0@NONE: <NOARP> mtu 1480 qdisc noop state DOWN mode DEFAULT group default qlen 1\\    link/sit 0.0.0.0 brd 0.0.0.0\n')
        self.module.validate_isolation(123, 456, ['sit0', 'lo'], links, ['', ''])
        unreachable = 'unreachable default dev lo  proto kernel  metric 4294967295  error -101 pref medium\n'
        self.module.validate_isolation(123, 456, ['sit0', 'lo'], links, ['', unreachable * 2])
        for changed_links, routes in ((links.replace('<NOARP>', '<NOARP,UP>'), ['', '']),
                                     (links.replace('sit0@NONE', 'sit0@eth0'), ['', '']),
                                     (links, ['default via 192.168.2.1', '']),
                                     (links, ['', unreachable.replace('unreachable ', '')]),
                                     (links, None), ('', ['', ''])):
            with self.subTest(links=changed_links, routes=routes), self.assertRaises(RuntimeError):
                self.module.validate_isolation(123, 456, ['sit0', 'lo'], changed_links, routes)

    def fake_ops(self, preexisting=()):
        ops = Mock()
        ops.preflight.return_value = {'modules': {name: {'loaded': name in preexisting} for name in self.module.MODULES}}
        ops.snapshot.return_value = {'containers': 'unchanged', 'firewall': 'unchanged'}
        ops.isolated_probe.return_value = {'state': 'passed', 'queue_bound': True, 'queue_unbound': True}
        ops.cleanup.return_value = {'state': 'restored', 'removed': []}
        ops.module_states.return_value = {name: name in preexisting for name in self.module.MODULES}
        return ops

    def test_without_explicit_approval_no_operations_are_invoked(self):
        ops = self.fake_ops()
        with self.assertRaises(PermissionError):
            self.module.execute(ops, approved=False)
        self.assertEqual(ops.method_calls, [])

    def test_success_cleans_up_only_modules_loaded_by_this_test(self):
        ops = self.fake_ops(preexisting=('nfnetlink_queue',))
        report = self.module.execute(ops, approved=True)
        ops.load.assert_called_once_with('xt_NFQUEUE')
        ops.cleanup.assert_called_once_with(['xt_NFQUEUE'])
        self.assertEqual(report['state'], 'passed')
        self.assertTrue(report['working_state_unchanged'])

    def test_bind_failure_still_cleans_up_and_checks_working_state(self):
        ops = self.fake_ops()
        ops.isolated_probe.side_effect = OSError(errno.EPERM, 'private details must not leak')
        report = self.module.execute(ops, approved=True)
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['failure']['errno'], errno.EPERM)
        self.assertNotIn('private details', str(report))
        ops.cleanup.assert_called_once_with(['nfnetlink_queue', 'xt_NFQUEUE'])
        self.assertEqual(ops.snapshot.call_count, 2)

    def test_partial_load_failure_cleans_up_only_confirmed_load(self):
        ops = self.fake_ops()
        ops.load.side_effect = [None, OSError(errno.ENOEXEC, 'bad module')]
        report = self.module.execute(ops, approved=True)
        self.assertEqual(report['state'], 'failed')
        ops.cleanup.assert_called_once_with(['nfnetlink_queue'])
        ops.isolated_probe.assert_not_called()

    def test_changed_working_state_or_incomplete_cleanup_is_not_pass(self):
        for changed, cleanup in ((True, 'restored'), (False, 'left_loaded')):
            ops = self.fake_ops()
            if changed:
                ops.snapshot.side_effect = [{'containers': 'before'}, {'containers': 'after'}]
            ops.cleanup.return_value = {'state': cleanup}
            report = self.module.execute(ops, approved=True)
            self.assertEqual(report['state'], 'incomplete')

    def test_timed_out_load_with_uncertain_module_state_is_not_reported_restored(self):
        ops = self.fake_ops()
        ops.load.side_effect = TimeoutError('load outcome is unknown')
        ops.module_states.return_value = {'nfnetlink_queue': True, 'xt_NFQUEUE': False}
        report = self.module.execute(ops, approved=True)
        self.assertEqual(report['state'], 'incomplete')
        self.assertFalse(report['module_state_restored'])
        ops.cleanup.assert_called_once_with([])  # Never force-remove a load of uncertain origin.

    def test_no_firewall_rule_or_routing_write_commands_in_source(self):
        source = SCRIPT.read_text(encoding='utf-8')
        for command in ('iptables-restore', 'ip6tables-restore', 'modprobe', 'docker restart', 'queue-bypass'):
            self.assertNotIn(command, source)

    def test_docker_template_handles_containers_without_healthcheck(self):
        self.assertNotIn('.State.Health', self.module.DOCKER_FORMAT)
        self.assertIn('{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}}', self.module.DOCKER_FORMAT)


if __name__ == '__main__':
    unittest.main()
