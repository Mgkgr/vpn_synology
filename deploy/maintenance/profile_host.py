"""Fixed NAS boundary for isolated probes and acknowledged, scoped Mihomo reloads."""
import base64
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

from .antidpi_host import atomic_private, read_private, digest
from .backups import private_directory, BackupManager, ResticRepository, file_digest
from .backup_cli import load_backup_manifest, source_revision
from .catalog import read_root_json
from .docker_adapter import bounded_run
from .profile_catalog import TARGETS, PROBES, identifier, acceptable
from .profile_links import public_ip, hostname
from .protocol import canonical

CONFIG=Path('/volume1/docker/vpn-gateway/mihomo/config.yaml')
PRIVATE=Path('/volume1/docker/vpn-dashboard-maintenance/private')
DOCKER='/usr/local/bin/docker'


class ProbeCleanupUnconfirmed(RuntimeError): pass


def _blocks(text):
    sections=list(re.finditer(r'(?m)^proxies:[ \t]*(?:#.*)?\r?$',text))
    if len(sections)!=1: raise ValueError('invalid_proxy_section')
    start=sections[0].end()+1
    following=re.search(r'(?m)^[A-Za-z][A-Za-z0-9_-]*:',text[start:])
    end=start+following.start() if following else len(text)
    items=list(re.finditer(r'(?m)^( *)- name:[ \t]*([^\r\n]+)\r?$',text[start:end]))
    result={}
    for index,item in enumerate(items):
        name=item.group(2).strip().strip('"\'')
        if name in result: raise ValueError('duplicate_outbound')
        result[name]=(start+item.start(),start+items[index+1].start() if index+1<len(items) else end,item.group(1))
    if not set(TARGETS)<=set(result): raise ValueError('missing_outbound')
    return result


def replace_outbound(text,profile):
    if (profile['name'],profile['type']) not in (('WG-IMP','vless'),('HY2-USA','hysteria2')):
        raise ValueError('unsupported_outbound')
    start,end,indent=_blocks(text)[profile['name']]
    lines=[indent+'- name: '+profile['name'],indent+'  type: '+profile['type']]
    for key,value in profile.items():
        if key in ('name','type'): continue
        if isinstance(value,dict):
            lines.append(indent+'  '+key+':')
            lines.extend(indent+'    '+child+': '+json.dumps(item) for child,item in value.items())
        else: lines.append(indent+'  '+key+': '+json.dumps(value))
    newline='\r\n' if '\r\n' in text else '\n'
    return text[:start]+newline.join(lines)+newline+text[end:]


def _scalar(block,key):
    rows=re.findall(r'(?m)^ +'+re.escape(key)+r':[ \t]*([^\r\n]+)',block)
    if len(rows)!=1: return None
    value=rows[0].strip()
    try: return json.loads(value)
    except ValueError: return value.strip("'")


def current_profiles(text):
    blocks=_blocks(text); result=[]
    for target in TARGETS:
        start,end,_=blocks[target]; block=text[start:end]
        server,port,protocol=_scalar(block,'server'),_scalar(block,'port'),_scalar(block,'type')
        if server is None or port is None: continue
        if protocol not in ('vless','hysteria2','wireguard'): raise ValueError('unsupported_current_profile')
        if type(port) is not int or not 1<=port<=65535: raise ValueError('invalid_current_port')
        result.append(dict(target=target,protocol=protocol,server=hostname(server),port=port))
    return result


def isolated_config(profile,endpoint_ip,password):
    public_ip(endpoint_ip)
    return {'mixed-port':17891,'bind-address':'0.0.0.0','allow-lan':True,
            'authentication':['check:'+password],'mode':'rule','log-level':'silent','ipv6':False,
            'tun':{'enable':False},'dns':{'enable':False},'profile':{'store-selected':False,'store-fake-ip':False},
            'proxies':[dict(profile,server=endpoint_ip)],'rules':['MATCH,'+profile['name']]}


