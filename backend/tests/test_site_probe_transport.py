import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.mihomo import MihomoClient, MihomoIntegrationError


class ProbeController:
    def __init__(self, *, status=200, dns=None, member="WG-IMP", fresh=True, alive=True):
        self.requests = []
        self.status, self.member, self.fresh, self.alive = status, member, fresh, alive
        self.dns = ["8.8.8.8"] if dns is None else dns
        self.tested = False
        self.unavailable_after_test = False
        self.unified_delay = False

    def handle(self, request):
        self.requests.append(request)
        if self.unavailable_after_test and self.tested:
            return httpx.Response(401, json={"message": "secret"})
        if request.url.path == "/configs":
            return httpx.Response(200, json={"unified-delay": self.unified_delay})
        if request.url.path == "/dns/query":
            values = self.dns if request.url.params["type"] == "A" else []
            return httpx.Response(200, json={"Status": 0, "Answer": [{"data": value, "type": 1} for value in values]})
        if request.url.path.endswith("/delay"):
            self.tested = True
            return httpx.Response(self.status, json={"delay": 81, "message": "private upstream detail"})
        if request.url.path == "/proxies/DASH-SITE-WG-IMP":
            fresh = self.tested and self.fresh
            return httpx.Response(200, json={
                "type": "Selector", "all": [self.member], "now": self.member, "hidden": True,
                "emptyFallback": "REJECT",
                "extra": {"https://api.github.com/": {
                    "alive": self.alive,
                    "history": [{"time": datetime.now(UTC).isoformat() if fresh else "2020-01-01T00:00:00Z",
                                 "delay": 81 if self.alive else 0}],
                }},
            })
        raise AssertionError("Unexpected controller endpoint: " + request.url.path)

    def client(self):
        return MihomoClient("http://mihomo:9090", transport=httpx.MockTransport(self.handle),
                            delay_test_host_supplier=lambda: ())


def run(controller, route="primary", service="github"):
    return asyncio.run(controller.client().site_probe(route, service))


def test_only_own_probe_group_is_tested_and_no_mutation_is_sent():
    controller = ProbeController()
    result = run(controller)
    assert (result.state, result.delay_ms, result.reason) == ("responded", 81, None)
    probes = [request for request in controller.requests if request.url.path.endswith("/delay")]
    assert len(probes) == 1
    assert probes[0].url.path == "/proxies/DASH-SITE-WG-IMP/delay"
    assert dict(probes[0].url.params) == {"url": "https://api.github.com/", "timeout": "10000", "expected": "200-399"}
    assert probes[0].extensions["timeout"]["read"] == 12
    assert all(request.method == "GET" for request in controller.requests)


@pytest.mark.parametrize("route,service", [("WG-IMP", "github"), ("VPS-FALLBACK", "github"),
                                           ("DASH-HEALTH-WG-IMP", "github"), ("primary", "https://localhost/")])
def test_unknown_route_or_raw_url_is_rejected_without_network(route, service):
    controller = ProbeController()
    with pytest.raises(MihomoIntegrationError):
        run(controller, route, service)
    assert not controller.requests


@pytest.mark.parametrize("answers", [[], ["192.168.2.103"], ["198.18.0.1"], ["127.0.0.1"],
                                    ["8.8.8.8", "10.0.0.1"], ["::1"], ["fd00::1"]])
def test_empty_or_nonpublic_dns_prevents_site_request(answers):
    controller = ProbeController(dns=answers)
    result = run(controller)
    assert result.state == "unknown"
    assert result.reason == "dns_preflight_failed"
    assert not controller.tested


def test_wrong_singleton_fails_closed_without_site_request():
    controller = ProbeController(member="VPS-FALLBACK")
    result = run(controller)
    assert (result.state, result.reason) == ("unknown", "probe_group_invalid")
    assert not controller.tested


@pytest.mark.parametrize("status,reason", [(503, "probe_failed"), (504, "probe_timeout")])
def test_completed_probe_failure_is_not_confused_with_controller_failure(status, reason):
    controller = ProbeController(status=status)
    result = run(controller)
    assert (result.state, result.reason, result.delay_ms) == ("failed", reason, None)
    controller = ProbeController(status=status)
    controller.unavailable_after_test = True
    result = run(controller)
    assert (result.state, result.reason) == ("unknown", "controller_unavailable")


def test_http_class_is_read_from_fresh_wrapper_result_not_delay_http_200():
    result = run(ProbeController(alive=False))
    assert (result.state, result.delay_ms, result.reason) == ("http_rejected", 81, "http_status_outside_expected")


def test_old_per_url_history_cannot_confirm_response():
    result = run(ProbeController(fresh=False))
    assert (result.state, result.reason, result.delay_ms) == ("unknown", "probe_result_unconfirmed", None)


def test_controller_transport_failure_is_sanitized_unknown():
    def failed(request):
        raise httpx.ConnectError("private-controller-secret")
    client = MihomoClient("http://mihomo:9090", transport=httpx.MockTransport(failed))
    result = asyncio.run(client.site_probe("primary", "github"))
    assert (result.state, result.reason) == ("unknown", "controller_unavailable")
    assert "secret" not in str(result)


@pytest.mark.parametrize("mode", [True, None, "false", 0])
def test_unified_delay_or_unproven_mode_does_not_send_multiple_heads(mode):
    controller = ProbeController()
    controller.unified_delay = mode
    result = run(controller)
    assert (result.state, result.reason) == ("unknown", "probe_mode_invalid")
    assert not controller.tested
