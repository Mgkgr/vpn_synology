"""Root-only daemon assembly; no arbitrary requests, URLs or shell execution."""
from contextlib import closing
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from antidpi.production import HOSTS,CONTROL_HOST,canonical
from .antidpi_host import read_private,atomic_private,digest,renewed_lease
from .antidpi_adapter import ProductionAdapter
from .antidpi_docker import DockerRuntime,CODE
from .backups import BackupManager,BackupSource,ResticRepository,file_digest
from .backup_cli import load_backup_manifest,source_revision
from .docker_control import DockerControl
from .image_pins import read_root_json
from .service import MaintenanceService,UnixServer
from .store import JobStore,JobBusy
from .strategy_state import StrategyStore
from .strategy_runner import StrategyRunner
from .worker import JobLoop
from .antidpi_dns import DnsRefresher
from .profile_state import ProfileDrafts
from .profile_host import ProfileHost
from .profile_runner import ProfileRunner

PRIVATE=Path('/volume1/docker/vpn-dashboard-maintenance/private')
RUNTIME=Path('/volume1/docker/vpn-antidpi/runtime')
SOCKET=Path('/run/vpn-maintenance/control.sock')


def validate_controller(value):
    if not isinstance(value,dict) or set(value)!={'url','secret'}:
        raise ValueError('invalid_controller_contract')
    parts=urllib.parse.urlsplit(value['url'])
    address=ipaddress.IPv4Address(parts.hostname)
    if (parts.scheme!='http' or parts.port not in (9090,9091) or parts.username or parts.password
            or parts.path or parts.query or parts.fragment
            or not any(address in ipaddress.IPv4Network(cidr) for cidr in
                       ('127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
            or not isinstance(value['secret'],str) or not re.fullmatch(r'[A-Za-z0-9_\-]{16,256}',value['secret'])):
        raise ValueError('invalid_controller_contract')
    return value


def verify_source(root,hashes):
    if not isinstance(hashes,dict) or not hashes or len(hashes)>100:
        raise ValueError('invalid_code_manifest')
    for name,expected in hashes.items():
        if not re.fullmatch(r'(?:antidpi|maintenance)/[a-z_]+\.py',name):
            raise ValueError('invalid_code_path')
        if digest(read_private(Path(root)/name,1024*1024))!=expected:
            raise ValueError('installed_code_changed')


def prepare_socket_parent(path=SOCKET.parent):
    if any(item.is_symlink() for item in (path,*path.parents)) or (path.exists() and not path.is_dir()):
        raise ValueError('unsafe_socket_directory')
    path.mkdir(mode=0o750,exist_ok=True)
    if os.name=='posix':
        if path.stat().st_uid!=0: raise ValueError('unsafe_socket_owner')
        os.chown(str(path),0,10001); os.chmod(str(path),0o750)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def fresh_dns_lease(fetch,revision,clock=time.time,sleep=time.sleep):
    # Mihomo serves an expired cache entry with TTL=1 while refreshing upstream.
    # Do not manufacture a longer lease or replace a still-valid one with that.
    for attempt in range(3):
        started=clock()
        lease=renewed_lease(fetch(),revision,started)
        if lease['expires_at']-clock()>=15:
            return lease
        if attempt<2: sleep(1)
    raise ValueError('dns_ttl_too_short')


def renew_dns(private=PRIVATE,runtime=RUNTIME,resolver=None):
    # The private argument is retained for installer compatibility. This path
    # never reads the main controller credential or main VPN configuration.
    lease=(resolver or DnsRefresher()).refresh()
    path=runtime/'dns.json'
    try: previous=json.loads(read_private(path))
    except (OSError,ValueError): previous={}
    if any(previous.get(key)!=lease[key] for key in ('expires_at','source_revision','hosts')):
        atomic_private(path,canonical(lease),owner=10002)
    return lease


class StrategyArchive:
    def __init__(self,state,runtime=RUNTIME,private=PRIVATE):
        self.state,self.runtime,self.private=state,runtime,private

    def create(self,before):
        manifest=load_backup_manifest(self.private,check_live_schema=False)
        repo=ResticRepository(manifest['repository'],manifest['password_file'],self.private)
        sources=list(manifest['_sources'])
        sources.extend([BackupSource('antidpi/selection.json',self.runtime/'selection.json','file','config','antidpi'),
                        BackupSource('antidpi/socks_password',self.runtime/'socks_password','file','dashboard_secrets','antidpi'),
                        BackupSource('antidpi/host.json',self.private/'antidpi-host.json','file','worker_state','antidpi'),
                        BackupSource('antidpi/jobs.sqlite3',self.state.jobs.path,'sqlite','worker_state','antidpi')])
        host=read_root_json(self.private/'antidpi-host.json')
        images=dict(manifest['images'],**{key:host['contracts'][key]['image_id'] for key in ('antidpi','socks')})
        lock=self.state.jobs.lock_state(); job_id=lock.get('token')
        if lock.get('kind')!='job' or lock.get('state')!='held': raise ValueError('job_lock_required')
        def held(job):
            value=self.state.jobs.lock_state()
            return value.get('kind')=='job' and value.get('state')=='held' and value.get('token')==job
        def revision():
            return digest(canonical(dict(base=source_revision(manifest),
                policy=self.state.snapshot(time.time())['revision'],runtime=file_digest(self.runtime/'selection.json'))))
        identity=next(source.path for source in sources if source.name==manifest['identity_source'])
        manager=BackupManager(self.private,repo,sources,revision=revision,image_digests=lambda:images,
                              identity_digest=lambda:file_digest(identity),lock_check=held)
        if json.loads(read_private(self.runtime/'selection.json'))!=before: raise ValueError('runtime_revision_changed')
        value=manager.create(('antidpi',),job_id)
        if value.verified_at is None: raise ValueError('backup_unverified')
        return value.snapshot_id


def assemble():
    manifest=read_root_json(PRIVATE/'antidpi-host.json')
    verify_source(Path(CODE),manifest['code'])
    control=DockerControl(manifest['contracts'],PRIVATE)
    transport=DockerRuntime(manifest['engine_image'],CODE,control)
    jobs=JobStore(PRIVATE/'jobs.sqlite3'); state=StrategyStore(jobs)
    adapter=ProductionAdapter(state,PRIVATE,RUNTIME,transport,StrategyArchive(state),manifest['identity'])
    current=adapter.config(); snapshot=state.snapshot(time.time())
    if snapshot['identity'] is None:
        state.register(manifest['identity'],current['selections'],time.time())
    elif snapshot['identity']!=manifest['identity']:
        # A new reviewed build revokes all old authorizations, never inherits auto.
        state.register(manifest['identity'],current['selections'],time.time())
    elif {row['service_id']:row['strategy_id'] for row in snapshot['services']}!=current['selections']:
        raise ValueError('runtime_requires_reconciliation')
    runner=StrategyRunner(state,adapter)
    return jobs,runner,adapter,control


def assemble_profiles(jobs):
    return ProfileRunner(jobs,ProfileDrafts(PRIVATE/'profile-drafts'),ProfileHost(jobs,private=PRIVATE))


def monitor_runtime(stop,jobs,adapter,control,clock=time.time):
    next_repair=0
    while not stop.is_set():
        now=clock()
        try:
            free=now>=next_repair and jobs.lock_state()['state']=='free'
        except sqlite3.OperationalError:
            # A slow fsync/other writer may exceed SQLite's bounded busy wait.
            # Do not lose the monitor thread or assume ownership of that lock.
            free=False
        if free:
            lease_id=None
            try:
                rows=control.inventory()
                if rows['antidpi']['running'] and (rows['socks']['namespace_stale'] or not rows['socks']['running']):
                    lease_id=jobs.acquire_writer('strategies')
                    control.recreate('socks'); next_repair=now+300
                    jobs.release_writer(lease_id,'complete'); lease_id=None
            except JobBusy: pass
            except Exception:
                next_repair=now+300
                if lease_id:
                    try: jobs.release_writer(lease_id,'uncertain')
                    except Exception: pass
        adapter.refresh()
        stop.wait(5)


def main():
    if os.name!='posix' or os.geteuid()!=0: raise ValueError('linux_root_required')
    prepare_socket_parent()  # Do not remove this bind-mounted directory on service stop.
    jobs,runner,adapter,control=assemble()
    profiles=assemble_profiles(jobs)
    stop,ready=threading.Event(),threading.Event()
    for signum in (signal.SIGTERM,signal.SIGINT): signal.signal(signum,lambda *_:stop.set())
    service=MaintenanceService(jobs,strategies=runner,profiles=profiles)  # General component mutations remain disabled.
    server=UnixServer(service,SOCKET)
    thread=threading.Thread(target=server.serve,args=(stop,ready),daemon=True); thread.start()
    if not ready.wait(10): raise ValueError('worker_socket_unavailable')
    # Recovery ran under the process lock before any scheduler/runtime work starts.
    def dns_monitor():
        resolver=DnsRefresher()
        while not stop.is_set():
            try: renew_dns(resolver=resolver)
            except Exception: pass  # Expired snapshots fail closed in both consumers.
            stop.wait(1)
    dns_thread=threading.Thread(target=dns_monitor,daemon=True); dns_thread.start()
    monitor_thread=threading.Thread(target=monitor_runtime,args=(stop,jobs,adapter,control),daemon=True); monitor_thread.start()
    loop=JobLoop(runner,profiles=profiles)
    try:
        while not stop.is_set() and thread.is_alive():
            try: loop.tick(time.time())
            except Exception: pass  # Durable job records, not raw exceptions, are exposed.
            stop.wait(1)
    finally:
        stop.set(); thread.join(timeout=10); monitor_thread.join(timeout=40); dns_thread.join(timeout=15)


if __name__=='__main__':
    try: main()
    except Exception:
        print('MAINTENANCE_WORKER=unavailable',flush=True)
        raise SystemExit(1)
