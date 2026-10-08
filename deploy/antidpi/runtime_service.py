"""Non-root container entry points. All mutable files are mounted read-only."""
import hashlib
import base64
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from .production import (HOSTS, PORTS, ARGUMENTS, CONTROL_HOST, EngineSupervisor,
                         auth_config, auth_fingerprint, canonical, engine_argv,
                         stop_child, validate_config, validate_lease, _start)
from .runtime import MIHOMO, probe_socks, validate_secret
from .probe import https_probe
from .auth_reload import MihomoUnixControl

RUNTIME=Path('/run/antidpi')


def read_file(path,limit=65536):
    fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>limit or info.st_mode & 0o077:
            raise ValueError('private_runtime_file_required')
        return stream.read(limit+1)


def runtime_files():
    return (validate_config(json.loads(read_file(RUNTIME/'selection.json'))),
            validate_lease(json.loads(read_file(RUNTIME/'dns.json'))))


def dns_context(lease):
    return hashlib.sha256(canonical(dict(source_revision=lease['source_revision'],hosts=lease['hosts']))).hexdigest()


def attestation_matches(value,config,now=None,lease=None):
    now=time.time() if now is None else now
    return (isinstance(value,dict) and value.get('config')==config
            and (lease is None or value.get('dns_context')==dns_context(lease))
            and type(value.get('checked_at')) in (int,float) and 0<=now-value['checked_at']<=10)


