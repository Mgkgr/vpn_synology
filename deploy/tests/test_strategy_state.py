import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
IDENTITY, CONTEXT = 'a' * 64, 'c' * 64


def observation(at, verdict='transport_error', **extra):
    value = dict(checked_at=at, verdict=verdict, reason='timeout' if verdict == 'transport_error' else 'verified',
                 latency_ms=123 if verdict == 'success' else None, http_status=200 if verdict == 'success' else None,
                 infrastructure_ok=True, identity=IDENTITY, context_id=CONTEXT)
    value.update(extra)
    return value


class StrategyStateTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'maintenance/strategy_state.py').is_file(), 'durable strategy control is missing')
        self.m = importlib.import_module('maintenance.strategy_state')
        self.catalog = importlib.import_module('maintenance.strategy_catalog')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        jobs = importlib.import_module('maintenance.store').JobStore(Path(self.temp.name) / 'private/state.sqlite3')
        self.state = self.m.StrategyStore(jobs)
        self.state.register(IDENTITY, {'youtube':'tlsrec-sni','discord':'tlsrec-sni',
            'telegram':'tlsrec-sni','instagram':'disorder-sni'}, now=1000)
        self.configure()

    def configure(self, **changes):
        settings = dict(enabled=True, mode='auto', interval_minutes=30, daily_enabled=True)
        settings.update(changes)
        return self.state.configure('youtube', settings, self.state.snapshot(1000)['revision'], 'owner', now=1000)

    def observe(self, at, verdict='transport_error', source='scheduled', **overrides):
        return self.state.observe('youtube', 'tlsrec-sni', observation(at, verdict, **overrides), source, now=at)

    def fail_three(self):
        for at in (1000,1030,1060): self.observe(at)

    def test_three_spaced_failures_not_one_or_duplicate_trigger_search(self):
        self.observe(1000)
        self.assertEqual(self.state.decision('youtube', 1000), 'suspect')
        self.observe(1000); self.observe(1010)
        self.assertEqual(self.state.snapshot(1010)['services'][0]['failure_count'], 1)
        self.observe(1030)
        self.assertEqual(self.state.decision('youtube', 1030), 'suspect')
        self.observe(1060)
        self.assertEqual(self.state.decision('youtube', 1060), 'search')

    def test_success_unknown_and_http_denial_break_failure_series(self):
        self.fail_three(); self.observe(1090, 'success')
        self.assertEqual(self.state.decision('youtube', 1090), 'healthy')
        self.observe(1120); self.observe(1150, 'http_error', reason='http_denied', http_status=403)
        self.assertEqual(self.state.decision('youtube', 1150), 'http_error')
        self.assertEqual(self.state.snapshot(1150)['services'][0]['failure_count'], 0)
        self.observe(1180, 'unknown', infrastructure_ok=False, reason='dns_unavailable')
        self.assertEqual(self.state.decision('youtube', 1180), 'unknown')
        self.assertEqual(self.state.snapshot(1180)['services'][0]['last_automatic']['reason'],'dns_unavailable')

    def test_manual_and_other_strategy_results_do_not_drive_auto_state(self):
        for at in (1000,1030,1060): self.observe(at, source='manual')
        self.state.observe('youtube', 'disorder-1', observation(1090), 'candidate', now=1090)
        self.assertEqual(self.state.decision('youtube', 1090), 'no_data')

    def test_stale_future_identity_and_clock_reversal_never_approve_change(self):
        self.fail_three()
        self.assertEqual(self.state.decision('youtube', 1241), 'stale')
        self.assertEqual(self.state.decision('youtube', 1050), 'unknown')
        self.state.observe('youtube','tlsrec-sni',observation(1500),'scheduled',now=1200)
        self.assertEqual(self.state.decision('youtube', 1200), 'unknown')
        self.observe(1230, identity='b'*64)
        self.assertEqual(self.state.decision('youtube', 1230), 'unknown')

    def test_pin_disabled_and_new_identity_prevent_automatic_search(self):
        self.configure(mode='pinned'); self.fail_three()
        self.assertEqual(self.state.decision('youtube',1060), 'pinned')
        self.configure(enabled=False)
        self.assertEqual(self.state.decision('youtube',1060), 'disabled')
        self.configure()
        self.state.register('b'*64, {'youtube':'tlsrec-sni'}, now=1070)
        self.assertEqual(self.state.decision('youtube',1070), 'disabled')

    def test_telemetry_preserves_revision_configuration_conflicts_fail(self):
        revision = self.state.snapshot(1000)['revision']; self.observe(1000)
        self.assertEqual(self.state.snapshot(1000)['revision'], revision)
        self.configure(mode='pinned')
        with self.assertRaises(self.m.StrategyConflict):
            self.state.configure('youtube', dict(enabled=True,mode='auto',interval_minutes=5,daily_enabled=True), revision,'owner',now=1100)

    def test_replacement_needs_fresh_same_context_successes_and_failed_current(self):
        self.fail_three()
        evidence = [observation(t,'success') for t in (1070,1080,1090)]
        revision = self.state.snapshot(1090)['revision']
        self.assertTrue(self.state.can_replace('youtube','disorder-1',evidence,observation(1100),revision,1100))
        variants = [evidence[:2], [dict(x,context_id='d'*64) if i==0 else x for i,x in enumerate(evidence)],
                    [dict(x,checked_at=1090) for x in evidence], [dict(x,checked_at=500) for x in evidence],
                    [dict(x,identity='b'*64) for x in evidence]]
        for invalid in variants:
            self.assertFalse(self.state.can_replace('youtube','disorder-1',invalid,observation(1100),revision,1100))
        self.assertFalse(self.state.can_replace('youtube','disorder-1',evidence,observation(1100,'success'),revision,1100))

    def test_persistent_cooldown_and_two_changes_per_hour(self):
        self.state.record_change('youtube','disorder-1',self.state.snapshot(1000)['revision'],'automatic',now=1060)
        restored = self.m.StrategyStore(self.state.jobs)
        for at in (1090,1120,1150): restored.observe('youtube','disorder-1',observation(at),'scheduled',now=at)
        self.assertEqual(restored.decision('youtube',1150), 'cooldown')
        for at in (1900,1930,1960): restored.observe('youtube','disorder-1',observation(at),'scheduled',now=at)
        self.assertEqual(restored.decision('youtube',1960), 'search')
        restored.record_change('youtube','disorder-sni',restored.snapshot(1960)['revision'],'automatic',now=1960)
        for at in (2800,2830,2860): restored.observe('youtube','disorder-sni',observation(at),'scheduled',now=at)
        self.assertEqual(restored.decision('youtube',2860), 'rate_limit')
        self.assertEqual(restored.snapshot(2860)['services'][1]['strategy_id'], 'tlsrec-sni')

    def test_closed_request_rejects_commands_extra_fields_and_nonfinite_data(self):
        parse = self.catalog.parse_strategy_request
        valid = dict(action='strategy_check',service_id='youtube',expected_revision='a'*64,settings={})
        self.assertEqual(parse(valid).service_id, 'youtube')
        for invalid in ({**valid,'shell':'id'},{**valid,'service_id':'http://127.0.0.1'},
                        {**valid,'settings':{'command':'--fake'}},{**valid,'action':'strategy_delete'}):
            with self.assertRaises(ValueError): parse(invalid)
        for value in (float('nan'),float('inf'),True,-1):
            with self.assertRaises(ValueError):
                self.state.observe('youtube','tlsrec-sni',observation(value),'scheduled',now=1100)

    def test_catalog_upgrade_revokes_previous_automatic_authority(self):
        with patch.object(self.catalog,'CATALOG_ID','e'*64):
            restored=self.m.StrategyStore(self.state.jobs)
            self.assertEqual(restored.decision('youtube',1000),'disabled')

    def test_observations_cannot_claim_success_from_http_denial(self):
        with self.assertRaises(ValueError):
            self.state.observe('youtube','tlsrec-sni',observation(1000,'success',http_status=403),'scheduled',1000)

    def test_pruning_preserves_policy_and_only_recent_telemetry(self):
        self.state.record_change('youtube','disorder-1',self.state.snapshot(1000)['revision'],'manual',1000)
        self.state.observe('youtube','disorder-1',observation(1000,'success'),'manual',1000)
        self.state.prune(1000+91*86400)
        row=self.state.snapshot(1000+91*86400)['services'][0]
        self.assertTrue(row['enabled']); self.assertEqual(row['strategy_id'],'disorder-1')
        self.assertEqual(row['history'],[]); self.assertEqual(row['results'],[])

    def test_observation_cap_applies_even_when_scheduler_is_stopped(self):
        with patch.object(self.m,'MAX_PROBES',5,create=True):
            for at in range(1000,1010): self.observe(at,source='manual')
        self.assertEqual(len(self.state.snapshot(1010)['services'][0]['results']),5)


if __name__ == '__main__': unittest.main()
