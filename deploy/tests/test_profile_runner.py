from dataclasses import asdict
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance.profile_catalog import ProfileRequest
from maintenance.profile_state import ProfileDrafts
from maintenance.protocol import canonical
from maintenance.store import JobStore, JobConflict
from maintenance.service import MaintenanceService
from test_profile_links import VLESS


class Clock:
    now = 1000
    def time(self): return self.now


class Host:
    def __init__(self, clock):
        self.clock=clock; self.rev='a'*64; self.events=[]
        self.good=True; self.verified=True; self.restored=True; self.backed=True; self.uncertain=False
        self.after_backup=None
    def revision(self): return self.rev
    def ready(self): return True
    def current(self): return []
    def preflight(self,profile,job_id,report):
        self.events.append('probe')
        rows=[]
        for _ in range(3):
            for target in ('cloudflare','google','github'):
                row=dict(target=target,ok=self.good,latency_ms=10 if self.good else None,
                         http_status=(200 if target=='github' else 204) if self.good else None,
                         reason=None if self.good else 'timeout',checked_at=self.clock.time())
                report(row); rows.append(row)
        return dict(results=rows,endpoint_ip='1.1.1.1')
    def backup(self,profile,endpoint_ip,revision,job_id):
        self.events.append('backup')
        if self.after_backup: self.after_backup()
        return dict(encrypted=self.backed,token='b'*64)
    def apply(self,token,revision):
        self.events.append('apply')
        if self.uncertain: raise TimeoutError('private payload must not leak')
    def verify(self,token,report):
        self.events.append('verify'); return self.verified
    def persist(self,token,revision):
        self.events.append('persist'); self.rev='c'*64
    def rollback(self,token,revision):
        self.events.append('rollback'); return self.restored


class ProfileRunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).parents[1]/'maintenance/profile_runner.py').exists(),'profile runner missing')
        m=importlib.import_module('maintenance.profile_runner')
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.jobs=JobStore(Path(self.temp.name)/'private/jobs.sqlite3')
        self.drafts=ProfileDrafts(Path(self.temp.name)/'drafts')
        self.clock=Clock(); self.host=Host(self.clock)
        self.runner=m.ProfileRunner(self.jobs,self.drafts,self.host,clock=self.clock.time)
        self.draft=self.runner.stage(VLESS,'owner')

    def request(self,action): return ProfileRequest(action,self.draft['draft_id'],'a'*64)
    def run_job(self,action,job_id):
        self.runner.submit(job_id,self.request(action),'owner')
        return self.runner.run(job_id)
    def check(self): return self.run_job('profile_check','1'*32)

    def test_check_produces_evidence_without_production_mutation(self):
        result=self.check()
        self.assertEqual(result['phase'],'completed'); self.assertEqual(self.host.events,['probe'])
        self.assertEqual(len(result['profile']['results']),9)
        self.assertTrue(self.runner.snapshot('owner')['drafts'][0]['check_passed'])
        self.assertNotIn('11111111-2222-4333-8444-555555555555',json.dumps(result))

    def test_failed_stale_or_wrong_revision_never_applies(self):
        with self.assertRaises(JobConflict): self.runner.submit('2'*32,self.request('profile_apply'),'owner')
        self.host.good=False; self.assertEqual(self.check()['phase'],'failed')
        with self.assertRaises(JobConflict): self.runner.submit('2'*32,self.request('profile_apply'),'owner')
        self.host.good=True; self.run_job('profile_check','3'*32)
        self.clock.now+=301
        with self.assertRaises(JobConflict): self.runner.submit('2'*32,self.request('profile_apply'),'owner')
        self.clock.now=1001; self.host.rev='d'*64
        with self.assertRaises(JobConflict): self.runner.submit('2'*32,self.request('profile_apply'),'owner')
        self.assertNotIn('apply',self.host.events)

    def test_apply_backup_verify_persist_once_even_if_response_lost(self):
        self.check(); result=self.run_job('profile_apply','2'*32)
        self.assertEqual(result['phase'],'completed')
        self.assertEqual(self.host.events,['probe','backup','apply','verify','persist'])
        self.assertEqual(self.runner.snapshot('owner')['drafts'],[])
        self.assertEqual(self.runner.submit('2'*32,self.request('profile_apply'),'owner')['phase'],'completed')
        self.runner.run('2'*32)
        self.assertEqual(self.host.events.count('apply'),1)

    def test_backup_failure_and_evidence_aging_during_backup_prevent_apply(self):
        self.check(); self.host.backed=False
        self.assertEqual(self.run_job('profile_apply','2'*32)['phase'],'failed')
        self.host.backed=True; self.host.after_backup=lambda:setattr(self.clock,'now',1400)
        self.assertEqual(self.run_job('profile_apply','3'*32)['phase'],'failed')
        self.assertNotIn('apply',self.host.events)

    def test_failed_verify_rolls_back_but_lost_apply_ack_retains_lock(self):
        self.check(); self.host.verified=False
        result=self.run_job('profile_apply','2'*32)
        self.assertEqual(result['phase'],'failed'); self.assertEqual(result['error_code'],'profile_reverted')
        self.assertNotIn('persist',self.host.events); self.assertEqual(self.jobs.lock_state(1000)['state'],'free')
        self.host.uncertain=True
        result=self.run_job('profile_apply','3'*32)
        self.assertEqual(result['phase'],'needs_reconcile'); self.assertEqual(self.host.events.count('rollback'),1)
        self.assertEqual(self.jobs.lock_state(1000)['state'],'needs_reconcile')
        self.assertNotIn('private payload',json.dumps(result))

    def test_rollback_failure_restart_and_cancel_do_not_replay(self):
        self.check(); self.host.verified=False; self.host.restored=False
        self.assertEqual(self.run_job('profile_apply','2'*32)['phase'],'needs_reconcile')
        self.jobs.recover_after_restart(now=1001); self.runner.run('2'*32)
        self.assertEqual(self.host.events.count('apply'),1)

    def test_ipc_stage_snapshot_and_jobs_never_probe_on_get(self):
        service=MaintenanceService(self.jobs,profiles=self.runner)
        frame=lambda method,params:canonical(dict(version=1,method=method,params=params))+b'\n'
        value=service.handle(frame('profile_snapshot',dict(actor='owner')),10001)
        self.assertTrue(value['ok']); self.assertEqual(self.host.events,[])
        job=service.handle(frame('submit',dict(job_id='1'*32,actor='owner',request=asdict(self.request('profile_check')))),10001)
        self.assertTrue(job['ok']); self.runner.run('1'*32)
        value=service.handle(frame('job',dict(job_id='1'*32)),10001)
        self.assertEqual(value['result']['phase'],'completed')
        self.assertEqual(len(value['result']['profile']['results']),9)

    def test_queued_profile_is_run_by_existing_worker_without_restarting_other_runners(self):
        from maintenance.worker import JobLoop
        from maintenance.strategy_runner import StrategyRunner
        from maintenance.strategy_state import StrategyStore
        strategy=StrategyRunner(StrategyStore(self.jobs))
        self.runner.submit('1'*32,self.request('profile_check'),'owner')
        loop=JobLoop(strategy,profiles=self.runner)
        loop.tick(1000)
        self.assertEqual(self.jobs.get_job('1'*32).phase,'completed')
        self.assertEqual(self.host.events,['probe'])

    def test_queued_cancel_and_restart_recovery_never_apply(self):
        self.check()
        self.runner.submit('2'*32,self.request('profile_apply'),'owner'); self.jobs.cancel('2'*32)
        self.runner.run('2'*32)
        self.runner.submit('3'*32,self.request('profile_apply'),'owner')
        self.jobs.claim_job('3'*32,now=1000); self.jobs.set_phase('3'*32,'apply',now=1000)
        self.jobs.begin_effect('3'*32,'profile.apply',now=1000); self.jobs.recover_after_restart(now=1001)
        self.runner.run('3'*32)
        self.assertNotIn('apply',self.host.events)
        self.assertEqual(self.jobs.get_job('3'*32).phase,'needs_reconcile')

    def test_host_assembly_is_disabled_until_root_installs_profile_contract(self):
        from unittest.mock import patch
        from maintenance import host_service
        self.assertTrue(hasattr(host_service,'assemble_profiles'),'host profile assembly missing')
        with patch.object(host_service,'PRIVATE',Path(self.temp.name)/'private'):
            runner=host_service.assemble_profiles(self.jobs)
            service=MaintenanceService(self.jobs,profiles=runner)
            raw=canonical(dict(version=1,method='profile_snapshot',params=dict(actor='owner')))+b'\n'
            self.assertEqual(service.handle(raw,10001),dict(ok=False,error='not_configured'))

    def test_unconfirmed_probe_cleanup_keeps_lock_and_never_marks_check_passed(self):
        from maintenance.profile_host import ProbeCleanupUnconfirmed
        def failed(*args): raise ProbeCleanupUnconfirmed('probe_cleanup_unconfirmed')
        self.host.preflight=failed
        result=self.check()
        self.assertEqual(result['phase'],'needs_reconcile')
        self.assertEqual(self.jobs.lock_state(1000)['state'],'needs_reconcile')
        self.assertFalse(self.runner.snapshot('owner')['drafts'][0]['check_passed'])


if __name__=='__main__': unittest.main()
