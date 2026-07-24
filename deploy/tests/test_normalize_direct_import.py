from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
NORMALIZER = REPOSITORY / "deploy" / "scripts" / "normalize-direct-import.py"


def normalize(source: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], str]:
    input_path = tmp_path / "source.txt"
    output_path = tmp_path / "normalized.txt"
    input_path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(NORMALIZER), str(input_path), str(output_path)],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, output_path.read_text(encoding="utf-8") if output_path.exists() else ""


def test_normalizes_domain_rows_and_a_glued_ip_cidr_without_duplicates(tmp_path: Path) -> None:
    result, text = normalize(
        """DOMAIN-SUFFIX,gov.ru
DOMAIN-SUFFIX,www.gosuslugi.ru
DOMAIN-SUFFIX,gov.ru
DOMAIN-SUFFIX,myyp47.news.ozon.ruIP-CIDR,192.168.3.0/24,DIRECT,no-resolve
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert text == """DOMAIN-SUFFIX,gov.ru,DIRECT
DOMAIN-SUFFIX,www.gosuslugi.ru,DIRECT
DOMAIN-SUFFIX,myyp47.news.ozon.ru,DIRECT
IP-CIDR,192.168.3.0/24,DIRECT
"""
    assert "NORMALIZED_RULES=4" in result.stdout
    assert "DUPLICATE_INPUT_RULES=1" in result.stdout


def test_rejects_non_direct_or_invalid_import_rows_without_writing_output(tmp_path: Path) -> None:
    result, text = normalize("DOMAIN-SUFFIX,gov.ru,WG-IMP\n", tmp_path)

    assert result.returncode == 2
    assert "line 1 is not an importable DIRECT rule" in result.stderr
    assert text == ""
