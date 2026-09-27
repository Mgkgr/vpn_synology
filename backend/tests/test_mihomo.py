from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from app.mihomo import MihomoClient, MihomoIntegrationError
from app.services import ServiceProbe


@pytest.fixture
def controller() -> "MockController":
    return MockController()


@pytest.fixture
def client(controller: "MockController") -> MihomoClient:
    return MihomoClient("http://mihomo:9090", secret="controller-secret", transport=controller.transport)


@dataclass
class ResponseSpec:
    status_code: int = 200
    json: Any = None
    content: bytes | None = None
    headers: dict[str, str] | None = None
    stream: httpx.AsyncByteStream | None = None


class MockController:
    """A local httpx transport that records requests without external dependencies."""

    def __init__(self) -> None:
        self._responses: dict[tuple[str, str], ResponseSpec] = {}
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def add(self, url: str, *, method: str = "GET", **kwargs: Any) -> None:
        self._responses[(method, url)] = ResponseSpec(**kwargs)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop((request.method, str(request.url)), None)
        if response is None:
            return httpx.Response(500, request=request)
        if response.content is not None:
            return httpx.Response(response.status_code, content=response.content, headers=response.headers, request=request)
        if response.stream is not None:
            return httpx.Response(response.status_code, stream=response.stream, headers=response.headers, request=request)
        return httpx.Response(response.status_code, json=response.json, headers=response.headers, request=request)


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_control_delay_has_fixed_internal_targets_and_timeout():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"delay": 57})
    client = MihomoClient("http://mihomo:9090", transport=httpx.MockTransport(handle), delay_test_host_supplier=lambda: ())
    assert run(client.control_delay("DASH-HEALTH-WG-IMP", "cloudflare")).delay_ms == 57
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/proxies/DASH-HEALTH-WG-IMP/delay"
    assert request.url.params["url"] == "https://cp.cloudflare.com/generate_204"
    assert request.url.params["timeout"] == "10000"
    assert request.extensions["timeout"]["read"] == 12
    for name, key in (("WG-IMP", "google"), ("DASH-HEALTH-WG-IMP", "http://127.0.0.1")):
        with pytest.raises(MihomoIntegrationError):
            run(client.control_delay(name, key))
    assert len(requests) == 1


class FirstSampleOnlyStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"up": 15, "down": 24}\n'
        raise AssertionError("the traffic reader consumed more than one sample")

    async def aclose(self) -> None:
        pass


class NdjsonStream(httpx.AsyncByteStream):
    def __init__(self, *lines: bytes) -> None:
        self._lines = lines

    async def __aiter__(self):
        for line in self._lines:
            yield line

    async def aclose(self) -> None:
        pass


class DelayedSampleStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.1)
        yield b'{"up": 15, "down": 24}\n'

    async def aclose(self) -> None:
        pass


def test_connections_keep_matched_rule_and_chain(controller: MockController, client: MihomoClient) -> None:
    controller.add(
        "http://mihomo:9090/connections",
        json={"connections": [{"id": "1", "chains": ["VPS-FALLBACK", "WG-IMP"], "rule": "MATCH"}]},
    )

    item = run(client.connections())[0]

    assert item.chain[-1] == "WG-IMP"
    assert item.rule == "MATCH"
    assert controller.requests[0].headers["Authorization"] == "Bearer controller-secret"


def test_read_request_omits_authorization_when_no_secret_is_configured(controller: MockController) -> None:
    controller.add("http://mihomo:9090/version", json={"version": "v1.0.0"})

    version = run(MihomoClient("http://mihomo:9090", transport=controller.transport).version())

    assert version.version == "v1.0.0"
    assert "Authorization" not in controller.requests[0].headers


