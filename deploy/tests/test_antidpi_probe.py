import importlib
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


class Peer:
    def __init__(self, data): self.data=data; self.sent=[]; self.closed=False
    def settimeout(self,value):
        if not 0 < value <= 10: raise AssertionError('unbounded socket timeout')
    def sendall(self,value): self.sent.append(value)
    def recv(self,size):
        data,self.data=self.data[:size],self.data[size:]; return data
    def close(self): self.closed=True


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'antidpi/probe.py').exists(),'bounded probe missing')
        self.m=importlib.import_module('antidpi.probe')

    def test_http_denial_redirect_and_success_are_not_transport_failures(self):
        for status,verdict in ((200,'success'),(302,'http_error'),(403,'http_error'),(429,'http_error'),(503,'http_error')):
            peer=Peer(('HTTP/1.1 %d Result\r\nLocation: https://127.0.0.1/\r\n\r\n'%status).encode())
            class TLS:
                def wrap_socket(self, stream, server_hostname):
                    self_name=server_hostname
                    if self_name!='discord.com': raise AssertionError('SNI changed')
                    return stream
            with patch.object(self.m.socket,'create_connection',return_value=peer), patch.object(self.m.ssl,'create_default_context',return_value=TLS()):
                result=self.m.https_probe('discord.com','162.159.135.232')
            self.assertEqual(result['verdict'],verdict)
            self.assertEqual(result['http_status'],status)
            from maintenance.strategy_state import validated_observation
            validated_observation(dict(result, checked_at=1000, identity='a'*64,
                                       context_id='b'*64, infrastructure_ok=True))
            self.assertEqual(len(peer.sent),1)  # A Location never causes another request.
            self.assertTrue(peer.closed)

    def test_certificate_failure_is_distinct_from_retryable_transport(self):
        peer=Peer(b'')
        with patch.object(self.m.socket,'create_connection',return_value=peer), patch.object(self.m.ssl,'create_default_context') as ctx:
            ctx.return_value.wrap_socket.side_effect=ssl.SSLCertVerificationError('invalid certificate')
            result=self.m.https_probe('discord.com','162.159.135.232')
        self.assertEqual(result['verdict'],'certificate_error')
        self.assertTrue(peer.closed)

    def test_invalid_target_never_opens_a_socket(self):
        with patch.object(self.m.socket,'create_connection') as connect:
            for host,ip in (('evil.example','8.8.8.8'),('discord.com','198.18.0.1'),('discord.com','127.0.0.1')):
                with self.assertRaises(ValueError): self.m.https_probe(host,ip)
            connect.assert_not_called()

    def test_response_header_is_capped(self):
        peer=Peer(b'x'*65536)
        with patch.object(self.m.socket,'create_connection',return_value=peer), patch.object(self.m.ssl,'create_default_context') as ctx:
            ctx.return_value.wrap_socket.return_value=peer
            result=self.m.https_probe('discord.com','162.159.135.232')
        self.assertEqual(len(peer.data),32768)
        self.assertEqual(result['verdict'],'unknown')


if __name__=='__main__': unittest.main()
