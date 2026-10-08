"""Concrete bounded Docker transport. No container/path/command comes from IPC."""
import base64
import ipaddress
import json
import re
import time
import uuid

from antidpi.production import HOSTS,CONTROL_HOST,ARGUMENTS,canonical
from antidpi.runtime_service import attestation_matches
from .docker_adapter import bounded_run

CODE='/volume1/docker/vpn-dashboard-maintenance/app'
DOCKER='/usr/local/bin/docker'


class DockerRuntime:
    def __init__(self,image,code,control,runner=bounded_run):
        if code!=CODE or not re.fullmatch(r'sha256:[a-f0-9]{64}',image):
            raise ValueError('invalid_runtime_transport')
        self.image,self.control,self.runner=image,control,runner

    def _run(self,args,timeout=20):
        return self.runner([DOCKER]+args,timeout=timeout,limit=262144)

    def _exec(self,role,*args,timeout=20):
        if role not in ('engine','socks'): raise ValueError('invalid_role')
        container='vpn-antidpi' if role=='engine' else 'vpn-antidpi-socks'
        return json.loads(self._run(['exec',container,'python3','-B','-m','antidpi.runtime_service',*args],timeout))

    def healthy(self,config):
        try:
            if self.control is None: return False
            rows=self.control.inventory()
            if set(rows)!={'antidpi','socks'} or any(not row['running'] or row['namespace_stale'] for row in rows.values()):
                return False
            return all(attestation_matches(self._exec(role,'state',role,timeout=5),config)
                       for role in ('engine','socks'))
        except Exception: return False

    def wait_applied(self,config):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if self.healthy(config): return True
            time.sleep(0.5)
        return False

    def validate(self,config,lease):
        encoded=base64.b64encode(canonical(dict(config=config,lease=lease))).decode('ascii')
        if self._exec('socks','validate',encoded).get('valid') is not True:
            raise ValueError('runtime_validation_failed')

    def probe(self,host,address,strategy,mode):
        if (host not in tuple(HOSTS.values())+(CONTROL_HOST,) or strategy not in ARGUMENTS
                or mode not in ('candidate','production','direct')):
            raise ValueError('unapproved_probe')
        ip=ipaddress.IPv4Address(address)
        if not ip.is_global or ip.is_reserved or ip.is_multicast or ip in ipaddress.IPv4Network('192.0.0.0/24'):
            raise ValueError('unapproved_address')
        encoded=base64.b64encode(canonical(dict(host=host,address=address,strategy=strategy,mode=mode))).decode('ascii')
        if mode=='production': return self._exec('socks','probe',encoded,timeout=15)
        name='vpn-antidpi-probe-'+uuid.uuid4().hex
        try:
            args=['run','--rm','--name',name,'--label','vpn.antidpi.scope=strategy-probe',
                  '--network','bridge','--read-only','--user','10002:10002','--cap-drop','ALL',
                  '--security-opt','no-new-privileges:true','--memory','256m',
                  '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777',
                  '--env','PYTHONPATH=/opt/vpn','--env','PYTHONDONTWRITEBYTECODE=1',
                  '--volume',CODE+':/opt/vpn:ro','--entrypoint','python3',self.image,
                  '-B','-m','antidpi.runtime_service','probe',encoded]
            return json.loads(self._run(args,timeout=15))
        finally:
            try: self._run(['rm','-f',name],timeout=10)
            except Exception: pass  # Reconciler removes only this fixed labelled probe namespace.
