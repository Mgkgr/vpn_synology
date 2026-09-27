from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from app.settings import Settings


def test_registry_keeps_fallback_separate():
    from app.outbounds import FALLBACK_MEMBERS, build_outbound_registry

    baseline = build_outbound_registry()
    assert [(entry.id, entry.label, entry.engine, entry.probe_name) for entry in baseline] == [
        ("WG-IMP", "VLESS-NL", "vless", "DASH-HEALTH-WG-IMP"),
        ("HY2-USA", "HY2-DE", "hysteria2", "DASH-HEALTH-HY2-USA"),
    ]
    for engine in ("zapret2", "byedpi"):
        entries = build_outbound_registry(engine)
        assert len(entries) == 3
        assert entries[:2] == baseline
        assert entries[2].id == "ANTIDPI"
        assert entries[2].engine == engine
    assert FALLBACK_MEMBERS == ("WG-IMP", "HY2-USA")
    with pytest.raises(FrozenInstanceError):
        baseline[0].label = "modified"
    with pytest.raises(ValueError):
        build_outbound_registry("untrusted")


def test_health_settings_are_opt_in_and_engine_is_validated(monkeypatch):
    monkeypatch.delenv("OUTBOUND_HEALTH_ENABLED", raising=False)
    monkeypatch.delenv("ANTIDPI_ENGINE", raising=False)
    key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    settings = Settings(dashboard_encryption_key=key)
    assert settings.outbound_health_enabled is False
    assert settings.antidpi_engine is None
    monkeypatch.setenv("OUTBOUND_HEALTH_ENABLED", "true")
    monkeypatch.setenv("ANTIDPI_ENGINE", "zapret2")
    enabled = Settings(dashboard_encryption_key=key)
    assert enabled.outbound_health_enabled is True
    assert enabled.antidpi_engine == "zapret2"
    with pytest.raises(ValidationError):
        Settings(dashboard_encryption_key=key, antidpi_engine="shell")
