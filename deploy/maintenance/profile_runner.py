"""Single-use profile jobs under the shared durable mutation lock."""
from contextlib import closing
from dataclasses import asdict
import json
import time

from .profile_catalog import ProfileRequest, parse_profile_request, identifier, observation, acceptable
from .protocol import canonical
from .store import JobConflict, TERMINAL


class ProfileUnavailable(RuntimeError): pass
class Halt(RuntimeError): pass


class ProfileRunner:
    def __init__(self,jobs,drafts,adapter=None,clock=time.time):
        self.jobs,self.drafts,self.adapter,self.clock=jobs,drafts,adapter,clock
        with jobs._write() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS profile_runs(job_id TEXT PRIMARY KEY,progress TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(job_id))''')

    def _ready(self):
        if self.adapter is None or self.adapter.ready() is not True: raise ProfileUnavailable('not_configured')

    def stage(self,uri,actor):
        self._ready()
        return self.drafts.stage(uri,actor,self.adapter.revision(),self.clock())

    def snapshot(self,actor):
        self._ready()
        return dict(revision=self.adapter.revision(),current=self.adapter.current(),drafts=self.drafts.list(actor,self.clock()))

    def _draft(self,request,actor,apply=False):
        self._ready()
        try:
            value=self.drafts.load(request.draft_id,actor,self.clock())
            if value['revision']!=request.expected_revision or self.adapter.revision()!=request.expected_revision:
                raise JobConflict('revision_changed')
            if apply and (not self.drafts.public(request.draft_id,actor,self.clock())['check_passed'] or not value.get('endpoint_ip')):
                raise JobConflict('profile_check_required')
            return value
        except (ValueError,FileNotFoundError):
            raise JobConflict('draft_unavailable') from None

    def submit(self,job_id,request,actor):
        request=parse_profile_request(asdict(request))
        # A repeated ID remains query-only even after its secret draft was deleted.
        try: self.jobs.get_job(job_id)
        except JobConflict as error:
            if str(error)!='job_not_found': raise
        else:
            self.jobs.submit(job_id,request,actor,now=self.clock())
            return self.job(job_id)
        self._draft(request,actor,request.action=='profile_apply')
        self.jobs.submit(job_id,request,actor,now=self.clock())
        self._initialize(job_id,request)
        return self.job(job_id)

    def _initialize(self,job_id,request):
        with self.jobs._write() as db:
            db.execute('INSERT OR IGNORE INTO profile_runs VALUES(?,?)',(job_id,canonical(dict(action=request.action,
                step='queued',results=[],completed=0,limit=9 if request.action=='profile_check' else 6)).decode()))

    def job(self,job_id):
        result=asdict(self.jobs.get_job(job_id))
        with closing(self.jobs._connect()) as db:
            row=db.execute('SELECT progress FROM profile_runs WHERE job_id=?',(job_id,)).fetchone()
            if row: result['profile']=json.loads(row[0])
        return result

    def _progress(self,job_id,step,row=None):
        with self.jobs._write() as db:
            data=json.loads(db.execute('SELECT progress FROM profile_runs WHERE job_id=?',(job_id,)).fetchone()[0])
            data['step']=step
            if row is not None:
                if len(data['results'])>=data['limit']: raise ValueError('probe_limit')
                data['results'].append(observation(row)); data['completed']=len(data['results'])
            db.execute('UPDATE profile_runs SET progress=? WHERE job_id=?',(canonical(data).decode(),job_id))

    def _guard(self,job_id,request,started,apply=False):
        job=self.jobs.get_job(job_id)
        if job.cancel_requested: raise Halt('cancelled')
        if not 0<=self.clock()-started<1200: raise Halt('profile_deadline')
        return self._draft(request,job.actor,apply)

    def run(self,job_id):
        request=self.jobs.get_request(job_id)
        if not isinstance(request,ProfileRequest): raise ValueError('not_profile_job')
        if not self.jobs.claim_job(job_id,now=self.clock()): return self.job(job_id)
        self._initialize(job_id,request); started=self.clock()
        try:
            draft=self._guard(job_id,request,started,request.action=='profile_apply')
            self.jobs.set_component(job_id,'mihomo')
            if request.action=='profile_check':
                def report(row):
                    self._guard(job_id,request,started)
                    self._progress(job_id,'checking',row)
                result=self.adapter.preflight(draft['profile'],job_id,report)
                self._guard(job_id,request,started)
                self.drafts.checked(request.draft_id,request.expected_revision,result['results'],self.clock(),result['endpoint_ip'])
                if not acceptable(result['results']): raise Halt('profile_check_failed')
                self._progress(job_id,'verified')
                self.jobs.set_phase(job_id,'completed',now=self.clock(),revision_after=request.expected_revision)
            else:
                self.jobs.set_phase(job_id,'backup',now=self.clock()); self._progress(job_id,'backup')
                backup=self.adapter.backup(draft['profile'],draft['endpoint_ip'],request.expected_revision,job_id)
                if not isinstance(backup,dict) or backup.get('encrypted') is not True: raise Halt('profile_backup_failed')
                token=identifier(backup.get('token'),64)
                self.jobs.save_recovery_plan(job_id,'mihomo',dict(token=token,revision=request.expected_revision))
                self._guard(job_id,request,started,True)
                self.jobs.set_phase(job_id,'apply',now=self.clock()); self._progress(job_id,'applying')
                self.jobs.begin_effect(job_id,'profile.apply',now=self.clock())
                # Lost acknowledgement is not retried or guessed to be a failure.
                self.adapter.apply(token,request.expected_revision)
                self.jobs.set_phase(job_id,'verify',now=self.clock()); self._progress(job_id,'verifying')
                valid=self.adapter.verify(token,lambda row:self._progress(job_id,'verifying',row)) is True
                if valid:
                    self.adapter.persist(token,request.expected_revision)
                    self.jobs.finish_effect(job_id,'profile.apply',now=self.clock())
                    self._progress(job_id,'verified')
                    self.jobs.set_phase(job_id,'completed',now=self.clock(),revision_after=self.adapter.revision())
                    self.drafts.discard(request.draft_id)
                else:
                    self.jobs.set_phase(job_id,'rollback',now=self.clock()); self._progress(job_id,'rollback')
                    if self.adapter.rollback(token,request.expected_revision) is not True:
                        self.jobs.needs_reconcile(job_id,'rollback_failed',now=self.clock())
                    else:
                        self.jobs.finish_effect(job_id,'profile.apply',now=self.clock())
                        self.jobs.set_phase(job_id,'failed',now=self.clock(),error_code='profile_reverted')
        except Exception as error:
            job=self.jobs.get_job(job_id)
            if job.phase in ('apply','verify','rollback'):
                self.jobs.needs_reconcile(job_id,'worker_interrupted',now=self.clock())
            elif job.phase not in TERMINAL and job.phase!='needs_reconcile':
                safe={'cancelled','profile_deadline','profile_check_failed','profile_backup_failed','profile_check_required','revision_changed','draft_unavailable'}
                code=str(error) if isinstance(error,(Halt,JobConflict)) and str(error) in safe else 'profile_unavailable'
                self.jobs.set_phase(job_id,'cancelled' if code=='cancelled' else 'failed',now=self.clock(),error_code=None if code=='cancelled' else code)
        return self.job(job_id)
