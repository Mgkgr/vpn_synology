from dataclasses import asdict
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance.store import JobStore
from maintenance.strategy_state import StrategyStore
from maintenance.strategy_catalog import StrategyRequest
from test_strategy_state import observation, IDENTITY, CONTEXT


class Clock:
    def __init__(self): self.now = 1000
    def time(self): return self.now
    def sleep(self, seconds): self.now += seconds


class Adapter:
    def __init__(self, clock):
        self.clock, self.calls, self.applied, self.restored = clock, [], [], []
        self.ready, self.recovered, self.verify_ok = True, False, True
        self.on_probe = None
    def capabilities(self):
        return dict(probe_ready=self.ready, apply_ready=self.ready,
                    blockers=[] if self.ready else ['runtime_unverified'])
    def comparison_context(self, sid):
        return dict(identity=IDENTITY, context_id=CONTEXT, infrastructure_ok=True, checked_at=self.clock.time())
    def probe(self, sid, strategy, context, timeout):
        self.calls.append((sid,strategy,timeout))
        if self.on_probe: self.on_probe()
        verdict = 'success' if strategy != 'tlsrec-sni' or self.recovered else 'transport_error'
        return observation(self.clock.time(),verdict)
    def backup(self, selections, expected_revision): return dict(encrypted=True, token='b'*64)
    def apply(self, selections, expected_revision, token): self.applied.append(dict(selections))
    def rollback(self, token, expected_revision): self.restored.append(token); return True
    def verify(self, sid, context, timeout):
        return observation(self.clock.time(),'success' if self.verify_ok else 'transport_error')


class StrategyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'maintenance/strategy_runner.py').exists(), 'runner missing')
        self.m = importlib.import_module('maintenance.strategy_runner')
        self.scheduling = importlib.import_module('maintenance.strategy_schedule')
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.jobs = JobStore(Path(self.temp.name)/'private/state.sqlite3')
        self.state = StrategyStore(self.jobs); self.clock = Clock(); self.adapter = Adapter(self.clock)
        self.state.register(IDENTITY,{sid:'tlsrec-sni' for sid in ('youtube','discord','telegram','instagram')},1000)
        self.state.configure('youtube',dict(enabled=True,mode='auto',interval_minutes=30,daily_enabled=True),
                             self.state.snapshot(1000)['revision'],'owner',1000)
        self.runner = self.m.StrategyRunner(self.state,self.adapter,clock=self.clock.time,sleep=self.clock.sleep)

    def submit(self, action='strategy_check',settings=None):
        request = StrategyRequest(action,'youtube',self.state.snapshot(self.clock.time())['revision'],settings or {})
        job = self.runner.submit('1'*32,request,'owner')
        self.runner.run(job['job_id'])
        return self.runner.job(job['job_id'])

    def test_manual_check_and_full_tune_never_apply_or_drive_failures(self):
        job = self.submit('strategy_tune')
        self.assertEqual(job['phase'],'completed')
        self.assertEqual(len(job['strategy']['results']),24)
        self.assertLessEqual(len(self.adapter.calls),60)
        self.assertFalse(self.adapter.applied)
        self.assertEqual(self.state.decision('youtube',self.clock.time()),'no_data')

    def test_automatic_switch_needs_three_failures_then_three_successes(self):
        for at in (1000,1030,1060):
            self.clock.now = at
            job_id = self.runner.enqueue_auto('youtube','confirmation')
            self.runner.run(job_id)
        self.assertEqual(self.adapter.applied, [{'youtube':'disorder-1','discord':'tlsrec-sni','telegram':'tlsrec-sni','instagram':'tlsrec-sni'}])
        self.assertEqual(self.state.snapshot(self.clock.time())['services'][0]['strategy_id'],'disorder-1')
        self.assertEqual(self.runner.job(job_id)['phase'],'completed')
        self.runner.run(job_id)
        self.assertEqual(len(self.adapter.applied),1)

    def test_recovered_current_and_revoked_policy_abort_apply(self):
        for at in (940,970,1000): self.state.observe('youtube','tlsrec-sni',observation(at),'scheduled',at)
        def recover():
            if sum(s=='disorder-1' for _,s,_ in self.adapter.calls) == 3: self.adapter.recovered=True
        self.adapter.on_probe = recover
        job = self.runner.enqueue_auto('youtube','confirmation'); self.runner.run(job)
        self.assertFalse(self.adapter.applied)
        self.assertEqual(self.state.decision('youtube',self.clock.time()),'healthy')

    def test_post_apply_failure_restores_configuration_without_claiming_health(self):
        self.adapter.verify_ok = False
        job = self.submit('strategy_apply',dict(strategy_id='disorder-1',mode='pinned'))
        self.assertEqual(job['phase'],'failed')
        self.assertEqual(job['error_code'],'strategy_verification_failed')
        self.assertEqual(self.adapter.restored,['b'*64])
        self.assertEqual(self.state.snapshot(self.clock.time())['services'][0]['strategy_id'],'tlsrec-sni')

    def test_identity_or_policy_drift_and_cancel_prevent_mutation(self):
        request = StrategyRequest('strategy_apply','youtube',self.state.snapshot(1000)['revision'],dict(strategy_id='disorder-1',mode='auto'))
        self.runner.submit('2'*32,request,'owner'); self.jobs.cancel('2'*32)
        self.runner.run('2'*32); self.assertFalse(self.adapter.calls)
        def revoke():
            self.state.configure('youtube',dict(enabled=False,mode='pinned',interval_minutes=30,daily_enabled=True),self.state.snapshot(1000)['revision'],'owner',1000)
            self.adapter.on_probe = None
        self.adapter.on_probe = revoke
        job = self.submit('strategy_apply',dict(strategy_id='disorder-1',mode='auto'))
        self.assertEqual(job['phase'],'failed'); self.assertFalse(self.adapter.applied)

    def test_unready_service_never_queues_and_get_never_probes(self):
        from maintenance.service import MaintenanceService
        self.adapter.ready=False
        service = MaintenanceService(self.jobs,strategies=self.runner)
        request=StrategyRequest('strategy_check','youtube',self.state.snapshot(1000)['revision'],{})
        frame=lambda method,params: json.dumps(dict(version=1,method=method,params=params)).encode()+b'\n'
        result=service.handle(frame('submit',dict(job_id='3'*32,actor='owner',request=asdict(request))),10001)
        self.assertEqual(result,dict(ok=False,error='not_configured'))
        self.assertFalse(self.jobs.jobs())
        result=service.handle(frame('strategy_snapshot',{}),10001)
        self.assertFalse(result['result']['capabilities']['can_check'])
        self.assertFalse(self.adapter.calls)

    def test_schedule_retries_and_daily_coalesce_without_bypassing_busy(self):
        schedule = self.scheduling.StrategySchedule(self.runner)
        self.clock.now = 17800  # 09:56:40 local, after daily 05:30.
        schedule.tick(self.clock.time())
        pending = schedule.pending()
        self.assertLessEqual(len(pending),4)
        active = [job for job in self.jobs.jobs() if job.phase=='queued']
        self.assertEqual(len(active),1)
        self.assertEqual(len(pending),0)  # The single pending item was dispatched.
        # A second tick neither replaces the queued job nor produces a backlog.
        schedule.tick(self.clock.time()+86400*10)
        self.assertEqual(len(self.jobs.jobs()),1)
        self.assertLessEqual(len(schedule.pending()),4)
        self.assertEqual(self.scheduling.daily_slot(1799), '1969-12-31')
        self.assertEqual(self.scheduling.daily_slot(1800), '1970-01-01')

    def test_new_failure_is_confirmed_after_30_seconds_not_regular_interval(self):
        self.state.observe('youtube','tlsrec-sni',observation(990,'success'),'scheduled',990)
        schedule=self.scheduling.StrategySchedule(self.runner)
        job=schedule.tick(1000); self.runner.run(job)
        self.clock.now=1029; self.assertIsNone(schedule.tick(1029))
        self.clock.now=1030; job=schedule.tick(1030)
        self.assertIsNotNone(job)
        self.runner.run(job)
        self.assertEqual(self.state.snapshot(1030)['services'][0]['failure_count'],2)

    def test_search_retry_is_limited_even_after_failed_candidates(self):
        self.state.mark_search('youtube',1000)
        schedule=self.scheduling.StrategySchedule(self.runner)
        self.clock.now=1500
        self.assertIsNone(schedule.tick(1500))

    def test_policy_can_be_revoked_when_runtime_unavailable(self):
        self.adapter.ready=False
        result=self.submit('strategy_configure',dict(enabled=False,mode='pinned',interval_minutes=30,daily_enabled=True))
        self.assertEqual(result['phase'],'completed')
        self.assertEqual(self.state.decision('youtube',1000),'disabled')

    def test_disabled_schedule_does_not_persist_a_write_on_every_idle_tick(self):
        from contextlib import closing
        self.state.configure('youtube',dict(enabled=False,mode='pinned',interval_minutes=30,daily_enabled=True),
                             self.state.snapshot(1000)['revision'],'owner',1000)
        schedule=self.scheduling.StrategySchedule(self.runner)
        for now in range(1000,1010): self.assertIsNone(schedule.tick(now))
        with closing(self.jobs._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM strategy_schedules').fetchone()[0],0)

    def test_dns_or_age_change_during_backup_prevents_apply(self):
        original=self.adapter.backup
        def slow_backup(selections,revision):
            self.clock.now+=181
            return original(selections,revision)
        self.adapter.backup=slow_backup
        job=self.submit('strategy_apply',dict(strategy_id='disorder-1',mode='pinned'))
        self.assertEqual(job['phase'],'failed')
        self.assertFalse(self.adapter.applied)

    def test_worker_restart_never_replays_uncertain_apply(self):
        request=StrategyRequest('strategy_apply','youtube',self.state.snapshot(1000)['revision'],dict(strategy_id='disorder-1',mode='pinned'))
        self.runner.submit('4'*32,request,'owner')
        self.jobs.claim_job('4'*32,now=1000)
        self.jobs.set_phase('4'*32,'apply',now=1000)
        self.jobs.begin_effect('4'*32,'strategy.apply',now=1000)
        self.jobs.recover_after_restart(now=1001)
        self.runner.run('4'*32)
        self.assertEqual(self.jobs.get_job('4'*32).phase,'needs_reconcile')
        self.assertFalse(self.adapter.applied)

    def test_repeated_cached_candidate_result_is_not_three_independent_successes(self):
        self.adapter.probe=lambda *args,**kwargs: observation(1000,'success')
        job=self.submit('strategy_apply',dict(strategy_id='disorder-1',mode='auto'))
        self.assertEqual(job['phase'],'failed')
        self.assertFalse(self.adapter.applied)

    def test_context_failure_breaks_existing_automatic_failure_streak(self):
        for at in (940,970): self.state.observe('youtube','tlsrec-sni',observation(at),'scheduled',at)
        self.adapter.comparison_context=lambda sid: dict(identity=IDENTITY,context_id=CONTEXT,infrastructure_ok=False,checked_at=self.clock.now)
        job=self.runner.enqueue_auto('youtube','confirmation'); self.runner.run(job)
        value=self.state.snapshot(1000)['services'][0]
        self.assertEqual(value['failure_count'],0)
        self.assertEqual(value['state'],'unknown')
        self.assertFalse(self.adapter.applied)


if __name__=='__main__': unittest.main()
