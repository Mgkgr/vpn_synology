import importlib
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_antidpi_production import pins


class RenewalTests(unittest.TestCase):
    def setUp(self): self.m=importlib.import_module('maintenance.host_service')

    def test_short_cached_answer_is_retried_without_inventing_a_longer_ttl(self):
        self.assertTrue(hasattr(self.m,'fresh_dns_lease'),'bounded cache-refresh retry missing')
        now=[1000.0]; calls=[]
        def fetch():
            calls.append(now[0]); ttl=1 if len(calls)==1 else 90
            return {host:dict(Status=0,Answer=[dict(type=1,TTL=ttl,data=values[0])]) for host,values in pins()['hosts'].items()}
        value=self.m.fresh_dns_lease(fetch,'c'*64,clock=lambda:now[0],sleep=lambda delay:now.__setitem__(0,now[0]+delay))
        self.assertEqual(len(calls),2)
        self.assertEqual(value['expires_at'],calls[-1]+90)

    def test_persistently_short_answers_stop_after_three_attempts(self):
        self.assertTrue(hasattr(self.m,'fresh_dns_lease'),'bounded cache-refresh retry missing')
        now=[1000.0]; calls=[]
        def fetch():
            calls.append(now[0])
            return {host:dict(Status=0,Answer=[dict(type=1,TTL=1,data=values[0])]) for host,values in pins()['hosts'].items()}
        with self.assertRaises(ValueError):
            self.m.fresh_dns_lease(fetch,'c'*64,clock=lambda:now[0],sleep=lambda delay:now.__setitem__(0,now[0]+delay))
        self.assertEqual(len(calls),3)

    def test_bad_address_is_never_accepted_after_retry(self):
        self.assertTrue(hasattr(self.m,'fresh_dns_lease'),'bounded cache-refresh retry missing')
        answers={host:dict(Status=0,Answer=[dict(type=1,TTL=120,data=values[0])]) for host,values in pins()['hosts'].items()}
        answers['discord.com']['Answer'][0]['data']='192.168.2.103'
        with self.assertRaises(ValueError): self.m.fresh_dns_lease(lambda:answers,'c'*64,clock=lambda:1000,sleep=lambda _:None)


if __name__=='__main__': unittest.main()
