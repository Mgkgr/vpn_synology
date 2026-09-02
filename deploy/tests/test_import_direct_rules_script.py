from __future__ import annotations

from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
IMPORTER = REPOSITORY / "deploy" / "scripts" / "import-direct-rules.sh"
LAUNCHER = REPOSITORY / "deploy" / "import-direct-rules.ps1"


def test_stages_the_normalized_rules_through_the_dashboard_tmpfs() -> None:
    source = IMPORTER.read_text(encoding="utf-8")

    assert '"$DOCKER_BIN" cp ' not in source
    assert '"$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" sh -c' in source
    assert 'cat > "$container_input"' in source


def test_normalizes_a_legacy_current_provider_before_the_dashboard_compares_it() -> None:
    source = IMPORTER.read_text(encoding="utf-8")

    assert 'legacy_current=$(mktemp' in source
    assert '"$NORMALIZER" "$DIRECT_FILE" "$legacy_current"' in source
    assert 'container_current=/tmp/direct-current.txt' in source
    assert 'Path("/tmp/direct-current.txt").read_text(encoding="utf-8")' in source


def test_launcher_accepts_a_single_additional_rule_without_a_source_file() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "[string]$InputPath = ''" in source
    assert "if (-not $InputPath -and $AdditionalRule.Count -eq 0)" in source
    assert "[System.IO.File]::WriteAllText($temporaryInput, '', [System.Text.UTF8Encoding]::new($false))" in source
