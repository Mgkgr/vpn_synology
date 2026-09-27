from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.main import create_app
from app.models import NotificationDeliveryState, ProbeEvent
from app.outbound_health import ControlCycle, HealthService
from app.outbounds import build_outbound_registry
from app.routes import RuntimeContainer


def test_health_api_works_when_controller_is_down(tmp_path):
    engine = create_sqlite_engine(tmp_path / "health.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    health = HealthService(sessions)
    old = (datetime.now(UTC) - timedelta(minutes=5)).replace(second=10, microsecond=0)
    health.record_cycle(ControlCycle("WG-IMP", old.replace(second=0).isoformat(), old, "healthy", 3, "HY2-USA", ()))
    class Offline:
        def __getattr__(self, key):
            raise AssertionError("health API must not access the controller")
    runtime = RuntimeContainer(sessions, Offline(), Offline(), Offline(), Offline())
    runtime.outbound_health = health
    runtime.outbound_health_enabled = True
    try:
        with TestClient(create_app(container=runtime), base_url="https://testserver") as client:
            assert client.get("/api/health/outbounds").status_code == 401
            assert client.post("/api/auth/bootstrap", json={"password": "correct horse battery staple"}).status_code == 201
            response = client.get("/api/health/outbounds")
            assert response.status_code == 200
            body = response.json()
            assert body["collector_state"] == "unknown"
            assert body["enabled"] is True
            assert [row["label"] for row in body["outbounds"]] == ["VLESS-NL", "HY2-DE"]
            assert [row["state"] for row in body["outbounds"]] == ["unknown", "unknown"]
            assert body["outbounds"][0]["reasons"] == ["stale"]
            assert body["delivery"]["state"] == "disabled"
            assert "token" not in response.text and "password" not in response.text
            runtime.outbound_health = HealthService(sessions, build_outbound_registry("zapret2"))
            assert len(client.get("/api/health/outbounds").json()["outbounds"]) == 3
    finally:
        engine.dispose()


def test_journal_distinguishes_checked_target_from_selected_fallback(tmp_path):
    engine = create_sqlite_engine(tmp_path / "health.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    runtime = RuntimeContainer(sessions, None, None, None, None)
    health = HealthService(sessions)
    base = datetime.now(UTC).replace(second=10, microsecond=0) - timedelta(minutes=20)
    for minute in range(11):
        at = base + timedelta(minutes=minute)
        health.record_cycle(ControlCycle("WG-IMP", at.replace(second=0).isoformat(), at, "failed", 0, "HY2-USA", ()))
    with sessions() as session, session.begin():
        session.add(ProbeEvent(target="WG-IMP", outbound="HY2-USA", observed_at=base, succeeded=False))
    try:
        with TestClient(create_app(container=runtime), base_url="https://testserver") as client:
            client.post("/api/auth/bootstrap", json={"password": "correct horse battery staple"})
            events = client.get("/api/journal").json()["events"]
            incident = next(event for event in events if event["kind"] == "outbound_health")
            assert incident["action"] == "incident_down" and incident["target"] == "WG-IMP"
            probe = next(event for event in events if event["kind"] == "probe")
            assert probe["target"] == "WG-IMP" and probe["outbound"] == "HY2-USA"
    finally:
        engine.dispose()