def test_documented_read_endpoints_return_normalized_browser_safe_values(
    controller: MockController, client: MihomoClient
) -> None:
    controller.add("http://mihomo:9090/version", json={"version": "v1.0.0"})
    controller.add(
        "http://mihomo:9090/group",
        json={"proxies": [{"name": "AUTO", "type": "Selector", "all": ["WG-IMP", "DIRECT"], "now": "WG-IMP"}]},
    )
    controller.add(
        "http://mihomo:9090/rules",
        json={"rules": [{"type": "DOMAIN-SUFFIX", "payload": "example.com", "proxy": "WG-IMP"}]},
    )
    controller.add("http://mihomo:9090/traffic", stream=FirstSampleOnlyStream())

    version = run(client.version())
    group = run(client.groups())[0]
    rule = run(client.rules())[0]
    traffic = run(client.traffic())

    assert version.version == "v1.0.0"
    assert (group.name, group.proxies, group.now) == ("AUTO", ("WG-IMP", "DIRECT"), "WG-IMP")
    assert (rule.type, rule.payload, rule.proxy) == ("DOMAIN-SUFFIX", "example.com", "WG-IMP")
    assert (traffic.up, traffic.down) == (15, 24)


def test_rules_accept_an_empty_payload_returned_by_mihomo(controller: MockController, client: MihomoClient) -> None:
    controller.add(
        "http://mihomo:9090/rules",
        json={"rules": [{"type": "MATCH", "payload": "", "proxy": "VPS-FALLBACK"}]},
    )

    rule = run(client.rules())[0]

    assert (rule.type, rule.payload, rule.proxy) == ("MATCH", "", "VPS-FALLBACK")


def test_malformed_traffic_stream_raises_a_safe_structured_error(
    controller: MockController, client: MihomoClient
) -> None:
    controller.add("http://mihomo:9090/traffic", stream=NdjsonStream(b"not json\n"))

    with pytest.raises(MihomoIntegrationError) as raised:
        run(client.traffic())

    assert raised.value.operation == "traffic"
    assert "controller-secret" not in str(raised.value)
    assert raised.value.reason == "invalid traffic stream"


def test_traffic_first_sample_has_a_bounded_read_timeout(controller: MockController) -> None:
    controller.add("http://mihomo:9090/traffic", stream=DelayedSampleStream())
    client = MihomoClient(
        "http://mihomo:9090",
        secret="controller-secret",
        traffic_sample_timeout=0.01,
        transport=controller.transport,
    )

    with pytest.raises(MihomoIntegrationError) as raised:
        run(client.traffic())

    assert (raised.value.operation, raised.value.reason) == ("traffic", "traffic sample timed out")


def test_rule_providers_and_delay_use_documented_read_only_endpoints(
    controller: MockController, client: MihomoClient
) -> None:
    controller.add("http://mihomo:9090/providers/rules", json={"providers": {"ads": {"behavior": "domain"}}})
    controller.add(
        "http://mihomo:9090/proxies/WG%2FIMP/delay?url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204&timeout=5000",
        json={"delay": 42},
    )

    providers = run(client.rule_providers())
    delay = run(client.proxy_delay("WG/IMP", "https://www.gstatic.com/generate_204"))

    assert providers[0].name == "ads"
    assert delay.delay_ms == 42
    assert controller.requests[-1].url.raw_path == (
        b"/proxies/WG%2FIMP/delay?url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204&timeout=5000"
    )


def test_dns_lookup_uses_the_mihomo_resolver_for_an_approved_probe_target(
    controller: MockController, client: MihomoClient
) -> None:
    controller.add(
        "http://mihomo:9090/dns/query?name=www.openai.com&type=A",
        json={"Answer": [{"name": "www.openai.com.", "type": 1, "data": "104.18.33.45"}]},
    )

    result = run(client.dns_lookup("https://www.openai.com/"))

    assert (result.hostname, result.addresses) == ("www.openai.com", ("104.18.33.45",))
    assert controller.requests[-1].url.raw_path == b"/dns/query?name=www.openai.com&type=A"


@pytest.mark.parametrize(
    "delay_url",
    [
        "http://www.gstatic.com/generate_204",
        "https://user:password@www.gstatic.com/generate_204",
        "https://unapproved.example/generate_204",
    ],
)
def test_proxy_delay_rejects_urls_outside_the_safe_allowlist(
    controller: MockController, client: MihomoClient, delay_url: str
) -> None:
    with pytest.raises(MihomoIntegrationError) as raised:
        run(client.proxy_delay("WG-IMP", delay_url))

    assert (raised.value.operation, raised.value.reason) == ("proxy_delay", "unsafe delay test URL")
    assert delay_url not in str(raised.value)
    assert controller.requests == []


