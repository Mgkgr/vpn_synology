from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[2]
JOB = REPOSITORY / "deploy" / "scripts" / "run-direct-import-job.sh"
BASH = Path(os.environ.get("GIT_BASH", r"C:\Program Files\Git\usr\bin\bash.exe"))
CYGPATH = BASH.parent / "cygpath.exe"


def _shell_path(path: Path) -> str:
    result = subprocess.run(
        [str(CYGPATH), "-u", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _run_job(tmp_path: Path, importer_body: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    importer = tmp_path / "importer.sh"
    normalizer = tmp_path / "normalizer.py"
    source = tmp_path / "input.txt"
    status = tmp_path / "status.txt"
    private_log = tmp_path / "private.log"
    importer.write_text(importer_body, encoding="utf-8")
    normalizer.write_text("# unused by the fake importer\n", encoding="utf-8")
    source.write_text("DOMAIN-SUFFIX,auth.permkrai.ru,DIRECT\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["PATH"] = str(BASH.parent) + os.pathsep + environment.get("PATH", "")
    result = subprocess.run(
        [
            str(BASH),
            _shell_path(JOB),
            _shell_path(importer),
            _shell_path(normalizer),
            _shell_path(source),
            _shell_path(status),
            _shell_path(private_log),
        ],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    return result, status, private_log


@pytest.mark.skipif(not BASH.exists() or not CYGPATH.exists(), reason="Git Bash is required to exercise Synology shell jobs")
def test_direct_import_job_publishes_safe_completion_after_success(tmp_path: Path) -> None:
    assert JOB.exists(), "the detached direct-import job is required"

    result, status, private_log = _run_job(
        tmp_path,
        """#!/bin/sh
echo DIRECT_RULES_CURRENT=380
echo DIRECT_IMPORT_RULES=1
echo DIRECT_IMPORT_ADDED=1
echo DIRECT_RULES_REVISION=42
echo MIHOMO_RELOAD=success
echo HY2_PASSWORD=must-never-be-public
exit 0
""",
    )

    assert result.returncode == 0, result.stderr
    assert status.exists(), result.stdout + result.stderr + repr(list(tmp_path.iterdir()))
    assert status.read_text(encoding="utf-8") == """RESULT=success
DIRECT_RULES_CURRENT=380
DIRECT_IMPORT_RULES=1
DIRECT_IMPORT_ADDED=1
DIRECT_RULES_REVISION=42
MIHOMO_RELOAD=success
"""
    assert "must-never-be-public" not in status.read_text(encoding="utf-8")
    assert "must-never-be-public" in private_log.read_text(encoding="utf-8")


@pytest.mark.skipif(not BASH.exists() or not CYGPATH.exists(), reason="Git Bash is required to exercise Synology shell jobs")
def test_direct_import_job_publishes_failure_after_import_error(tmp_path: Path) -> None:
    assert JOB.exists(), "the detached direct-import job is required"

    result, status, private_log = _run_job(
        tmp_path,
        """#!/bin/sh
echo internal import error >&2
exit 1
""",
    )

    assert result.returncode == 1
    assert status.exists(), result.stdout + result.stderr + repr(list(tmp_path.iterdir()))
    assert status.read_text(encoding="utf-8") == "RESULT=failed\n"
    assert "internal import error" in private_log.read_text(encoding="utf-8")
