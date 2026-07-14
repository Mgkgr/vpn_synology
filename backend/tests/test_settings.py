from __future__ import annotations

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
        "geodata_dir": "/gateway/geodata",
        "dashboard_encryption_key": TEST_FERNET_KEY,
    }


def test_rejects_a_non_lan_dashboard_bind() -> None:
    with pytest.raises(ValidationError):
        Settings(dashboard_bind="0.0.0.0:8080", **valid_values())


def test_accepts_the_localhost_reverse_proxy_bind() -> None:
    assert (
        Settings(dashboard_bind="127.0.0.1:8088", **valid_values()).dashboard_bind.endswith(":8088")
    )


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
        Settings(dashboard_bind="192.168.2.103:8088", **values)


def test_rejects_wgeasy_userinfo_without_echoing_it_from_validation() -> None:
    unsafe_url = "http://user:password@vpn-wireguard:51821"
    values = valid_values()
    values["wgeasy_url"] = unsafe_url

    with pytest.raises(ValidationError) as raised:
        Settings(dashboard_bind="192.168.2.103:8088", **values)

    assert unsafe_url not in str(raised.value)


def test_rejects_an_encryption_key_that_is_not_32_bytes() -> None:
    values = valid_values()
    values["dashboard_encryption_key"] = "bm90LXRoaXJ0eS10d28tYnl0ZXM="

    with pytest.raises(ValidationError):
        Settings(dashboard_bind="192.168.2.103:8088", **values)


def test_rejects_startup_without_persistent_dashboard_encryption_key() -> None:
    values = valid_values()
    values.pop("dashboard_encryption_key")

    with pytest.raises(ValidationError, match="dashboard_encryption_key"):
        Settings(dashboard_bind="127.0.0.1:8088", **values)
