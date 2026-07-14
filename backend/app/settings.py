from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, field_validator
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.probe_targets import APPROVED_PROBE_TARGETS, approved_probe_hosts


DELAY_TEST_HOST_ALLOWLIST = approved_probe_hosts()
DELAY_TEST_URLS = tuple(item.url for item in APPROVED_PROBE_TARGETS if item.default_enabled)


class Settings(BaseSettings):
    """Runtime configuration for the isolated VPN dashboard."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", hide_input_in_errors=True, validate_default=True)

    dashboard_bind: str = "127.0.0.1:8088"
    mihomo_url: AnyHttpUrl = "http://vpn-wireguard:9091"
    wgeasy_url: AnyHttpUrl = "http://vpn-wireguard:51821"
    metacubexd_url: AnyHttpUrl = "http://metacubexd:80"
    kuma_url: AnyHttpUrl = "http://uptime-kuma:3001"
    mihomo_api_secret: SecretStr | None = None
    dashboard_encryption_key: SecretStr
    delay_test_host_allowlist: tuple[str, ...] = DELAY_TEST_HOST_ALLOWLIST
    delay_test_urls: tuple[str, ...] = DELAY_TEST_URLS
    database_path: Path = Path("/data/dashboard.sqlite3")
    direct_rules_path: Path = Path("/gateway/rules/direct.txt")
    geodata_dir: Path = Path("/gateway/geodata")

    @field_validator("dashboard_bind")
    @classmethod
    def require_nas_lan_bind(cls, value: str) -> str:
        if value != "127.0.0.1:8088":
            raise ValueError("dashboard_bind must be 127.0.0.1:8088")
        return value

    @field_validator("mihomo_url")
    @classmethod
    def reject_mihomo_url_userinfo(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        parsed = urlsplit(str(value))
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("mihomo_url must not contain userinfo")
        return value

    @field_validator("wgeasy_url")
    @classmethod
    def reject_wgeasy_url_userinfo(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        parsed = urlsplit(str(value))
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("wgeasy_url must not contain userinfo")
        return value

    @field_validator("dashboard_encryption_key")
    @classmethod
    def require_fernet_key(cls, value: SecretStr) -> SecretStr:
        try:
            encoded_key = value.get_secret_value().encode("ascii")
            decoded_key = base64.urlsafe_b64decode(encoded_key)
        except (UnicodeEncodeError, ValueError) as error:
            raise ValueError("dashboard_encryption_key must be URL-safe base64") from error
        if len(decoded_key) != 32 or base64.urlsafe_b64encode(decoded_key) != encoded_key:
            raise ValueError("dashboard_encryption_key must encode exactly 32 bytes")
        return value

    @field_validator("delay_test_host_allowlist")
    @classmethod
    def restrict_delay_test_host_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != DELAY_TEST_HOST_ALLOWLIST:
            raise ValueError("delay_test_host_allowlist must use the approved hosts")
        return value

    @field_validator("delay_test_urls")
    @classmethod
    def restrict_delay_test_urls(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != DELAY_TEST_URLS:
            raise ValueError("delay_test_urls are managed by the dashboard probe catalogue")
        return value

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from the process environment and safe defaults."""
        return cls()
