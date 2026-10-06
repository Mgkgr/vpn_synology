from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.settings import Settings


TEST_FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


def valid_values() -> dict[str, str]:
    return {
        "mihomo_url": "http://vpn-wireguard:9091",
        "wgeasy_url": "http://vpn-wireguard:51821",
        "database_path": "/data/dashboard.sqlite3",
        "direct_rules_path": "/gateway/rules/direct.txt",
        "geodata_dir": "/geodata",
        "dashboard_encryption_key": TEST_FERNET_KEY,
    }


def test_rejects_a_non_lan_dashboard_bind() -> None:
    with pytest.raises(ValidationError):
        Settings(dashboard_bind="0.0.0.0:8080", **valid_values())


def test_daily_site_probes_are_opt_in(monkeypatch):
    monkeypatch.delenv("SITE_PROBES_ENABLED", raising=False)
    assert Settings(**valid_values()).site_probes_enabled is False
    monkeypatch.setenv("SITE_PROBES_ENABLED", "true")
    assert Settings(**valid_values()).site_probes_enabled is True


def test_accepts_the_localhost_reverse_proxy_bind() -> None:
    assert (
        Settings(dashboard_bind="127.0.0.1:8088", **valid_values()).dashboard_bind.endswith(":8088")
    )


def test_accepts_only_valid_trusted_proxy_cidr_networks() -> None:
    settings = Settings(
        dashboard_bind="127.0.0.1:8088",
        trusted_proxy_cidrs=("172.24.7.9/16",),
        **valid_values(),
    )

    assert settings.trusted_proxy_cidrs == ("172.24.0.0/16",)
    with pytest.raises(ValidationError):
        Settings(dashboard_bind="127.0.0.1:8088", trusted_proxy_cidrs=("not-a-network",), **valid_values())


def test_rejects_unsafe_body_limit_setting() -> None:
    with pytest.raises(ValidationError):
        Settings(dashboard_bind="127.0.0.1:8088", max_request_body_bytes=1023, **valid_values())


def test_defaults_geodata_to_the_dedicated_read_only_mount() -> None:
    values = valid_values()
    del values["geodata_dir"]

    assert Settings(dashboard_bind="127.0.0.1:8088", **values).geodata_dir.as_posix() == "/geodata"


def test_accepts_configurable_urls_for_every_read_only_service_probe() -> None:
    values = valid_values()
    values.update(
        wgeasy_url="http://wg-easy.internal:51821/health",
        mihomo_url="http://mihomo.internal:9091/controller",
        metacubexd_url="http://metacubexd.internal:9097/ui",
        kuma_url="http://kuma.internal:3001/status",
        mihomo_api_secret="controller-secret",
    )
    settings = Settings(dashboard_bind="127.0.0.1:8088", **values)

    assert str(settings.wgeasy_url).startswith("http://wg-easy.internal:51821")
    assert str(settings.mihomo_url).startswith("http://mihomo.internal:9091")
    assert str(settings.metacubexd_url).startswith("http://metacubexd.internal:9097")
    assert str(settings.kuma_url).startswith("http://kuma.internal:3001")
    assert settings.mihomo_api_secret is not None
    assert {"www.gstatic.com", "api.github.com", "www.google.com", "www.openai.com", "www.anthropic.com"} <= set(
        settings.delay_test_host_allowlist
    )
    assert settings.delay_test_urls == (
        "https://www.gstatic.com/generate_204",
        "https://api.github.com/",
        "https://www.google.com/generate_204",
    )


def test_rejects_mihomo_url_with_userinfo() -> None:
    values = valid_values()
    values["mihomo_url"] = "http://user:password@vpn-wireguard:9091"

    with pytest.raises(ValidationError):
        Settings(dashboard_bind="127.0.0.1:8088", **values)


def test_rejects_wgeasy_userinfo_without_echoing_it_from_validation() -> None:
    unsafe_url = "http://user:password@vpn-wireguard:51821"
    values = valid_values()
    values["wgeasy_url"] = unsafe_url

    with pytest.raises(ValidationError) as raised:
        Settings(dashboard_bind="127.0.0.1:8088", **values)

    assert unsafe_url not in str(raised.value)


def test_rejects_an_encryption_key_that_is_not_32_bytes() -> None:
    values = valid_values()
    values["dashboard_encryption_key"] = "bm90LXRoaXJ0eS10d28tYnl0ZXM="

    with pytest.raises(ValidationError):
        Settings(dashboard_bind="127.0.0.1:8088", **values)


def test_rejects_startup_without_persistent_dashboard_encryption_key() -> None:
    values = valid_values()
    values.pop("dashboard_encryption_key")

    with pytest.raises(ValidationError, match="dashboard_encryption_key"):
        Settings(dashboard_bind="127.0.0.1:8088", **values)


def test_reads_runtime_secrets_from_owner_only_files(monkeypatch, tmp_path) -> None:
    encryption_key_file = tmp_path / "dashboard_encryption_key"
    controller_secret_file = tmp_path / "mihomo_api_secret"
    encryption_key_file.write_text(TEST_FERNET_KEY + "\n", encoding="utf-8")
    controller_secret_file.write_text("controller-secret\n", encoding="utf-8")
    encryption_key_file.chmod(0o600)
    controller_secret_file.chmod(0o600)
    monkeypatch.setenv("DASHBOARD_ENCRYPTION_KEY_FILE", str(encryption_key_file))
    monkeypatch.setenv("MIHOMO_API_SECRET_FILE", str(controller_secret_file))
    monkeypatch.delenv("DASHBOARD_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("MIHOMO_API_SECRET", raising=False)

    settings = Settings.from_env()

    assert settings.dashboard_encryption_key.get_secret_value() == TEST_FERNET_KEY
    assert settings.mihomo_api_secret is not None
    assert settings.mihomo_api_secret.get_secret_value() == "controller-secret"


def test_rejects_group_readable_runtime_secret_file(monkeypatch, tmp_path) -> None:
    if os.name == "nt":
        pytest.skip("Windows test permissions do not model POSIX 0600 semantics")
    encryption_key_file = tmp_path / "dashboard_encryption_key"
    encryption_key_file.write_text(TEST_FERNET_KEY, encoding="utf-8")
    encryption_key_file.chmod(0o644)
    monkeypatch.setenv("DASHBOARD_ENCRYPTION_KEY_FILE", str(encryption_key_file))
    monkeypatch.delenv("DASHBOARD_ENCRYPTION_KEY", raising=False)

    with pytest.raises(ValueError, match="secret file"):
        Settings.from_env()
