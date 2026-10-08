"""Persistent coalescing schedule; at most one pending entry per fixed service."""

from datetime import datetime, timedelta, timezone
import json

from .protocol import canonical
from .store import JobBusy, JobConflict
from .strategy_runner import StrategyUnavailable


def daily_slot(now):
    # Yekaterinburg has fixed UTC+5. Subtract 05:30 to label the last due run.
    return (datetime.fromtimestamp(now,timezone(timedelta(hours=5)))-timedelta(hours=5,minutes=30)).date().isoformat()


class StrategySchedule:
    def __init__(self, runner):
        self.runner, self.jobs = runner, runner.jobs
        self._pruned_day=None

    def pending(self):
        with self.jobs._write() as db:
            return [dict(service_id=row['service'],**json.loads(row['value']))
                    for row in db.execute('SELECT * FROM strategy_schedules') if json.loads(row['value']).get('pending')]

    def tick(self, now):
        day=daily_slot(now)
        if self._pruned_day!=day:
            self.runner.state.prune(now)
            self._pruned_day=day
        snapshot=self.runner.state.snapshot(now)
        with self.jobs._write() as db:
            for policy in snapshot['services']:
                sid=policy['service_id']
                row=db.execute('SELECT value FROM strategy_schedules WHERE service=?',(sid,)).fetchone()
                if not policy['enabled']:
                    if row:
                        value=json.loads(row[0])
                        if value.get('pending') is not None:
                            value['pending']=None
                            db.execute('UPDATE strategy_schedules SET value=? WHERE service=?',(canonical(value).decode(),sid))
                    continue
                value=json.loads(row[0]) if row else dict(last_tick=now,next_check=now,daily=None,pending=None,search_at=None)
                if now<value['last_tick']:
                    # Do not turn wall-clock reversal into a burst of probes.
                    value.update(next_check=now+policy['interval_minutes']*60,pending=None)
                elif policy['last_search_at'] is not None and now-policy['last_search_at']<900:
                    value['pending']=None
                elif policy['state']=='suspect' and policy['mode']=='auto':
                    last=policy['last_automatic']['checked_at']
                    value['pending']='confirmation' if now-last>=30 else None
                elif now>=value['next_check']:
                    value['pending']='scheduled'
                elif policy['daily_enabled'] and value['daily']!=daily_slot(now) and value['pending'] not in ('scheduled','confirmation'):
                    value['pending']='daily'
                value['last_tick']=now
                db.execute('INSERT OR REPLACE INTO strategy_schedules VALUES(?,?)',(sid,canonical(value).decode()))
        if self.jobs.lock_state(now)['state']!='free' or not self.runner.capabilities()['can_check']:
            return None
        choices=sorted(self.pending(),key=lambda row:({'confirmation':0,'scheduled':1,'daily':2}[row['pending']],row['service_id']))
        for entry in choices[:1]:
            try:
                job_id=self.runner.enqueue_auto(entry['service_id'],entry['pending'])
            except (JobBusy,JobConflict,StrategyUnavailable):
                return None
            with self.jobs._write() as db:
                value=json.loads(db.execute('SELECT value FROM strategy_schedules WHERE service=?',(entry['service_id'],)).fetchone()[0])
                policy=next(row for row in snapshot['services'] if row['service_id']==entry['service_id'])
                if entry['pending']=='daily': value['daily']=daily_slot(now)
                # One initial probe and two spaced confirmations; search retry <= once/15m.
                retry=30 if policy['state'] in ('suspect','no_data') and policy['mode']=='auto' else policy['interval_minutes']*60
                if policy['state'] in ('search','cooldown','rate_limit'): retry=max(retry,900)
                value.update(pending=None,next_check=now+retry)
                db.execute('UPDATE strategy_schedules SET value=? WHERE service=?',(canonical(value).decode(),entry['service_id']))
            return job_id
        return None
