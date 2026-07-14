"""Editable GeoSite/GeoIP policy rules rendered into fixed Mihomo providers."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import ManagedRulePolicy


@dataclass(frozen=True, slots=True)
class PolicyCategory:
    kind: str
    category: str
    label: str
    description: str = ""


# These names are maintained by MetaCubeX meta-rules-dat.  The catalogue makes
# the dashboard usable without accepting arbitrary geodata selectors.
POLICY_CATEGORIES: tuple[PolicyCategory, ...] = (
    PolicyCategory("GEOSITE", "category-ai-!cn", "AI: OpenAI, Claude, Gemini и другие"),
    PolicyCategory("GEOSITE", "openai", "OpenAI / ChatGPT"),
    PolicyCategory("GEOSITE", "anthropic", "Claude / Anthropic"),
    PolicyCategory("GEOSITE", "google", "Google"),
    PolicyCategory("GEOSITE", "github", "GitHub"),
    PolicyCategory("GEOSITE", "youtube", "YouTube"),
    PolicyCategory("GEOSITE", "telegram", "Telegram"),
    PolicyCategory("GEOSITE", "microsoft", "Microsoft"),
    PolicyCategory("GEOSITE", "apple", "Apple"),
    PolicyCategory("GEOSITE", "netflix", "Netflix"),
    PolicyCategory("GEOSITE", "spotify", "Spotify"),
    PolicyCategory("GEOSITE", "twitter", "X / Twitter"),
    PolicyCategory("GEOSITE", "tiktok", "TikTok"),
    # Russian GeoSite tags below are checked against the GeoSite.dat currently
    # installed on the gateway.  Broad categories are intentionally distinct
    # from individual services, so an administrator can make a narrow DIRECT
    # rule for a bank or marketplace instead of routing all Russian domains.
    PolicyCategory("GEOSITE", "category-bank-ru", "Банки и финансы РФ"),
    PolicyCategory("GEOSITE", "category-ecommerce-ru", "Маркетплейсы РФ"),
    PolicyCategory("GEOSITE", "category-retail-ru", "Ритейл и доставка РФ"),
    PolicyCategory("GEOSITE", "category-gov-ru", "Госуслуги и ведомства РФ"),
    PolicyCategory("GEOSITE", "category-entertainment-ru", "Видео и развлечения РФ"),
    PolicyCategory("GEOSITE", "category-media-ru", "СМИ и медиа РФ"),
    PolicyCategory("GEOSITE", "category-travel-ru", "Путешествия и билеты РФ"),
    PolicyCategory("GEOSITE", "category-medicine-ru", "Медицина и аптеки РФ"),
    PolicyCategory("GEOSITE", "category-ru", "Все сайты РФ — широкое правило"),
    PolicyCategory("GEOSITE", "ozon", "Ozon"),
    PolicyCategory("GEOSITE", "wildberries", "Wildberries"),
    PolicyCategory("GEOSITE", "avito", "Авито"),
    PolicyCategory("GEOSITE", "sber", "СберБанк"),
    PolicyCategory("GEOSITE", "tbank-ru", "Т-Банк / Росбанк"),
    PolicyCategory("GEOSITE", "yandex", "Яндекс"),
    PolicyCategory("GEOSITE", "vk", "VK"),
    PolicyCategory("GEOSITE", "mailru-group", "Mail.ru Group"),
    PolicyCategory("GEOSITE", "cdek", "СДЭК"),
    PolicyCategory("GEOSITE", "megafon", "МегаФон"),
    PolicyCategory("GEOSITE", "mts-ru", "МТС"),
    PolicyCategory("GEOSITE", "rostelecom", "Ростелеком"),
    PolicyCategory("GEOSITE", "t2-ru", "Т2"),
    PolicyCategory(
        "GEOSITE", "geolocation-!cn", "Зарубежные сайты (GeoSite)",
        "Широкая доменная категория за пределами Китая; может включать инфраструктуру и CDN.",
    ),
    PolicyCategory(
        "GEOSITE", "proxy", "VPN / прокси-инфраструктура",
        "Домены VPN, прокси и обходной инфраструктуры; это не категория всех зарубежных сайтов.",
    ),
    PolicyCategory("GEOIP", "google", "Google — IP-сети", "Срабатывает по IP назначения, а не по названию домена."),
    PolicyCategory("GEOIP", "telegram", "Telegram — IP-сети", "Срабатывает по IP назначения, а не по названию домена."),
    PolicyCategory("GEOIP", "netflix", "Netflix — IP-сети", "Срабатывает по IP назначения, а не по названию домена."),
    PolicyCategory("GEOIP", "cloudflare", "Cloudflare — IP-сети", "Общее правило CDN: может затронуть много несвязанных сайтов."),
    PolicyCategory("GEOIP", "cloudfront", "CloudFront — IP-сети", "Общее правило CDN: может затронуть много несвязанных сайтов."),
    PolicyCategory(
        "GEOIP", "US", "США — IP-сети",
        "Срабатывает по IP назначения, указанному как США в GeoIP. Сайт может быть не американским: CDN выбирает регион отдельно.",
    ),
    PolicyCategory(
        "GEOIP", "NL", "Нидерланды — IP-сети",
        "Срабатывает по IP назначения, указанному как Нидерланды в GeoIP. Сайт может быть не нидерландским: CDN выбирает регион отдельно.",
    ),
)

_CATEGORIES = {(item.kind, item.category): item for item in POLICY_CATEGORIES}
POLICY_ACTIONS = ("DIRECT", "VPS-FALLBACK", "WG-IMP", "HY2-NL")
_ACTION_FILES = {
    "DIRECT": "managed-direct.txt",
    "VPS-FALLBACK": "managed-fallback.txt",
    "WG-IMP": "managed-wg-imp.txt",
    "HY2-NL": "managed-hy2-nl.txt",
}
_ACTION_PROVIDERS = {
    "DIRECT": "managed-direct",
    "VPS-FALLBACK": "managed-fallback",
    "WG-IMP": "managed-wg-imp",
    "HY2-NL": "managed-hy2-nl",
}
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class ManagedRuleValidationError(ValueError):
    """A policy was not one of the finite supported GeoSite/GeoIP choices."""


class ManagedRuleConfigurationError(RuntimeError):
    """The one-time Mihomo provider bootstrap has not been applied."""


@dataclass(frozen=True, slots=True)
class PolicyRule:
    id: int
    kind: str
    category: str
    label: str
    action: str
    enabled: bool


class ManagedRuleService:
    """Atomically rewrite action-specific provider files then reload Mihomo."""

    def __init__(self, rules_dir: Path, mihomo: Any, session_factory: sessionmaker[Session]) -> None:
        self._rules_dir = Path(rules_dir)
        self._mihomo = mihomo
        self._session_factory = session_factory
        self._lock = _lock_for(self._rules_dir)

    def list_policies(self) -> list[PolicyRule]:
        with self._session_factory() as session:
            rows = session.scalars(select(ManagedRulePolicy).order_by(ManagedRulePolicy.id)).all()
        return [_to_policy(row) for row in rows]

    async def create(self, *, kind: str, category: str, action: str, enabled: bool) -> PolicyRule:
        kind, category, action = _validate(kind, category, action)
        if not isinstance(enabled, bool):
            raise ManagedRuleValidationError("enabled must be boolean")
        await asyncio.to_thread(self._lock.acquire)
        try:
            now = datetime.now(UTC)
            with self._session_factory.begin() as session:
                row = ManagedRulePolicy(kind=kind, category=category, action=action, enabled=enabled, created_at=now, updated_at=now)
                session.add(row)
                session.flush()
                policy_id = row.id
            try:
                await self._apply_current()
            except BaseException:
                with self._session_factory.begin() as session:
                    stale = session.get(ManagedRulePolicy, policy_id)
                    if stale is not None:
                        session.delete(stale)
                raise
            return next(item for item in self.list_policies() if item.id == policy_id)
        finally:
            self._lock.release()

    async def update(self, policy_id: int, *, kind: str, category: str, action: str, enabled: bool) -> PolicyRule:
        kind, category, action = _validate(kind, category, action)
        if not isinstance(enabled, bool):
            raise ManagedRuleValidationError("enabled must be boolean")
        await asyncio.to_thread(self._lock.acquire)
        try:
            with self._session_factory.begin() as session:
                row = session.get(ManagedRulePolicy, policy_id)
                if row is None:
                    raise KeyError(policy_id)
                previous = (row.kind, row.category, row.action, row.enabled, row.updated_at)
                row.kind, row.category, row.action, row.enabled = kind, category, action, enabled
                row.updated_at = datetime.now(UTC)
            try:
                await self._apply_current()
            except BaseException:
                with self._session_factory.begin() as session:
                    row = session.get(ManagedRulePolicy, policy_id)
                    if row is not None:
                        row.kind, row.category, row.action, row.enabled, row.updated_at = previous
                raise
            return next(item for item in self.list_policies() if item.id == policy_id)
        finally:
            self._lock.release()

    async def delete(self, policy_id: int) -> None:
        await asyncio.to_thread(self._lock.acquire)
        try:
            with self._session_factory.begin() as session:
                row = session.get(ManagedRulePolicy, policy_id)
                if row is None:
                    raise KeyError(policy_id)
                previous = (row.id, row.kind, row.category, row.action, row.enabled, row.created_at, row.updated_at)
                session.delete(row)
            try:
                await self._apply_current()
            except BaseException:
                with self._session_factory.begin() as session:
                    session.add(
                        ManagedRulePolicy(
                            id=previous[0], kind=previous[1], category=previous[2], action=previous[3], enabled=previous[4],
                            created_at=previous[5], updated_at=previous[6],
                        )
                    )
                raise
        finally:
            self._lock.release()

    async def _apply_current(self) -> None:
        providers = await self._mihomo.rule_providers()
        names = {getattr(item, "name", None) for item in providers}
        required = set(_ACTION_PROVIDERS.values())
        if not required <= names:
            raise ManagedRuleConfigurationError("managed Mihomo providers are not configured")
        policies = self.list_policies()
        content = {action: _provider_content(policies, action).encode("utf-8") for action in POLICY_ACTIONS}
        previous = {action: _read_or_none(self._rules_dir / filename) for action, filename in _ACTION_FILES.items()}
        try:
            for action, filename in _ACTION_FILES.items():
                _atomic_replace(self._rules_dir / filename, content[action])
            outcome = self._mihomo.reload()
            if asyncio.iscoroutine(outcome):
                await outcome
        except BaseException:
            for action, filename in _ACTION_FILES.items():
                _restore(self._rules_dir / filename, previous[action])
            raise


def _to_policy(row: ManagedRulePolicy) -> PolicyRule:
    category = _CATEGORIES.get((row.kind, row.category))
    if category is None:
        # A stale row must never become an arbitrary controller instruction.
        raise ManagedRuleValidationError("stored policy is no longer supported")
    return PolicyRule(row.id, row.kind, row.category, category.label, row.action, row.enabled)


def _validate(kind: str, category: str, action: str) -> tuple[str, str, str]:
    if not isinstance(kind, str) or not isinstance(category, str) or not isinstance(action, str):
        raise ManagedRuleValidationError("policy values must be text")
    normalized = (kind.strip().upper(), category.strip(), action.strip().upper())
    if normalized[:2] not in _CATEGORIES or normalized[2] not in POLICY_ACTIONS:
        raise ManagedRuleValidationError("unsupported policy rule")
    return normalized


def _provider_content(policies: list[PolicyRule], action: str) -> str:
    return "".join(f"{item.kind},{item.category}\n" for item in policies if item.enabled and item.action == action)


def _lock_for(directory: Path) -> threading.Lock:
    path = directory.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_replace(path, content)


def _atomic_replace(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
