from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from app.auth import AuthService
from app.collectors import Collector, register_collector_jobs
from app.db import create_all, create_session_factory, create_sqlite_engine
from app.mihomo import MihomoClient
from app.policy_rules import ManagedRuleService
from app.probe_targets import ProbeTargetService
from app.routes import RuntimeContainer, build_api_router
from app.rules import RuleService
from app.services import ServiceProbe
from app.settings import Settings
from app.wgeasy import WgEasyAdapter, WgEasyCredentialVault


STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass(slots=True)
class CollectorRuntime:
    """Lifespan-owned collector dependencies with no import-time side effects."""

    collector: Collector
    scheduler: Any
    close: Callable[[], None] | None = None
    container: RuntimeContainer | None = None


def create_collector_runtime(settings: Settings | None = None) -> CollectorRuntime:
    """Build the production collector graph; absent wg-easy setup remains observable."""

    settings = settings or Settings.from_env()
    engine = create_sqlite_engine(settings.database_path)
    create_all(engine)
    session_factory = create_session_factory(engine)
    encryption_key = settings.dashboard_encryption_key
    vault = (
        WgEasyCredentialVault(session_factory, encryption_key.get_secret_value())
        if encryption_key is not None
        else None
    )
    mihomo_secret = settings.mihomo_api_secret.get_secret_value() if settings.mihomo_api_secret is not None else None
    probe_targets = ProbeTargetService(session_factory)
    mihomo = MihomoClient(
        str(settings.mihomo_url),
        secret=mihomo_secret,
        delay_test_host_allowlist=settings.delay_test_host_allowlist,
        delay_test_host_supplier=probe_targets.enabled_hosts,
    )
    wgeasy = WgEasyAdapter(settings, credential_vault=vault, audit_session_factory=session_factory)
    collector = Collector(
        session_factory,
        wgeasy=wgeasy,
        mihomo=mihomo,
        service_probe=ServiceProbe(
            wgeasy_url=str(settings.wgeasy_url),
            mihomo_url=str(settings.mihomo_url),
            metacubexd_url=str(settings.metacubexd_url),
            kuma_url=str(settings.kuma_url),
            mihomo_secret=mihomo_secret,
        ),
        geodata_dir=settings.geodata_dir,
        probe_urls=settings.delay_test_urls,
        probe_url_supplier=probe_targets.enabled_urls,
    )
    container = RuntimeContainer(
        session_factory=session_factory,
        wgeasy=wgeasy,
        mihomo=mihomo,
        rule_service=RuleService(
            settings.direct_rules_path,
            mihomo=mihomo,
            audit_session_factory=session_factory,
        ),
        collector=collector,
        csrf_encryption_key=encryption_key.get_secret_value() if encryption_key is not None else None,
        probe_targets=probe_targets,
        policy_rule_service=ManagedRuleService(settings.direct_rules_path.parent, mihomo, session_factory),
    )
    return CollectorRuntime(collector, AsyncIOScheduler(timezone="UTC"), engine.dispose, container)


def create_app(
    *,
    runtime_factory: Callable[[], CollectorRuntime] = create_collector_runtime,
    container: RuntimeContainer | None = None,
) -> FastAPI:
    """Create an app whose real scheduler is opt-in and whose routes are injectable."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if container is not None:
            # Tests and controlled embedding provide fakes here.  In particular,
            # no scheduler, network client, or production settings are started.
            yield
            return
        runtime = runtime_factory()
        if runtime.container is not None:
            app.state.runtime = runtime.container
            app.state.auth_service = AuthService(
                runtime.container.session_factory,
                cookie_secure=runtime.container.cookie_secure,
                csrf_encryption_key=runtime.container.csrf_encryption_key,
            )
        register_collector_jobs(runtime.scheduler, runtime.collector)
        runtime.scheduler.start()
        app.state.collector = runtime.collector
        try:
            yield
        finally:
            runtime.scheduler.shutdown(wait=False)
            if runtime.close is not None:
                runtime.close()

    application = FastAPI(title="VPN Dashboard", docs_url=None, redoc_url=None, lifespan=lifespan)
    if container is not None:
        application.state.runtime = container
        application.state.auth_service = AuthService(
            container.session_factory,
            cookie_secure=container.cookie_secure,
            csrf_encryption_key=container.csrf_encryption_key,
        )

    @application.get("/api/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.exception_handler(RequestValidationError)
    async def redact_validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        """Never reflect submitted passwords, rule text, or other request values."""

        locations = [list(item.get("loc", ())) for item in error.errors()]
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": "invalid request", "loc": locations})

    application.include_router(build_api_router())

    @application.get("/{path:path}", include_in_schema=False)
    async def serve_spa(path: str) -> FileResponse:
        """Serve the compiled SPA without shadowing API routes or startup."""

        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        index_file = STATIC_DIR / "index.html"
        if not index_file.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        static_root = STATIC_DIR.resolve()
        requested_file = (STATIC_DIR / path).resolve()
        if requested_file.is_relative_to(static_root) and requested_file.is_file():
            return FileResponse(requested_file)
        if path.startswith("assets/"):
            # Never return index.html for a missing module: browsers report an
            # opaque MIME error and keep retrying stale hashed asset URLs.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        # The entry document must always point at the current hashed assets.
        return FileResponse(index_file, headers={"Cache-Control": "no-store"})

    return application


app = create_app()
