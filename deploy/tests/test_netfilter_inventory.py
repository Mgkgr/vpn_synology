import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'inspect-netfilter-prerequisites.py'


class NetfilterInventoryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('netfilter_inventory', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def test_missing_evidence_is_unknown_not_unsupported(self):
        result = self.module.collect(self.root)
        self.assertEqual(result['assessment'], 'unknown')
        self.assertEqual(result['registrations']['matches']['state'], 'unavailable')
        self.assertEqual(result['symbols']['state'], 'unavailable')

    def test_only_allowlisted_kernel_symbol_names_are_exported_without_addresses(self):
        self.write('proc/kallsyms', 'ffffffffa0010000 t owner_mt [xt_owner]\nffffffffa0020000 t connmark_tg [xt_connmark]\n0000000000000000 t unrelated_secret\n')
        result = self.module.collect(self.root)
        self.assertEqual(result['symbols']['owner'], ['owner_mt'])
        self.assertEqual(result['symbols']['connmark'], ['connmark_tg'])
        self.assertNotIn('ffffffff', str(result))
        self.assertNotIn('unrelated_secret', str(result))

    def test_registrations_and_module_files_are_not_claimed_to_be_a_packet_test(self):
        self.write('proc/net/ip_tables_matches', 'tcp\nowner\nconnmark\n')
        self.write('proc/net/ip_tables_targets', 'CONNMARK\nMARK\n')
        self.write('usr/lib/modules/nested/xt_owner.ko', 'fixture')
        self.write('usr/lib/modules/nested/xt_connmark.ko', 'fixture')
        self.write('usr/lib/modules/modules.builtin', 'kernel/net/netfilter/xt_owner.ko\n')
        result = self.module.collect(self.root)
        self.assertTrue(result['registrations']['matches']['owner'])
        self.assertTrue(result['registrations']['targets']['CONNMARK'])
        self.assertEqual(len(result['module_files']['/usr/lib/modules']['files']), 3)
        self.assertEqual(result['assessment'], 'unknown')
        self.assertFalse(result['packet_path_tested'])

    def test_dsm_parser_does_not_export_unrelated_settings(self):
        self.write('etc.defaults/VERSION', 'majorversion="7"\nminorversion="3"\nbuildnumber="12345"\nprivate_setting="example-sensitive-value"\n')
        result = self.module.collect(self.root)
        self.assertEqual(result['dsm']['values'], {'majorversion': '7', 'minorversion': '3', 'buildnumber': '12345'})
        self.assertNotIn('example-sensitive-value', str(result))


if __name__ == '__main__':
    unittest.main()
