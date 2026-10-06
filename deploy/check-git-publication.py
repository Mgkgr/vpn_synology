"""Read-only pre-publication gate. Requires Git and Gitleaks (tested: 8.30.1).

Scans history, the index and a temporary export of tracked/unignored files.
Does not stage, commit, push, install hooks, change Git config or contact NAS.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


class PublicationError(Exception):
    pass


def git(repo, *args):
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo), *args],
        capture_output=True, timeout=60,
    )
    if result.returncode:
        raise PublicationError("Git read failed; check repository path and Git installation")
    return result.stdout


def git_paths(repo, *args):
    return sorted(set(part.decode("utf-8") for part in git(repo, "ls-files", "-z", *args).split(b"\0") if part))


def export_working_tree(repo, destination):
    count = 0
    for relative in git_paths(repo, "--cached", "--others", "--exclude-standard"):
        source = repo / relative
        if source.is_symlink() or not source.resolve().is_relative_to(repo):
            raise PublicationError("Refusing a symlink or a path outside the repository")
        if not source.exists():  # tracked deletion: history/index scans still cover it
            continue
        if not source.is_file():
            raise PublicationError("Submodule or non-file entry needs a separate publication review")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        count += 1
    return count


def scan(executable, repo, config, temporary, phase, *args):
    print(f"SCAN_PHASE={phase}", flush=True)
    report = temporary / f"{phase}.json"
    env = os.environ.copy()
    # Trust only this explicitly chosen repository, only for this child process.
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env.update({"GIT_CONFIG_COUNT": str(count + 1), f"GIT_CONFIG_KEY_{count}": "safe.directory",
                f"GIT_CONFIG_VALUE_{count}": repo.as_posix()})
    result = subprocess.run([
        executable, *args, "--config", str(config), "--redact=100", "--no-banner", "--no-color",
        "--ignore-gitleaks-allow", "--exit-code=23", "--log-level=error", "--timeout=180",
        "--report-format=json", "--report-path", str(report),
    ], cwd=repo, env=env, capture_output=True, timeout=210)
    if result.returncode == 23:
        findings = json.loads(report.read_text(encoding="utf-8"))
        print(f"FINDINGS={len(findings)}")
        # Never echo Match, Secret, source contents or raw CLI output.
        for finding in findings:
            print(json.dumps({"file": finding.get("File"), "line": finding.get("StartLine"),
                              "rule": finding.get("RuleID")}, ensure_ascii=True))
        raise PublicationError(f"Possible secrets found in {phase}; review before publishing")
    if result.returncode:
        raise PublicationError(f"Gitleaks failed in {phase} (exit {result.returncode}); check CLI/config")
    print(f"SCAN_{phase.upper()}=passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--gitleaks", default=os.environ.get("GITLEAKS_PATH") or "gitleaks")
    args = parser.parse_args()
    try:
        repo = args.repo.resolve(strict=True)
        actual = Path(git(repo, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
        if actual != repo:
            raise PublicationError("--repo must point to the Git repository root")
        config = repo / ".gitleaks.toml"
        if not config.is_file():
            raise PublicationError("Missing project .gitleaks.toml")
        executable = shutil.which(args.gitleaks)
        if not executable:
            raise PublicationError("Gitleaks is not installed; set --gitleaks or GITLEAKS_PATH")
        ignored = git_paths(repo, "--cached", "--ignored", "--exclude-standard")
        if ignored:
            print(f"TRACKED_IGNORED_FILES={len(ignored)}")
            for path in ignored:
                print(json.dumps({"path": path}, ensure_ascii=True))
            raise PublicationError("Runtime/ignored files already exist in the index; .gitignore is not sufficient")
        with tempfile.TemporaryDirectory(prefix="vpn-git-scan-") as name:
            temporary = Path(name)
            scan(executable, repo, config, temporary, "history", "git", str(repo), "--log-opts=--all")
            scan(executable, repo, config, temporary, "staged", "git", str(repo), "--pre-commit", "--staged")
            working = temporary / "working"
            working.mkdir()
            print(f"CANDIDATE_FILES={export_working_tree(repo, working)}", flush=True)
            scan(executable, repo, config, temporary, "working_tree", "dir", str(working))
        print("PUBLICATION_CHECK=passed")
        print("No files staged or published. Automated detection does not replace review.")
        return 0
    except (PublicationError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("PUBLICATION_CHECK=failed")
        # OS/subprocess errors can contain sensitive path/argument data.
        print(str(error) if isinstance(error, PublicationError) else "Local scan failed; nothing was published")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
