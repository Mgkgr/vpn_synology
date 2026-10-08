import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

HOST='www.youtube.com'

def answer(host=HOST,ttl=90,address='142.250.74.206'):
    return dict(Status=0,TC=False,Question=[dict(name=host+'.',type=1)],
                Answer=[dict(name=host+'.',type=1,TTL=ttl,data=address)])

class IndependentDnsTests(unittest.TestCase):
    def setUp(self): self.m=importlib.import_module('maintenance.antidpi_dns')

    def test_answer_ttl_counts_request_time_and_http_age(self):
        row=self.m.parse_answer(HOST,answer(),started=1000,completed=1003,age=5)
        self.assertEqual(row['expires_at'],1085)
        self.assertEqual(row['addresses'],['142.250.74.206'])

    def test_valid_cname_chain_uses_shortest_ttl(self):
        value=answer()
        value['Answer']=[dict(name=HOST+'.',type=5,TTL=50,data='edge.example.com.'),
                         dict(name='edge.example.com.',type=1,TTL=90,data='142.250.74.206')]
        row=self.m.parse_answer(HOST,value,started=1000,completed=1001)
        self.assertEqual(row['expires_at'],1050)

    def test_unrelated_or_ambiguous_answers_fail_closed(self):
        for mutate in (
            lambda v:v['Question'][0].update(name='other.example'),
            lambda v:v.update(TC=True),lambda v:v.update(Status=True),
            lambda v:v['Answer'][0].update(name='unrelated.example'),
            lambda v:v['Answer'].append(dict(name=HOST,type=5,TTL=20,data='edge.example.com')),
            lambda v:v['Answer'][0].update(TTL=True),
            lambda v:v['Answer'][0].update(data='198.18.0.1'),
            lambda v:v['Answer'][0].update(data='192.168.2.103')):
            value=answer(); mutate(value)
            with self.assertRaises(ValueError): self.m.parse_answer(HOST,value,started=1000,completed=1001)
        with self.assertRaises(ValueError): self.m.parse_answer(HOST,answer(ttl=2),started=1000,completed=1003)

    def test_fallback_after_primary_error_and_keep_preferred_address(self):
        calls=[]
        def query(provider,host):
            calls.append(provider)
            if provider=='cloudflare': raise OSError('network unavailable')
            return dict(addresses=['142.250.74.206','142.250.74.207'],expires_at=1090)
        resolver=self.m.DnsRefresher(query=query,clock=lambda:1000)
        row=resolver.fetch_host(HOST,'142.250.74.207')
        self.assertEqual(calls,['cloudflare','google'])
        self.assertEqual(row['address'],'142.250.74.207')

    def test_short_primary_answer_uses_longer_secondary_without_extending_ttl(self):
        def query(provider,host):
            return dict(addresses=['142.250.74.206'],expires_at=1008 if provider=='cloudflare' else 1090)
        row=self.m.DnsRefresher(query=query,clock=lambda:1000).fetch_host(HOST,None)
        self.assertEqual(row['expires_at'],1090)

    def test_cache_retains_absolute_expiry_during_provider_outage(self):
        now=[1000.0]; failing=[False]; calls=[]
        def query(provider,host):
            calls.append((provider,host))
            if failing[0]: raise OSError('offline')
            return dict(addresses=['142.250.74.206'],expires_at=1060)
        resolver=self.m.DnsRefresher(query=query,clock=lambda:now[0])
        first=resolver.refresh()
        self.assertEqual(len(calls),5)
        now[0]=1010; resolver.refresh(); self.assertEqual(len(calls),5)
        now[0]=1021; failing[0]=True
        retained=resolver.refresh()
        self.assertEqual(first['expires_at'],retained['expires_at'])
        now[0]=1061
        with self.assertRaises(ValueError): resolver.refresh()

    def test_refresh_is_per_host_and_unchanged_host_keeps_its_deadline(self):
        now=[1000.0]; calls=[]
        def query(provider,host):
            calls.append(host)
            return dict(addresses=['142.250.74.206'],expires_at=now[0]+(60 if host==HOST else 300))
        resolver=self.m.DnsRefresher(query=query,clock=lambda:now[0])
        resolver.refresh(); calls.clear(); now[0]=1021
        value=resolver.refresh()
        self.assertEqual(calls,[HOST])
        self.assertEqual(value['expires_at'],1081)

    def test_fixed_host_and_provider_boundary(self):
        for provider,host in [('evil',HOST),('google','192.168.2.103'),('google','other.example')]:
            with self.assertRaises(ValueError): self.m.query_answers(provider,host)

    def test_clock_rollback_cannot_extend_cached_dns(self):
        now=[1000.0]; failing=[False]
        def query(p,h):
            if failing[0]: raise OSError('offline')
            return dict(addresses=['142.250.74.206'],expires_at=1060)
        resolver=self.m.DnsRefresher(query=query,clock=lambda:now[0])
        resolver.refresh(); now[0]=900; failing[0]=True
        with self.assertRaises(ValueError): resolver.refresh()

    def test_transport_pins_bootstrap_ip_and_verifies_provider_hostname(self):
        with patch.object(self.m.socket,'create_connection') as tcp,patch.object(self.m.ssl,'create_default_context') as context:
            conn=self.m.PinnedHTTPS('dns.google','8.8.8.8',timeout=4)
            conn.connect()
            self.assertEqual(tcp.call_args.args[0],('8.8.8.8',443))
            self.assertEqual(context.return_value.wrap_socket.call_args.kwargs['server_hostname'],'dns.google')

    def test_query_rejects_redirect_large_body_and_invalid_age(self):
        import json
        for status,body,age in [(302,b'{}','0'),(200,b'x'*32769,'0'),(200,json.dumps(answer()).encode(),'bad')]:
            with patch.object(self.m,'PinnedHTTPS') as connection:
                response=connection.return_value.getresponse.return_value
                response.status=status
                response.getheader.side_effect=lambda name,default=None: {'Age':age,'Content-Type':'application/dns-json'}.get(name,default)
                response.read.return_value=body
                with self.assertRaises(ValueError): self.m.query_answers('google',HOST)

    def test_host_renewal_uses_independent_resolver_without_controller_or_config(self):
        import tempfile
        from maintenance import host_service
        import json
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            resolver=self.m.DnsRefresher(query=lambda p,h:dict(addresses=['142.250.74.206'],expires_at=1060),clock=lambda:1000)
            with patch.object(host_service,'validate_controller',side_effect=AssertionError('main resolver touched')):
                value=host_service.renew_dns(private=path/'absent',runtime=path,resolver=resolver)
            self.assertEqual(json.loads((path/'dns.json').read_text(encoding='utf-8')),value)
            self.assertEqual(value['source_revision'],self.m.REVISION)

if __name__=='__main__': unittest.main()
