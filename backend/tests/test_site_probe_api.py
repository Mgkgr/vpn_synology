from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.main import create_app
from app.routes import RuntimeContainer
from app.site_probes import SiteProbeService


def test_api_reads_database_only_and_requires_admin(tmp_path):
    engine = create_sqlite_engine(tmp_path / "api.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    class Offline:
        def __getattr__(self, name):
            raise AssertionError("GET must never access an upstream")
    runtime = RuntimeContainer(sessions, Offline(), Offline(), Offline(), Offline())
    runtime.site_probes = SiteProbeService(sessions, enabled=True, targets=("github",))
    try:
        with TestClient(create_app(container=runtime), base_url="https://testserver") as client:
            assert client.get("/api/rules/service-checks").status_code == 401
            assert client.post("/api/auth/bootstrap", json={"password": "correct horse battery staple"}).status_code == 201
            response = client.get("/api/rules/service-checks")
            assert response.status_code == 200
            body = response.json()
            assert body["enabled"] is True and body["run"] is None
            assert body["services"][0]["key"] == "github"
            assert all(item["state"] == "unknown" and item["observed_at"] is None for item in body["services"][0]["routes"])
            assert "secret" not in response.text
            assert client.post("/api/rules/service-checks").status_code == 405
            class Unavailable:
                def snapshot(self, now):
                    raise SQLAlchemyError("private database detail")
            runtime.site_probes = Unavailable()
            failure = client.get("/api/rules/service-checks")
            assert failure.status_code == 503 and "private" not in failure.text
            assert client.get("/api/auth/csrf").status_code == 200
    finally:
        engine.dispose()
