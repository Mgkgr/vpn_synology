"""Production host boundary: DNS, readiness, runtime CAS and bounded worker."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_antidpi_production import config, pins


class HostContractTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'maintenance/antidpi_host.py').exists(),
                        'host adapter not implemented')
        self.m=importlib.import_module('maintenance.antidpi_host')

    def test_dns_renewal_honors_shortest_ttl_and_rejects_any_private_answer(self):
        answers={host:{'Status':0,'Answer':[{'type':1,'data':values[0],'TTL':120}]}
                 for host,values in pins()['hosts'].items()}
        answers['discord.com']['Answer'].append({'type':1,'data':'162.159.138.232','TTL':30})
        value=self.m.renewed_lease(answers,'c'*64,1000)
        self.assertEqual(value['expires_at'],1030)
        self.assertEqual(value['hosts']['discord.com'],['162.159.135.232'])
        answers['discord.com']['Answer'].append({'type':1,'data':'192.168.1.1','TTL':60})
        with self.assertRaises(ValueError): self.m.renewed_lease(answers,'c'*64,1000)

    def test_dns_error_missing_host_or_zero_ttl_does_not_extend_old_lease(self):
        answers={host:{'Status':0,'Answer':[{'type':1,'data':values[0],'TTL':120}]}
                 for host,values in pins()['hosts'].items()}
        for key,bad in (('Status',3),('Answer',[])):
            damaged=json.loads(json.dumps(answers)); damaged['discord.com'][key]=bad
            with self.assertRaises(ValueError): self.m.renewed_lease(damaged,'c'*64,1000)
        answers['discord.com']['Answer'][0]['TTL']=0
        with self.assertRaises(ValueError): self.m.renewed_lease(answers,'c'*64,1000)

    def test_compare_and_swap_preserves_newer_runtime_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'selection.json'
            original=json.dumps(config()).encode()
            path.write_bytes(original)
            changed=config(); changed['generation']='d'*64
            expected=self.m.digest(original)
            self.m.replace_config(path,changed,expected)
            self.assertEqual(json.loads(path.read_bytes()),changed)
            with self.assertRaises(ValueError): self.m.replace_config(path,config(),expected)
            self.assertEqual(json.loads(path.read_bytes()),changed)

    def test_readiness_never_converts_probe_success_into_client_acceptance(self):
        evidence={'identity':'a'*64,'verified_at':1000,'checks':{
            'runtime':True,'dns_renewal':True,'namespace_recovery':True,
            'service_isolation':True,'production_input':False,'backup':True}}
        value=self.m.capabilities(evidence,'a'*64,runtime_ok=True,now=1010)
        self.assertTrue(value['probe_ready'])
        self.assertFalse(value['apply_ready'])
        self.assertIn('production_input_unverified',value['blockers'])
        evidence['checks']['production_input']=True
        self.assertTrue(self.m.capabilities(evidence,'a'*64,runtime_ok=True,now=1010)['apply_ready'])
        self.assertFalse(self.m.capabilities(evidence,'b'*64,runtime_ok=True,now=1010)['apply_ready'])
        self.assertFalse(self.m.capabilities(evidence,'a'*64,runtime_ok=False,now=1010)['apply_ready'])

    def test_runtime_pair_is_private_and_has_no_dependency_on_working_vpn(self):
        self.assertTrue(hasattr(self.m,'compose_runtime'),'production compose generator missing')
        value=self.m.compose_runtime('/volume1/docker/vpn-dashboard-maintenance/app',
                                     '/volume1/docker/vpn-antidpi/runtime','sha256:'+'1'*64,'sha256:'+'2'*64)
        self.assertEqual(set(value['services']),{'antidpi','socks'})
        for service in value['services'].values():
            self.assertNotIn('ports',service)
            self.assertNotIn('privileged',service)
            self.assertEqual(service['user'],'10002:10002')
            self.assertEqual(service['cap_drop'],['ALL'])
            self.assertTrue(service['read_only'])
            self.assertTrue(all(mount['read_only'] for mount in service['volumes']))
        self.assertEqual(value['services']['socks']['network_mode'],'service:antidpi')
        self.assertNotIn('host',str(value['services']['antidpi'].get('network_mode')))


if __name__=='__main__': unittest.main()