def test_explicit_write_methods_are_not_called_by_read_operations(
    controller: MockController, client: MihomoClient
) -> None:
    controller.add("http://mihomo:9090/version", json={"version": "v1.0.0"})

    run(client.version())

    assert [request.url.path for request in controller.requests] == ["/version"]


def test_explicit_maintenance_operations_use_documented_write_endpoints(
    controller: MockController, client: MihomoClient
) -> None:
    controller.add("http://mihomo:9090/upgrade/geo", method="POST", status_code=204)
    controller.add("http://mihomo:9090/configs?force=true", method="PUT", status_code=204)

    geo_result = run(client.geo_upgrade())
    reload_result = run(client.reload())

    assert (geo_result.operation, geo_result.status_code) == ("geo_upgrade", 204)
    assert (reload_result.operation, reload_result.status_code) == ("reload", 204)
    assert [json.loads(request.content) for request in controller.requests] == [
        {"path": "", "payload": ""},
        {"path": "", "payload": ""},
    ]


def test_mihomo_401_without_secret_is_an_unhealthy_service_integration(controller: MockController) -> None:
    controller.add("http://mihomo:9090/version", status_code=401)
    probes = ServiceProbe(
        wgeasy_url="http://wg-easy:51821/status",
        mihomo_url="http://mihomo:9090",
        metacubexd_url="http://metacubexd:9090/",
        kuma_url="http://kuma:3001/",
        transport=controller.transport,
    )

    statuses = run(probes.check_all())

    mihomo = next(status for status in statuses if status.name == "mihomo")
    assert mihomo.healthy is False
    assert mihomo.reason == "authentication required but no Mihomo secret is configured"


def test_service_probes_reject_mihomo_401_even_when_a_secret_is_configured(controller: MockController) -> None:
    controller.add("http://wg-easy:51821/status", status_code=200)
    controller.add("http://mihomo:9090/version", status_code=401)
    controller.add("http://metacubexd:9090/", status_code=302, headers={"Location": "/ui"})
    controller.add("http://kuma:3001/", status_code=204)
    probes = ServiceProbe(
        wgeasy_url="http://wg-easy:51821/status",
        mihomo_url="http://mihomo:9090",
        metacubexd_url="http://metacubexd:9090/",
        kuma_url="http://kuma:3001/",
        mihomo_secret="controller-secret",
        redirect_statuses={302},
        transport=controller.transport,
    )

    statuses = run(probes.check_all())

    assert [(status.name, status.healthy) for status in statuses] == [
        ("wg-easy", True),
        ("mihomo", False),
        ("metacubexd", True),
        ("kuma", True),
    ]
    assert all(status.latency_ms >= 0 for status in statuses)
    mihomo_request = next(request for request in controller.requests if request.url.path == "/version")
    assert mihomo_request.headers["Authorization"] == "Bearer controller-secret"
    mihomo = next(status for status in statuses if status.name == "mihomo")
    assert mihomo.reason == "authentication rejected"


def test_mihomo_401_cannot_be_declared_healthy_by_a_misconfigured_redirect_set(
    controller: MockController,
) -> None:
    controller.add("http://wg-easy:51821/status", status_code=200)
    controller.add("http://mihomo:9090/version", status_code=401)
    controller.add("http://metacubexd:9090/", status_code=200)
    controller.add("http://kuma:3001/", status_code=200)
    probes = ServiceProbe(
        wgeasy_url="http://wg-easy:51821/status",
        mihomo_url="http://mihomo:9090",
        metacubexd_url="http://metacubexd:9090/",
        kuma_url="http://kuma:3001/",
        mihomo_secret="controller-secret",
        redirect_statuses={301, 401},
        transport=controller.transport,
    )

    statuses = run(probes.check_all())

    mihomo = next(status for status in statuses if status.name == "mihomo")
    assert (mihomo.healthy, mihomo.reason) == (False, "authentication rejected")
