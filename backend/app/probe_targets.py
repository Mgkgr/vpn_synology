"""Safe delay-test targets and their small persisted control plane."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import ProbeTarget
from app.maintenance_client import mark_mutation


@dataclass(frozen=True, slots=True)
class ApprovedProbeTarget:
    key: str
    label: str
    url: str
    default_enabled: bool
    position: int


# Built-ins are maintained as a useful starting catalogue. Custom targets are
# separately validated before they can ever reach Mihomo's delay endpoint.
APPROVED_PROBE_TARGETS: tuple[ApprovedProbeTarget, ...] = (
    ApprovedProbeTarget("gstatic", "Google Static", "https://www.gstatic.com/generate_204", True, 10),
    ApprovedProbeTarget("github", "GitHub API", "https://api.github.com/", True, 20),
    ApprovedProbeTarget("google", "Google", "https://www.google.com/generate_204", True, 30),
    ApprovedProbeTarget("cloudflare", "Cloudflare", "https://cp.cloudflare.com/generate_204", False, 40),
    ApprovedProbeTarget("openai", "OpenAI", "https://www.openai.com/", False, 50),
    ApprovedProbeTarget("anthropic", "Claude / Anthropic", "https://www.anthropic.com/", False, 60),
    ApprovedProbeTarget("youtube", "YouTube", "https://www.youtube.com/generate_204", False, 70),
    # Russia: these remain opt-in so a scheduled check never changes the
    # footprint or meaning of the existing international health checks.
    ApprovedProbeTarget("yandex", "Яндекс", "https://ya.ru/", False, 100),
    ApprovedProbeTarget("ozon", "Ozon", "https://www.ozon.ru/", False, 110),
    ApprovedProbeTarget("wildberries", "Wildberries", "https://www.wildberries.ru/", False, 120),
    ApprovedProbeTarget("avito", "Авито", "https://www.avito.ru/", False, 130),
    ApprovedProbeTarget("sber", "СберБанк", "https://www.sberbank.ru/", False, 140),
    ApprovedProbeTarget("tbank", "Т-Банк", "https://www.tbank.ru/", False, 150),
    ApprovedProbeTarget("alfabank", "Альфа-Банк", "https://alfabank.ru/", False, 160),
    ApprovedProbeTarget("vtb", "ВТБ", "https://www.vtb.ru/", False, 170),
    ApprovedProbeTarget("gosuslugi", "Госуслуги", "https://www.gosuslugi.ru/", False, 180),
    ApprovedProbeTarget("nalog", "ФНС России", "https://www.nalog.gov.ru/", False, 190),
)

_BY_KEY = {item.key: item for item in APPROVED_PROBE_TARGETS}
_CUSTOM_PREFIX = "custom:"
Resolver = Callable[[str], Collection[ipaddress.IPv4Address | ipaddress.IPv6Address]]


class ProbeTargetValidationError(ValueError):
    """A custom URL is not a public HTTPS target suitable for Mihomo."""


@dataclass(frozen=True, slots=True)
class ProbeTargetState:
    key: str
    label: str
    url: str
    enabled: bool
    position: int
    is_custom: bool = False


class ProbeTargetService:
    """Manage built-in targets plus safely bounded public HTTPS custom targets."""

    def __init__(self, session_factory: sessionmaker[Session], *, resolver: Resolver | None = None) -> None:
        self._session_factory = session_factory
        self._resolver = resolver or _resolve_public_host

    def list_targets(self) -> list[ProbeTargetState]:
        with self._session_factory() as session:
            rows = {row.key: row for row in session.scalars(select(ProbeTarget)).all()}
        built_ins = [
            ProbeTargetState(
                key=item.key,
                label=item.label,
                url=item.url,
                enabled=rows.get(item.key).enabled if item.key in rows else item.default_enabled,
                position=item.position,
            )
            for item in APPROVED_PROBE_TARGETS
        ]
        custom = [
            _state_from_row(row)
            for key, row in rows.items()
            if key.startswith(_CUSTOM_PREFIX)
        ]
        return sorted((*built_ins, *custom), key=lambda item: (item.position, item.key))

    def enabled_urls(self) -> tuple[str, ...]:
        safe: list[str] = []
        for item in self.list_targets():
            if not item.enabled:
                continue
            if item.is_custom:
                try:
                    safe.append(self._normalize_custom_url(item.url))
                except ProbeTargetValidationError:
                    # DNS rebinding or a no-longer-public host must not turn a
                    # scheduled probe into an SSRF request.
                    continue
            else:
                safe.append(item.url)
        return tuple(safe)

    def enabled_hosts(self) -> tuple[str, ...]:
        return tuple(sorted({urlsplit(url).hostname for url in self.enabled_urls() if urlsplit(url).hostname}))

    def enabled_target(self, key: str) -> ProbeTargetState:
        """Return one enabled, still-safe target for a manual diagnosis."""

        if not isinstance(key, str) or not key.strip():
            raise ValueError("probe target is invalid")
        target = next((item for item in self.list_targets() if item.key == key), None)
        if target is None:
            raise KeyError(key)
        if not target.enabled:
            raise ValueError("probe target is disabled")
        if target.is_custom:
            return ProbeTargetState(
                key=target.key,
                label=target.label,
                url=self._normalize_custom_url(target.url),
                enabled=True,
                position=target.position,
                is_custom=True,
            )
        return target

    def set_enabled(self, requested: Mapping[str, bool]) -> list[ProbeTargetState]:
        current = {item.key for item in self.list_targets()}
        if set(requested) != current or any(not isinstance(value, bool) for value in requested.values()):
            raise ValueError("probe target selection is invalid")
        mark_mutation()
        with self._session_factory.begin() as session:
            rows = {row.key: row for row in session.scalars(select(ProbeTarget)).all()}
            for item in APPROVED_PROBE_TARGETS:
                row = rows.get(item.key)
                if row is None:
                    session.add(
                        ProbeTarget(
                            key=item.key,
                            label=item.label,
                            url=item.url,
                            enabled=requested[item.key],
                            position=item.position,
                        )
                    )
                else:
                    row.enabled = requested[item.key]
            for key, row in rows.items():
                if key.startswith(_CUSTOM_PREFIX):
                    row.enabled = requested[key]
        return self.list_targets()

    def create_custom_target(self, *, label: str, url: str) -> ProbeTargetState:
        normalized_label = _normalized_label(label)
        normalized_url = self._normalize_custom_url(url)
        mark_mutation()
        with self._session_factory.begin() as session:
            largest_position = session.scalar(select(ProbeTarget.position).order_by(ProbeTarget.position.desc()).limit(1))
            row = ProbeTarget(
                key=f"{_CUSTOM_PREFIX}{uuid4().hex}",
                label=normalized_label,
                url=normalized_url,
                enabled=True,
                position=(largest_position or 0) + 10,
            )
            session.add(row)
            session.flush()
            return _state_from_row(row)

    def update_custom_target(self, key: str, *, label: str, url: str, enabled: bool) -> ProbeTargetState:
        if not key.startswith(_CUSTOM_PREFIX) or not isinstance(enabled, bool):
            raise ValueError("custom probe target is invalid")
        normalized_label = _normalized_label(label)
        normalized_url = self._normalize_custom_url(url)
        with self._session_factory.begin() as session:
            row = session.get(ProbeTarget, key)
            if row is None or not row.key.startswith(_CUSTOM_PREFIX):
                raise KeyError(key)
            mark_mutation()
            row.label = normalized_label
            row.url = normalized_url
            row.enabled = enabled
            session.flush()
            return _state_from_row(row)

    def delete_custom_target(self, key: str) -> None:
        if not key.startswith(_CUSTOM_PREFIX):
            raise ValueError("custom probe target is invalid")
        with self._session_factory.begin() as session:
            row = session.get(ProbeTarget, key)
            if row is None or not row.key.startswith(_CUSTOM_PREFIX):
                raise KeyError(key)
            mark_mutation()
            session.delete(row)

    def _normalize_custom_url(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ProbeTargetValidationError("URL is required")
        try:
            parsed = urlsplit(value.strip())
        except ValueError as error:
            raise ProbeTargetValidationError("invalid URL") from error
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.query
            or parsed.fragment
        ):
            raise ProbeTargetValidationError("only public HTTPS URLs on port 443 are allowed")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise ProbeTargetValidationError("an HTTPS hostname is required")
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii").lower()
            addresses = tuple(self._resolver(hostname))
        except (UnicodeError, OSError, ValueError) as error:
            raise ProbeTargetValidationError("hostname cannot be resolved safely") from error
        if not addresses or any(not address.is_global for address in addresses):
            raise ProbeTargetValidationError("hostname must resolve only to public addresses")
        return urlunsplit(("https", hostname, parsed.path or "/", "", ""))


def approved_probe_urls() -> tuple[str, ...]:
    return tuple(item.url for item in APPROVED_PROBE_TARGETS)


def approved_probe_hosts() -> tuple[str, ...]:
    return tuple(sorted({urlsplit(item.url).hostname for item in APPROVED_PROBE_TARGETS if urlsplit(item.url).hostname}))


def validate_approved_probe_urls(urls: Collection[str]) -> tuple[str, ...]:
    normalized = tuple(urls)
    approved = set(approved_probe_urls())
    if len(set(normalized)) != len(normalized) or any(url not in approved for url in normalized):
        raise ValueError("probe URLs must use the approved catalogue")
    return normalized


def _state_from_row(row: ProbeTarget) -> ProbeTargetState:
    return ProbeTargetState(row.key, row.label, row.url, row.enabled, row.position, True)


def _normalized_label(value: str) -> str:
    if not isinstance(value, str) or not (label := value.strip()) or len(label) > 255:
        raise ProbeTargetValidationError("label is required")
    return label


def _resolve_public_host(hostname: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    raw_addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    return tuple({ipaddress.ip_address(item[4][0]) for item in raw_addresses})
