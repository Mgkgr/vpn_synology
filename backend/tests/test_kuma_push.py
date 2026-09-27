import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging

import httpx
import pytest
from sqlalchemy import select

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.outbound_health import ControlCycle, HealthService


NOW = datetime(2026, 9, 27, 8, 0, 20, tzinfo=UTC)
TOKENS = {name: "test-token-" + name for name in ("WG-IMP", "HY2-USA", "collector")}


@pytest.fixture
def setup(tmp_path):
    from app.kuma_push import KumaPublisher

    engine = create_sqlite_engine(tmp_path / "health.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    health = HealthService(sessions)
    class Client:
        calls = []
        error = None
        async def publish(self, token, *, status, message):
            self.calls.append((token, status, message))
            if self.error:
                raise self.error
    client = Client()
    publisher = KumaPublisher(sessions, health, client, TOKENS, monotonic_clock=lambda: 0)
    yield publisher, client, health, sessions
    engine.dispose()


def observe(health, minute, result="healthy"):
    at = NOW + timedelta(minutes=minute)
    for name in ("WG-IMP", "HY2-USA"):
        health.record_cycle(ControlCycle(name, at.replace(second=0).isoformat(), at, result, 3 if result == "healthy" else 0, "HY2-USA", ()))


def test_publisher_coalesces_and_never_retries_within_60_seconds(setup):
    from app.kuma_push import KumaPublisher, KumaPushError
    from app.models import NotificationDeliveryState

    publisher, client, health, sessions = setup
    observe(health, 0)
    client.error = KumaPushError("request_failed")
    for second in range(10):
        asyncio.run(publisher.run_once(NOW + timedelta(seconds=second)))
    assert len(client.calls) == 3
    restarted = KumaPublisher(sessions, health, client, TOKENS, monotonic_clock=lambda: 0)
    asyncio.run(restarted.run_once(NOW + timedelta(seconds=59)))
    assert len(client.calls) == 3
    asyncio.run(restarted.run_once(NOW + timedelta(seconds=60)))
    assert len(client.calls) == 6
    asyncio.run(restarted.run_once(NOW + timedelta(seconds=179)))
    assert len(client.calls) == 6
    client.error = None
    observe(health, 1)
    observe(health, 2)
    observe(health, 3)
    asyncio.run(restarted.run_once(NOW + timedelta(seconds=180)))
    assert len(client.calls) == 9
    with sessions() as session:
        rows = list(session.scalars(select(NotificationDeliveryState)))
        assert len(rows) == 3
        assert all(row.last_error_code is None and row.last_accepted_at for row in rows)


def test_unknown_preserves_incident_and_has_distinct_message(setup):
    publisher, client, health, _ = setup
    asyncio.run(publisher.run_once(NOW))
    assert len(client.calls) == 1
    assert client.calls[0][1] == "down" and "MONITORING_UNKNOWN" in client.calls[0][2]
    for minute in range(11):
        observe(health, minute, "failed")
    asyncio.run(publisher.run_once(NOW + timedelta(minutes=10)))
    down = [call for call in client.calls if call[0] == TOKENS["WG-IMP"]][-1]
    assert down[1] == "down" and "INCIDENT_DOWN" in down[2]
    asyncio.run(publisher.run_once(NOW + timedelta(minutes=14)))
    stale = [call for call in client.calls if call[0] == TOKENS["WG-IMP"]][-1]
    assert stale[1] == "down" and "MONITORING_UNKNOWN" in stale[2]
    assert "INCIDENT_RECOVERED" not in stale[2]
    for minute in range(15, 18):
        observe(health, minute)
    asyncio.run(publisher.run_once(NOW + timedelta(minutes=17)))
    recovered = [call for call in client.calls if call[0] == TOKENS["WG-IMP"]][-1]
    assert recovered[1] == "up" and "INCIDENT_RECOVERED" in recovered[2]
    observe(health, 18)
    asyncio.run(publisher.run_once(NOW + timedelta(minutes=18)))
    healthy = [call for call in client.calls if call[0] == TOKENS["WG-IMP"]][-1]
    assert healthy[1] == "up" and "INCIDENT_RECOVERED" not in healthy[2]


@pytest.mark.parametrize("status,payload", [(404, {}), (500, {}), (200, {"ok": False}), (302, {"ok": True})])
def test_push_requires_explicit_acceptance_and_never_redirects(status, payload, caplog):
    from app.kuma_push import KumaPushClient, KumaPushError

    requests = []
    token = "secret-push-token"
    def respond(request):
        requests.append(request)
        # Even DEBUG library diagnostics/headers in this context must be hidden.
        logging.getLogger("httpcore.http11").debug("redirect token %s", token)
        return httpx.Response(status, json=payload, headers={"Location": "https://example.invalid/" + token})
    caplog.set_level(logging.DEBUG)
    client = KumaPushClient("http://kuma:3001", transport=httpx.MockTransport(respond))
    with pytest.raises(KumaPushError) as error:
        asyncio.run(client.publish(token, status="down", message="INCIDENT_DOWN VLESS-NL"))
    assert len(requests) == 1
    assert token not in str(error.value) and token not in caplog.text


def test_lost_response_is_safe_and_not_claimed_delivered(caplog):
    from app.kuma_push import KumaPushClient, KumaPushError

    caplog.set_level(logging.DEBUG)
    token = "secret-push-token"
    def respond(request):
        raise httpx.ReadTimeout("lost reply " + str(request.url), request=request)
    client = KumaPushClient("http://kuma:3001", transport=httpx.MockTransport(respond))
    with pytest.raises(KumaPushError) as error:
        asyncio.run(client.publish(token, status="down", message="INCIDENT_DOWN VLESS-NL"))
    assert str(error.value) == "request_failed"
    assert token not in caplog.text


def test_tokens_unavailable_disable_publishing_without_breaking_startup(tmp_path):
    from app.kuma_push import load_push_tokens

    assert load_push_tokens(None, ("WG-IMP", "HY2-USA")) == ({}, None)
    path = tmp_path / "tokens.json"
    assert load_push_tokens(path, ("WG-IMP", "HY2-USA"))[1] == "tokens_unavailable"
    path.write_text(json.dumps(TOKENS), encoding="utf-8")
    path.chmod(0o600)
    tokens, error = load_push_tokens(path, ("WG-IMP", "HY2-USA"))
    assert tokens == TOKENS and error is None
    path.write_text(json.dumps({"collector": "secret"}), encoding="utf-8")
    assert load_push_tokens(path, ("WG-IMP", "HY2-USA")) == ({}, "tokens_invalid")


def test_kuma_232_down_counter_model_never_repeats_before_one_hour():
    # Model of 2.3.2 server/routers/api-router.js lines 93-110, not production code.
    # Controlled live monitor acceptance is separately required before enabling.
    from app.kuma_push import MIN_PUSH_INTERVAL_SECONDS
    count, notices = 0, [0]
    for heartbeat in range(1, 181):
        count += 1
        if count >= 60:
            notices.append(heartbeat * MIN_PUSH_INTERVAL_SECONDS)
            count = 0
    assert notices == [0, 3600, 7200, 10800]
