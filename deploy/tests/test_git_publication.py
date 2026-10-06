"""Exercise publication excludes with real Git, never the user's index."""

from pathlib import Path
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[2]
GITLEAKS = os.environ.get("GITLEAKS_PATH") or shutil.which("gitleaks")


class GitPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copyfile(ROOT / ".gitignore", self.root / ".gitignore")
        self.git("init", "--quiet")

    def git(self, *args, input=None, check=True):
        return subprocess.run(["git", "-C", str(self.root), *args], input=input, text=True,
                              capture_output=True, encoding="utf-8", check=check)

    def test_runtime_secrets_and_sidecars_are_excluded(self):
        paths = [
            "deploy/dashboard.env", "private/prod.env", "private/prod.env.previous",
            "deploy/data/dashboard.sqlite3-wal", "runtime/kuma.db-shm", "runtime/clients.sqlite-journal",
            "deploy/gateway/mihomo/config.yaml", "deploy/gateway/wireguard/wg0.json",
            "deploy/gateway/wg-easy/wg-easy.db", "deploy/gateway/uptime-kuma/kuma.db-wal",
            "backups/full.tar.gz", "private/export.zip", "runtime/client.conf", "runtime/server.key",
            "backend/.venv/pyvenv.cfg", ".playwright-cli/trace.log", ".playwright-mcp/page.png",
            ".superpowers/sdd/task/progress.md", "runtime/GeoSite.dat", "gitleaks-report.json",
        ]
        result = self.git("check-ignore", "--no-index", "-z", "--stdin", input="\0".join(paths) + "\0", check=False)
        self.assertEqual(set(result.stdout.rstrip("\0").split("\0")), set(paths))

    def test_sources_and_safe_examples_remain_trackable(self):
        paths = ["README.md", "backend/app/main.py", "frontend/src/App.tsx",
                 "deploy/gateway/compose.yaml", "deploy/dashboard.env.example", ".env.example",
                 "deploy/rules/max-messenger-direct.txt", "deploy/tests/test_git_publication.py"]
        result = self.git("check-ignore", "--no-index", "-z", "--stdin", input="\0".join(paths) + "\0", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_bulk_add_does_not_pick_up_generated_runtime_data(self):
        for name in ["src/app.py", "runtime/clients.sqlite3-wal", ".playwright-mcp/trace.txt", "backend/.venv/pyvenv.cfg"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        self.git("add", ".")
        self.assertEqual(set(self.git("ls-files").stdout.splitlines()), {".gitignore", "src/app.py"})


@unittest.skipUnless(GITLEAKS, "Gitleaks CLI is needed for rule integration tests")
class GitleaksRuleTests(unittest.TestCase):
    def scan(self, content, name="settings.txt"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input"
            source.mkdir()
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            report = root / "report.json"
            command = [GITLEAKS, "dir", str(source), "--no-banner", "--redact=100",
                       "--exit-code=23", "--log-level=error", "--report-format=json", "--report-path", str(report)]
            config = ROOT / ".gitleaks.toml"
            if config.exists():
                command += ["--config", str(config)]
            result = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=30)
            body = report.read_text(encoding="utf-8") if report.exists() else "[]"
            return result, json.loads(body), body

    def test_vpn_credentials_are_detected_and_redacted(self):
        credentials = [
            (str(uuid.uuid4()), lambda value: "vless://" + value + "@vpn.example.invalid:443"),
            (uuid.uuid4().hex, lambda value: "hy2://" + value + "@vpn.example.invalid:443"),
            (base64.b64encode(os.urandom(32)).decode(), lambda value: "PrivateKey = " + value),
            (base64.b64encode(os.urandom(32)).decode(), lambda value: '{"privateKey": "' + value + '"}'),
            (base64.b64encode(os.urandom(32)).decode(), lambda value: "PresharedKey = " + value),
        ]
        for secret, make_content in credentials:
            with self.subTest(protocol=make_content("hidden").split(":")[0]):
                result, findings, body = self.scan(make_content(secret))
                self.assertEqual(result.returncode, 23, "VPN credential was not detected")
                self.assertTrue(findings)
                self.assertNotIn(secret, result.stdout + result.stderr + body)

    def test_safe_configuration_placeholders_are_allowed(self):
        result, findings, _body = self.scan("vless://<UUID>@vpn.example.invalid:443\nhy2://<PASSWORD>@vpn.example.invalid:443\nPrivateKey = <PRIVATE_KEY>\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(findings, [])

    def test_fixture_exceptions_require_both_exact_value_and_test_path(self):
        dummy_key = base64.b64encode(b"0123456789abcdef" * 2).decode()
        cases = [
            ("backend/tests/test_settings.py", dummy_key,
             lambda value: 'dashboard_encryption_key = "' + value + '"',
             base64.b64encode(os.urandom(32)).decode()),
            ("deploy/test-replace-hy2-profile.ps1", "fixture-password",
             lambda value: "hy2://" + value + "@fixture-hy2.test:443", uuid.uuid4().hex),
            ("deploy/tests/test_replace_primary_vless.py", "test-auth-secret",
             lambda value: "hy2://" + value + "@reserve.example:443", uuid.uuid4().hex),
            ("deploy/tests/test_replace_primary_vless.py", "11111111-2222-4333-8444-555555555555",
             lambda value: "vless://" + value + "@vpn.example:443", str(uuid.uuid4())),
        ]
        for path, dummy, make_content, real_like in cases:
            with self.subTest(path=path):
                allowed, _findings, _body = self.scan(make_content(dummy), path)
                self.assertEqual(allowed.returncode, 0, allowed.stderr)
                outside, findings, _body = self.scan(make_content(dummy), "src/settings.txt")
                self.assertEqual(outside.returncode, 23)
                self.assertTrue(findings)
                different, findings, body = self.scan(make_content(real_like), path)
                self.assertEqual(different.returncode, 23)
                self.assertTrue(findings)
                self.assertNotIn(real_like, different.stdout + different.stderr + body)


@unittest.skipUnless(GITLEAKS, "Gitleaks CLI is needed for publication integration tests")
class PublicationCommandTests(unittest.TestCase):
    git = GitPublicationTests.git

    def setUp(self):
        GitPublicationTests.setUp(self)
        shutil.copyfile(ROOT / ".gitleaks.toml", self.root / ".gitleaks.toml")
        self.git("config", "user.name", "Publication test")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")

    def run_check(self):
        result = subprocess.run([
            sys.executable, str(ROOT / "deploy/check-git-publication.py"),
            "--repo", str(self.root), "--gitleaks", GITLEAKS,
        ], capture_output=True, encoding="utf-8", timeout=60)
        return result

    def write_secret(self, path):
        secret = uuid.uuid4().hex
        (self.root / path).write_text("hy2://" + secret + "@vpn.example.invalid:443\n", encoding="utf-8")
        return secret

    def test_clean_check_does_not_modify_index_or_worktree(self):
        (self.root / "readme.txt").write_text("public fixture\n", encoding="utf-8")
        self.write_secret("runtime.env")
        before = self.git("status", "--porcelain=v1").stdout
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PUBLICATION_CHECK=passed", result.stdout)
        self.assertEqual(self.git("status", "--porcelain=v1").stdout, before)

    def test_untracked_credential_fails_without_disclosure(self):
        secret = self.write_secret("untracked.txt")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SCAN_PHASE=working_tree", result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_staged_secret_is_detected_even_if_working_copy_is_clean(self):
        secret = self.write_secret("staged.txt")
        self.git("add", "staged.txt")
        (self.root / "staged.txt").write_text("now clean\n", encoding="utf-8")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SCAN_PHASE=staged", result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_deleted_historical_secret_is_still_detected(self):
        secret = self.write_secret("old.txt")
        self.git("add", "old.txt")
        self.git("commit", "--quiet", "-m", "unsafe fixture")
        self.git("rm", "old.txt")
        self.git("commit", "--quiet", "-m", "remove fixture")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SCAN_PHASE=history", result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_already_tracked_runtime_files_fail_closed(self):
        (self.root / "runtime.env").write_text("safe-looking fixture\n", encoding="utf-8")
        self.git("add", "--force", "runtime.env")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TRACKED_IGNORED_FILES=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
