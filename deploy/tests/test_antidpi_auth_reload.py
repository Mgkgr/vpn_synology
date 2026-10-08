import importlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


class AuthReloadTests(unittest.TestCase):
    def setUp(self): self.m=importlib.import_module('antidpi.auth_reload')

    def test_reload_requires_success_ack_and_does_not_follow_redirect(self):
        for status in (301,400,503):
            requests=[]
            def request(path,method,target,payload,timeout):
                requests.append((method,target))
                return (200,b'{"version":"v1.19.28"}') if method=='GET' else (status,b'private detail')
            control=self.m.MihomoUnixControl(Path('/tmp/auth/control.sock'),request=request)
            with self.assertRaises(ValueError): control.apply({'hosts':{'discord.com':['162.159.135.232']}})
            self.assertEqual(requests,[('GET','/version'),('PUT','/configs?force=false')])

    def test_lost_mutation_response_is_not_retried(self):
        calls=[]
        def request(path,method,target,payload,timeout):
            calls.append(method)
            if method=='PUT': raise TimeoutError('response lost')
            return 200,b'{"version":"v1.19.28"}'
        with self.assertRaises(TimeoutError):
            self.m.MihomoUnixControl(Path('/tmp/auth/control.sock'),request=request).apply({'mode':'rule'})
        self.assertEqual(calls,['GET','PUT'])

    def test_reload_is_bounded_and_keeps_secret_only_in_private_payload(self):
        import json
        captured=[]
        def request(path,method,target,payload,timeout):
            captured.append((path,method,target,payload,timeout))
            return (200,b'{"version":"v1.19.28"}') if method=='GET' else (204,b'')
        value={'authentication':['gateway:test-fixture-only']}
        self.m.MihomoUnixControl(Path('/tmp/auth/control.sock'),request=request).apply(value)
        sent=captured[-1]
        self.assertEqual(sent[0],Path('/tmp/auth/control.sock'))
        self.assertEqual(json.loads(json.loads(sent[3])['payload']),value)
        self.assertLessEqual(sent[4],3)
        with self.assertRaises(ValueError):
            self.m.MihomoUnixControl(Path('/tmp/auth/control.sock'),request=request).apply({'large':'x'*20000})


if __name__=='__main__': unittest.main()
