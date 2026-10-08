import importlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from maintenance.store import JobStore
from maintenance.strategy_state import StrategyStore
from maintenance.strategy_catalog import StrategyRequest
from maintenance.strategy_runner import StrategyRunner
from test_strategy_runner import Adapter,Clock,IDENTITY


class WorkerLoopTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'maintenance/worker.py').exists(),'host worker missing')
        self.m=importlib.import_module('maintenance.worker')
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.jobs=JobStore(Path(self.temp.name)/'private/jobs.sqlite3')
        self.state=StrategyStore(self.jobs); self.clock=Clock(); self.adapter=Adapter(self.clock)
        self.state.register(IDENTITY,{sid:'tlsrec-sni' for sid in ('youtube','discord','telegram','instagram')},1000)
        self.runner=StrategyRunner(self.state,self.adapter,clock=self.clock.time,sleep=self.clock.sleep)

    def test_manual_queued_job_runs_once_without_waiting_for_schedule(self):
        request=StrategyRequest('strategy_check','youtube',self.state.snapshot(1000)['revision'],{})
        self.runner.submit('1'*32,request,'owner')
        loop=self.m.JobLoop(self.runner)
        loop.tick(1000)
        self.assertEqual(self.jobs.get_job('1'*32).phase,'completed')
        calls=len(self.adapter.calls); loop.tick(1001)
        self.assertEqual(len(self.adapter.calls),calls)

    def test_uncertain_job_is_never_replayed_after_restart(self):
        request=StrategyRequest('strategy_apply','youtube',self.state.snapshot(1000)['revision'],dict(strategy_id='split-1',mode='pinned'))
        self.runner.submit('1'*32,request,'owner'); self.jobs.claim_job('1'*32,now=1000)
        self.jobs.set_phase('1'*32,'apply',now=1000); self.jobs.begin_effect('1'*32,'strategy.apply',now=1000)
        self.jobs.recover_after_restart(now=1001)
        self.m.JobLoop(self.runner).tick(1001)
        self.assertEqual(self.jobs.get_job('1'*32).phase,'needs_reconcile')
        self.assertEqual(self.adapter.applied,[])


if __name__=='__main__': unittest.main()
