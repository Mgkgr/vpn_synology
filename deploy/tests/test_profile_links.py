import importlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VLESS = ('vless://11111111-2222-4333-8444-555555555555@vpn.example:443'
         '?type=tcp&security=reality&encryption=none&flow=xtls-rprx-vision'
         '&sni=vpn.example&fp=firefox&pbk=' + 'A' * 43 + '&sid=0123456789abcdef&spx=%2Ftest')
HY2 = 'hy2://test-secret@reserve.example:443/?obfs=salamander&obfs-password=obfs-test-secret&sni=reserve.example'


class ProfileLinkTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).parents[1] / 'maintenance/profile_links.py').exists(), 'profile parser missing')
        self.m = importlib.import_module('maintenance.profile_links')

    def test_telegram_vless_maps_primary_and_preserves_tls_fields(self):
        p = self.m.parse_link(VLESS.replace('://', '\\://').replace('@', '\\@').replace('&', '\\&') + '#test&#x20;')
        self.assertEqual((p['name'], p['type'], p['port']), ('WG-IMP', 'vless', 443))
        self.assertEqual(p['uuid'], '11111111-2222-4333-8444-555555555555')
        self.assertEqual(p['reality-opts'], {'public-key': 'A' * 43, 'short-id': '0123456789abcdef'})
        self.assertEqual(p['client-fingerprint'], 'firefox')
        self.assertTrue(p['tls']); self.assertFalse(p['skip-cert-verify'])
        self.assertNotIn('spx', p)
        self.assertEqual(self.m.safe_summary(p), dict(target='WG-IMP', protocol='vless', server='vpn.example', port=443))

    def test_hysteria_maps_reserve_and_only_explicit_port_hopping(self):
        p = self.m.parse_link(HY2)
        self.assertEqual(p['name'], 'HY2-USA')
        self.assertEqual(p['password'], 'test-secret')
        self.assertEqual(p['obfs-password'], 'obfs-test-secret')
        self.assertFalse(p['skip-cert-verify']); self.assertNotIn('ports', p)
        self.assertEqual(self.m.parse_link(HY2.replace('hy2:', 'hysteria2:') + '&mport=20000-50000')['ports'], '20000-50000')

    def test_duplicate_unknown_insecure_malformed_inputs_are_rejected_without_secrets(self):
        invalid = [VLESS + '&fp=chrome', VLESS + '&host=secret', VLESS.replace('reality', 'none'),
                   VLESS.replace('tcp', 'xhttp'), VLESS.replace('443?', '70000?'),
                   VLESS.replace('0123456789abcdef', 'Z'), VLESS.replace('A'*43, 'A'*42),
                   VLESS.replace('@vpn.example', ':password@vpn.example'), VLESS.replace(':443?', ':443/path?'),
                   HY2 + '&insecure=1', HY2 + '&mport=50000-20000', HY2 + '&sni=second.example',
                   HY2.replace('obfs-test-secret', '%0Asecret'), VLESS + '\nmore', HY2.replace('hy2:', 'https:')]
        for value in invalid:
            with self.subTest(index=invalid.index(value)), self.assertRaises(ValueError) as error:
                self.m.parse_link(value)
            self.assertEqual(str(error.exception), 'invalid_profile_link')

    def test_nonpublic_fakeip_numeric_alias_and_local_names_cannot_be_endpoint(self):
        for server in ('127.0.0.1', '192.168.2.103', '198.18.1.1', '100.64.1.1', '224.0.0.1',
                       '169.254.169.254', 'localhost', 'vpn.local', '2130706433', '127.1', '0x7f000001',
                       '-invalid.example', 'foo..example', '[::1]'):
            with self.subTest(server=server), self.assertRaises(ValueError):
                self.m.parse_link(VLESS.replace('vpn.example:443', server + ':443'))
        self.assertEqual(self.m.parse_link(VLESS.replace('vpn.example:443', '1.1.1.1:443'))['server'], '1.1.1.1')


if __name__ == '__main__': unittest.main()
