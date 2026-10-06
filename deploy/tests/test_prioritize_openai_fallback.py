import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('priority', Path(__file__).parents[1] / 'scripts' / 'prioritize-openai-fallback.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OpenAIPriorityTests(unittest.TestCase):
    def test_adds_only_conditional_openai_priority_before_broad_pinning(self):
        before = ('secret: untouched\nproxies: []\nrules:\n'
                  '  - RULE-SET,managed-direct,DIRECT\n'
                  '  - RULE-SET,managed-wg-imp,WG-IMP\n'
                  '  - RULE-SET,managed-fallback,VPS-FALLBACK\n'
                  '  - MATCH,VPS-FALLBACK\n')
        after = MODULE.prioritize_openai_fallback(before)
        expected = before.replace('  - RULE-SET,managed-wg-imp,WG-IMP\n',
            '  - AND,((GEOSITE,openai),(RULE-SET,managed-fallback)),VPS-FALLBACK\n'
            '  - RULE-SET,managed-wg-imp,WG-IMP\n')
        self.assertEqual(expected, after)
        self.assertEqual(after, MODULE.prioritize_openai_fallback(after))

    def test_rejects_missing_anchor_without_guessing(self):
        with self.assertRaises(ValueError):
            MODULE.prioritize_openai_fallback('rules:\n  - MATCH,DIRECT\n')

    def test_preserves_crlf(self):
        before = 'rules:\r\n  - RULE-SET,managed-wg-imp,WG-IMP\r\n  - RULE-SET,managed-fallback,VPS-FALLBACK\r\n'
        after = MODULE.prioritize_openai_fallback(before)
        self.assertEqual(4, after.count('\r\n'))
        self.assertNotIn('\n', after.replace('\r\n', ''))


if __name__ == '__main__':
    unittest.main()
