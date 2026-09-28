"""Offline HTTP acceptance tests with real auth, grants and durable intents."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.main import create_app
from app.maintenance_client import MaintenanceClient
from app.models import MaintenanceSubmitIntent
from app.routes import RuntimeContainer

PASSWORD = "correct horse battery staple"
JOB = "b" * 32
OPERATION = {"action": "restart", "components": ["mihomo"], "expected_revision": "a" * 64,
    "release_ids": {}, "enable_stopped": [], "snapshot_id": None, "accept_data_loss": False}


@pytest.fixture
def system(tmp_path):
    engine = create_sqlite_engine(tmp_path / "state.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    frames = []
    state = {"lost": False, "busy": False}

    async def transport(frame):
        frames.append(frame)
        method = frame["method"]
        if method == "submit":
            with sessions() as session:
                intent = session.get(MaintenanceSubmitIntent, (JOB, "submit"))
                assert intent is not None, "persist submit intent before IPC"
                assert PASSWORD not in intent.request_json
            if state["lost"]:
                raise TimeoutError()
        if method == "writer_acquire" and state["busy"]:
            return {"ok": False, "error": "busy", "job_id": JOB}
        result = {"job_id": JOB, "phase": "queued"}
        if method == "components":
            result = {"components": [], "revision": "a" * 64}
        elif method == "jobs":
            result = []
        elif method == "writer_acquire":
            result = {"lease_id": JOB}
        elif method == "writer_release":
            result = {"released": True}
        return {"ok": True, "result": result}

    runtime = RuntimeContainer(sessions, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    # Assertion rather than constructor error explains RED when the integration is absent.
    assert hasattr(runtime, "maintenance_client"), "maintenance API is not wired"
    runtime.maintenance_client = MaintenanceClient(enabled=True, transport=transport)
    with TestClient(create_app(container=runtime), base_url="https://testserver") as client:
        result = client.post("/api/auth/bootstrap", json={"username": "owner", "password": PASSWORD})
        client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
        frames.clear()  # Auth bootstrap legitimately acquires a configuration lease.
        yield client, sessions, frames, state, runtime
    engine.dispose()


def grant(client, operation=OPERATION):
    response = client.post("/api/maintenance/authorize", json={"operation": operation, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["grant"]


def test_components_capability_is_owner_only_but_admin_can_read(system):
    client, _, frames, _, _ = system
    assert client.get("/api/maintenance/components").json()["can_maintain"] is True
    assert client.post("/api/auth/admins", json={"username": "second-admin", "password": PASSWORD}).status_code == 201
    login = client.post("/api/auth/login", json={"username": "second-admin", "password": PASSWORD})
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert client.get("/api/maintenance/components").json()["can_maintain"] is False
    for url, body in (("authorize", {"operation": OPERATION, "password": PASSWORD}), ("check", {}),
                      ("jobs", {"operation": OPERATION, "grant": "fake", "job_id": JOB}),
                      (f"jobs/{JOB}/cancel", {"grant": "fake"})):
        assert client.post("/api/maintenance/" + url, json=body).status_code == 403
    assert not any(f["method"] in {"submit", "cancel", "check_releases"} for f in frames)


def test_busy_actual_writers_return_409_and_login_stays_available(system, tmp_path):
    from app.collectors import Collector
    from app.policy_rules import ManagedRuleService
    from app.rules import RuleService
    from app.settings import Settings
    from app.wgeasy import WgEasyAdapter
    client, sessions, _, state, runtime = system
    state["busy"] = True
    worker = runtime.maintenance_client
    runtime.rule_service = RuleService(tmp_path / "direct.txt", mihomo=runtime.mihomo, maintenance_client=worker)
    runtime.policy_rule_service = ManagedRuleService(tmp_path, runtime.mihomo, sessions, maintenance_client=worker)
    runtime.wgeasy = WgEasyAdapter(Settings(dashboard_encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="), maintenance_client=worker)
    runtime.collector = Collector(sessions, wgeasy=runtime.wgeasy, mihomo=runtime.mihomo, service_probe=None, geodata_dir=tmp_path, maintenance_client=worker)
    for url, body in (("rules/apply", {"text": "DOMAIN,example.com,DIRECT\n"}),
        ("rules/policies", {"kind": "GEOSITE", "category": "openai", "action": "DIRECT", "enabled": True}),
        ("clients", {"name": "phone"}), ("updates/geo", {}),
        ("auth/admins", {"username": "second-admin", "password": PASSWORD}),
        ("probes/targets", {"label": "Example", "url": "https://example.com"})):
        response = client.post("/api/" + url, json=body)
        assert response.status_code == 409, (url, response.text)
        assert response.json()["job_id"] == JOB
    assert client.post("/api/auth/login", json={"username": "owner", "password": PASSWORD}).status_code == 200


def test_all_maintenance_writes_require_csrf_and_closed_operation(system):
    client, _, frames, _, _ = system
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/maintenance/authorize", json={"operation": OPERATION, "password": PASSWORD}).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    for extra in ({"shell": "rm -rf /"}, {"components": ["unknown"]}, {"release_ids": {"mihomo": "custom"}}, {"accept_data_loss": "true"}):
        response = client.post("/api/maintenance/authorize", json={"operation": {**OPERATION, **extra}, "password": PASSWORD})
        assert response.status_code == 422
        assert PASSWORD not in response.text
    assert frames == []


def test_double_submit_uses_one_ipc_and_canonical_hash(system):
    client, sessions, frames, _, _ = system
    token = grant(client)
    body = {"operation": OPERATION, "grant": token, "job_id": JOB}
    assert client.post("/api/maintenance/jobs", json=body).status_code == 202
    assert client.post("/api/maintenance/jobs", json=body).status_code == 202
    assert [f["method"] for f in frames] == ["submit", "job"]
    with sessions() as session:
        intent = session.scalar(select(MaintenanceSubmitIntent))
        expected = hashlib.sha256(json.dumps(OPERATION, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        assert intent.request_hash == expected
    assert PASSWORD not in json.dumps(frames) and token not in json.dumps(frames)


def test_lost_submit_response_is_unknown_not_a_resubmission(system):
    client, _, frames, state, _ = system
    token = grant(client)
    state["lost"] = True
    body = {"operation": OPERATION, "grant": token, "job_id": JOB}
    response = client.post("/api/maintenance/jobs", json=body)
    assert response.status_code == 202 and response.json()["phase"] == "unknown"
    assert client.post("/api/maintenance/jobs", json=body).json()["phase"] == "queued"
    assert [f["method"] for f in frames] == ["submit", "job"]


def test_cancel_requires_its_own_grant_and_only_calls_once(system):
    client, _, frames, _, _ = system
    restart = grant(client)
    assert client.post(f"/api/maintenance/jobs/{JOB}/cancel", json={"grant": restart}).status_code == 403
    token = grant(client, {"action": "cancel", "job_id": JOB})
    for _ in range(2):
        assert client.post(f"/api/maintenance/jobs/{JOB}/cancel", json={"grant": token}).status_code == 202
    assert [f["method"] for f in frames] == ["cancel", "job"]


def test_worker_failure_does_not_break_auth_or_readonly_capability(system):
    client, _, _, _, runtime = system
    runtime.maintenance_client.enabled = False
    response = client.get("/api/maintenance/components")
    assert response.status_code == 200 and response.json()["available"] is False
    assert response.json()["can_maintain"] is False
    assert client.get("/api/auth/csrf").status_code == 200
    assert client.post("/api/auth/login", json={"username": "owner", "password": PASSWORD}).status_code == 200


def test_enabled_unreachable_worker_fails_writes_closed_with_503(system):
    client, _, _, _, runtime = system
    async def unreachable(_frame):
        raise OSError("socket unavailable")
    runtime.maintenance_client = MaintenanceClient(enabled=True, transport=unreachable)
    response = client.post("/api/auth/admins", json={"username": "new-admin", "password": PASSWORD})
    assert response.status_code == 503
    assert "Исполнитель обслуживания недоступен" in response.json()["detail"]
    assert client.get("/api/auth/admins").json() == [{"username": "owner", "bootstrap_owner": True}]
    assert client.get("/api/auth/csrf").status_code == 200
