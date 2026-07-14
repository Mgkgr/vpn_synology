from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.main import create_app
from app.mihomo import ControllerWriteResult, MihomoVersion, ProxyGroup, Traffic
from app.models import DashboardOwner
from app.routes import RuntimeContainer
from app.wgeasy import WireGuardClient


PASSWORD = "correct horse battery staple"


class FakeWgEasy:
    def __init__(self) -> None:
        self.clients = [WireGuardClient(42, "laptop", True, "10.8.0.42", None, 0, 0)]

    async def list_clients(self):
        return list(self.clients)

    async def create_client(self, name: str):
        client = WireGuardClient(43, name, True, "10.8.0.43", None, 0, 0)
        self.clients.append(client)
        return client

    async def disable_client(self, client_id: int):
        return SimpleNamespace(client_id=client_id, name="laptop", operation="disable")

    async def delete_client(self, client_id: int):
        return SimpleNamespace(client_id=client_id, name="laptop", operation="delete")

    async def config(self, client_id: int):
        return "[Interface]\nPrivateKey = fake-private-key\n[Peer]\nPublicKey = fake-public-key\n"

    async def qrcode(self, client_id: int):
        return '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z"/></svg>'


class FakeMihomo:
    async def version(self):
        return MihomoVersion("v1")

    async def traffic(self):
        return Traffic(1, 2)

    async def groups(self):
        return [ProxyGroup("AUTO", "Selector", ("DIRECT",), "DIRECT")]

    async def rules(self):
        return []

    async def rule_providers(self):
        return []

    async def geo_upgrade(self):
        return ControllerWriteResult("geo_upgrade", 204)

    async def reload(self):
        return ControllerWriteResult("reload", 204)


@dataclass
class FakeRules:
    async def apply_direct_rules_async(self, text: str, actor: str):
        return SimpleNamespace(number=1, sha256="a" * 64)


@dataclass
class FakeCollector:
    calls: int = 0

    async def manual_geo_upgrade(self, actor: str) -> None:
        self.calls += 1


@pytest.fixture
def client(tmp_path):
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    runtime = RuntimeContainer(
        session_factory=create_session_factory(engine),
        wgeasy=FakeWgEasy(),
        mihomo=FakeMihomo(),
        rule_service=FakeRules(),
        collector=FakeCollector(),
    )
    with TestClient(create_app(container=runtime), base_url="https://testserver") as test_client:
        yield test_client
    engine.dispose()


def bootstrap(client: TestClient) -> str:
    response = client.post("/api/auth/bootstrap", json={"password": PASSWORD})
    assert response.status_code == 201
    return response.json()["csrf_token"]


def test_healthz_is_the_only_anonymous_status_endpoint(client: TestClient) -> None:
    assert client.get("/api/healthz").json() == {"status": "ok"}
    assert client.get("/api/overview").status_code == 401


def test_bootstrap_creates_one_owner_and_secure_session_cookie(client: TestClient) -> None:
    response = client.post("/api/auth/bootstrap", json={"password": PASSWORD})

    assert response.status_code == 201
    assert response.json()["csrf_token"]
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert client.post("/api/auth/bootstrap", json={"password": PASSWORD}).status_code == 409


def test_dashboard_owner_model_has_a_database_singleton_constraint() -> None:
    table = DashboardOwner.__table__
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    check_expressions = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert ("singleton_marker",) in unique_columns
    assert "singleton_marker = 1" in check_expressions
    assert table.c.singleton_marker.server_default is not None
    assert table.c.username.unique is True


def test_database_rejects_a_second_dashboard_owner(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            session.add(
                DashboardOwner(
                    username="first",
                    password_hash="hash",
                    created_at=datetime.now(UTC),
                )
            )
        with pytest.raises(IntegrityError):
            with factory.begin() as session:
                session.add(
                    DashboardOwner(
                        username="second",
                        password_hash="hash",
                        created_at=datetime.now(UTC),
                    )
                )
    finally:
        engine.dispose()


def test_bootstrap_conflict_is_a_redacted_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    submitted_username = "race-owner"
    submitted_password = "race secret that must never be reflected"
    original_flush = Session.flush

    def raise_singleton_conflict(session: Session, *_args, **_kwargs) -> None:
        if not any(isinstance(record, DashboardOwner) for record in session.new):
            original_flush(session)
            return
        raise IntegrityError(
            "INSERT INTO dashboard_owners",
            {"username": submitted_username, "password": submitted_password},
            Exception("UNIQUE constraint failed: dashboard_owners.singleton_marker"),
        )

    monkeypatch.setattr("app.auth.Session.flush", raise_singleton_conflict)

    response = client.post(
        "/api/auth/bootstrap",
        json={"username": submitted_username, "password": submitted_password},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "bootstrap is unavailable"}
    assert submitted_username not in response.text
    assert submitted_password not in response.text
    assert "singleton_marker" not in response.text


def test_csrf_get_returns_the_session_token_without_invalidating_it(client: TestClient) -> None:
    csrf_token = bootstrap(client)

    first_response = client.get("/api/auth/csrf")
    second_response = client.get("/api/auth/csrf")
    accepted = client.post("/api/clients", json={"name": "phone"}, headers={"X-CSRF-Token": csrf_token})

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json() == {"csrf_token": csrf_token}
    assert second_response.json() == {"csrf_token": csrf_token}
    assert accepted.status_code == 201


def test_login_and_all_state_changes_require_a_valid_csrf_header(client: TestClient) -> None:
    csrf_token = bootstrap(client)

    denied = client.post("/api/clients", json={"name": "phone"})
    accepted = client.post("/api/clients", json={"name": "phone"}, headers={"X-CSRF-Token": csrf_token})
    logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_token})
    login = client.post("/api/auth/login", json={"password": PASSWORD})

    assert denied.status_code == 403
    assert accepted.status_code == 201
    assert logout.status_code == 204
    assert login.status_code == 200
    assert login.json()["csrf_token"]


def test_anonymous_client_cannot_download_config(client: TestClient) -> None:
    assert client.get("/api/clients/42/config").status_code == 401


def test_validation_errors_do_not_echo_submitted_credentials(client: TestClient) -> None:
    submitted_password = "too-short"

    response = client.post("/api/auth/bootstrap", json={"password": submitted_password})

    assert response.status_code == 422
    assert submitted_password not in response.text
