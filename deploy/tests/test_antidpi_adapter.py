import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from maintenance.store import JobStore
from maintenance.strategy_state import StrategyStore
from maintenance.antidpi_host import atomic_private
from test_antidpi_production import config,pins


class Transport:
    def __init__(self): self.requests=[]; self.validated=[]; self.ok=True
    def healthy(self,config): return self.ok
    def probe(self,host,address,strategy,mode):
        self.requests.append((host,address,strategy,mode))
        return dict(verdict='success',reason='verified',latency_ms=80,http_status=200)
    def validate(self,value,lease): self.validated.append(value)
    def wait_applied(self,value): return True


class Archive:
    def __init__(self): self.copies=[]
    def create(self,before): self.copies.append(before); return 'e'*64


class AdapterTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[1]/'maintenance/antidpi_adapter.py'
        self.assertTrue(path.exists(),'concrete host adapter missing')
        self.m=importlib.import_module('maintenance.antidpi_adapter')
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name); self.runtime=root/'runtime'; self.runtime.mkdir()
        self.private=root/'private'; self.private.mkdir()
        self.jobs=JobStore(self.private/'jobs.sqlite3'); self.state=StrategyStore(self.jobs)
        self.state.register('a'*64,config()['selections'],1000)
        atomic_private(self.runtime/'selection.json',json.dumps(config()).encode())
        atomic_private(self.runtime/'dns.json',json.dumps(pins()).encode())
        checks={key:True for key in ('runtime','dns_renewal','namespace_recovery','service_isolation','production_input','backup')}
        atomic_private(self.private/'antidpi-acceptance.json',json.dumps(dict(identity='a'*64,verified_at=1000,checks=checks)).encode())
        self.transport=Transport(); self.archive=Archive(); self.now=1000
        self.adapter=self.m.ProductionAdapter(self.state,self.private,self.runtime,self.transport,self.archive,'a'*64,clock=lambda:self.now)
        self.adapter.refresh()

    def test_current_probe_uses_real_input_candidate_uses_isolated_process(self):
        ctx=self.adapter.comparison_context('youtube')
        self.adapter.probe('youtube','tlsrec-sni',ctx,timeout=10)
        self.adapter.probe('youtube','split-1',ctx,timeout=10)
        self.assertEqual([row[3] for row in self.transport.requests],['direct','production','candidate'])
        self.assertEqual(self.transport.requests[-1][1],self.transport.requests[-2][1])

    def test_dns_context_change_blocks_candidate_and_expiry_blocks_readiness(self):
        ctx=self.adapter.comparison_context('youtube')
        lease=pins(); lease['hosts']['www.youtube.com']=['142.250.74.207']
        atomic_private(self.runtime/'dns.json',json.dumps(lease).encode())
        with self.assertRaises(ValueError): self.adapter.probe('youtube','split-1',ctx,timeout=10)
        self.now=1100; self.adapter.refresh()
        self.assertFalse(self.adapter.capabilities()['probe_ready'])

    def test_apply_has_verified_backup_and_scoped_compare_and_swap_rollback(self):
        selection=config()['selections']; selection['instagram']='disorder-1'
        revision=self.state.snapshot(1000)['revision']
        backup=self.adapter.backup(selection,revision)
        self.assertTrue(backup['encrypted'])
        self.assertEqual(self.archive.copies,[config()])
        self.assertEqual(len(self.transport.validated),1)
        self.adapter.apply(selection,revision,backup['token'])
        actual=json.loads((self.runtime/'selection.json').read_bytes())
        self.assertEqual(actual['selections']['instagram'],'disorder-1')
        self.assertEqual(actual['selections']['youtube'],'tlsrec-sni')
        self.assertTrue(self.adapter.rollback(backup['token'],revision))
        self.assertEqual(json.loads((self.runtime/'selection.json').read_bytes()),config())

    def test_newer_runtime_or_policy_prevents_apply_and_rollback(self):
        selection=config()['selections']; selection['instagram']='disorder-1'
        revision=self.state.snapshot(1000)['revision']; backup=self.adapter.backup(selection,revision)
        newer=config(); newer['generation']='f'*64
        atomic_private(self.runtime/'selection.json',json.dumps(newer).encode())
        with self.assertRaises(ValueError): self.adapter.apply(selection,revision,backup['token'])
        self.assertFalse(self.adapter.rollback(backup['token'],revision))
        self.assertEqual(json.loads((self.runtime/'selection.json').read_bytes()),newer)

    def test_read_only_capabilities_do_not_wait_for_slow_backup(self):
        began,release,responded=threading.Event(),threading.Event(),threading.Event()
        def archive(_): began.set(); release.wait(3); return 'e'*64
        self.archive.create=archive
        selection=config()['selections']; selection['instagram']='split-1'
        revision=self.state.snapshot(1000)['revision']
        thread=threading.Thread(target=lambda:self.adapter.backup(selection,revision)); thread.start()
        self.assertTrue(began.wait(1))
        reader=threading.Thread(target=lambda:(self.adapter.capabilities(),responded.set())); reader.start()
        try: self.assertTrue(responded.wait(0.2),'snapshot waits for backup lock')
        finally: release.set(); thread.join(3); reader.join(3)


if __name__=='__main__': unittest.main()
