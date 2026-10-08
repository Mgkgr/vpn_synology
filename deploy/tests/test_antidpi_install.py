import importlib
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'maintenance/host_install.py').exists(),'scoped installer missing')
        self.m=importlib.import_module('maintenance.host_install')

    def test_archive_rejects_traversal_links_and_secret_files(self):
        for name,kind in (('../escape.py','file'),('/etc/passwd','file'),('deploy/dashboard.env','file'),
                          ('deploy/maintenance/link.py','symlink')):
            with tempfile.TemporaryDirectory() as tmp:
                archive=Path(tmp)/'release.tar'
                with tarfile.open(archive,'w') as tar:
                    info=tarfile.TarInfo(name); info.size=1
                    if kind=='symlink': info.type=tarfile.SYMTYPE; info.linkname='/etc/passwd'; info.size=0
                    tar.addfile(info,io.BytesIO(b'x') if kind=='file' else None)
                with self.assertRaises(ValueError): self.m.read_release(archive)

    def test_only_exact_reviewed_source_members_are_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive=Path(tmp)/'release.tar'
            with tarfile.open(archive,'w') as tar:
                for name,data in (('deploy/maintenance/worker.py',b'value=1\n'),('deploy/antidpi/versions.json',b'{}')):
                    info=tarfile.TarInfo(name); info.size=len(data); tar.addfile(info,io.BytesIO(data))
            value=self.m.read_release(archive)
            self.assertEqual(set(value),{'maintenance/worker.py','antidpi/versions.json'})
            self.assertEqual(value['maintenance/worker.py'],b'value=1\n')

    def test_controller_discovery_does_not_expand_shell_or_send_secret_to_hostname(self):
        text='MIHOMO_URL=http://vpn-wireguard:9090\nMIHOMO_API_SECRET='+('x'*48)+'\n'
        value=self.m.controller_contract(text,'172.24.0.2')
        self.assertEqual(value['url'],'http://172.24.0.2:9090')
        with self.assertRaises(ValueError): self.m.controller_contract(text.replace('vpn-wireguard','evil.example'),'172.24.0.2')
        with self.assertRaises(ValueError): self.m.controller_contract(text.replace('x'*48,'$(whoami)'),'172.24.0.2')

    def test_source_remains_readable_by_nonroot_container_under_private_umask(self):
        self.assertTrue(hasattr(self.m,'write_source'),'safe source copy missing')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'module.py'
            previous=os.umask(0o077)
            try: self.m.write_source(path,b'value=1\n')
            finally: os.umask(previous)
            self.assertTrue(path.stat().st_mode & 0o004)
            self.assertFalse(path.stat().st_mode & 0o022)

    def test_controller_uses_only_the_fixed_mounted_secret_file(self):
        text='MIHOMO_URL=http://vpn-wireguard:9090\nMIHOMO_API_SECRET_FILE=/run/secrets/mihomo_api_secret\n'
        with patch.object(self.m,'read_private',return_value=b'x'*48) as read:
            result=self.m.controller_contract(text,'172.24.0.2')
            self.assertEqual(result['secret'],'x'*48)
            self.assertEqual(read.call_args.args[0],Path('/volume1/docker/vpn-dashboard/deploy/secrets/mihomo_api_secret'))
        for invalid in (text.replace('/run/secrets/mihomo_api_secret','/etc/shadow'),text+'MIHOMO_API_SECRET='+('y'*48)+'\n'):
            with self.assertRaises(ValueError): self.m.controller_contract(invalid,'172.24.0.2')

    def test_dsm_systemd_219_enable_and_start_are_separate(self):
        self.assertTrue(hasattr(self.m,'activate_unit'),'compatible systemd activation missing')
        with patch.object(self.m,'_command') as run:
            self.m.activate_unit()
        self.assertEqual([call.args for call in run.call_args_list],[
            ('/bin/systemctl','daemon-reload'),
            ('/bin/systemctl','enable','vpn-maintenance.service'),
            ('/bin/systemctl','start','vpn-maintenance.service')])


if __name__=='__main__': unittest.main()
