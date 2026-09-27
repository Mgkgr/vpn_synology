"""Read-only client for the Mihomo external controller API."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx

from app.settings import DELAY_TEST_HOST_ALLOWLIST


CONTROL_ENDPOINTS = {
    "cloudflare": "https://cp.cloudflare.com/generate_204",
    "google": "https://www.google.com/generate_204",
    "github": "https://api.github.com/",
}
CONTROL_PROBE_NAMES = frozenset({"DASH-HEALTH-WG-IMP", "DASH-HEALTH-HY2-USA", "DASH-HEALTH-ANTIDPI"})


class MihomoIntegrationError(RuntimeError):
    """A structured controller failure whose fields never include request credentials."""

    def __init__(self, operation: str, reason: str, status_code: int | None = None) -> None:
        self.operation = operation
        self.reason = reason
        self.status_code = status_code
        super().__init__(self.__str__())

    def __str__(self) -> str:
        status = f" (HTTP {self.status_code})" if self.status_code is not None else ""
        return f"Mihomo {self.operation} failed: {self.reason}{status}"


@dataclass(frozen=True, slots=True)
class MihomoVersion:
    version: str


@dataclass(frozen=True, slots=True)
class ProxyGroup:
    name: str
    type: str | None
    proxies: tuple[str, ...]
    now: str | None


@dataclass(frozen=True, slots=True)
class ControllerRule:
    type: str
    payload: str
    proxy: str | None


@dataclass(frozen=True, slots=True)
class RuleProvider:
    name: str
    behavior: str | None
    rule_count: int | None


@dataclass(frozen=True, slots=True)
class Connection:
    id: str
    chain: tuple[str, ...]
    rule: str


@dataclass(frozen=True, slots=True)
class Traffic:
    up: int
    down: int


@dataclass(frozen=True, slots=True)
class ProxyDelay:
    delay_ms: int


@dataclass(frozen=True, slots=True)
class DnsLookup:
    """A bounded, browser-safe DNS answer returned by Mihomo itself."""

    hostname: str
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ControllerWriteResult:
    operation: str
    status_code: int


class MihomoClient:
    """Call documented controller endpoints without exposing raw controller payloads."""

    def __init__(
        self,
        controller_url: str,
        *,
        secret: str | None = None,
        timeout: float = 5.0,
        delay_timeout_ms: int = 5000,
        traffic_sample_timeout: float = 5.0,
        delay_test_host_allowlist: Collection[str] = DELAY_TEST_HOST_ALLOWLIST,
        delay_test_host_supplier: Callable[[], Collection[str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            base_url = httpx.URL(controller_url)
        except (TypeError, ValueError) as error:
            raise MihomoIntegrationError("configuration", "invalid controller URL") from error
        if (
            base_url.scheme not in {"http", "https"}
            or base_url.host is None
            or base_url.query
            or bool(base_url.username or base_url.password)
        ):
            raise MihomoIntegrationError("configuration", "invalid controller URL")
        if isinstance(delay_timeout_ms, bool) or not isinstance(delay_timeout_ms, int) or delay_timeout_ms <= 0:
            raise MihomoIntegrationError("configuration", "invalid delay timeout")
        if (
            isinstance(traffic_sample_timeout, bool)
            or not isinstance(traffic_sample_timeout, (int, float))
            or traffic_sample_timeout <= 0
        ):
            raise MihomoIntegrationError("configuration", "invalid traffic sample timeout")
        try:
            allowed_hosts = frozenset(_normalized_hostname(host) for host in delay_test_host_allowlist)
        except (TypeError, ValueError) as error:
            raise MihomoIntegrationError("configuration", "invalid delay host allowlist") from error
        if not allowed_hosts:
            raise MihomoIntegrationError("configuration", "invalid delay host allowlist")

        self._base_url = base_url.copy_with(path=base_url.path.rstrip("/") + "/")
        self._secret = secret.strip() if secret and secret.strip() else None
        self._timeout = timeout
        self._delay_timeout_ms = delay_timeout_ms
        self._traffic_sample_timeout = float(traffic_sample_timeout)
        self._delay_test_host_allowlist = allowed_hosts
        self._delay_test_host_supplier = delay_test_host_supplier
        self._transport = transport

    async def version(self) -> MihomoVersion:
        payload = await self._json("version", "GET", "version")
        try:
            return MihomoVersion(version=_required_text(payload.get("version")))
        except (TypeError, ValueError) as error:
            raise self._invalid_payload("version") from error

    async def groups(self) -> list[ProxyGroup]:
        payload = await self._json("groups", "GET", "group")
        try:
            raw_groups = payload.get("proxies")
            groups: list[ProxyGroup] = []
            if isinstance(raw_groups, Mapping):
                items = []
                for name, value in raw_groups.items():
                    if not isinstance(value, Mapping):
                        raise ValueError("group must be an object")
                    items.append((name, value))
            elif isinstance(raw_groups, list):
                items = []
                for value in raw_groups:
                    if not isinstance(value, Mapping):
                        raise ValueError("group must be an object")
                    items.append((value.get("name"), value))
            else:
                raise ValueError("proxies must be a list or object")

            for fallback_name, group in items:
                name = _required_text(group.get("name", fallback_name))
                group_type = _optional_text(group.get("type"))
                raw_proxies = group.get("all", group.get("proxies", []))
                groups.append(
                    ProxyGroup(
                        name=name,
                        type=group_type,
                        proxies=_text_tuple(raw_proxies),
                        now=_optional_text(group.get("now")),
                    )
                )
            return groups
        except (TypeError, ValueError) as error:
            raise self._invalid_payload("groups") from error

    async def rules(self) -> list[ControllerRule]:
        payload = await self._json("rules", "GET", "rules")
        try:
            raw_rules = payload.get("rules")
            if not isinstance(raw_rules, list):
                raise ValueError("rules must be a list")
            result: list[ControllerRule] = []
            for rule in raw_rules:
                if not isinstance(rule, Mapping):
                    raise ValueError("rule must be an object")
                result.append(
                    ControllerRule(
                        type=_required_text(rule.get("type")),
                        payload=_text_or_empty(rule.get("payload")),
                        proxy=_optional_text(rule.get("proxy")),
                    )
                )
            return result
        except (TypeError, ValueError) as error:
            raise self._invalid_payload("rules") from error

    async def rule_providers(self) -> list[RuleProvider]:
        payload = await self._json("rule_providers", "GET", "providers/rules")
        try:
            raw_providers = payload.get("providers")
            providers: list[RuleProvider] = []
            if isinstance(raw_providers, Mapping):
                items = raw_providers.items()
            elif isinstance(raw_providers, list):
                items = ((_required_text(value.get("name")), value) for value in raw_providers if isinstance(value, Mapping))
                if len(raw_providers) != sum(1 for value in raw_providers if isinstance(value, Mapping)):
                    raise ValueError("provider must be an object")
            else:
                raise ValueError("providers must be a list or object")

            for name, provider in items:
                if not isinstance(provider, Mapping):
                    raise ValueError("provider must be an object")
                rule_count = provider.get("ruleCount", provider.get("count"))
                providers.append(
                    RuleProvider(
                        name=_required_text(name),
                        behavior=_optional_text(provider.get("behavior")),
                        rule_count=_optional_nonnegative_int(rule_count),
                    )
                )
            return providers
        except (TypeError, ValueError) as error:
            raise self._invalid_payload("rule_providers") from error

    async def connections(self) -> list[Connection]:
        payload = await self._json("connections", "GET", "connections")
        try:
            raw_connections = payload.get("connections")
            if not isinstance(raw_connections, list):
                raise ValueError("connections must be a list")
            result: list[Connection] = []
            for connection in raw_connections:
                if not isinstance(connection, Mapping):
                    raise ValueError("connection must be an object")
                result.append(
                    Connection(
                        id=_required_text(connection.get("id")),
                        chain=_text_tuple(connection.get("chains", connection.get("chain", []))),
                        rule=_normalized_rule(connection.get("rule")),
                    )
                )
            return result
        except (TypeError, ValueError) as error:
            raise self._invalid_payload("connections") from error

    async def traffic(self) -> Traffic:
        payload = await self._first_traffic_sample()
        try:
            return Traffic(
                up=_nonnegative_int(payload.get("up", payload.get("Up"))),
                down=_nonnegative_int(payload.get("down", payload.get("Down"))),
            )
        except (TypeError, ValueError) as error:
            raise self._invalid_payload("traffic") from error

    async def proxy_delay(self, name: str, url: str) -> ProxyDelay:
        try:
            proxy_name = _required_text(name)
            test_url = self._validated_delay_url(url)
        except (TypeError, ValueError) as error:
            raise MihomoIntegrationError("proxy_delay", "unsafe delay test URL") from error
        payload = await self._json(
            "proxy_delay",
            "GET",
            f"proxies/{quote(proxy_name, safe='')}/delay",
            params={"url": test_url, "timeout": str(self._delay_timeout_ms)},
        )
        try:
            return ProxyDelay(delay_ms=_nonnegative_int(payload.get("delay")))
        except (TypeError, ValueError) as error:
            raise self._invalid_payload("proxy_delay") from error

    async def control_delay(self, probe_name: str, endpoint_key: Literal["cloudflare", "google", "github"]) -> ProxyDelay:
        """Internal probes never accept user URLs or touch a working proxy's /delay."""
        if probe_name not in CONTROL_PROBE_NAMES or endpoint_key not in CONTROL_ENDPOINTS:
            raise MihomoIntegrationError("control_delay", "invalid control target")
        payload = await self._json(
            "control_delay", "GET", f"proxies/{quote(probe_name, safe='')}/delay",
            params={"url": CONTROL_ENDPOINTS[endpoint_key], "timeout": "10000"}, timeout=12.0,
        )
        try:
            delay = _nonnegative_int(payload.get("delay"))
            if delay == 0:
                raise ValueError
            return ProxyDelay(delay)
        except (TypeError, ValueError) as error:
            raise self._invalid_payload("control_delay") from error

    async def dns_lookup(self, url: str) -> DnsLookup:
        """Resolve one approved probe hostname through Mihomo DNS.

        This is deliberately restricted to the same safe target catalogue as
        delay checks, so an administrator cannot turn diagnostics into an
        arbitrary DNS proxy or an internal-network probe.
        """

        try:
            test_url = self._validated_delay_url(url)
            hostname = _normalized_hostname(httpx.URL(test_url).host)
        except (TypeError, ValueError) as error:
            raise MihomoIntegrationError("dns_lookup", "unsafe delay test URL") from error
        payload = await self._json("dns_lookup", "GET", "dns/query", params={"name": hostname, "type": "A"})
        try:
            return DnsLookup(hostname=hostname, addresses=_dns_addresses(payload))
        except (TypeError, ValueError) as error:
            raise self._invalid_payload("dns_lookup") from error

    async def geo_upgrade(self) -> ControllerWriteResult:
        """Explicitly request a GeoData update; callers must opt in to this write."""

        response = await self._request(
            "geo_upgrade", "POST", "upgrade/geo", json_body={"path": "", "payload": ""}
        )
        return ControllerWriteResult(operation="geo_upgrade", status_code=response.status_code)

    async def reload(self) -> ControllerWriteResult:
        """Explicitly reload controller config; never invoked by a read operation."""

        response = await self._request(
            "reload", "PUT", "configs", params={"force": "true"}, json_body={"path": "", "payload": ""}
        )
        return ControllerWriteResult(operation="reload", status_code=response.status_code)

    async def _json(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        response = await self._request(operation, method, path, params=params, timeout=timeout)
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise MihomoIntegrationError(operation, "invalid JSON response", response.status_code) from error
        if not isinstance(payload, Mapping):
            raise self._invalid_payload(operation, response.status_code)
        return payload

    async def _request(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout if timeout is None else timeout,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.request(method, path, params=params, json=json_body, headers=self._headers())
        except httpx.TimeoutException as error:
            raise MihomoIntegrationError(operation, "controller request timed out") from error
        except httpx.HTTPError as error:
            raise MihomoIntegrationError(operation, "controller request failed") from error

        self._require_success(operation, response)
        return response

    async def _first_traffic_sample(self) -> Mapping[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", "traffic", headers=self._headers()) as response:
                    self._require_success("traffic", response)
                    try:
                        async with asyncio.timeout(self._traffic_sample_timeout):
                            async for line in response.aiter_lines():
                                sample = line.strip()
                                if not sample:
                                    continue
                                try:
                                    payload = json.loads(sample)
                                except (TypeError, ValueError) as error:
                                    raise MihomoIntegrationError("traffic", "invalid traffic stream") from error
                                if not isinstance(payload, Mapping):
                                    raise MihomoIntegrationError("traffic", "invalid traffic stream")
                                return payload
                    except TimeoutError as error:
                        raise MihomoIntegrationError("traffic", "traffic sample timed out") from error
                    raise MihomoIntegrationError("traffic", "traffic stream contained no sample")
        except MihomoIntegrationError:
            raise
        except httpx.TimeoutException as error:
            raise MihomoIntegrationError("traffic", "controller request timed out") from error
        except httpx.HTTPError as error:
            raise MihomoIntegrationError("traffic", "controller request failed") from error

    def _require_success(self, operation: str, response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        if response.status_code == 401:
            reason = "authentication rejected" if self._secret else "authentication required but no Mihomo secret is configured"
            raise MihomoIntegrationError(operation, reason, response.status_code)
        raise MihomoIntegrationError(operation, "unexpected controller response", response.status_code)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret}"} if self._secret else {}

    def _validated_delay_url(self, value: str) -> str:
        test_url = _required_text(value)
        try:
            parsed = httpx.URL(test_url)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid delay URL") from error
        if (
            parsed.scheme != "https"
            or parsed.host is None
            or bool(parsed.username or parsed.password)
            or parsed.port not in {None, 443}
            or parsed.query
            or parsed.fragment
            or _normalized_hostname(parsed.host) not in self._allowed_delay_hosts()
        ):
            raise ValueError("unsafe delay URL")
        return test_url

    def _allowed_delay_hosts(self) -> frozenset[str]:
        values = self._delay_test_host_supplier() if self._delay_test_host_supplier is not None else self._delay_test_host_allowlist
        try:
            hosts = frozenset(_normalized_hostname(host) for host in values)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid delay host supplier") from error
        if not hosts:
            raise ValueError("empty delay host supplier")
        return hosts

    @staticmethod
    def _invalid_payload(operation: str, status_code: int | None = None) -> MihomoIntegrationError:
        return MihomoIntegrationError(operation, "invalid controller payload", status_code)


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("non-empty string required")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _required_text(value)


def _text_or_empty(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("string required")
    return value.strip()


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("string list required")
    return tuple(_required_text(item) for item in value)


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("non-negative integer required")
    return value


def _optional_nonnegative_int(value: object) -> int | None:
    return None if value is None else _nonnegative_int(value)


def _normalized_hostname(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("hostname must be a string")
    hostname = value.strip().rstrip(".").lower()
    if not hostname or any(character.isspace() for character in hostname):
        raise ValueError("hostname must not be empty")
    return hostname


def _normalized_rule(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        rule_type = _optional_text(value.get("type"))
        payload = _optional_text(value.get("payload"))
        normalized = " ".join(part for part in (rule_type, payload) if part)
        if normalized:
            return normalized
    return "UNKNOWN"


def _dns_addresses(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract only public IP literals from a DNS-over-HTTP response."""

    answers = payload.get("Answer", payload.get("answer", []))
    if not isinstance(answers, list):
        raise ValueError("DNS answer must be a list")
    addresses: list[str] = []
    for answer in answers:
        if not isinstance(answer, Mapping):
            raise ValueError("DNS answer must be an object")
        value = answer.get("data")
        if not isinstance(value, str):
            continue
        try:
            address = ipaddress.ip_address(value.strip())
        except ValueError:
            continue
        if address.is_global and str(address) not in addresses:
            addresses.append(str(address))
        if len(addresses) == 8:
            break
    return tuple(addresses)
