import importlib.util
from pathlib import Path
import unittest


class StrategyBatchTests(unittest.TestCase):
    def module(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/confirm-antidpi-services.py'
        self.assertTrue(path.is_file(), 'fixed service confirmation batch is missing')
        spec = importlib.util.spec_from_file_location('strategy_batch', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_negative_site_result_continues_without_repeated_sudo(self):
        m = self.module()
        seen = []
        def execute(args):
            seen.append(args)
            return 2 if args[-1] == 'discord.com' else 0
        code = m.run_batch(execute)
        self.assertEqual(code, 2)
        self.assertEqual([args[-1] for args in seen], [
            'www.youtube.com','discord.com','web.telegram.org','www.instagram.com','www.wikipedia.org'])
        for args in seen:
            self.assertNotIn('sudo', args)
            self.assertEqual(args[-2], 'confirm')

    def test_runtime_failure_stops_following_tests(self):
        m = self.module()
        seen = []
        def execute(args):
            seen.append(args)
            return 1
        self.assertEqual(m.run_batch(execute), 1)
        self.assertEqual(len(seen), 1)


if __name__ == '__main__':
    unittest.main()
