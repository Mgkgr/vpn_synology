import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'repair-udp-receive-buffers.py'


class BufferTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'UDP receive-buffer repair has not been implemented')
        spec = importlib.util.spec_from_file_location('buffers', SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def prepare(self, default=212992, maximum=212992):
        proc = self.root / 'core'
        proc.mkdir()
        (proc / 'rmem_default').write_text(str(default), encoding='utf-8')
        (proc / 'rmem_max').write_text(str(maximum), encoding='utf-8')
        (proc / 'wmem_default').write_text('212992', encoding='utf-8')
        return proc

    def test_raises_only_receive_buffers_and_persists_them(self):
        proc = self.prepare()
        config = self.root / 'vpn.conf'
        result = self.m.apply_buffers(proc, config, self.root / 'before.json')
        self.assertEqual(result['rmem_default'], 4194304)
        self.assertEqual((proc / 'rmem_default').read_text().strip(), '4194304')
        self.assertEqual((proc / 'rmem_max').read_text().strip(), '4194304')
        self.assertEqual((proc / 'wmem_default').read_text(), '212992')
        self.assertIn('net.core.rmem_default = 4194304', config.read_text())

    def test_does_not_reduce_preexisting_larger_buffers(self):
        proc = self.prepare(8388608, 16777216)
        result = self.m.apply_buffers(proc, self.root / 'vpn.conf', self.root / 'before.json')
        self.assertEqual(result['rmem_default'], 8388608)
        self.assertEqual(result['rmem_max'], 16777216)

    def test_refuses_overwriting_unknown_persistent_configuration(self):
        proc = self.prepare()
        config = self.root / 'vpn.conf'
        config.write_text('net.ipv4.ip_forward = 0\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'unmanaged'):
            self.m.apply_buffers(proc, config, self.root / 'before.json')
        self.assertEqual((proc / 'rmem_default').read_text(), '212992')
        self.assertEqual(config.read_text(), 'net.ipv4.ip_forward = 0\n')

    def test_restore_uses_original_values_even_after_repeated_apply(self):
        proc = self.prepare()
        config = self.root / 'vpn.conf'
        backup = self.root / 'before.json'
        self.m.apply_buffers(proc, config, backup)
        self.m.apply_buffers(proc, config, backup)
        self.m.restore_buffers(proc, config, backup)
        self.assertEqual((proc / 'rmem_default').read_text().strip(), '212992')
        self.assertEqual((proc / 'rmem_max').read_text().strip(), '212992')
        self.assertFalse(config.exists())

    def test_persistence_failure_does_not_change_runtime(self):
        proc = self.prepare()
        parent = self.root / 'not-directory'
        parent.write_text('file', encoding='utf-8')
        with self.assertRaises(OSError):
            self.m.apply_buffers(proc, parent / 'vpn.conf', self.root / 'before.json')
        self.assertEqual((proc / 'rmem_default').read_text(), '212992')

    def test_partial_runtime_failure_restores_both_values_and_configuration(self):
        proc = self.prepare()
        config = self.root / 'vpn.conf'
        original = self.m.write_values
        first = True

        def fail_once(directory, values):
            nonlocal first
            if first:
                first = False
                (directory / 'rmem_max').write_text('4194304', encoding='ascii')
                raise OSError('simulated kernel write failure')
            original(directory, values)

        with patch.object(self.m, 'write_values', fail_once):
            with self.assertRaises(OSError):
                self.m.apply_buffers(proc, config, self.root / 'before.json')
        self.assertEqual((proc / 'rmem_default').read_text().strip(), '212992')
        self.assertEqual((proc / 'rmem_max').read_text().strip(), '212992')
        self.assertFalse(config.exists())


if __name__ == '__main__':
    unittest.main()
