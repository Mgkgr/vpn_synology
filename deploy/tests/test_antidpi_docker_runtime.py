import importlib
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'maintenance/antidpi_docker.py').exists(),'Docker transport missing')
        self.m=importlib.import_module('maintenance.antidpi_docker')
        self.calls=[]
        def run(argv,timeout=60,limit=1048576):
            self.calls.append(argv)
            if argv[1]=='create': return 'a'*64
            if argv[1]=='start': return json.dumps(dict(verdict='success',reason='verified',latency_ms=20,http_status=200))
            if 'inspect' in argv: return 'true'
            return '{}'
        self.runtime=self.m.DockerRuntime('sha256:'+'1'*64,'/volume1/docker/vpn-dashboard-maintenance/app',None,runner=run)

    def test_candidate_is_isolated_with_no_secret_mount_or_public_listener(self):
        self.runtime.probe('discord.com','162.159.135.232','tlsrec-sni','candidate')
        run=next(argv for argv in self.calls if argv[1]=='create')
        self.assertIn('--read-only',run)
        self.assertIn('10002:10002',run)
        self.assertNotIn('--privileged',run)
        self.assertNotIn('--publish',run)
        self.assertEqual(run[run.index('--network')+1],'bridge')
        self.assertFalse(any('runtime:' in arg or 'socks_password' in arg for arg in run))
        self.assertTrue(any('rm' in argv for argv in self.calls))

    def test_existing_vpn_container_can_never_be_selected_as_probe(self):
        for strategy in ('vpn-mihomo',';id','../ciadpi'):
            with self.assertRaises(ValueError): self.runtime.probe('discord.com','162.159.135.232',strategy,'candidate')
        with self.assertRaises(ValueError): self.runtime.probe('private.local','127.0.0.1','tlsrec-sni','candidate')
        self.assertEqual(self.calls,[])

    def test_cleanup_runs_even_when_candidate_times_out(self):
        for failed_stage in ('create','start'):
            self.calls.clear()
            def run(argv,**kwargs):
                self.calls.append(argv)
                if argv[1]==failed_stage: raise TimeoutError('timeout')
                return '{}'
            self.runtime.runner=run
            with self.assertRaises(TimeoutError): self.runtime.probe('discord.com','162.159.135.232','tlsrec-sni','candidate')
            self.assertTrue(any(argv[1]=='rm' for argv in self.calls))


if __name__=='__main__': unittest.main()