def isolated_argv(job_id,image,path):
    identifier(job_id)
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',image) or not path.is_absolute(): raise ValueError('invalid_probe_container')
    return [DOCKER,'run','--detach','--rm','--pull=never','--name=vpn-profile-check-'+job_id,
            '--label=vpn.dashboard.profile-check='+job_id,'--network=bridge','--read-only','--cap-drop=ALL',
            '--security-opt=no-new-privileges:true','--user=10002:10002','--memory=128m','--pids-limit=64',
            '--tmpfs=/tmp:rw,noexec,nosuid,size=16m,mode=1777','--log-driver=none',
            '--mount=type=bind,src='+str(path)+',dst=/input.json,readonly',
            '--entrypoint=/mihomo',image,'-d','/tmp','-f','/input.json']


def resolve_public(server,run=bounded_run):
    hostname(server)
    try: return public_ip(server)
    except ValueError: pass
    # Separate process bounds libc DNS lookup; no shell or credentials in argv.
    code='import socket,json,sys; print(json.dumps(sorted({r[4][0] for r in socket.getaddrinfo(sys.argv[1],443,socket.AF_INET,socket.SOCK_STREAM)})))'
    rows=json.loads(run(['/usr/bin/python3','-c',code,server],timeout=10,limit=4096))
    if not isinstance(rows,list) or not 1<=len(rows)<=16: raise ValueError('dns_unavailable')
    return sorted(public_ip(row) for row in rows)[0]


def proxy_probe(address,password,target,context):
    url,expected=PROBES[target]; parts=urlsplit(url)
    connection=http.client.HTTPSConnection(address,17891,timeout=10,context=context)
    authorization=base64.b64encode(('check:'+password).encode()).decode('ascii')
    connection.set_tunnel(parts.hostname,443,headers={'Proxy-Authorization':'Basic '+authorization})
    result=dict(target=target,ok=False,latency_ms=None,http_status=None,reason='probe_failed',checked_at=time.time())
    started=time.monotonic()
    def close():
        try:
            if connection.sock: connection.sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        connection.close()
    timer=threading.Timer(12,close); timer.daemon=True; timer.start()
    try:
        connection.request('GET',parts.path or '/',headers={'Host':parts.hostname,'User-Agent':'vpn-dashboard-profile-check','Connection':'close'})
        response=connection.getresponse(); body=response.read(32769)
        ok=response.status==expected and len(body)<=32768 and time.monotonic()-started<12
        result.update(ok=ok,http_status=response.status,latency_ms=min(30000,int((time.monotonic()-started)*1000)),
                      reason=None if ok else 'http_status' if response.status!=expected else 'invalid_response')
    except (socket.timeout,TimeoutError): result['reason']='timeout'
    except ssl.SSLError: result['reason']='tls_failed'
    except (OSError,http.client.HTTPException): result['reason']='connect_failed'
    finally:
        timer.cancel(); connection.close()
    return result


class Controller:
    def __init__(self,text):
        values=re.findall(r'(?m)^secret:[ \t]*([^\r\n]+)',text)
        if len(values)!=1: raise ValueError('controller_secret_unavailable')
        self.secret=values[0].strip().strip('"\'')
        if not re.fullmatch(r'[A-Za-z0-9_\-]{16,256}',self.secret): raise ValueError('invalid_controller_secret')

    def request(self,path,payload=None):
        connection=http.client.HTTPConnection('192.168.2.103',9091,timeout=60 if payload else 14)
        try:
            connection.request('PUT' if payload is not None else 'GET',path,
                body=canonical(payload) if payload is not None else None,
                headers={'Authorization':'Bearer '+self.secret,'Content-Type':'application/json'})
            response=connection.getresponse(); data=response.read(524289)
            if response.status not in (200,204) or len(data)>524288: raise ValueError('controller_request_failed')
            return json.loads(data) if data else {}
        finally: connection.close()

    def apply(self,text): self.request('/configs',dict(payload=text))

    def state(self,target):
        proxies=self.request('/proxies')['proxies']
        group=proxies['VPS-FALLBACK']; companion=next(name for name in TARGETS if name!=target)
        rules=self.request('/rules')['rules']
        stable=[{key:row.get(key) for key in ('type','payload','proxy')} for row in rules]
        return dict(order=group.get('all'),group_type=group.get('type'),companion_type=proxies[companion]['type'],
                    rules_hash=digest(canonical(stable)))

    def probe(self,target,key):
        path='/proxies/'+quote('DASH-HEALTH-'+target,safe='')
        def validate():
            value=self.request(path)
            if (value.get('type')!='Selector' or value.get('all')!=[target] or value.get('now')!=target
                    or value.get('hidden') is not True or value.get('emptyFallback')!='REJECT'):
                raise ValueError('diagnostic_wrapper_changed')
        validate(); url,expected=PROBES[key]
        row=dict(target=key,ok=False,latency_ms=None,http_status=None,reason='probe_failed',checked_at=time.time())
        try:
            result=self.request(path+'/delay?'+urlencode(dict(url=url,timeout=10000,expected=expected)))
            delay=result.get('delay')
            if type(delay) is int and 0<delay<=10000:
                row.update(ok=True,latency_ms=delay,http_status=expected,reason=None)
        except (OSError,ValueError,http.client.HTTPException): pass
        validate()
        return row


