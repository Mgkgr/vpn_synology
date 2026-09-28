from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import AuthService, LoginThrottle
from app.collectors import Collector, register_collector_jobs
from app.db import create_all, create_session_factory, create_sqlite_engine
from app.mihomo import MihomoClient
from app.health_collector import HealthCollector, register_health_jobs
from app.health_api import build_health_router
from app.maintenance_api import build_maintenance_router, install_maintenance_handlers
from app.maintenance_client import MaintenanceClient
from app.kuma_push import KumaPublisher, KumaPushClient, load_push_tokens, register_kuma_jobs
from app.outbound_health import HealthService
from app.outbounds import build_outbound_registry
from app.policy_rules import ManagedRuleService
from app.probe_targets import ProbeTargetService
from app.routes import RuntimeContainer, build_api_router
from app.rules import RuleService
from app.services import ServiceProbe
from app.settings import DEFAULT_TRUSTED_HOSTS, Settings
from app.wgeasy import WgEasyAdapter, WgEasyCredentialVault


STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass(slots=True)
class CollectorRuntime:
    """Lifespan-owned collector dependencies with no import-time side effects."""

    collector: Collector
    scheduler: Any
    close: Callable[[], None] | None = None
    container: RuntimeContainer | None = None
    health_collector: HealthCollector | None = None
    kuma_publisher: KumaPublisher | None = None


def create_collector_runtime(settings: Settings | None = None) -> CollectorRuntime:
    """Build the production collector graph; absent wg-easy setup remains observable."""

    settings = settings or Settings.from_env()
    engine = create_sqlite_engine(settings.database_path)
    create_all(engine)
    session_factory = create_session_factory(engine)
    encryption_key = settings.dashboard_encryption_key
    vault = WgEasyCredentialVault(session_factory, encryption_key.get_secret_value())
    maintenance = MaintenanceClient(enabled=settings.maintenance_enabled)
    mihomo_secret = settings.mihomo_api_secret.get_secret_value() if settings.mihomo_api_secret is not None else None
    probe_targets = ProbeTargetService(session_factory)
    mihomo = MihomoClient(
        str(settings.mihomo_url),
        secret=mihomo_secret,
        delay_test_host_allowlist=settings.delay_test_host_allowlist,
        delay_test_host_supplier=probe_targets.enabled_hosts,
    )
    wgeasy = WgEasyAdapter(settings, credential_vault=vault, audit_session_factory=session_factory, maintenance_client=maintenance)
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
        maintenance_client=maintenance,
    )
    container = RuntimeContainer(
        session_factory=session_factory,
        wgeasy=wgeasy,
        mihomo=mihomo,
        rule_service=RuleService(
            settings.direct_rules_path,
            mihomo=mihomo,
            audit_session_factory=session_factory,
            maintenance_client=maintenance,
        ),
        collector=collector,
        csrf_encryption_key=encryption_key.get_secret_value(),
        probe_targets=probe_targets,
        policy_rule_service=ManagedRuleService(settings.direct_rules_path.parent, mihomo, session_factory, maintenance_client=maintenance),
        login_throttle=LoginThrottle(session_factory),
        trusted_proxy_networks=tuple(ip_network(item) for item in settings.trusted_proxy_cidrs),
        max_request_body_bytes=settings.max_request_body_bytes,
        maintenance_client=maintenance,
    )
    health = HealthService(session_factory, build_outbound_registry(settings.antidpi_engine))
    container.outbound_health = health
    container.outbound_health_enabled = settings.outbound_health_enabled
    health_collector = None
    publisher = None
    if settings.outbound_health_enabled:
        health_collector = HealthCollector(mihomo, health)
        tokens, token_error = load_push_tokens(settings.kuma_push_tokens_file, tuple(entry.id for entry in health_collector.service.registry))
        publisher = KumaPublisher(session_factory, health_collector.service, KumaPushClient(str(settings.kuma_url)), tokens, token_error)
        container.kuma_publisher = publisher
    return CollectorRuntime(collector, AsyncIOScheduler(timezone="UTC"), engine.dispose, container, health_collector, publisher)


def create_app(
    *,
    runtime_factory: Callable[[], CollectorRuntime] = create_collector_runtime,
    container: RuntimeContainer | None = None,
    trusted_hosts: tuple[str, ...] | None = None,
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
            app.state.login_throttle = runtime.container.login_throttle or LoginThrottle(
                runtime.container.session_factory
            )
            app.state.trusted_proxy_networks = runtime.container.trusted_proxy_networks
            app.state.max_request_body_bytes = runtime.container.max_request_body_bytes
            if runtime.container.policy_rule_service is not None:
                try:
                    await runtime.container.policy_rule_service.ensure_default_openai_fallback()
                except Exception:
                    # A dashboard must remain operable if a legacy gateway does
                    # not yet expose managed rule providers. The service writes
                    # a safe audit event explaining the deferred default.
                    pass
        register_collector_jobs(runtime.scheduler, runtime.collector)
        register_health_jobs(runtime.scheduler, runtime.health_collector)
        register_kuma_jobs(runtime.scheduler, runtime.kuma_publisher)
        runtime.scheduler.start()
        app.state.collector = runtime.collector
        try:
            yield
        finally:
            runtime.scheduler.shutdown(wait=False)
            if runtime.close is not None:
                runtime.close()

    application = FastAPI(title="VPN Dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(container.trusted_hosts if container is not None else trusted_hosts or DEFAULT_TRUSTED_HOSTS),
    )

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next: Callable[[Request], Any]):
        content_length = request.headers.get("content-length")
        maximum_body_bytes = getattr(request.app.state, "max_request_body_bytes", 65_536)
        try:
            oversized = content_length is not None and int(content_length) > maximum_body_bytes
        except ValueError:
            oversized = True
        if oversized:
            return JSONResponse(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, content={"detail": "request too large"})
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    if container is not None:
        application.state.runtime = container
        application.state.auth_service = AuthService(
            container.session_factory,
            cookie_secure=container.cookie_secure,
            csrf_encryption_key=container.csrf_encryption_key,
        )
        application.state.login_throttle = container.login_throttle or LoginThrottle(container.session_factory)
        application.state.trusted_proxy_networks = container.trusted_proxy_networks
        application.state.max_request_body_bytes = container.max_request_body_bytes

    @application.get("/api/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/ready")
    async def ready() -> dict[str, str]:
        runtime = getattr(application.state, "runtime", None)
        if runtime is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service unavailable")
        try:
            with runtime.session_factory() as session:
                session.execute(text("SELECT 1"))
            await runtime.mihomo.version()
        except Exception as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service unavailable") from error
        return {"status": "ready"}

    @application.exception_handler(RequestValidationError)
    async def redact_validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        """Never reflect submitted passwords, rule text, or other request values."""

        locations = [list(item.get("loc", ())) for item in error.errors()]
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": "invalid request", "loc": locations})

    application.include_router(build_api_router())
    application.include_router(build_health_router())
    application.include_router(build_maintenance_router())
    install_maintenance_handlers(application)

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
