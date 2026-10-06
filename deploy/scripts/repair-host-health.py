"""Install the missing DSM minute task; never restart any container.

Run as root on DSM. The scheduled collector is a root-owned copy, not a
user-writable script. Only this task and its non-secret health snapshot change.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

TASK_NAME = 'VPN Dashboard — host health'
COLLECTOR = Path('/usr/local/libexec/vpn-gateway/collect-host-health.sh')
COMMAND = '/bin/sh ' + COLLECTOR.as_posix()
SOURCE = Path('/volume1/docker/vpn-dashboard/deploy/scripts/collect-host-health.sh')
SOURCE_SHA256 = '286ba54c9e12d4c021ad1c63e359ad3156c31b6c9a66ae308f09249ed08262f2'
JOB = Path('/volume1/docker/vpn-gateway/.vless-maintenance')


def task_schedule():
    return dict(date_type=0, monthly_week='[]', hour=0, minute=0,
                repeat_hour=0, repeat_min=1, last_work_hour=23,
                week_day='0,1,2,3,4,5,6', repeat_date=1001)


def ensure_health_task(api):
    tasks = api.list_tasks()
    matched = [t for t in tasks if t.get('name') == TASK_NAME]
    if len(matched) > 1:
        raise RuntimeError('health task conflict: duplicate names')
    if not matched:
        # An older collector task must be inspected rather than duplicated.
        if any('collect-host-health.sh' in t.get('action', '') for t in tasks):
            raise RuntimeError('health task conflict: existing collector under another name')
        api.create_task(dict(name=TASK_NAME, owner='root', real_owner='root',
                             type='script', enable=True, schedule=task_schedule(),
                             extra=dict(script=COMMAND, notify_enable=False,
                                        notify_mail='', notify_if_error=False)))
        matched = [t for t in api.list_tasks() if t.get('name') == TASK_NAME]
    if len(matched) != 1:
        raise RuntimeError('health task creation was not confirmed')
    task_id = matched[0]['id']
    task = api.get_task(task_id)
    if task.get('owner') != 'root' or task.get('extra', {}).get('script') != COMMAND:
        raise RuntimeError('health task conflict: owner or command differs')
    expected = task_schedule()
    schedule = task.get('schedule', {})
    fields = ['date_type', 'hour', 'minute', 'repeat_hour', 'repeat_min',
              'last_work_hour', 'week_day', 'repeat_date']
    if not task.get('enable') or any(schedule.get(k) != expected[k] for k in fields):
        raise RuntimeError('health task schedule is disabled or incomplete')
    return task_id


class SynologyTasks:
    def request(self, method, version, root=False, **params):
        api = 'SYNO.Core.TaskScheduler.Root' if root else 'SYNO.Core.TaskScheduler'
        args = ['/usr/syno/bin/synowebapi', '--exec', 'api=' + api,
                'method=' + method, 'version=' + str(version)]
        for key, value in params.items():
            encoded = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            args.append(key + '=' + encoded)
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
        raw = result.stdout.decode('utf-8', errors='replace')
        try:
            response, _ = json.JSONDecoder().raw_decode(raw[raw.index('{'):])
        except ValueError:
            raise RuntimeError('DSM scheduler returned an invalid response') from None
        if not response.get('success'):
            raise RuntimeError('DSM scheduler failed: code=' + str(response.get('error', {}).get('code')))
        return response.get('data', {})

    def list_tasks(self):
        data = self.request('list', 3, offset=0, limit=500)
        tasks = data.get('tasks', [])
        if data.get('total', len(tasks)) > len(tasks):
            raise RuntimeError('scheduler list is incomplete')
        return tasks

    def create_task(self, payload):
        return self.request('create', 4, root=True, **payload)

    def get_task(self, task_id):
        return self.request('get', 4, id=task_id, real_owner='root')


def install_collector():
    body = SOURCE.read_bytes()
    if hashlib.sha256(body).hexdigest() != SOURCE_SHA256:
        raise RuntimeError('collector source changed: review it before installation')
    COLLECTOR.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    for directory in [COLLECTOR.parent, COLLECTOR.parent.parent]:
        s = directory.lstat()
        if directory.is_symlink() or s.st_uid != 0 or s.st_mode & 0o022:
            raise RuntimeError('collector directory is not root-controlled')
    if COLLECTOR.is_symlink():
        raise RuntimeError('collector path is a symlink')
    if COLLECTOR.exists() and COLLECTOR.read_bytes() != body:
        raise RuntimeError('installed collector differs: review before replacing')
    fd, tmp = tempfile.mkstemp(prefix='.collect-', dir=str(COLLECTOR.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o755)
        os.replace(tmp, str(COLLECTOR))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_snapshot():
    code = """
import json
from pathlib import Path
from app.routes import _read_host_health
r = _read_host_health(Path('/data/host-health.json'))
assert r.memory_total_bytes > 0 and r.volume_total_bytes > 0
assert len(r.containers) == 5
print(r.model_dump_json())
"""
    result = subprocess.run(['/usr/local/bin/docker', 'exec', '-i', 'vpn-dashboard', 'python', '-'],
                            input=code.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise RuntimeError('dashboard UID cannot validate the new host snapshot')
    return json.loads(result.stdout)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('run as root on DSM')
    install_collector()
    task_id = ensure_health_task(SynologyTasks())
    result = subprocess.run(['/bin/sh', str(COLLECTOR)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if result.returncode:
        raise RuntimeError('collector failed; exit=' + str(result.returncode))
    snapshot = validate_snapshot()
    report = dict(task_id=task_id, schedule='every minute, 00:00-23:59 daily', snapshot=snapshot)
    fd, tmp = tempfile.mkstemp(prefix='.health-repair-', dir=str(JOB))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2)
    os.chown(tmp, 1026, 100)
    os.chmod(tmp, 0o600)
    os.replace(tmp, str(JOB / 'host-health-repair.json'))
    print('HOST_HEALTH_REPAIR=success|TASK_ID=' + str(task_id), flush=True)


if __name__ == '__main__':
    main()
