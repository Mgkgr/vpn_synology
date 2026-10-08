"""Runtime-CAS adapter. Transports/archive are constructed by the root host only."""
import json
from pathlib import Path
import secrets
import threading
import time

from antidpi.production import HOSTS, CONTROL_HOST, validate_config, validate_lease, canonical
from .antidpi_host import read_private, atomic_private, replace_config, digest, capabilities
from .strategy_state import validated_observation


class ProductionAdapter:
    def __init__(self,state,private,runtime,transport,archive,identity,clock=time.time):
        self.state,self.private,self.runtime=state,Path(private),Path(runtime)
        self.transport,self.archive,self.identity,self.clock=transport,archive,identity,clock
        self._cached=dict(probe_ready=False,apply_ready=False,blockers=['runtime_unverified'])
        self._observed=0; self._mutex=threading.RLock(); self._cache_mutex=threading.Lock()

    def config(self):
        value=validate_config(json.loads(read_private(self.runtime/'selection.json')))
        if value['identity']!=self.identity: raise ValueError('runtime_identity_changed')
        return value

    def lease(self):
        return validate_lease(json.loads(read_private(self.runtime/'dns.json')),self.clock())

    def refresh(self):
        try:
            config=self.config(); self.lease()
            runtime_ok=self.transport.healthy(config) is True
            try: evidence=json.loads(read_private(self.private/'antidpi-acceptance.json'))
            except (OSError,ValueError): evidence={}
            value=capabilities(evidence,self.identity,runtime_ok=runtime_ok,now=self.clock())
        except Exception:
            value=dict(probe_ready=False,apply_ready=False,blockers=['runtime_unverified'])
        with self._cache_mutex: self._cached=value; self._observed=self.clock()

    def capabilities(self):
        with self._cache_mutex:
            if not 0<=self.clock()-self._observed<=90:
                return dict(probe_ready=False,apply_ready=False,blockers=['worker_unavailable'])
            return dict(self._cached,blockers=list(self._cached['blockers']))

    def _context_id(self,sid,lease):
        if sid not in HOSTS: raise ValueError('invalid_service')
        return digest(canonical(dict(identity=self.identity,service=sid,
            source_revision=lease['source_revision'],address=lease['hosts'][HOSTS[sid]][0])))

    def comparison_context(self,sid):
        lease=self.lease(); config=self.config()
        control=self.transport.probe(CONTROL_HOST,lease['hosts'][CONTROL_HOST][0],'tlsrec-sni','direct')
        latest=self.lease()
        valid=(self._context_id(sid,lease)==self._context_id(sid,latest)
               and control.get('verdict')=='success' and self.transport.healthy(config))
        return dict(identity=self.identity,context_id=self._context_id(sid,latest),
                    infrastructure_ok=bool(valid),checked_at=self.clock())

    def _check_context(self,sid,context):
        lease=self.lease()
        if (not isinstance(context,dict) or context.get('identity')!=self.identity
                or context.get('context_id')!=self._context_id(sid,lease)):
            raise ValueError('dns_context_changed')
        return lease

    def _measurement(self,sid,strategy,context,mode):
        lease=self._check_context(sid,context)
        result=self.transport.probe(HOSTS[sid],lease['hosts'][HOSTS[sid]][0],strategy,mode)
        self._check_context(sid,context)  # The lease must also survive the request.
        return validated_observation(dict(result,identity=self.identity,context_id=context['context_id'],
                                          infrastructure_ok=True,checked_at=self.clock()))

    def probe(self,sid,strategy,context,timeout):
        if timeout!=10: raise ValueError('invalid_timeout')
        mode='production' if self.config()['selections'][sid]==strategy else 'candidate'
        return self._measurement(sid,strategy,context,mode)

    def verify(self,sid,context,timeout):
        if timeout!=10: raise ValueError('invalid_timeout')
        config=self.config()
        if self.transport.wait_applied(config) is not True: raise ValueError('runtime_not_applied')
        return self._measurement(sid,config['selections'][sid],context,'production')

    def _revision(self,expected):
        if self.state.snapshot(self.clock())['revision']!=expected:
            raise ValueError('policy_revision_changed')

    def backup(self,selections,expected_revision):
        with self._mutex:
            self._revision(expected_revision)
            before=self.config(); candidate=dict(before,selections=selections,generation=secrets.token_hex(32))
            validate_config(candidate)
            if sum(before['selections'][sid]!=selections[sid] for sid in HOSTS)!=1:
                raise ValueError('exactly_one_service_required')
            self.transport.validate(candidate,self.lease())
            before_hash=digest(read_private(self.runtime/'selection.json'))
            snapshot=self.archive.create(before)  # Concrete archive verifies decrypted files before return.
            if not isinstance(snapshot,str) or len(snapshot)!=64 or any(c not in '0123456789abcdef' for c in snapshot):
                raise ValueError('encrypted_backup_unverified')
            self._revision(expected_revision)
            if digest(read_private(self.runtime/'selection.json'))!=before_hash:
                raise ValueError('runtime_revision_changed')
            token=secrets.token_hex(32)
            record=dict(before=before,before_hash=before_hash,after=candidate,after_hash=digest(canonical(candidate)),
                        revision=expected_revision,snapshot=snapshot)
            atomic_private(self.private/('antidpi-recovery-'+token+'.json'),canonical(record))
            return dict(encrypted=True,token=token)

    def _record(self,token,expected):
        if not isinstance(token,str) or len(token)!=64 or any(c not in '0123456789abcdef' for c in token):
            raise ValueError('invalid_recovery_token')
        record=json.loads(read_private(self.private/('antidpi-recovery-'+token+'.json')))
        if record['revision']!=expected: raise ValueError('recovery_revision_changed')
        validate_config(record['before']); validate_config(record['after'])
        return record

    def apply(self,selections,expected_revision,token):
        with self._mutex:
            self._revision(expected_revision); record=self._record(token,expected_revision)
            if selections!=record['after']['selections']: raise ValueError('recovery_selection_changed')
            self.lease()
            replace_config(self.runtime/'selection.json',record['after'],record['before_hash'])
            if self.transport.wait_applied(record['after']) is not True: raise ValueError('runtime_not_applied')

    def rollback(self,token,expected_revision):
        with self._mutex:
            try:
                self._revision(expected_revision); record=self._record(token,expected_revision)
                current=read_private(self.runtime/'selection.json')
                if json.loads(current)==record['before']:
                    return self.transport.wait_applied(record['before']) is True
                if digest(current)!=record['after_hash']: return False
                replace_config(self.runtime/'selection.json',record['before'],record['after_hash'])
                return self.transport.wait_applied(record['before']) is True
            except (OSError,ValueError,KeyError): return False
