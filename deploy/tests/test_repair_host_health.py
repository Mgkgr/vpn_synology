import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'repair-host-health.py'


class TaskApi:
    def __init__(self, tasks=()):
        self.tasks = list(tasks)
        self.created = []

    def list_tasks(self):
        return list(self.tasks)

    def create_task(self, payload):
        self.created.append(payload)
        self.tasks.append(dict(payload, id=7))

    def get_task(self, task_id):
        return next(t for t in self.tasks if t['id'] == task_id)


class HealthTaskTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'health task installer has not been implemented')
        spec = importlib.util.spec_from_file_location('repair_health', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_creates_a_full_day_minute_schedule_without_modifying_other_tasks(self):
        other = {'id': 3, 'name': 'Existing backup', 'owner': 'root', 'extra': {'script': 'old command'}}
        api = TaskApi([other])
        self.assertEqual(self.module.ensure_health_task(api), 7)
        self.assertEqual(api.tasks[0], other)
        task = api.created[0]
        self.assertTrue(task['enable'])
        self.assertEqual(task['owner'], 'root')
        self.assertEqual(task['extra']['script'], '/bin/sh /usr/local/libexec/vpn-gateway/collect-host-health.sh')
        self.assertEqual(task['schedule']['repeat_min'], 1)
        self.assertEqual(task['schedule']['hour'], 0)
        self.assertEqual(task['schedule']['last_work_hour'], 23)
        self.assertEqual(task['schedule']['week_day'], '0,1,2,3,4,5,6')
        self.assertEqual(task['schedule']['repeat_date'], 1001)

    def test_repeated_install_does_not_create_duplicate_task(self):
        api = TaskApi()
        first = self.module.ensure_health_task(api)
        self.assertEqual(self.module.ensure_health_task(api), first)
        self.assertEqual(len(api.created), 1)

    def test_refuses_same_name_with_different_command(self):
        api = TaskApi([{'id': 3, 'name': 'VPN Dashboard — host health', 'owner': 'root', 'extra': {'script': 'some other command'}}])
        with self.assertRaisesRegex(RuntimeError, 'conflict'):
            self.module.ensure_health_task(api)
        self.assertEqual(api.created, [])

    def test_does_not_report_success_for_disabled_or_partial_day_schedule(self):
        for field, value in [('enable', False), ('last_work_hour', 0)]:
            api = TaskApi()
            self.module.ensure_health_task(api)
            if field == 'enable':
                api.tasks[0][field] = value
            else:
                api.tasks[0]['schedule'][field] = value
            with self.assertRaisesRegex(RuntimeError, 'schedule'):
                self.module.ensure_health_task(api)


if __name__ == '__main__':
    unittest.main()
