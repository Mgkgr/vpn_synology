"""Bounded, single-job strategy orchestration using an explicitly trusted host adapter.

An absent adapter is a real capability blocker, not a successful dry run. Adapters
must enforce pinned IPv4/TLS, no redirects, response limits, encrypted backups,
runtime CAS and scoped rollback. A browser can never construct an adapter.
"""

from contextlib import closing
from dataclasses import asdict
import json
import time
import uuid

from . import strategy_catalog as cat
from .protocol import canonical, require_id
from .store import JobConflict, TERMINAL
from .strategy_state import StrategyConflict, fresh, timestamp, validated_observation

BLOCKERS = ('runtime_unverified', 'dns_renewal_unverified', 'namespace_recovery_unverified',
            'service_isolation_unverified', 'production_input_unverified', 'worker_unavailable', 'backup_unavailable')


class StrategyUnavailable(RuntimeError):
    pass


class Halt(RuntimeError):
    pass


def revocation(request):
    return request.action=='strategy_configure' and (not request.settings['enabled'] or request.settings['mode']=='pinned')


class StrategyRunner:
    def __init__(self, state, adapter=None, clock=time.time, sleep=time.sleep):
        self.state, self.jobs, self.adapter = state, state.jobs, adapter
        self.clock, self.sleep = clock, sleep
        with self.jobs._write() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS strategy_runs (job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL, progress TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES jobs(job_id))''')

    def capabilities(self):
        if self.adapter is None:
            return dict(can_check=False, can_apply=False, can_configure=False, blockers=list(BLOCKERS[:5]))
        try:
            value = self.adapter.capabilities()  # Cached host evidence only. No probes in GET.
            checks = value.get('probe_ready') is True
            apply = checks and value.get('apply_ready') is True
            blockers = [item for item in value.get('blockers', []) if item in BLOCKERS]
            if not checks and not blockers: blockers = ['runtime_unverified']
            return dict(can_check=checks, can_apply=apply, can_configure=checks, blockers=blockers)
        except (ValueError, TypeError, OSError, AttributeError):
            return dict(can_check=False, can_apply=False, can_configure=False, blockers=['worker_unavailable'])

    def snapshot(self):
        return dict(self.state.snapshot(self.clock()), capabilities=self.capabilities())

    def submit(self, job_id, request, actor, kind='manual'):
        request = cat.parse_strategy_request(asdict(request))
        if kind not in ('manual','scheduled','confirmation','daily'):
            raise ValueError('invalid_run_kind')
        caps = self.capabilities()
        needed = 'can_apply' if request.action in ('strategy_apply','strategy_rollback') else 'can_check'
        if request.action == 'strategy_configure' and request.settings['enabled'] and request.settings['mode'] == 'auto':
            needed = 'can_apply'
        if not caps[needed] and not revocation(request):
            raise StrategyUnavailable('not_configured')
        if self.state.snapshot(self.clock())['revision'] != request.expected_revision:
            raise JobConflict('revision_changed')
        job = self.jobs.submit(job_id,request,actor,now=self.clock())
        with self.jobs._write() as db:
            # Only host-generated runs get an automatic kind. Never inferred from actor.
            db.execute('INSERT OR IGNORE INTO strategy_runs VALUES(?,?,?)',
                (job_id,kind,canonical(dict(step='queued',completed=0,limit=60,results=[])).decode()))
        return self.job(job_id)

    def enqueue_auto(self, sid, kind):
        if kind not in ('scheduled','confirmation','daily'):
            raise ValueError('invalid_run_kind')
        request = cat.StrategyRequest('strategy_check',cat.service(sid),self.state.snapshot(self.clock())['revision'],{})
        return self.submit(uuid.uuid4().hex, request,'system',kind)['job_id']

    def job(self, job_id):
        result = asdict(self.jobs.get_job(job_id))
        with closing(self.jobs._connect()) as db:
            row = db.execute('SELECT kind,progress FROM strategy_runs WHERE job_id=?',(job_id,)).fetchone()
            if row:
                result['strategy'] = dict(json.loads(row['progress']),kind=row['kind'])
        return result

    def _progress(self, job_id, step, result=None):
        with self.jobs._write() as db:
            row = db.execute('SELECT progress FROM strategy_runs WHERE job_id=?',(job_id,)).fetchone()
            data = json.loads(row[0])
            data['step'] = step
            if result:
                data['results'].append(result)
                data['completed'] = len(data['results'])
            db.execute('UPDATE strategy_runs SET progress=? WHERE job_id=?',(canonical(data).decode(),job_id))

    def _guard(self, job_id, request, started, can_apply=False):
        job = self.jobs.get_job(job_id)
        if job.cancel_requested:
            raise Halt('cancelled')
        now = self.clock()
        if now < started or now-started >= 1200:
            raise Halt('strategy_deadline')
        snapshot = self.state.snapshot(now)
        if request.expected_revision != snapshot['revision']:
            raise Halt('revision_changed')
        caps = self.capabilities()
        if not caps['can_apply' if can_apply else 'can_check'] and not revocation(request):
            raise Halt('strategy_infrastructure_unavailable')
        return snapshot

    def _context(self, request):
        value = self.adapter.comparison_context(request.service_id)
        if not isinstance(value,dict) or set(value) != {'identity','context_id','infrastructure_ok','checked_at'}:
            raise Halt('strategy_infrastructure_unavailable')
        cat.digest(value['identity']); cat.digest(value['context_id']); timestamp(value['checked_at'])
        if (value['infrastructure_ok'] is not True or not 0 <= self.clock()-value['checked_at'] <= 180
                or value['identity'] != self.state.snapshot(self.clock())['identity']):
            raise Halt('strategy_infrastructure_unavailable')
        return value

    def _probe(self, job_id, request, strategy, context, source, started):
        self._guard(job_id,request,started)
        if self.job(job_id)['strategy']['completed'] >= 60:
            raise Halt('strategy_attempt_limit')
        self._progress(job_id,'checking')
        # The adapter has a hard 10s socket/process timeout and 32KiB response cap.
        try:
            result = validated_observation(self.adapter.probe(request.service_id,strategy,context,timeout=10))
        except Exception:
            result = dict(checked_at=self.clock(),verdict='unknown',reason='probe_failed',latency_ms=None,
                          http_status=None,infrastructure_ok=False,identity=context['identity'],context_id=context['context_id'])
        if result['context_id'] != context['context_id'] or not fresh(result,context['identity'],self.clock()):
            result = dict(result,verdict='unknown',reason='invalid_response',infrastructure_ok=False)
        self.state.observe(request.service_id,strategy,result,source,self.clock())
        self._progress(job_id,'checking',dict(result,strategy_id=strategy,source=source))
        return result

    def _three(self, job_id, request, strategy, context, source, started):
        evidence = []
        for index in range(3):
            if index: self._wait(job_id,request,started,10)
            evidence.append(self._probe(job_id,request,strategy,context,source,started))
        return evidence

    def _wait(self, job_id, request, started, seconds):
        # Cancellation becomes visible within one second, even during spacing waits.
        for _ in range(seconds):
            self._guard(job_id,request,started)
            self.sleep(1)

    def _eligible(self, evidence, context):
        return (len(evidence)==3 and all(item['verdict']=='success' and fresh(item,context['identity'],self.clock())
            and item['context_id']==context['context_id'] for item in evidence)
            and all(b['checked_at']-a['checked_at']>=10 for a,b in zip(evidence,evidence[1:])))

    def _unknown_automatic(self, request, kind):
        if kind not in ('scheduled','confirmation'):
            return
        snapshot=self.state.snapshot(self.clock())
        if snapshot['revision']!=request.expected_revision:
            return
        row=next(item for item in snapshot['services'] if item['service_id']==request.service_id)
        if row['strategy_id'] is None or snapshot['identity'] is None:
            return
        result=dict(checked_at=self.clock(),verdict='unknown',reason='probe_failed',latency_ms=None,
            http_status=None,infrastructure_ok=False,identity=snapshot['identity'],context_id='0'*64)
        self.state.observe(request.service_id,row['strategy_id'],result,kind,self.clock())

    def _apply(self, job_id, request, target, context, started, reason, evidence, current_result=None):
        snapshot = self._guard(job_id,request,started,can_apply=True)
        current_context = self._context(request)
        if current_context['context_id'] != context['context_id']:
            raise Halt('strategy_dns_changed')
        selections = {row['service_id']:row['strategy_id'] for row in snapshot['services']}
        if any(value is None for value in selections.values()):
            raise Halt('strategy_infrastructure_unavailable')
        selections[request.service_id] = target
        self.jobs.set_phase(job_id,'backup',now=self.clock())
        self._progress(job_id,'backup')
        backup = self.adapter.backup(selections,request.expected_revision)
        if not isinstance(backup,dict) or backup.get('encrypted') is not True:
            raise Halt('strategy_backup_unavailable')
        token = cat.digest(backup.get('token'))
        self.jobs.save_recovery_plan(job_id,'antidpi',dict(token=token,revision=request.expected_revision))
        self._guard(job_id,request,started,can_apply=True)
        # Backup may take time or span DNS renewal: evidence must still hold at write time.
        if self._context(request)['context_id']!=context['context_id']:
            raise Halt('strategy_dns_changed')
        if not self._eligible(evidence,context):
            raise Halt('strategy_candidate_failed')
        if reason=='automatic' and not self.state.can_replace(request.service_id,target,evidence,current_result,request.expected_revision,self.clock()):
            raise Halt('strategy_candidate_failed')
        # The adapter checks runtime revision again immediately before atomic replace.
        self.jobs.set_phase(job_id,'apply',now=self.clock())
        self.jobs.set_component(job_id,'antidpi')
        self.jobs.begin_effect(job_id,'strategy.apply',now=self.clock())
        try:
            self._progress(job_id,'applying')
            self.adapter.apply(selections,request.expected_revision,token)
            self.jobs.set_phase(job_id,'verify',now=self.clock())
            self._progress(job_id,'verifying')
            result = validated_observation(self.adapter.verify(request.service_id,context,timeout=10))
            if (result['verdict']!='success' or result['context_id']!=context['context_id']
                    or not fresh(result,context['identity'],self.clock())):
                raise Halt('strategy_verification_failed')
            self._guard(job_id,request,started,can_apply=True)
            self.state.record_change(request.service_id,target,request.expected_revision,reason,self.clock(),
                actor=self.jobs.get_job(job_id).actor,mode=request.settings.get('mode'))
            self.jobs.finish_effect(job_id,'strategy.apply',now=self.clock())
            self._progress(job_id,'verified')
            self.jobs.set_phase(job_id,'completed',now=self.clock(),revision_after=self.state.snapshot(self.clock())['revision'])
        except Exception:
            # Never overwrite a newer policy/runtime on rollback. Adapter proves scoped restoration.
            if self.state.snapshot(self.clock())['revision'] != request.expected_revision:
                self.jobs.needs_reconcile(job_id,'identity_changed',now=self.clock())
                return
            self.jobs.set_phase(job_id,'rollback',now=self.clock())
            self._progress(job_id,'rollback')
            try:
                restored = self.adapter.rollback(token,request.expected_revision) is True
            except Exception:
                restored = False
            if not restored:
                self.jobs.needs_reconcile(job_id,'rollback_failed',now=self.clock())
                return
            self.jobs.finish_effect(job_id,'strategy.apply',now=self.clock())
            self.jobs.set_phase(job_id,'failed',now=self.clock(),error_code='strategy_verification_failed')

    def run(self, job_id):
        require_id(job_id)
        request = self.jobs.get_request(job_id)
        if not isinstance(request,cat.StrategyRequest):
            raise ValueError('not_strategy_job')
        if not self.jobs.claim_job(job_id,now=self.clock()):
            return self.job(job_id)
        started = self.clock()
        with self.jobs._write() as db:
            # Crash between submit and run metadata is manual-only, never auto-authorized.
            db.execute('INSERT OR IGNORE INTO strategy_runs VALUES(?,?,?)',
                (job_id,'manual',canonical(dict(step='preflight',completed=0,limit=60,results=[])).decode()))
        kind = self.job(job_id)['strategy']['kind']
        try:
            snapshot = self._guard(job_id,request,started)
            sid = request.service_id
            policy = next(row for row in snapshot['services'] if row['service_id']==sid)
            current = policy['strategy_id']
            if request.action=='strategy_configure':
                if request.settings['enabled'] and request.settings['mode']=='auto':
                    self._guard(job_id,request,started,can_apply=True)
                    self._context(request)
                self.state.configure(sid,request.settings,request.expected_revision,self.jobs.get_job(job_id).actor,self.clock())
            else:
                context = self._context(request)
                if current is None: raise Halt('strategy_infrastructure_unavailable')
                if kind in ('scheduled','confirmation') and not policy['enabled']:
                    raise Halt('strategy_policy_revoked')
                if request.action in ('strategy_apply','strategy_rollback'):
                    target = request.settings.get('strategy_id') if request.action=='strategy_apply' else policy['previous_strategy_id']
                    if target is None or target==current: raise Halt('strategy_no_candidate')
                    evidence = self._three(job_id,request,target,context,'candidate',started)
                    if not self._eligible(evidence,context): raise Halt('strategy_candidate_failed')
                    self._apply(job_id,request,target,context,started,'manual' if request.action=='strategy_apply' else 'rollback',evidence)
                    return self.job(job_id)
                if request.action=='strategy_tune' or kind=='daily':
                    candidates = [current]+[item for item in cat.STRATEGIES if item!=current]
                    if kind=='daily': candidates=candidates[:3]
                    for candidate in candidates:
                        self._three(job_id,request,candidate,context,'daily' if kind=='daily' else 'manual',started)
                else:
                    result = self._probe(job_id,request,current,context,kind,started)
                    if (kind in ('scheduled','confirmation') and self.state.decision(sid,self.clock())=='search'
                            and self.state.mark_search(sid,self.clock())):
                        for candidate in cat.STRATEGIES:
                            if candidate==current: continue
                            evidence=self._three(job_id,request,candidate,context,'candidate',started)
                            if not self._eligible(evidence,context): continue
                            result=self._probe(job_id,request,current,context,'confirmation',started)
                            if self.state.can_replace(sid,candidate,evidence,result,request.expected_revision,self.clock()):
                                self._apply(job_id,request,candidate,context,started,'automatic',evidence,result)
                                return self.job(job_id)
                            break  # Recovered/current context invalid: no competing switcher.
            self._progress(job_id,'finished')
            self.jobs.set_phase(job_id,'completed',now=self.clock(),revision_after=self.state.snapshot(self.clock())['revision'])
        except Halt as error:
            job=self.jobs.get_job(job_id)
            if job.phase not in TERMINAL and job.phase!='needs_reconcile':
                if str(error) in ('strategy_infrastructure_unavailable','strategy_dns_changed'):
                    self._unknown_automatic(request,kind)
                self.jobs.set_phase(job_id,'cancelled' if str(error)=='cancelled' and job.cancel_allowed else 'failed',
                    now=self.clock(),error_code=None if str(error)=='cancelled' else str(error))
        except Exception:
            job=self.jobs.get_job(job_id)
            if job.phase in ('apply','verify','rollback'):
                self.jobs.needs_reconcile(job_id,'worker_interrupted',now=self.clock())
            elif job.phase not in TERMINAL and job.phase!='needs_reconcile':
                self._unknown_automatic(request,kind)
                self.jobs.set_phase(job_id,'failed',now=self.clock(),error_code='strategy_infrastructure_unavailable')
        return self.job(job_id)