class ProfileHost:
    def __init__(self,jobs,config=CONFIG,private=PRIVATE,run=bounded_run):
        self.jobs,self.config,self.private,self.run=jobs,config,private,run

    def ready(self):
        try:
            contract=read_root_json(self.private/'profile-host.json')
            return (set(contract)=={'version','image_id'} and contract['version']==1
                    and bool(re.fullmatch(r'sha256:[a-f0-9]{64}',contract['image_id'])))
        except (OSError,ValueError,KeyError,TypeError): return False

    def _command(self,*args,timeout=40): return self.run([DOCKER,*args],timeout=timeout,limit=262144)

    def _text(self): return read_private(self.config,524288).decode('utf-8')

    def revision(self):
        files={str(self.config.name):digest(read_private(self.config,524288))}
        rules=self.config.parent/'rules'
        if rules.exists():
            paths=sorted(rules.glob('*.txt'))
            if len(paths)>32: raise ValueError('rules_limit')
            files.update({path.name:digest(read_private(path,2097152)) for path in paths})
        return digest(canonical(files))

    def current(self): return current_profiles(self._text())
    def controller(self): return Controller(self._text())

    def _image(self):
        image=read_root_json(self.private/'profile-host.json')['image_id']
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',image): raise ValueError('image_not_pinned')
        actual=self._command('inspect','--format','{{.Image}}|{{index .Config.Labels "com.docker.compose.project"}}|{{.State.Running}}','vpn-mihomo').strip()
        if actual!=image+'|vpn-gateway|true': raise ValueError('mihomo_identity_changed')
        return image

    def cleanup_probe(self,job_id):
        name='vpn-profile-check-'+identifier(job_id)
        def present():
            return self._command('ps','-aq','--filter','name=^/'+name+'$').strip()
        try:
            if not present(): return
            label=self._command('inspect','--format','{{index .Config.Labels "vpn.dashboard.profile-check"}}',name).strip()
            if label!=job_id: raise ValueError('probe_not_owned')
            self._command('rm','-f',name,timeout=20)
            if present(): raise ValueError('probe_removal_unconfirmed')
        except Exception:
            raise ProbeCleanupUnconfirmed('probe_cleanup_unconfirmed') from None

    def preflight(self,profile,job_id,report):
        identifier(job_id); image=self._image(); endpoint=resolve_public(profile['server'],self.run)
        private_directory(self.private/'profile-probes')
        path=self.private/'profile-probes'/(job_id+'.json')
        if path.exists(): raise ValueError('probe_already_exists')
        password=secrets.token_urlsafe(32)
        atomic_private(path,canonical(isolated_config(profile,endpoint,password)),owner=10002)
        name='vpn-profile-check-'+job_id; container_id=None
        context=ssl.create_default_context(); context.set_alpn_protocols(['http/1.1'])
        try:
            container_id=self.run(isolated_argv(job_id,image,path),timeout=30,limit=4096).strip()
            if not re.fullmatch(r'[a-f0-9]{64}',container_id): raise ValueError('probe_start_unconfirmed')
            address=self._command('inspect','--format','{{.NetworkSettings.IPAddress}}',container_id).strip()
            if not ipaddress.IPv4Address(address).is_private: raise ValueError('unsafe_probe_network')
            for attempt in range(20):
                try:
                    with socket.create_connection((address,17891),timeout=.2): break
                except OSError:
                    if attempt==19: raise ValueError('probe_not_ready')
                    time.sleep(.25)
            rows=[]
            for _ in range(3):
                for target in PROBES:
                    row=proxy_probe(address,password,target,context); report(row); rows.append(row)
            return dict(results=rows,endpoint_ip=endpoint)
        finally:
            # Exact owned name+label, including a lost docker-run acknowledgement.
            try: self.cleanup_probe(job_id)
            finally:
                if path.exists(): path.unlink()

    def _record(self,token):
        identifier(token,64)
        return self.private/'profile-recovery'/(token+'.json')

    def _load(self,token): return read_root_json(self._record(token))

    def _cas(self,revision):
        if self.revision()!=revision: raise ValueError('revision_changed')

    def backup(self,profile,endpoint_ip,revision,job_id):
        self._cas(revision); self._image()
        before=self._text(); candidate=replace_outbound(before,dict(profile,server=public_ip(endpoint_ip)))
        path=self.config.parent/('.profile-check-'+identifier(job_id)+'.yaml')
        if path.exists(): raise ValueError('validation_file_exists')
        atomic_private(path,candidate.encode('utf-8'))
        try:
            self._command('exec','-u','0','vpn-mihomo','/mihomo','-t','-d','/root/.config/mihomo',
                          '-f','/root/.config/mihomo/'+path.name)
        finally: path.unlink()
        manifest=load_backup_manifest(self.private)
        repo=ResticRepository(manifest['repository'],manifest['password_file'],self.private)
        identity=next(row.path for row in manifest['_sources'] if row.name==manifest['identity_source'])
        def held(job):
            lock=self.jobs.lock_state()
            return lock.get('kind')=='job' and lock.get('state')=='held' and lock.get('token')==job
        manager=BackupManager(self.private,repo,manifest['_sources'],revision=lambda:source_revision(manifest),
            image_digests=lambda:manifest['images'],identity_digest=lambda:file_digest(identity),lock_check=held)
        backup=manager.create(tuple(manifest['components']),job_id)
        if backup.verified_at is None: raise ValueError('backup_unverified')
        self._cas(revision)
        state=self.controller().state(profile['name'])
        if state['order']!=list(TARGETS) or state['group_type']!='Fallback': raise ValueError('fallback_changed')
        token=secrets.token_hex(32); private_directory(self.private/'profile-recovery')
        atomic_private(self._record(token),canonical(dict(before=before,candidate=candidate,revision=revision,
            target=profile['name'],verified=False,before_state=state,source_server=profile['server'],snapshot_id=backup.snapshot_id)))
        return dict(encrypted=True,token=token)

    def apply(self,token,revision):
        record=self._load(token); self._cas(revision)
        if record['revision']!=revision or self._text()!=record['before']: raise ValueError('revision_changed')
        self.controller().apply(record['candidate'])

    def verify(self,token,report):
        record=self._load(token); controller=self.controller(); self._cas(record['revision'])
        if controller.state(record['target'])!=record['before_state']: return False
        rows=[]
        for _ in range(2):
            for target in PROBES:
                row=controller.probe(record['target'],target); report(row); rows.append(row)
        self._cas(record['revision'])
        valid=acceptable(rows,2,2) and controller.state(record['target'])==record['before_state']
        if valid:
            record['verified']=True
            atomic_private(self._record(token),canonical(record))
        return valid

    def persist(self,token,revision):
        record=self._load(token); self._cas(revision)
        if record.get('verified') is not True or record['revision']!=revision: raise ValueError('runtime_not_verified')
        info=self.config.stat()
        atomic_private(self.config,record['candidate'].encode('utf-8'))
        if os.name=='posix':
            os.chown(str(self.config),info.st_uid,info.st_gid); os.chmod(str(self.config),info.st_mode&0o777)

    def rollback(self,token,revision):
        record=self._load(token)
        if self.revision()!=revision or self._text()!=record['before']: return False
        controller=self.controller(); controller.apply(record['before'])
        return self.revision()==revision and controller.state(record['target'])==record['before_state']
