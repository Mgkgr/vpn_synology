"""Persistent strategy worker; UnixServer owns the process lock and recovery gate."""
from .strategy_schedule import StrategySchedule
from .strategy_catalog import StrategyRequest
from .profile_catalog import ProfileRequest


class JobLoop:
    def __init__(self,runner,profiles=None):
        self.runner=runner
        self.profiles=profiles
        self.schedule=StrategySchedule(runner)

    def tick(self,now):
        # Only the single durable mutation-lock owner can be runnable.
        lock=self.runner.jobs.lock_state(now)
        if lock.get('kind')=='job' and lock.get('state')=='held':
            job=self.runner.jobs.get_job(lock['token'])
            if job.phase=='queued' and self.profiles and isinstance(self.runner.jobs.get_request(job.job_id),ProfileRequest):
                return self.profiles.run(job.job_id)
            if job.phase=='queued' and isinstance(self.runner.jobs.get_request(job.job_id),StrategyRequest):
                return self.runner.run(job.job_id)
            return None
        if lock['state']!='free': return None
        job_id=self.schedule.tick(now)
        return self.runner.run(job_id) if job_id else None
