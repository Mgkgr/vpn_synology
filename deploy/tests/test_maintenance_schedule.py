import importlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.m = importlib.import_module('maintenance.schedule')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private/schedule.sqlite3'
        self.time = ['2026-09-28', 4]
        self.calls = []
        self.scheduler = self.m.ReleaseSchedule(self.path, lambda: self.calls.append('check'), local_calendar=lambda now: tuple(self.time))

    def test_daily_05_and_single_catchup_across_process_restart(self):
        self.assertFalse(self.scheduler.tick(1000)['queued'])
        self.time[1] = 11
        self.assertTrue(self.scheduler.tick(1100)['queued'])
        other = self.m.ReleaseSchedule(self.path, lambda: self.calls.append('check'), local_calendar=lambda now: tuple(self.time))
        self.assertFalse(other.tick(1200)['queued'])
        self.time[:] = ['2026-09-29', 5]
        self.assertTrue(other.tick(1300)['queued'])
        self.assertEqual(self.calls, ['check', 'check'])

    def test_manual_cooldown_and_clock_reversal_never_spam(self):
        self.assertTrue(self.scheduler.manual(1000)['queued'])
        self.assertFalse(self.scheduler.manual(1059)['queued'])
        self.assertTrue(self.scheduler.manual(1060)['queued'])
        result = self.scheduler.manual(900)
        self.assertFalse(result['queued'])
        self.assertEqual(result['reason'], 'clock_anomaly')
        self.assertEqual(len(self.calls), 2)

    def test_schedule_only_checks_and_does_not_retry_callback_ambiguity(self):
        def lost():
            self.calls.append('lost')
            raise TimeoutError('private URL must not appear')
        scheduler = self.m.ReleaseSchedule(self.path, lost, local_calendar=lambda now: ('2026-09-28', 5))
        result = scheduler.tick(1000)
        self.assertEqual(result, {'queued': False, 'reason': 'check_queue_unknown'})
        self.assertFalse(scheduler.tick(1300)['queued'])
        self.assertEqual(self.calls, ['lost'])


if __name__ == '__main__': unittest.main()
