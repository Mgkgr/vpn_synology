from __future__ import annotations

from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
IMPORTER = REPOSITORY / "deploy" / "scripts" / "import-direct-rules.sh"


def test_stages_the_normalized_rules_through_the_dashboard_tmpfs() -> None:
    source = IMPORTER.read_text(encoding="utf-8")

    assert '"$DOCKER_BIN" cp ' not in source
    assert '"$DOCKER_BIN" exec -i "$DASHBOARD_CONTAINER" sh -c' in source
    assert 'cat > "$container_input"' in source
