"""DNS churn of one service must not disconnect the other three services."""
import copy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_antidpi_production import Child, config, pins


class Control:
    def __init__(self): self.applied=[]; self.fail=False
    def apply(self, value):
        if self.fail: raise OSError('test control transport failure')
        self.applied.append(copy.deepcopy(value))


class DnsIsolationTests(unittest.TestCase):
    def setUp(self): self.m=importlib.import_module('antidpi.runtime_service')

    def test_dns_address_change_replaces_only_affected_handler_connections(self):
        started=[]
        def start(argv):
            child=Child(argv); started.append(child); return child
        service=self.m.EngineService(start=start)
        service.tick(config(),pins(),now=1000)
        original=list(started)
        changed=pins(); changed['hosts']['www.instagram.com']=['157.240.16.175']
        service.tick(config(),changed,now=1001)
        self.assertEqual(len(started),5)
        self.assertEqual([child.stopped for child in original],[False,False,False,True])
        service.stop()

    def test_auth_dns_update_is_acknowledged_without_replacing_shared_process(self):
        children=[];control=Control()
        def start(argv):
            child=Child(argv);children.append(child);return child
        with tempfile.TemporaryDirectory() as directory:
            service=self.m.AuthService(Path(directory),start=start,validate=lambda _:None,control=control)
            service.tick('s'*48,config(),pins(),now=1000)
            changed=pins();changed['hosts']['www.instagram.com']=['157.240.16.175']
            service.tick('s'*48,config(),changed,now=1001)
            self.assertEqual(len(children),1)
            self.assertFalse(children[0].stopped)
            self.assertEqual(control.applied[-1]['hosts']['www.instagram.com'],['157.240.16.175'])
            self.assertNotIn('external-controller',control.applied[-1])
            self.assertEqual(control.applied[-1]['external-controller-unix'],str(Path(directory)/'control.sock'))
            persisted=json.loads((Path(directory)/'config.json').read_text(encoding='utf-8'))
            self.assertEqual(persisted['hosts']['www.instagram.com'],['157.240.16.175'])
            service.stop()

    def test_unacknowledged_reload_stops_auth_instead_of_claiming_applied(self):
        children=[];control=Control()
        def start(argv):
            child=Child(argv);children.append(child);return child
        with tempfile.TemporaryDirectory() as directory:
            service=self.m.AuthService(Path(directory),start=start,validate=lambda _:None,control=control)
            service.tick('s'*48,config(),pins(),now=1000)
            changed=pins();changed['hosts']['www.instagram.com']=['157.240.16.175']
            control.fail=True
            with self.assertRaises(OSError): service.tick('s'*48,config(),changed,now=1001)
            self.assertTrue(children[0].stopped)
            self.assertIsNone(service.child)

    def test_secret_rotation_still_closes_old_auth_sessions(self):
        children=[];control=Control()
        def start(argv):
            child=Child(argv);children.append(child);return child
        with tempfile.TemporaryDirectory() as directory:
            service=self.m.AuthService(Path(directory),start=start,validate=lambda _:None,control=control)
            service.tick('s'*48,config(),pins(),now=1000)
            service.tick('t'*48,config(),pins(),now=1001)
            self.assertEqual(len(children),2)
            self.assertTrue(children[0].stopped)
            service.stop()

    def test_dns_change_invalidates_old_runtime_attestation(self):
        original=pins();changed=pins();changed['hosts']['www.instagram.com']=['157.240.16.175']
        value=dict(config=config(),checked_at=1000,dns_context=self.m.dns_context(original))
        self.assertTrue(self.m.attestation_matches(value,config(),now=1001,lease=original))
        self.assertFalse(self.m.attestation_matches(value,config(),now=1001,lease=changed))
        renewal=pins();renewal.update(created_at=1001,expires_at=1101)
        self.assertTrue(self.m.attestation_matches(value,config(),now=1001,lease=renewal))


if __name__=='__main__': unittest.main()
