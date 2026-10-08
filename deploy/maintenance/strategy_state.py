"""Durable anti-DPI policy and evidence. Network and Docker stay in trusted adapters."""

from contextlib import closing
import hashlib
import json
import math

from .protocol import canonical
from . import strategy_catalog as cat

SOURCES = ('scheduled', 'confirmation', 'manual', 'daily', 'candidate', 'verify')
MAX_PROBES = 50000
VERDICTS = ('success', 'transport_error', 'http_error', 'unknown', 'certificate_error')
REASONS = ('verified', 'timeout', 'reset', 'tls_transport', 'http_denied', 'http_status',
           'dns_unavailable', 'runtime_unavailable', 'control_failed', 'certificate_invalid',
           'identity_changed', 'clock_invalid', 'stale', 'invalid_response', 'probe_failed')


class StrategyConflict(RuntimeError):
    pass


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid_time')
    return float(value)


def validated_observation(value):
    fields = {'checked_at', 'verdict', 'reason', 'latency_ms', 'http_status',
              'infrastructure_ok', 'identity', 'context_id'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid_observation')
    result = dict(value)
    result['checked_at'] = timestamp(value['checked_at'])
    if value['verdict'] not in VERDICTS or value['reason'] not in REASONS or type(value['infrastructure_ok']) is not bool:
        raise ValueError('invalid_observation')
    cat.digest(value['identity']); cat.digest(value['context_id'])
    latency, status = value['latency_ms'], value['http_status']
    if latency is not None and (type(latency) not in (int, float) or not math.isfinite(latency) or not 0 <= latency <= 10000):
        raise ValueError('invalid_latency')
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ValueError('invalid_http_status')
    if value['verdict'] == 'success' and (status != 200 or latency is None):
        raise ValueError('invalid_success')
    if value['verdict'] == 'transport_error' and (value['reason'] not in ('timeout', 'reset', 'tls_transport') or status is not None):
        raise ValueError('invalid_transport_error')
    return result


def fresh(result, identity, now):
    return (result['identity'] == identity and result['infrastructure_ok'] and
            0 <= now - result['checked_at'] <= 180)


class StrategyStore:
    def __init__(self, jobs):
        self.jobs = jobs
        with self.jobs._write() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS strategy_meta (slot INTEGER PRIMARY KEY CHECK(slot=1), identity TEXT, epoch INTEGER NOT NULL);
                INSERT OR IGNORE INTO strategy_meta(slot,identity,epoch) VALUES(1,NULL,0);
                CREATE TABLE IF NOT EXISTS strategy_policies (service TEXT PRIMARY KEY, policy TEXT NOT NULL, telemetry TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS strategy_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL,
                    strategy TEXT NOT NULL, source TEXT NOT NULL, observed_at REAL NOT NULL, result TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS strategy_observation_service ON strategy_observations(service,id);
                CREATE TABLE IF NOT EXISTS strategy_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL,
                    changed_at REAL NOT NULL, previous TEXT, strategy TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS strategy_schedules (service TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            for sid in cat.SERVICES:
                policy = dict(enabled=False, mode='pinned', interval_minutes=30, daily_enabled=True,
                              strategy_id=None, previous_strategy_id=None)
                db.execute('INSERT OR IGNORE INTO strategy_policies VALUES(?,?,?)', (sid, canonical(policy).decode(), '{}'))
            columns={row[1] for row in db.execute('PRAGMA table_info(strategy_meta)')}
            if 'catalog_id' not in columns:
                db.execute('ALTER TABLE strategy_meta ADD COLUMN catalog_id TEXT')
            previous=db.execute('SELECT catalog_id FROM strategy_meta WHERE slot=1').fetchone()[0]
            if previous!=cat.CATALOG_ID:
                for row in db.execute('SELECT service,policy FROM strategy_policies').fetchall():
                    policy=json.loads(row['policy']); policy['enabled']=False
                    db.execute('UPDATE strategy_policies SET policy=?,telemetry=? WHERE service=?',
                               (canonical(policy).decode(),'{}',row['service']))
                db.execute('UPDATE strategy_meta SET catalog_id=?,epoch=epoch+1 WHERE slot=1',(cat.CATALOG_ID,))

    def _data(self, db):
        meta = dict(db.execute('SELECT identity,epoch FROM strategy_meta WHERE slot=1').fetchone())
        rows = {row['service']: {'policy': json.loads(row['policy']), 'telemetry': json.loads(row['telemetry'])}
                for row in db.execute('SELECT * FROM strategy_policies')}
        revision = hashlib.sha256(canonical(dict(meta, catalog_id=cat.CATALOG_ID,
            policies={sid: row['policy'] for sid, row in rows.items()}))).hexdigest()
        return meta['identity'], rows, revision

    def _save(self, db, sid, row, configuration=False):
        db.execute('UPDATE strategy_policies SET policy=?,telemetry=? WHERE service=?',
                   (canonical(row['policy']).decode(), canonical(row['telemetry']).decode(), sid))
        if configuration:
            db.execute('UPDATE strategy_meta SET epoch=epoch+1 WHERE slot=1')

    def register(self, identity, selections, now):
        """Trusted host adapter only; a changed build/catalog revokes automatic policies."""
        cat.digest(identity); timestamp(now)
        if not isinstance(selections, dict):
            raise ValueError('invalid_selections')
        for sid, value in selections.items():
            cat.service(sid); cat.strategy(value)
        with self.jobs._write() as db:
            old, rows, _ = self._data(db)
            changed = old != identity
            for sid, row in rows.items():
                if changed or row['policy']['strategy_id'] != selections.get(sid):
                    row['policy'].update(enabled=False, strategy_id=selections.get(sid), previous_strategy_id=None)
                    row['telemetry'] = {}
                    self._save(db, sid, row, True)
            db.execute('UPDATE strategy_meta SET identity=? WHERE slot=1', (identity,))

    def configure(self, sid, settings, revision, actor, now):
        cat.service(sid); cat.digest(revision); settings = cat.settings(settings); timestamp(now)
        with self.jobs._write() as db:
            identity, rows, current = self._data(db)
            if revision != current:
                raise StrategyConflict('revision_changed')
            row = rows[sid]
            if settings['enabled'] and (identity is None or row['policy']['strategy_id'] is None):
                raise StrategyConflict('runtime_unconfigured')
            row['policy'].update(settings)
            # Evidence from a revoked policy must never authorize a newly enabled policy.
            row['telemetry'] = {}
            self._save(db, sid, row, True)
        return self.snapshot(now)

    def observe(self, sid, strategy, result, source, now):
        cat.service(sid); cat.strategy(strategy); timestamp(now)
        result = validated_observation(result)
        if source not in SOURCES:
            raise ValueError('invalid_source')
        with self.jobs._write() as db:
            identity, rows, _ = self._data(db)
            row, at = rows[sid], result['checked_at']
            inserted=db.execute('INSERT INTO strategy_observations(service,strategy,source,observed_at,result) VALUES(?,?,?,?,?)',
                                (sid, strategy, source, now, canonical(result).decode()))
            db.execute('DELETE FROM strategy_observations WHERE id <= ?', (inserted.lastrowid-MAX_PROBES,))
            telemetry = row['telemetry']
            telemetry['last_check'] = dict(result, strategy_id=strategy, source=source)
            if source in ('scheduled', 'confirmation') and strategy == row['policy']['strategy_id']:
                previous = telemetry.get('automatic')
                valid = fresh(result, identity, now) and (not previous or at >= previous['checked_at'])
                if not valid:
                    reason=result['reason']
                    if result['identity']!=identity:
                        reason='identity_changed'
                    elif at>now or (previous and at<previous['checked_at']):
                        reason='clock_invalid'
                    elif now-at>180:
                        reason='stale'
                    elif reason=='verified':
                        reason='control_failed'
                    result = dict(result, verdict='unknown', reason=reason)
                telemetry['automatic'] = result
                if valid and result['verdict'] == 'transport_error':
                    if previous and previous['context_id'] != result['context_id']:
                        telemetry.update(failure_count=0, first_failure_at=None, counted_failure_at=None)
                    counted = telemetry.get('counted_failure_at')
                    if counted is None or at - counted >= 30:
                        telemetry['failure_count'] = min(3, telemetry.get('failure_count', 0) + 1)
                        telemetry['counted_failure_at'] = at
                        if telemetry.get('first_failure_at') is None:
                            telemetry['first_failure_at'] = at
                else:
                    telemetry.update(failure_count=0, first_failure_at=None, counted_failure_at=None)
                if valid and result['verdict'] == 'success':
                    telemetry['last_success_at'] = at
            self._save(db, sid, row)

    def _decision(self, db, sid, row, identity, now):
        policy, telemetry = row['policy'], row['telemetry']
        if not policy['enabled']:
            return 'disabled'
        if policy['mode'] == 'pinned':
            return 'pinned'
        result = telemetry.get('automatic')
        if not result:
            return 'no_data'
        if now < result['checked_at']:
            return 'unknown'
        if now - result['checked_at'] > 180:
            return 'stale'
        if not fresh(result, identity, now):
            return 'unknown'
        verdict = result['verdict']
        if verdict != 'transport_error':
            return {'success':'healthy', 'http_error':'http_error'}.get(verdict, 'unknown')
        if telemetry.get('failure_count', 0) < 3:
            return 'suspect'
        times = [r[0] for r in db.execute("SELECT changed_at FROM strategy_changes WHERE service=? AND reason='automatic' ORDER BY id DESC LIMIT 2", (sid,))]
        if times and now < times[0]:
            return 'unknown'
        if times and now - times[0] < 900:
            return 'cooldown'
        if len(times) >= 2 and now - times[1] < 3600:
            return 'rate_limit'
        return 'search'

    def decision(self, sid, now):
        cat.service(sid); timestamp(now)
        with closing(self.jobs._connect()) as db:
            identity, rows, _ = self._data(db)
            return self._decision(db, sid, rows[sid], identity, now)

    def snapshot(self, now):
        timestamp(now)
        with closing(self.jobs._connect()) as db:
            db.execute('BEGIN')
            identity, rows, revision = self._data(db)
            services = []
            for sid, (name, host) in cat.SERVICES.items():
                row = rows[sid]
                history = [dict(r) for r in db.execute('SELECT changed_at,previous,strategy,reason,actor FROM strategy_changes WHERE service=? ORDER BY id DESC LIMIT 5', (sid,))]
                recent = [dict(strategy_id=r['strategy'], source=r['source'], **json.loads(r['result']))
                          for r in db.execute('SELECT strategy,source,result FROM strategy_observations WHERE service=? ORDER BY id DESC LIMIT 24', (sid,))]
                services.append(dict(service_id=sid, name=name, host=host, **row['policy'],
                    state=self._decision(db,sid,row,identity,now), failure_count=row['telemetry'].get('failure_count',0),
                    first_failure_at=row['telemetry'].get('first_failure_at'), last_success_at=row['telemetry'].get('last_success_at'),
                    last_check=row['telemetry'].get('last_check'), last_automatic=row['telemetry'].get('automatic'),
                    last_search_at=row['telemetry'].get('last_search_at'), history=history, results=recent))
            return dict(revision=revision, identity=identity, catalog=cat.catalog(), services=services, observed_at=now)

    def can_replace(self, sid, strategy, evidence, current_result, revision, now):
        try:
            cat.service(sid); cat.strategy(strategy); timestamp(now)
            if len(evidence) != 3:
                return False
            evidence = [validated_observation(item) for item in evidence]
            current_result = validated_observation(current_result)
            with closing(self.jobs._connect()) as db:
                identity, rows, actual_revision = self._data(db)
                if revision != actual_revision or strategy == rows[sid]['policy']['strategy_id'] or self._decision(db, sid, rows[sid], identity, now) != 'search':
                    return False
                if current_result['verdict'] != 'transport_error' or not fresh(current_result, identity, now):
                    return False
                if any(item['verdict'] != 'success' or not fresh(item,identity,now) or item['context_id'] != current_result['context_id'] for item in evidence):
                    return False
                return (all(b['checked_at'] - a['checked_at'] >= 10 for a,b in zip(evidence,evidence[1:]))
                        and current_result['checked_at'] >= evidence[-1]['checked_at'])
        except (ValueError, TypeError):
            return False

    def record_change(self, sid, strategy, revision, reason, now, actor='system', mode=None):
        """Root runner only, after verified production apply. Revision is compare-and-swap."""
        cat.service(sid); cat.strategy(strategy); timestamp(now)
        if reason not in ('automatic','manual','rollback') or mode not in (None,'auto','pinned'):
            raise ValueError('invalid_change')
        if not isinstance(actor,str) or not 1 <= len(actor) <= 64 or not actor.isprintable():
            raise ValueError('invalid_actor')
        with self.jobs._write() as db:
            _, rows, actual = self._data(db)
            if revision != actual:
                raise StrategyConflict('revision_changed')
            row, previous = rows[sid], rows[sid]['policy']['strategy_id']
            row['policy'].update(previous_strategy_id=previous, strategy_id=strategy)
            if mode is not None:
                row['policy']['mode'] = mode
            row['telemetry'] = {}
            db.execute('INSERT INTO strategy_changes(service,changed_at,previous,strategy,reason,actor) VALUES(?,?,?,?,?,?)',
                       (sid,now,previous,strategy,reason,actor))
            self._save(db,sid,row,True)

    def mark_search(self, sid, now):
        cat.service(sid); timestamp(now)
        with self.jobs._write() as db:
            _, rows, _ = self._data(db)
            previous = rows[sid]['telemetry'].get('last_search_at')
            if previous is not None and now-previous < 900:
                return False
            rows[sid]['telemetry']['last_search_at'] = now
            self._save(db,sid,rows[sid])
            return True

    def prune(self, now):
        timestamp(now)
        with self.jobs._write() as db:
            db.execute('DELETE FROM strategy_observations WHERE observed_at < ?', (now-30*86400,))
            db.execute('DELETE FROM strategy_observations WHERE id IN (SELECT id FROM strategy_observations ORDER BY id DESC LIMIT -1 OFFSET ?)',(MAX_PROBES,))
            db.execute('DELETE FROM strategy_changes WHERE changed_at < ?', (now-90*86400,))
