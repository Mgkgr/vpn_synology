import importlib
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).parents[1]))
from maintenance.profile_links import parse_link
from test_profile_links import VLESS, HY2
from test_replace_primary_vless import BEFORE


class ProfileHostTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).parents[1]/'maintenance/profile_host.py').exists(),'profile host missing')
        self.m=importlib.import_module('maintenance.profile_host')

    def test_replacement_only_touches_target_and_safe_active_summary(self):
        profile=parse_link(VLESS)
        after=self.m.replace_outbound(BEFORE,profile)
        self.assertEqual(after.split('  - name: HY2-USA')[1],BEFORE.split('  - name: HY2-USA')[1])
        self.assertEqual(after.split('proxies:')[0],BEFORE.split('proxies:')[0])
        self.assertEqual(self.m.current_profiles(after)[0],dict(target='WG-IMP',protocol='vless',server='vpn.example',port=443))
        self.assertNotIn('11111111',json.dumps(self.m.current_profiles(after)))
        for bad in (BEFORE.replace('name: WG-IMP','name: OTHER'), BEFORE.replace('name: HY2-USA','name: WG-IMP')):
            with self.assertRaises(ValueError): self.m.replace_outbound(bad,profile)

    def test_isolated_config_has_auth_no_direct_and_no_privileged_transport(self):
        profile=parse_link(HY2)
        config=self.m.isolated_config(profile,'1.1.1.1','private-test-proxy-password')
        self.assertEqual(config['proxies'][0]['server'],'1.1.1.1')
        self.assertEqual(config['rules'],['MATCH,HY2-USA'])
        self.assertEqual(config['authentication'],['check:private-test-proxy-password'])
        self.assertFalse(config['tun']['enable']); self.assertFalse(config['dns']['enable'])
        self.assertNotIn('external-controller',config)
        argv=self.m.isolated_argv('a'*32,'sha256:'+'b'*64,PurePosixPath('/private/input.json'))
        self.assertIn('--read-only',argv); self.assertIn('--cap-drop=ALL',argv)
        self.assertIn('--user=10002:10002',argv); self.assertIn('--pull=never',argv)
        for bad in ('--privileged','--network=host','--publish','-p','NET_ADMIN','private-test-proxy-password'):
            self.assertNotIn(bad,argv)
        with self.assertRaises(ValueError): self.m.isolated_argv('../escape','latest',Path('/private/input.json'))

    def test_transaction_checks_cas_before_reload_and_persists_only_verified(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); config=root/'config.yaml'; config.write_bytes(BEFORE.encode('utf-8'))
            calls=[]
            class Controller:
                def apply(self,value): calls.append(value)
            host=self.m.ProfileHost.__new__(self.m.ProfileHost)
            host.config=config; host.private=root; host.controller=lambda:Controller()
            host.revision=lambda:self.m.digest(config.read_bytes())
            original=host.revision(); candidate=self.m.replace_outbound(BEFORE,parse_link(VLESS))
            token='b'*64
            self.m.private_directory(root/'profile-recovery')
            self.m.atomic_private(root/'profile-recovery'/(token+'.json'),self.m.canonical(dict(
                before=BEFORE,candidate=candidate,revision=original,target='WG-IMP',verified=False,before_state={},source_server='vpn.example')))
            host.apply(token,original)
            self.assertEqual(config.read_text(encoding='utf-8'),BEFORE)
            self.assertEqual(calls,[candidate])
            with self.assertRaises(ValueError): host.persist(token,original)
            config.write_text('concurrent change',encoding='utf-8')
            with self.assertRaises(ValueError): host.apply(token,original)
            self.assertFalse(host.rollback(token,original))
            self.assertEqual(config.read_text(encoding='utf-8'),'concurrent change')
            self.assertEqual(len(calls),1)

    def test_probe_cleanup_checks_exact_label_and_confirms_removal(self):
        host=self.m.ProfileHost.__new__(self.m.ProfileHost)
        job='a'*32
        host._command=Mock(side_effect=['b'*64, job, '', ''])
        host.cleanup_probe(job)
        self.assertEqual(host._command.call_args_list[2].args,('rm','-f','vpn-profile-check-'+job))
        host._command=Mock(side_effect=['b'*64,'not-owned'])
        with self.assertRaises(self.m.ProbeCleanupUnconfirmed): host.cleanup_probe(job)
        self.assertEqual(host._command.call_count,2)
        host._command=Mock(side_effect=['b'*64,job,'','b'*64])
        with self.assertRaises(self.m.ProbeCleanupUnconfirmed): host.cleanup_probe(job)

    def test_fixed_github_probe_does_not_depend_on_shared_api_rate_limit(self):
        # Real NAS response: API 403, X-RateLimit-Remaining: 0; HTTPS robots: 200.
        self.assertEqual(self.m.PROBES['github'],('https://github.com/robots.txt',200))


if __name__=='__main__': unittest.main()
