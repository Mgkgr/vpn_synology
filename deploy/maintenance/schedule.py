"""Daily metadata checks at 05:00 NAS local time. Never installs a release."""

from contextlib import closing
import datetime
import os
from pathlib import Path
import sqlite3
import time

from .backups import no_symlink, private_directory


def nas_calendar(now):
    # Worker installation verifies the NAS timezone. Do not set system timezone
    # or rely on zoneinfo (unavailable in the NAS Python 3.8 standard library).
    local = time.localtime(now)
    return time.strftime('%Y-%m-%d', local), local.tm_hour


class ReleaseSchedule:
    def __init__(self, path, enqueue, *, local_calendar=nas_calendar):
        self.path, self.enqueue, self.local_calendar = Path(path), enqueue, local_calendar
        no_symlink(self.path)
        private_directory(self.path.parent)
        if not self.path.exists():
            descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        with closing(self._db()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS release_schedule (slot INTEGER PRIMARY KEY CHECK(slot=1), day TEXT, attempted REAL, observed REAL NOT NULL)')
            db.commit()

    def _db(self):
        no_symlink(self.path)
        for suffix in ('-wal', '-shm'):
            no_symlink(Path(str(self.path) + suffix))
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    def manual(self, now=None):
        return self._request(True, time.time() if now is None else now)

    def tick(self, now=None):
        return self._request(False, time.time() if now is None else now)

    def _request(self, manual, now):
        day, hour = self.local_calendar(now)
        datetime.date.fromisoformat(day)
        if type(hour) is not int or not 0 <= hour <= 23:
            raise ValueError('invalid_local_calendar')
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM release_schedule WHERE slot=1').fetchone()
            if row and now < row['observed']:
                db.rollback()
                return {'queued': False, 'reason': 'clock_anomaly'}
            old_day, last = (row['day'], row['attempted']) if row else (None, None)
            due = manual or (hour >= 5 and (old_day is None or day > old_day))
            permitted = due and (last is None or now - last >= 60)
            # Persist before invoking queue: a lost response cannot enqueue twice.
            db.execute('INSERT OR REPLACE INTO release_schedule VALUES(1,?,?,?)',
                (day if permitted and not manual else old_day, now if permitted else last, now))
            db.commit()
        if not permitted:
            return {'queued': False, 'reason': 'cooldown' if due else 'not_due'}
        try:
            self.enqueue()
        except Exception:
            return {'queued': False, 'reason': 'check_queue_unknown'}
        return {'queued': True, 'reason': 'metadata_only'}
