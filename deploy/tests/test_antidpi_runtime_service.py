import importlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_antidpi_production import config,pins,Child


class RuntimeServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'antidpi/runtime_service.py').exists(),'supervised runtime missing')
        self.m=importlib.import_module('antidpi.runtime_service')

    def test_expired_dns_kills_all_existing_engine_processes(self):
        children=[]
        def start(argv):
            value=Child(argv); children.append(value); return value
        service=self.m.EngineService(start=start)
        service.tick(config(),pins(),now=1000)
        self.assertEqual(len(children),4)
        with self.assertRaises(ValueError): service.tick(config(),pins(),now=1100)
        self.assertTrue(all(value.stopped for value in children))

    def test_auth_renewal_and_strategy_change_keep_sessions_but_dns_expiry_closes(self):
        children=[]; checked=[]
        def start(argv):
            value=Child(argv); children.append(value); return value
        with tempfile.TemporaryDirectory() as tmp:
            service=self.m.AuthService(Path(tmp),start=start,validate=lambda argv: checked.append(argv))
            service.tick('s'*48,config(),pins(),now=1000)
            lease=pins(); lease.update(created_at=1050,expires_at=1150)
            changed=config(); changed['selections']['instagram']='split-1'
            service.tick('s'*48,changed,lease,now=1050)
            self.assertEqual(len(children),1)
            self.assertEqual(len(checked),1)
            lease['hosts']['discord.com']=['162.159.138.232']
            service.tick('s'*48,changed,lease,now=1051)
            self.assertEqual(len(children),2)
            self.assertTrue(children[0].stopped)
            with self.assertRaises(ValueError): service.tick('s'*48,changed,lease,now=1150)
            self.assertTrue(children[1].stopped)

    def test_invalid_full_auth_config_never_starts_listener(self):
        children=[]
        def reject(_): raise ValueError('config rejected')
        with tempfile.TemporaryDirectory() as tmp:
            service=self.m.AuthService(Path(tmp),start=lambda argv:children.append(argv),validate=reject)
            with self.assertRaises(ValueError): service.tick('s'*48,config(),pins(),now=1000)
        self.assertEqual(children,[])

    def test_attestation_rejects_requested_but_unapplied_generation(self):
        self.assertTrue(hasattr(self.m,'attestation_matches'),'runtime attestation missing')
        value={'config':config(),'checked_at':1000}
        self.assertTrue(self.m.attestation_matches(value,config(),now=1001))
        changed=config(); changed['generation']='d'*64
        self.assertFalse(self.m.attestation_matches(value,changed,now=1001))
        self.assertFalse(self.m.attestation_matches(value,config(),now=1011))


if __name__=='__main__': unittest.main()