def attest(role,config,lease):
    path=Path('/tmp/'+role+'-state.json'); candidate=path.with_suffix('.new')
    fd=os.open(str(candidate),os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(canonical(dict(config=config,dns_context=dns_context(lease),checked_at=time.time())))
    os.replace(str(candidate),str(path))


class EngineService:
    def __init__(self,start=_start): self.manager=EngineSupervisor(start=start);self.hosts=None
    def tick(self,config,lease,now=None):
        try:
            validate_lease(lease,now);validate_config(config)
            if self.hosts is not None:
                for sid,host in HOSTS.items():
                    if self.hosts[host]!=lease['hosts'][host]:
                        previous=self.manager.children.pop(sid,None)
                        if previous: stop_child(previous[1])
            self.manager.reconcile(config)
            self.hosts={host:list(values) for host,values in lease['hosts'].items()}
        except Exception:
            self.stop(); raise
    def stop(self): self.manager.stop();self.hosts=None


def validate_mihomo(argv):
    value=subprocess.run(argv+['-t'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,timeout=15)
    if value.returncode: raise ValueError('auth_configuration_rejected')


class AuthService:
    def __init__(self,directory,start=_start,validate=validate_mihomo,control=None):
        self.directory,self.start,self.validate=directory,start,validate
        self.control=control or MihomoUnixControl(directory/'control.sock')
        self.child,self.fingerprint,self.secret_hash=None,None,None
    def tick(self,secret,config,lease,now=None):
        try:
            secret_hash=hashlib.sha256(validate_secret(secret).encode()).hexdigest()
            fingerprint=auth_fingerprint(config,lease,now)+secret_hash
            if self.child is not None and self.child.poll() is None and fingerprint==self.fingerprint:
                return
            self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            path=self.directory/'config.json'
            value=auth_config(secret,config,lease,now)
            value['external-controller-unix']=str(self.directory/'control.sock')
            candidate=self.directory/'candidate.json'
            fd=os.open(str(candidate),os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
            with os.fdopen(fd,'wb') as stream: stream.write(canonical(value))
            self.validate([MIHOMO,'-d',str(self.directory),'-f',str(candidate)])
            running=self.child is not None and self.child.poll() is None
            if not running or secret_hash!=self.secret_hash:
                self.stop()
                os.replace(str(candidate),str(path))
                self.child=self.start([MIHOMO,'-d',str(self.directory),'-f',str(path)])
            # DNS change reloads hosts/rules, not the shared process. The engine
            # supervisor closes only the affected service's old DNS-bound flows.
            self.control.apply(value)
            if candidate.exists(): os.replace(str(candidate),str(path))
            self.fingerprint,self.secret_hash=fingerprint,secret_hash
        except Exception:
            self.stop(); raise
    def stop(self):
        if self.child is not None: stop_child(self.child)
        self.child,self.fingerprint,self.secret_hash=None,None,None


def run_service(role):
    stopping=[]
    for signum in (signal.SIGTERM,signal.SIGINT):
        signal.signal(signum,lambda *_:stopping.append(True))
    service=EngineService() if role=='engine' else AuthService(Path('/tmp/auth'))
    try:
        while not stopping:
            try:
                config,lease=runtime_files()
                if role=='engine': service.tick(config,lease)
                else:
                    if not any(probe_socks(port) for port in PORTS.values()):
                        raise ValueError('all_handlers_unavailable')
                    service.tick(read_file(RUNTIME/'socks_password',128).decode('ascii'),config,lease)
                if all(probe_socks(port) for port in PORTS.values()):
                    if role=='engine' or probe_socks(1080,read_file(RUNTIME/'socks_password',128).decode('ascii')):
                        attest(role,config,lease)
            except (OSError,ValueError,subprocess.SubprocessError):
                service.stop()  # No bypass and no secret exception tracebacks.
            time.sleep(0.5)
    finally: service.stop()


def run_probe(request):
    if (not isinstance(request,dict) or set(request)!={'host','address','strategy','mode'}
            or request['mode'] not in ('candidate','production','direct')
            or request['host'] not in tuple(HOSTS.values())+(CONTROL_HOST,)):
        raise ValueError('invalid_probe_request')
    mode,strategy,host=request['mode'],request['strategy'],request['host']
    if strategy not in ARGUMENTS: raise ValueError('unreviewed_strategy')
    child=None
    try:
        if mode=='candidate':
            sid=next((sid for sid,value in HOSTS.items() if value==host),'youtube')
            child=_start(engine_argv(sid,strategy))
            deadline=time.monotonic()+2
            while not probe_socks(PORTS[sid]):
                if child.poll() is not None or time.monotonic()>=deadline:
                    raise ValueError('candidate_not_ready')
                time.sleep(0.05)
            return https_probe(host,request['address'],socks_port=PORTS[sid])
        if mode=='production':
            config,lease=runtime_files()
            if lease['hosts'].get(host)!=[request['address']]: raise ValueError('dns_context_changed')
            sid=next(sid for sid,value in HOSTS.items() if value==host)
            if config['selections'][sid]!=strategy: raise ValueError('strategy_not_applied')
            return https_probe(host,request['address'],socks_port=1080,
                               password=read_file(RUNTIME/'socks_password',128).decode('ascii'))
        return https_probe(host,request['address'])
    finally:
        if child is not None: stop_child(child)


def main():
    args=sys.argv[1:]
    if args in (['engine'],['socks']):
        run_service(args[0]); return 0
    if args in (['health'],['state','engine'],['state','socks']):
        config,lease=runtime_files()
        if not all(probe_socks(port) for port in PORTS.values()): return 1
        if args!=['state','engine']:
            secret=read_file(RUNTIME/'socks_password',128).decode('ascii')
            if not probe_socks(1080,secret): return 1
        if args[0]=='state':
            value=json.loads(read_file(Path('/tmp/'+args[1]+'-state.json')))
            if not attestation_matches(value,config,lease=lease): return 1
            print(json.dumps(value))
        return 0
    if len(args)==2 and args[0]=='probe' and len(args[1])<=8192:
        request=json.loads(base64.b64decode(args[1],validate=True))
        print(json.dumps(run_probe(request)),flush=True); return 0
    if len(args)==2 and args[0]=='validate' and len(args[1])<=32768:
        value=json.loads(base64.b64decode(args[1],validate=True))
        if set(value)!={'config','lease'}: return 2
        directory=Path('/tmp/validation'); directory.mkdir(mode=0o700,exist_ok=True)
        path=directory/'config.json'
        fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(canonical(auth_config('x'*48,value['config'],value['lease'])))
        validate_mihomo([MIHOMO,'-d',str(directory),'-f',str(path)])
        print('{"valid":true}'); return 0
    return 2


if __name__=='__main__':
    try: sys.exit(main())
    except Exception:
        print('ANTIDPI_RUNTIME=unavailable',file=sys.stderr); sys.exit(1)
