from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
CONFIGURATOR = REPOSITORY / "deploy" / "scripts" / "configure-domain-sniffing.py"


def run_configurator(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CONFIGURATOR), str(path)],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )


def test_appends_pure_ip_domain_sniffer_to_a_config_without_sniffer(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("mixed-port: 7890\nrules:\n  - MATCH,DIRECT\n", encoding="utf-8")

    result = run_configurator(config)

    assert result.returncode == 0, result.stderr
    assert config.read_text(encoding="utf-8") == """mixed-port: 7890
rules:
  - MATCH,DIRECT

sniffer:
  enable: true
  force-dns-mapping: true
  parse-pure-ip: true
  override-destination: false
  sniff:
    HTTP:
      ports: [80, 8080-8880]
      override-destination: true
    TLS:
      ports: [443, 8443]
    QUIC:
      ports: [443, 8443]
"""


def test_refuses_to_overwrite_an_existing_sniffer_block(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    original = """sniffer:
  enable: false
rules:
  - MATCH,DIRECT
"""
    config.write_text(original, encoding="utf-8")

    result = run_configurator(config)

    assert result.returncode == 3
    assert "sniffer is already configured" in result.stderr
    assert config.read_text(encoding="utf-8") == original
