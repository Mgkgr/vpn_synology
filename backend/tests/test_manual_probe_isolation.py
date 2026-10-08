"""Manual diagnostics must not write health history of production outbounds."""
import asyncio

import httpx
import pytest

from app.mihomo import MihomoClient, MihomoIntegrationError


URL = 'https://www.google.com/generate_204'
GROUP = {
    'name': 'DASH-HEALTH-WG-IMP', 'type': 'Selector', 'now': 'WG-IMP',
    'all': ['WG-IMP'], 'hidden': True, 'emptyFallback': 'REJECT',
}


def test_manual_check_uses_validated_wrapper_and_longer_api_deadline():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path == '/proxies/DASH-HEALTH-WG-IMP':
            return httpx.Response(200, json=GROUP)
        if request.url.path == '/proxies/DASH-HEALTH-WG-IMP/delay':
            assert request.url.params['timeout'] == '5000'
            assert request.url.params['url'] == URL
            assert request.extensions['timeout']['read'] >= 7
            return httpx.Response(200, json={'delay': 142})
        pytest.fail('diagnostic touched a production proxy or unexpected endpoint: ' + request.url.path)

    client = MihomoClient('http://mihomo:9091', transport=httpx.MockTransport(handle))
    result = asyncio.run(client.proxy_delay('WG-IMP', URL))
    assert result.delay_ms == 142
    assert [request.method for request in seen] == ['GET', 'GET', 'GET']
    assert [request.url.path for request in seen] == [
        '/proxies/DASH-HEALTH-WG-IMP', '/proxies/DASH-HEALTH-WG-IMP/delay',
        '/proxies/DASH-HEALTH-WG-IMP',
    ]


@pytest.mark.parametrize('change', [
    {'type': 'Fallback'}, {'now': 'DIRECT'}, {'all': ['WG-IMP', 'DIRECT']},
    {'hidden': False}, {'emptyFallback': 'DIRECT'},
])
def test_invalid_wrapper_never_falls_back_to_working_proxy(change):
    seen = []

    def handle(request):
        seen.append(request.url.path)
        assert request.url.path == '/proxies/DASH-HEALTH-WG-IMP'
        return httpx.Response(200, json={**GROUP, **change})

    client = MihomoClient('http://mihomo:9091', transport=httpx.MockTransport(handle))
    with pytest.raises(MihomoIntegrationError):
        asyncio.run(client.proxy_delay('WG-IMP', URL))
    assert seen == ['/proxies/DASH-HEALTH-WG-IMP']


def test_wrapper_changed_during_probe_does_not_claim_success():
    read_count = 0

    def handle(request):
        nonlocal read_count
        if request.url.path == '/proxies/DASH-HEALTH-WG-IMP/delay':
            return httpx.Response(200, json={'delay': 42})
        assert request.url.path == '/proxies/DASH-HEALTH-WG-IMP'
        read_count += 1
        return httpx.Response(200, json=GROUP if read_count == 1 else {**GROUP, 'now': 'DIRECT'})

    client = MihomoClient('http://mihomo:9091', transport=httpx.MockTransport(handle))
    with pytest.raises(MihomoIntegrationError):
        asyncio.run(client.proxy_delay('WG-IMP', URL))
    assert read_count == 2


def test_unknown_member_is_rejected_without_network():
    def handle(request):
        pytest.fail('unknown outbound reached the controller')

    client = MihomoClient('http://mihomo:9091', transport=httpx.MockTransport(handle))
    with pytest.raises(MihomoIntegrationError):
        asyncio.run(client.proxy_delay('WG/IMP', URL))


def test_manual_diagnostics_limit_concurrent_network_probes():
    active = peak = 0

    async def handle(request):
        nonlocal active, peak
        if request.url.path == '/proxies/DASH-HEALTH-WG-IMP':
            return httpx.Response(200, json=GROUP)
        assert request.url.path == '/proxies/DASH-HEALTH-WG-IMP/delay'
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={'delay': 42})
        finally:
            active -= 1

    async def run():
        client = MihomoClient('http://mihomo:9091', transport=httpx.MockTransport(handle))
        return await asyncio.gather(*(client.proxy_delay('WG-IMP', URL) for _ in range(8)))

    results = asyncio.run(run())
    assert len(results) == 8 and all(row.delay_ms == 42 for row in results)
    assert 1 <= peak <= 3
