from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.main import CollectorRuntime, create_app
from app.routes import RuntimeContainer


class _Scheduler:
    def add_job(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def shutdown(self, *, wait: bool = True) -> None:
        pass


class _Collector:
    async def run_minute(self) -> None:
        pass

    async def run_probe_cycle(self) -> None:
        pass

    def run_daily(self) -> None:
        pass

    async def scheduled_geo_upgrade(self) -> None:
        pass

    async def catch_up_geo_upgrade(self) -> bool:
        return False


def test_production_lifespan_keeps_login_endpoint_available(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    runtime = RuntimeContainer(
        session_factory=create_session_factory(engine),
        wgeasy=None,
        mihomo=None,
        rule_service=None,
        collector=_Collector(),
        cookie_secure=False,
    )
    application = create_app(
        runtime_factory=lambda: CollectorRuntime(
            collector=_Collector(),
            scheduler=_Scheduler(),
            close=engine.dispose,
            container=runtime,
        )
    )

    with TestClient(application) as client:
        response = client.post("/api/auth/login", json={"username": "unknown", "password": "not-a-real-password"})

    assert response.status_code == 401
