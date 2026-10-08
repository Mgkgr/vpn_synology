"""Runtime safety contract; network/process boundaries are replaced, not policy."""
import copy
import importlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def config():
    return {'version': 1, 'identity': 'a' * 64, 'generation': 'b' * 64,
            'selections': {'youtube': 'tlsrec-sni', 'discord': 'tlsrec-sni',
                           'telegram': 'tlsrec-sni', 'instagram': 'disorder-sni'}}


def pins():
    return {'version': 2, 'created_at': 1000, 'expires_at': 1100,
            'source_revision': 'c' * 64,
            'hosts': {'www.youtube.com': ['142.250.74.206'], 'discord.com': ['162.159.135.232'],
                      'web.telegram.org': ['149.154.167.99'], 'www.instagram.com': ['157.240.16.174'],
                      'www.wikipedia.org': ['185.15.59.224']}}


class Child:
    def __init__(self, argv):
        self.argv, self.dead, self.stopped = argv, False, False
    def poll(self): return 1 if self.dead else None
    def terminate(self): self.dead = self.stopped = True
    def wait(self, timeout): return 0
    def kill(self): self.dead = self.stopped = True


class ProductionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1] / 'antidpi/production.py').exists(),
                        'production runtime is not implemented')
        self.m = importlib.import_module('antidpi.production')

    def test_strategy_change_replaces_only_its_process(self):
        started = []
        def start(argv):
            child = Child(argv); started.append(child); return child
        manager = self.m.EngineSupervisor(start=start)
        manager.reconcile(config())
        original = list(started)
        changed = config(); changed['selections']['instagram'] = 'disorder-1'
        manager.reconcile(changed)
        self.assertEqual(len(started), 5)
        self.assertEqual([p.stopped for p in original], [False, False, False, True])
        self.assertIn('--no-domain', started[-1].argv)
        self.assertIn('--no-udp', started[-1].argv)
        self.assertNotIn('--auto', started[-1].argv)
        manager.stop()
        self.assertTrue(all(p.stopped for p in started))

    def test_invalid_configuration_never_starts_or_replaces_a_process(self):
        called = []
        manager = self.m.EngineSupervisor(start=lambda argv: called.append(argv))
        for bad in ('--exec evil', '../split-1', 'tlsrec-sni;id', ''):
            candidate = config(); candidate['selections']['youtube'] = bad
            with self.assertRaises(ValueError): manager.reconcile(candidate)
        self.assertEqual(called, [])

    def test_auth_input_has_no_direct_fallback_and_routes_each_service(self):
        value = self.m.auth_config('s' * 48, config(), pins(), now=1000)
        self.assertEqual(value['rules'][-1], 'MATCH,REJECT')
        self.assertNotIn('external-controller', value)
        self.assertEqual(value['dns'], {'enable': False})
        self.assertEqual(value['listeners'][0]['users'], [{'username': 'gateway', 'password': 's' * 48}])
        self.assertEqual([p['port'] for p in value['proxies']], [1081, 1082, 1083, 1084])
        self.assertIn('AND,((DOMAIN,www.instagram.com),(DST-PORT,443)),instagram', value['rules'])
        self.assertFalse(any('DIRECT' in line for line in value['rules']))

    def test_dns_lease_expiry_private_or_extra_host_is_rejected(self):
        for candidate, now in ((pins(),1100), (pins(),999)):
            with self.assertRaises(ValueError): self.m.auth_config('s'*48,config(),candidate,now=now)
        for address in ('127.0.0.1','198.18.0.1','192.168.2.103','169.254.169.254','::1'):
            candidate=pins(); candidate['hosts']['discord.com']=[address]
            with self.assertRaises(ValueError): self.m.auth_config('s'*48,config(),candidate,now=1000)
        candidate=pins(); candidate['hosts']['unapproved.example']=['8.8.8.8']
        with self.assertRaises(ValueError): self.m.auth_config('s'*48,config(),candidate,now=1000)

    def test_renewal_same_addresses_does_not_restart_auth_but_address_change_does(self):
        before=self.m.auth_fingerprint(config(),pins(),now=1000)
        renewed=pins(); renewed.update(created_at=1050,expires_at=1150)
        self.assertEqual(before,self.m.auth_fingerprint(config(),renewed,now=1050))
        changed=config(); changed['generation']='d'*64; changed['selections']['youtube']='split-1'
        self.assertEqual(before,self.m.auth_fingerprint(changed,renewed,now=1050))
        renewed['hosts']['www.youtube.com']=['142.250.74.207']
        self.assertNotEqual(before,self.m.auth_fingerprint(config(),renewed,now=1050))

    def test_ambiguous_ip_never_chooses_another_services_strategy(self):
        candidate=pins(); candidate['hosts']['www.instagram.com']=candidate['hosts']['discord.com'][:]
        value=self.m.auth_config('s'*48,config(),candidate,now=1000)
        self.assertFalse(any('IP-CIDR,162.159.135.232/' in rule for rule in value['rules']))
        self.assertIn('AND,((DOMAIN,discord.com),(DST-PORT,443)),discord',value['rules'])


if __name__ == '__main__': unittest.main()
