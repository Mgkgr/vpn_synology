import importlib
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from test_maintenance_backups import MemoryRepository
from test_antidpi_production import config
from maintenance.store import JobStore
from maintenance.strategy_state import StrategyStore
from maintenance.backups import BackupManager,BackupSource
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


class HostServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).resolve().parents[1]/'maintenance/host_service.py').exists(),'daemon bootstrap missing')
        self.m=importlib.import_module('maintenance.host_service')

    def test_controller_contract_accepts_only_resolved_private_endpoint(self):
        valid=dict(url='http://172.24.0.2:9090',secret='x'*48)
        self.assertEqual(self.m.validate_controller(valid),valid)
        for url in ('https://evil.example:9090','http://8.8.8.8:9090','http://198.18.1.1:9090',
                    'http://172.24.0.2:22','http://user:pass@172.24.0.2:9090','http://172.24.0.2:9090/a'):
            with self.assertRaises(ValueError): self.m.validate_controller(dict(valid,url=url))

    def test_runtime_monitor_survives_busy_database_and_repairs_on_next_tick(self):
        stop=Mock();stop.is_set.side_effect=[False,False,True]
        jobs=Mock();jobs.lock_state.side_effect=[sqlite3.OperationalError('database is locked'),dict(state='free')]
        jobs.acquire_writer.return_value='test-lease'
        control=Mock();control.inventory.return_value=dict(
            antidpi=dict(running=True),socks=dict(running=True,namespace_stale=True))
        adapter=Mock()
        self.m.monitor_runtime(stop,jobs,adapter,control,clock=lambda:1000)
        control.recreate.assert_called_once_with('socks')
        jobs.release_writer.assert_called_once_with('test-lease','complete')
        self.assertEqual(adapter.refresh.call_count,2)
        self.assertEqual(stop.wait.call_args_list,[((5,),),((5,),)])

    def test_runtime_monitor_preserves_uncertain_lock_after_recreate_error(self):
        stop=Mock();stop.is_set.side_effect=[False,False,True]
        jobs=Mock();jobs.lock_state.return_value=dict(state='free')
        jobs.acquire_writer.return_value='test-lease'
        control=Mock();control.inventory.return_value=dict(
            antidpi=dict(running=True),socks=dict(running=True,namespace_stale=True))
        control.recreate.side_effect=RuntimeError('lost acknowledgment')
        self.m.monitor_runtime(stop,jobs,Mock(),control,clock=lambda:1000)
        control.recreate.assert_called_once_with('socks')
        jobs.release_writer.assert_called_once_with('test-lease','uncertain')

    def test_runtime_monitor_never_mutates_while_another_job_holds_lock(self):
        stop=Mock();stop.is_set.side_effect=[False,True]
        jobs=Mock();jobs.lock_state.return_value=dict(state='held')
        control=Mock()
        self.m.monitor_runtime(stop,jobs,Mock(),control,clock=lambda:1000)
        control.inventory.assert_not_called()
        control.recreate.assert_not_called()
        jobs.acquire_writer.assert_not_called()

    def test_changed_installed_source_never_reuses_accepted_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'antidpi').mkdir()
            path=root/'antidpi/one.py'; path.write_text('value=1\n',encoding='utf-8')
            from maintenance.antidpi_host import digest
            manifest={'antidpi/one.py':digest(path.read_bytes())}
            self.m.verify_source(root,manifest)
            path.write_text('value=2\n',encoding='utf-8')
            with self.assertRaises(ValueError): self.m.verify_source(root,manifest)
            with self.assertRaises(ValueError): self.m.verify_source(root,{'../private/key':'a'*64})

    def test_socket_parent_persists_across_daemon_restarts_and_rejects_file(self):
        self.assertTrue(hasattr(self.m,'prepare_socket_parent'),'socket directory preparation missing')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'vpn-maintenance'
            self.m.prepare_socket_parent(path)
            marker=path/'marker'; marker.write_text('keep',encoding='utf-8')
            self.m.prepare_socket_parent(path)
            self.assertEqual(marker.read_text(encoding='utf-8'),'keep')
            bad=Path(tmp)/'file'; bad.write_text('keep',encoding='utf-8')
            with self.assertRaises(ValueError): self.m.prepare_socket_parent(bad)

    def test_strategy_snapshot_has_runtime_images_and_secret_and_verifies_actual_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); private=root/'private'; private.mkdir(mode=0o700)
            runtime=root/'runtime'; runtime.mkdir()
            (runtime/'selection.json').write_text(json.dumps(config()),encoding='utf-8')
            (runtime/'socks_password').write_text('test-only-secret',encoding='utf-8')
            jobs=JobStore(private/'jobs.sqlite3'); state=StrategyStore(jobs)
            state.register('a'*64,config()['selections'],1000)
            sources=[]
            for name,role in (('wg','wg_identity'),('secret','dashboard_secrets'),('config','config'),('worker','worker_state')):
                path=root/name; path.write_text('test-only-'+name,encoding='utf-8')
                sources.append(BackupSource(name,path,'file',role,'common'))
            images={'antidpi':'sha256:'+'a'*64,'socks':'sha256:'+'b'*64}
            host=dict(contracts={key:dict(image_id=value) for key,value in images.items()})
            (private/'antidpi-host.json').write_text(json.dumps(host),encoding='utf-8')
            manifest=dict(_sources=sources,repository='dummy',password_file='dummy',identity_source='wg',images={'wireguard':'sha256:'+'c'*64})
            repo=MemoryRepository()
            def manager(*args,**kwargs): return BackupManager(*args,source_roots=(root,),**kwargs)
            with patch.object(self.m,'load_backup_manifest',return_value=manifest),patch.object(self.m,'ResticRepository',return_value=repo), \
                 patch.object(self.m,'source_revision',return_value='d'*64),patch.object(self.m,'BackupManager',side_effect=manager), \
                 patch.object(jobs,'lock_state',return_value=dict(kind='job',state='held',token='e'*32)):
                snapshot=self.m.StrategyArchive(state,runtime,private).create(config())
            self.assertEqual(len(snapshot),64)
            actual=json.loads(repo.files['manifest.json'])
            self.assertEqual(actual['images']['antidpi'],images['antidpi'])
            self.assertEqual(actual['images']['socks'],images['socks'])
            self.assertEqual(repo.files['antidpi/socks_password'],b'test-only-secret')


if __name__=='__main__': unittest.main()
