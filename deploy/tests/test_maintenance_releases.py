import importlib
import json
from pathlib import Path
import sys
import unittest
import threading
import time
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64


def release(tag="v1.19.28", **changes):
    return dict(id=123, tag_name=tag, published_at="2026-09-01T00:00:00Z", draft=False,
                prerelease=False, html_url="https://github.com/MetaCubeX/mihomo/releases/tag/" + tag, **changes)


class ReleasesTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "maintenance/releases.py").is_file(), "release inventory is not implemented")
        self.m = importlib.import_module("maintenance.releases")
        catalog_module = importlib.import_module("maintenance.catalog")
        self.catalog = catalog_module.load_catalog(ROOT / "maintenance/components.json")[:1]

    def test_release_failure_is_unknown_and_retains_previous(self):
        previous = self.m.check_releases(self.catalog, {}, NOW, fetch=lambda url, etag: (200, '"abc"', json.dumps([release()]).encode()))
        for failure in (TimeoutError(), (429, None, b""), (200, None, b"bad json"), (302, None, b"")):
            def fetch(url, etag):
                if isinstance(failure, Exception):
                    raise failure
                return failure
            current = self.m.check_releases(self.catalog, previous, NOW + timedelta(hours=1), fetch=fetch)["mihomo"]
            self.assertIsNotNone(current.freshness_error)
            self.assertEqual(current.releases, previous["mihomo"].releases)
            self.assertEqual(current.checked_at, previous["mihomo"].checked_at)

    def test_unverified_latest_is_not_installable_and_unstable_hidden(self):
        rows = [release(), dict(release("v2.0-beta"), prerelease=True), dict(release("v2.0-draft"), draft=True)]
        current = self.m.check_releases(self.catalog, {}, NOW, fetch=lambda *args: (200, None, json.dumps(rows).encode()))["mihomo"]
        self.assertEqual(len(current.releases), 1)
        self.assertEqual(current.releases[0].compatibility, "unverified")
        with self.assertRaises(self.m.ReleaseError):
            self.m.resolve_approved_release(self.catalog[0], current, "123", {}, "linux/amd64")

    def test_approval_requires_platform_manifest_source_and_rollback_evidence(self):
        current = self.m.check_releases(self.catalog, {}, NOW, fetch=lambda *args: (200, None, json.dumps([release()]).encode()))["mihomo"]
        good = {"component": "mihomo", "release_id": "123", "version": "v1.19.28", "repository": "MetaCubeX/mihomo", "image": "metacubex/mihomo@" + DIGEST, "digest": DIGEST, "platform": "linux/amd64", "compatibility": "approved", "validation_id": "isolated-123", "rollback_verified": True, "signature": "not_published"}
        for change in ({"platform": "linux/arm64"}, {"digest": "sha256:" + "b" * 64}, {"repository": "attacker/repo"}, {"validation_id": ""}, {"rollback_verified": False}, {"signature": "failed"}, {"image": "attacker/image@" + DIGEST}):
            with self.subTest(change=change), self.assertRaises(self.m.ReleaseError):
                self.m.resolve_approved_release(self.catalog[0], current, "123", {"123": dict(good, **change)}, "linux/amd64")
        approved = self.m.resolve_approved_release(self.catalog[0], current, "123", {"123": good}, "linux/amd64")
        self.assertEqual(approved.digest, DIGEST)

    def test_etag_and_invalid_source_url(self):
        first = self.m.check_releases(self.catalog, {}, NOW, fetch=lambda *args: (200, '"abc"', json.dumps([release()]).encode()))
        def fetch(url, etag):
            self.assertEqual(url, "https://api.github.com/repos/MetaCubeX/mihomo/releases?per_page=20")
            self.assertEqual(etag, '"abc"')
            return (304, '"abc"', b"")
        cached = self.m.check_releases(self.catalog, first, NOW + timedelta(hours=1), fetch=fetch)["mihomo"]
        self.assertIsNone(cached.freshness_error)
        self.assertEqual(cached.checked_at, NOW + timedelta(hours=1))
        bad = dict(release(), html_url="https://attacker.invalid/path")
        result = self.m.check_releases(self.catalog, {}, NOW, fetch=lambda *args: (200, None, json.dumps([bad]).encode()))["mihomo"]
        self.assertEqual(result.freshness_error, "invalid_metadata")

    def test_no_fake_upstream_for_local_dashboard_or_unselected_antidpi(self):
        catalog = importlib.import_module("maintenance.catalog").load_catalog(ROOT / "maintenance/components.json")[-2:]
        def fetch(*args):
            self.fail("No remote source for local dashboard or unselected anti-DPI")
        current = self.m.check_releases(catalog, {}, NOW, fetch=fetch)
        self.assertEqual(current["dashboard"].freshness_error, "local_release_not_prepared")
        self.assertEqual(current["antidpi"].freshness_error, "engine_not_selected")

    def test_local_dashboard_requires_commit_and_verified_build(self):
        catalog = importlib.import_module("maintenance.catalog").load_catalog(ROOT / "maintenance/components.json")[4:5]
        commit = "a" * 40
        artifact = {"component": "dashboard", "release_id": commit, "version": commit, "git_commit": commit, "repository": None, "published_at": NOW.isoformat(), "image": "vpn-dashboard-dashboard@" + DIGEST, "digest": DIGEST, "platform": "linux/amd64", "compatibility": "approved", "validation_id": "local-build-1", "rollback_verified": True, "signature": "not_published"}
        result = self.m.check_releases(catalog, {}, NOW, local_artifacts={"dashboard": artifact})["dashboard"]
        self.assertIsNone(result.freshness_error)
        self.assertEqual(result.releases[0].version, commit)
        self.assertEqual(result.releases[0].compatibility, "approved")
        for change in ({"git_commit": "main"}, {"compatibility": "unverified"}, {"digest": "unknown"}):
            result = self.m.check_releases(catalog, {}, NOW, local_artifacts={"dashboard": dict(artifact, **change)})["dashboard"]
            self.assertIsNotNone(result.freshness_error)
            self.assertEqual(result.releases, ())

    def test_bounded_parallelism_and_metadata_size(self):
        catalog = importlib.import_module("maintenance.catalog").load_catalog(ROOT / "maintenance/components.json")[:4]
        lock = threading.Lock()
        active = peak = 0
        def fetch(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return 200, None, b" " * (self.m.MAX_BODY + 1)
        results = self.m.check_releases(catalog, {}, NOW, fetch=fetch)
        self.assertLessEqual(peak, 2)
        self.assertTrue(all(item.freshness_error == "release_metadata_too_large" for item in results.values()))


if __name__ == "__main__":
    unittest.main()
