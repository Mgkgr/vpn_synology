import importlib
from contextlib import closing
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maintenance.protocol import canonical, decode_frame, parse_request
from maintenance.store import JobStore
from test_profile_links import VLESS


class ProfileDraftTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).parents[1] / 'maintenance/profile_state.py').exists(), 'profile drafts missing')
        self.m = importlib.import_module('maintenance.profile_state')
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'drafts'
        self.drafts = self.m.ProfileDrafts(self.root)

    def stage(self, now=1000):
        return self.drafts.stage(VLESS, 'owner', 'a'*64, now)

    def test_stage_only_returns_safe_summary_and_owner_bound_opaque_reference(self):
        result = self.stage()
        self.assertEqual(result['server'], 'vpn.example')
        self.assertEqual(result['expires_at'], 2800)
        self.assertEqual(len(result['draft_id']), 32)
        encoded = json.dumps(result)
        for secret in ('11111111', 'public-key', 'short-id', 'vless://'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(self.drafts.load(result['draft_id'], 'owner', 1001)['profile']['type'], 'vless')
        with self.assertRaises(ValueError): self.drafts.load(result['draft_id'], 'admin', 1001)
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.root / (result['draft_id']+'.json')).stat().st_mode), 0o600)

    def test_expiry_backward_clock_and_cap_reject_without_renewal(self):
        result = self.stage()
        for now in (999, 2800):
            with self.assertRaises(ValueError): self.drafts.load(result['draft_id'], 'owner', now)
        for _ in range(7): self.stage()
        with self.assertRaises(ValueError): self.stage()
        self.assertEqual(len(self.drafts.list('owner', 1001)), 8)
        self.assertEqual(self.drafts.list('owner', 2800), [])
        self.stage(2801)

    def test_checked_evidence_is_bounded_revision_bound_and_expires(self):
        result = self.stage(); identifier = result['draft_id']
        rows = [dict(target=target, ok=True, latency_ms=20, http_status=204 if target!='github' else 200,
                     reason=None, checked_at=1100) for _ in range(3) for target in ('cloudflare','google','github')]
        self.drafts.checked(identifier, 'a'*64, rows, 1100)
        self.assertTrue(self.drafts.public(identifier, 'owner', 1200)['check_passed'])
        self.assertFalse(self.drafts.public(identifier, 'owner', 1401)['check_passed'])
        with self.assertRaises(ValueError): self.drafts.checked(identifier, 'b'*64, rows, 1100)
        with self.assertRaises(ValueError): self.drafts.checked(identifier, 'a'*64, [dict(rows[0], secret=VLESS)], 1100)

    def test_jobs_contain_only_reference_and_closed_schema(self):
        result = self.stage()
        value = dict(action='profile_check', draft_id=result['draft_id'], expected_revision='a'*64)
        request = parse_request(value)
        store = JobStore(Path(self.temp.name) / 'private/jobs.sqlite3')
        store.submit('1'*32, request, 'owner', now=1000)
        with closing(store._connect()) as db:
            saved = db.execute('SELECT request_json FROM jobs').fetchone()[0]
        self.assertEqual(json.loads(saved), value)
        self.assertNotIn('11111111', saved)
        for bad in (dict(value, uri=VLESS), dict(value, draft_id='../escape'), dict(value, action='profile_delete')):
            with self.assertRaises(ValueError): parse_request(bad)
        frame = canonical(dict(version=1, method='profile_stage', params=dict(uri=VLESS, actor='owner')))+b'\n'
        self.assertEqual(decode_frame(frame,10001)['method'], 'profile_stage')
        with self.assertRaises(ValueError): decode_frame(frame,10002)


if __name__ == '__main__': unittest.main()
