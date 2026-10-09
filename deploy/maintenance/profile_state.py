"""Root-private, expiring import drafts; the public database gets references only."""
import json
import os
from pathlib import Path
import stat
import threading
import uuid

from .antidpi_host import atomic_private, read_private
from .backups import private_directory
from .profile_catalog import identifier, timestamp, observation, acceptable
from .profile_links import parse_link, safe_summary
from .profile_links import public_ip
from .protocol import canonical


class ProfileDrafts:
    def __init__(self, root):
        self.root = Path(root)
        private_directory(self.root)
        self.lock = threading.RLock()

    def _path(self, draft_id):
        private_directory(self.root)
        return self.root/(identifier(draft_id)+'.json')

    def _read(self, draft_id):
        path = self._path(draft_id)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (os.name=='posix' and (info.st_uid!=0 or info.st_mode&0o077)):
            raise ValueError('unsafe_draft')
        return json.loads(read_private(path, 32768))

    def _prune(self, now):
        timestamp(now)
        for path in self.root.glob('*.json'):
            value = self._read(path.stem)
            if now>=value['expires_at']: path.unlink()

    def stage(self, uri, actor, revision, now):
        identifier(revision,64); timestamp(now)
        if not isinstance(actor,str) or not 1<=len(actor)<=64 or not actor.isprintable(): raise ValueError('invalid_actor')
        profile = parse_link(uri)
        with self.lock:
            self._prune(now)
            if len(list(self.root.glob('*.json')))>=8: raise ValueError('draft_limit')
            draft_id = uuid.uuid4().hex
            value = dict(draft_id=draft_id,actor=actor,revision=revision,created_at=now,expires_at=now+1800,
                         profile=profile,checked_at=None,results=[],endpoint_ip=None)
            atomic_private(self._path(draft_id),canonical(value))
            return self.public(draft_id,actor,now)

    def load(self, draft_id, actor, now):
        timestamp(now)
        with self.lock:
            value = self._read(draft_id)
            if value['actor']!=actor or not value['created_at']<=now<value['expires_at']:
                raise ValueError('draft_unavailable')
            return value

    def checked(self, draft_id, revision, results, now, endpoint_ip=None):
        timestamp(now)
        if not isinstance(results,list) or len(results)!=9: raise ValueError('invalid_probe_count')
        rows = [observation(row) for row in results]
        if any(not 0<=now-row['checked_at']<=300 for row in rows): raise ValueError('invalid_probe_time')
        with self.lock:
            value = self._read(draft_id)
            if value['revision']!=revision or not value['created_at']<=now<value['expires_at']:
                raise ValueError('draft_changed')
            value.update(checked_at=now,results=rows,endpoint_ip=public_ip(endpoint_ip) if endpoint_ip else None)
            atomic_private(self._path(draft_id),canonical(value))

    def public(self, draft_id, actor, now):
        value = self.load(draft_id,actor,now)
        checked_at = value['checked_at']
        passed = checked_at is not None and 0<=now-checked_at<=300 and acceptable(value['results'])
        return dict(safe_summary(value['profile']), draft_id=draft_id, revision=value['revision'],
                    created_at=value['created_at'], expires_at=value['expires_at'], checked_at=checked_at,
                    check_passed=passed, results=value['results'], endpoint_ip=value.get('endpoint_ip'))

    def list(self, actor, now):
        with self.lock:
            self._prune(now)
            return [self.public(path.stem,actor,now) for path in sorted(self.root.glob('*.json'))
                    if self._read(path.stem)['actor']==actor and self._read(path.stem)['created_at']<=now]

    def discard(self, draft_id):
        with self.lock:
            path = self._path(draft_id)
            if path.exists():
                self._read(draft_id)
                path.unlink()
