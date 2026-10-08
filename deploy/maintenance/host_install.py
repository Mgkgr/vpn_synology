"""One-time root installation of a separate runtime. Never edits gateway routes.

Input archive is source-only, pinned by the caller's SHA256. Existing installation
drift is a stop condition, not permission to recursively overwrite directories.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import time
import urllib.parse

from .antidpi_host import atomic_private,compose_runtime,digest,read_private
from .backups import no_symlink,private_directory,file_digest
from .backup_cli import run as backup_run,PRIVATE
from .docker_adapter import bounded_run
from .docker_control import CONTROL_FORMAT,runtime_hash
from .host_service import validate_controller,renew_dns
from antidpi.production import canonical,validate_config

ROOT=Path('/volume1/docker/vpn-dashboard-maintenance')
CODE=ROOT/'app'
RUNTIME=Path('/volume1/docker/vpn-antidpi/runtime')
UNIT=Path('/etc/systemd/system/vpn-maintenance.service')
DOCKER='/usr/local/bin/docker'


def read_release(path):
    result={}; total=0
    with tarfile.open(path,'r:') as archive:
        for member in archive.getmembers():
            if (not member.isfile() or not re.fullmatch(r'deploy/(?:maintenance/[a-z_]+\.py|antidpi/(?:[a-z_]+\.py|versions\.json))',member.name)
                    or not 0<=member.size<=1048576 or member.name[7:] in result):
                raise ValueError('invalid_release_member')
            total+=member.size
            if total>4194304: raise ValueError('release_too_large')
            data=archive.extractfile(member).read(1048577)
            data.decode('utf-8')
            result[member.name[7:]]=data
    if not result: raise ValueError('empty_release')
    return result


def controller_contract(env_text,wireguard_ip):
    values={}
    for line in env_text.splitlines():
        if '=' not in line or line.lstrip().startswith('#'): continue
        key,value=line.split('=',1)
        if key in ('MIHOMO_URL','MIHOMO_API_SECRET','MIHOMO_API_SECRET_FILE'):
            if key in values: raise ValueError('ambiguous_controller_setting')
            if len(value)>=2 and value[0]==value[-1] and value[0] in ('"',"'"): value=value[1:-1]
            values[key]=value
    if 'MIHOMO_API_SECRET_FILE' in values:
        if values['MIHOMO_API_SECRET_FILE']!='/run/secrets/mihomo_api_secret' or 'MIHOMO_API_SECRET' in values:
            raise ValueError('ambiguous_controller_secret')
        values['MIHOMO_API_SECRET']=read_private(Path('/volume1/docker/vpn-dashboard/deploy/secrets/mihomo_api_secret'),4096).decode('utf-8').strip()
    if not {'MIHOMO_URL','MIHOMO_API_SECRET'}<=set(values): raise ValueError('missing_controller_setting')
    parts=urllib.parse.urlsplit(values['MIHOMO_URL'])
    if parts.hostname in ('vpn-wireguard','vpn-mihomo'):
        if parts.username or parts.password: raise ValueError('invalid_controller_setting')
        url=urllib.parse.urlunsplit((parts.scheme,str(ipaddress.IPv4Address(wireguard_ip))+':'+str(parts.port),parts.path,parts.query,parts.fragment))
    else: url=values['MIHOMO_URL']
    return validate_controller(dict(url=url,secret=values['MIHOMO_API_SECRET']))


def _command(*args,timeout=60): return bounded_run(list(args),timeout=timeout,limit=262144)


def activate_unit():
    # DSM ships systemd 219: enable --now is not supported there.
    _command('/bin/systemctl','daemon-reload')
    _command('/bin/systemctl','enable','vpn-maintenance.service')
    _command('/bin/systemctl','start','vpn-maintenance.service')


def write_source(path,data):
    fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o444)
    with os.fdopen(fd,'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.chmod(str(path),0o444)  # Explicitly independent of the root shell's umask.


def install(archive,expected_hash):
    if os.name!='posix' or os.geteuid()!=0: raise ValueError('linux_root_required')
    if not re.fullmatch(r'[a-f0-9]{64}',expected_hash) or file_digest(archive)!=expected_hash:
        raise ValueError('release_hash_changed')
    files=read_release(archive)
    required={'maintenance/host_service.py','maintenance/worker.py','antidpi/runtime_service.py','antidpi/versions.json'}
    if not required<=set(files): raise ValueError('incomplete_release')
    versions=json.loads(files['antidpi/versions.json'])
    for name in ('engine_image_id','socks_image_id'):
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',versions[name]): raise ValueError('unverified_image')
        actual=_command(DOCKER,'image','inspect','--format','{{.Id}}',versions[name]).strip()
        if actual!=versions[name]: raise ValueError('image_changed')
    # First installation only. Never silently replace a running worker/strategy store.
    for path in (CODE, RUNTIME.parent, UNIT, PRIVATE/'antidpi-host.json'):
        no_symlink(path)
        if path.exists(): raise ValueError('existing_installation_requires_review')
    occupied=_command(DOCKER,'ps','-a','--format','{{.Names}}').splitlines()
    if set(occupied)&{'vpn-antidpi','vpn-antidpi-socks'}: raise ValueError('container_name_conflict')
    before=_command(DOCKER,'inspect','--format','{{.Name}}|{{.State.StartedAt}}|{{.RestartCount}}','vpn-mihomo','vpn-wireguard')
    backup=backup_run('backup')  # Fresh full encrypted backup + actual restore verification.
    print('ANTIDPI_BACKUP=verified',flush=True)
    code_hashes={name:digest(data) for name,data in files.items() if name.endswith('.py')}
    identity=digest(canonical(dict(code=code_hashes,engine=versions['engine_image_id'],auth=versions['socks_image_id'])))
    # Private parent was already validated by the existing backup implementation.
    CODE.mkdir(mode=0o755)
    for name,data in files.items():
        path=CODE/name; path.parent.mkdir(mode=0o755,exist_ok=True)
        write_source(path,data)
    RUNTIME.parent.mkdir(mode=0o700); RUNTIME.mkdir(mode=0o755)
    os.chmod(CODE,0o755); os.chmod(RUNTIME,0o755)
    for directory in (CODE/'antidpi',CODE/'maintenance'): os.chmod(directory,0o755)
    config=validate_config(dict(version=1,identity=identity,generation=secrets.token_hex(32),
        selections=dict(youtube='tlsrec-sni',discord='tlsrec-sni',telegram='tlsrec-sni',instagram='disorder-sni')))
    atomic_private(RUNTIME/'selection.json',canonical(config),owner=10002)
    atomic_private(RUNTIME/'socks_password',secrets.token_urlsafe(48).encode('ascii'),owner=10002)
    address=_command(DOCKER,'inspect','--format','{{(index .NetworkSettings.Networks "vpn-gateway_default").IPAddress}}','vpn-wireguard').strip()
    env=read_private(Path('/volume1/docker/vpn-dashboard/deploy/dashboard.env')).decode('utf-8')
    atomic_private(PRIVATE/'antidpi-controller.json',canonical(controller_contract(env,address)))
    renew_dns()
    compose=compose_runtime(str(CODE),str(RUNTIME),versions['engine_image_id'],versions['socks_image_id'])
    compose_path=PRIVATE/'antidpi-compose.json'
    atomic_private(compose_path,canonical(compose))
    _command(DOCKER,'compose','-p','vpn-antidpi','-f',str(compose_path),'config','--quiet')
    _command(DOCKER,'compose','-p','vpn-antidpi','-f',str(compose_path),'up','-d','--no-build','--pull','never',timeout=120)
    contracts={}
    for service,name in (('antidpi','vpn-antidpi'),('socks','vpn-antidpi-socks')):
        row=json.loads(_command(DOCKER,'inspect','--format',CONTROL_FORMAT,name))
        if row['project']!='vpn-antidpi' or row['service']!=service: raise ValueError('container_identity_changed')
        contracts[service]=dict(project='vpn-antidpi',image_id=row['image_id'],runtime_hash=runtime_hash(row),
                               files=[dict(path=str(compose_path),sha256=file_digest(compose_path))])
    manifest=dict(identity=identity,code=code_hashes,engine_image=versions['engine_image_id'],contracts=contracts,
                  installed_at=time.time(),backup_snapshot=backup['snapshot_id'])
    atomic_private(PRIVATE/'antidpi-host.json',canonical(manifest))
    evidence=dict(identity=identity,verified_at=time.time(),checks={key:False for key in
        ('runtime','dns_renewal','namespace_recovery','service_isolation','production_input','backup')})
    evidence['checks']['backup']=True
    atomic_private(PRIVATE/'antidpi-acceptance.json',canonical(evidence))
    unit=('''[Unit]\nDescription=VPN scoped strategy maintenance worker\nAfter=network-online.target\nRequiresMountsFor=/volume1/docker/vpn-dashboard-maintenance\n\n'''
          '''[Service]\nType=simple\nUser=root\nUMask=0077\nWorkingDirectory=/volume1/docker/vpn-dashboard-maintenance/app\nEnvironment=PYTHONPATH=/volume1/docker/vpn-dashboard-maintenance/app\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nExecStart=/usr/bin/python3 -B -m maintenance.host_service\nRestart=on-failure\nRestartSec=15\n\n'''
          '''[Install]\nWantedBy=multi-user.target\n''')
    atomic_private(UNIT,unit.encode('utf-8'))
    activate_unit()
    after=_command(DOCKER,'inspect','--format','{{.Name}}|{{.State.StartedAt}}|{{.RestartCount}}','vpn-mihomo','vpn-wireguard')
    if before!=after: raise ValueError('working_vpn_changed_during_install')
    return dict(installed=True,automation_enabled=False,client_routes_changed=False,working_vpn_unchanged=True)


if __name__=='__main__':
    try:
        if len(sys.argv)!=3: raise ValueError('archive_and_hash_required')
        print(json.dumps(install(Path(sys.argv[1]),sys.argv[2])),flush=True)
    except Exception as error:
        # Only our closed ValueError codes; never raw Docker/env/archive tracebacks.
        message=str(error) if type(error) is ValueError and re.fullmatch(r'[a-z_]{1,80}',str(error)) else type(error).__name__
        print('ANTIDPI_INSTALL=failed; stage='+message,flush=True); sys.exit(1)
