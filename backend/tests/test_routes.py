from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import ipaddress
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.collectors import GeoFileSnapshot
from app.db import create_all, create_session_factory, create_sqlite_engine
from app.main import create_app
from app.mihomo import ControllerWriteResult, MihomoVersion, ProxyGroup, Traffic
from app.models import AuditEvent, GatewayTrafficSample, GeoFileMetadata, GeoUpdate, ManagedRulePolicy, ProbeEvent, RouteEvent, TrafficMonthly
from app.policy_rules import ManagedRuleService
from app.routes import RuntimeContainer
from app.probe_targets import ProbeTargetService
from app.rules import DirectRuleValidationError, RuleService
from app.wgeasy import WireGuardClient


PASSWORD = "correct horse battery staple"
CONFIG = "[Interface]\nPrivateKey = fake-private-key\n[Peer]\nPublicKey = fake-public-key\n"


class FakeWgEasy:
    def __init__(self) -> None:
        self.list_calls = 0
        self.credentials_configured_value = False
        self.configure_calls = 0
        self.configure_error: Exception | None = None

    async def list_clients(self):
        self.list_calls += 1
        return [WireGuardClient(42, "laptop", True, "10.8.0.42", None, 12, 34)]

    def credentials_configured(self) -> bool:
        return self.credentials_configured_value

    async def configure_credentials(self, _username: str, _password: str) -> None:
        self.configure_calls += 1
        if self.configure_error is not None:
            raise self.configure_error
        self.credentials_configured_value = True

    async def create_client(self, name: str):
        return WireGuardClient(43, name, True, "10.8.0.43", None, 0, 0)

    async def disable_client(self, client_id: int):
        return SimpleNamespace(client_id=client_id, name="laptop", operation="disable")

    async def delete_client(self, client_id: int):
        return SimpleNamespace(client_id=client_id, name="laptop", operation="delete")

    async def config(self, client_id: int):
        return CONFIG

    async def qrcode(self, client_id: int):
        return '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z"/></svg>'


class FakeMihomo:
    async def version(self):
        return MihomoVersion("v1")

    async def traffic(self):
        return Traffic(12, 34)

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
    direct_text: str = "DOMAIN-SUFFIX,example.local,DIRECT\nIP-CIDR,192.168.0.0/16,DIRECT\n"

    def read_direct_rules(self) -> str:
        return self.direct_text

    async def apply_direct_rules_async(self, text: str, actor: str):
        return SimpleNamespace(number=3, sha256="b" * 64)


class InvalidRules:
    async def apply_direct_rules_async(self, text: str, actor: str):
        raise DirectRuleValidationError("invalid rule")


class LiveFallbackMihomo(FakeMihomo):
    async def groups(self):
        return [ProxyGroup("VPS-FALLBACK", "Fallback", ("WG-IMP", "HY2-NL"), "WG-IMP")]


class FakePolicyMihomo(FakeMihomo):
    async def rule_providers(self):
        return [
            SimpleNamespace(name="managed-direct"),
            SimpleNamespace(name="managed-fallback"),
            SimpleNamespace(name="managed-wg-imp"),
            SimpleNamespace(name="managed-hy2-nl"),
        ]


@dataclass
class FakeCollector:
    calls: int = 0
    probe_calls: int = 0
    probe_result: bool = True

    async def manual_geo_upgrade(self, actor: str) -> None:
        self.calls += 1

    async def run_probe_cycle(self) -> bool:
        self.probe_calls += 1
        return self.probe_result

    def start_probe_cycle(self) -> bool:
        self.probe_calls += 1
        return self.probe_result

    async def diagnose_probe(self, outbound: str, endpoint: str):
        return SimpleNamespace(
            observed_at=datetime(2026, 7, 14, 12, tzinfo=UTC),
            outbound=outbound,
            endpoint=endpoint,
            conclusion="exit_failure",
            conclusion_text="Контроллер и DNS отвечают; проверка через выбранный выход не завершилась.",
            controller=SimpleNamespace(succeeded=True, latency_ms=4, reason=None),
            dns=SimpleNamespace(succeeded=True, latency_ms=9, reason=None, hostname="www.openai.com", addresses=("104.18.33.45",)),
            exit=SimpleNamespace(succeeded=False, latency_ms=None, reason="controller request timed out"),
        )


@pytest.fixture
def route_parts(tmp_path, monkeypatch: pytest.MonkeyPatch):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>VPN Dashboard SPA</body></html>", encoding="utf-8")
    monkeypatch.setattr("app.main.STATIC_DIR", static_dir)
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    runtime = RuntimeContainer(
        session_factory=factory,
        wgeasy=FakeWgEasy(),
        mihomo=FakeMihomo(),
        rule_service=FakeRules(),
        collector=FakeCollector(),
    )
    try:
        with TestClient(create_app(container=runtime), base_url="https://testserver") as client:
            response = client.post("/api/auth/bootstrap", json={"password": PASSWORD})
            assert response.status_code == 201
            client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            yield client, factory
    finally:
        engine.dispose()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.headers["X-CSRF-Token"]}


def test_root_serves_the_spa_entry_document(route_parts) -> None:
    client, _factory = route_parts

    response = client.get("/")

    assert response.status_code == 200
    assert response.text == "<html><body>VPN Dashboard SPA</body></html>"


def test_browser_router_deep_link_serves_the_spa_entry_document(route_parts) -> None:
    client, _factory = route_parts

    response = client.get("/routes")

    assert response.status_code == 200
    assert response.text == "<html><body>VPN Dashboard SPA</body></html>"


def test_spa_entry_is_not_cached_between_dashboard_releases(route_parts) -> None:
    client, _factory = route_parts

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_missing_frontend_asset_is_not_replaced_with_the_spa_document(route_parts) -> None:
    client, _factory = route_parts

    response = client.get("/assets/index-expired.js")

    assert response.status_code == 404


def test_overview_never_contains_secret(route_parts) -> None:
    client, _factory = route_parts

    payload = client.get("/api/overview").text

    assert "MIHOMO_API_SECRET" not in payload
    assert "private_key" not in payload.lower()
    assert "fake-private-key" not in payload


def test_config_is_an_authenticated_plain_text_attachment(route_parts) -> None:
    client, _factory = route_parts

    response = client.get("/api/clients/42/config")

    assert response.status_code == 200
    assert response.text == CONFIG
    assert response.headers["content-type"].startswith("text/plain")
    assert "attachment" in response.headers["content-disposition"]
    assert 'filename="laptop.conf"' in response.headers["content-disposition"]
    assert "filename*=UTF-8''laptop.conf" in response.headers["content-disposition"]


def test_wgeasy_credential_status_is_authenticated_and_never_lists_clients(route_parts) -> None:
    client, _factory = route_parts
    wgeasy = client.app.state.runtime.wgeasy

    response = client.get("/api/wgeasy/credentials/status")
    client.cookies.clear()
    anonymous = client.get("/api/wgeasy/credentials/status")

    assert anonymous.status_code == 401
    assert response.status_code == 200
    assert response.json() == {"configured": False}
    assert wgeasy.list_calls == 0


def test_wgeasy_credential_setup_requires_csrf_and_redacts_validation_and_upstream_failures(route_parts) -> None:
    client, _factory = route_parts
    wgeasy = client.app.state.runtime.wgeasy
    username = "submitted-user"
    password = "submitted-password"
    csrf_token = client.headers.pop("X-CSRF-Token")

    denied = client.post("/api/wgeasy/credentials", json={"username": username, "password": password})
    client.headers["X-CSRF-Token"] = csrf_token
    invalid = client.post(
        "/api/wgeasy/credentials",
        json={"username": "   ", "password": password, "unexpected": "field"},
    )
    wgeasy.configure_error = RuntimeError("upstream failure with sensitive context")
    rejected = client.post("/api/wgeasy/credentials", json={"username": username, "password": password})
    wgeasy.configure_error = None
    accepted = client.post("/api/wgeasy/credentials", json={"username": f" {username} ", "password": f" {password} "})

    assert denied.status_code == 403
    assert invalid.status_code == 422
    assert rejected.status_code == 502
    assert rejected.json() == {"detail": "backend operation failed"}
    assert accepted.status_code == 200
    assert accepted.json() == {"configured": True}
    assert wgeasy.configure_calls == 2
    for response in (invalid, rejected):
        assert username not in response.text
        assert password not in response.text
        assert "sensitive context" not in response.text


def test_mutations_write_redacted_audit_events(route_parts) -> None:
    client, factory = route_parts

    assert client.post("/api/rules/apply", json={"text": "DOMAIN,example.com,DIRECT"}, headers=csrf(client)).status_code == 200
    assert client.post("/api/updates/geo", headers=csrf(client)).status_code == 202
    journal = client.get("/api/journal")
    with factory() as session:
        audit = session.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()

    assert journal.status_code == 200
    assert {event.action for event in audit} >= {"geo_upgrade"}
    rendered = "\n".join((event.detail or "") + (event.error_text or "") for event in audit)
    assert CONFIG not in rendered
    assert "fake-private-key" not in rendered
    assert "MIHOMO_API_SECRET" not in rendered


def test_rules_returns_the_exact_loaded_direct_file_only_to_authenticated_owner(route_parts) -> None:
    client, _factory = route_parts

    response = client.get("/api/rules")

    assert response.status_code == 200
    assert response.json()["direct_text"] == "DOMAIN-SUFFIX,example.local,DIRECT\nIP-CIDR,192.168.0.0/16,DIRECT\n"


def test_live_fallback_selection_overrides_a_stored_observation(route_parts) -> None:
    client, factory = route_parts
    client.app.state.runtime.mihomo = LiveFallbackMihomo()
    with factory.begin() as session:
        session.add(
            ProbeEvent(
                observed_at=datetime(2026, 7, 13, 12, tzinfo=UTC),
                target="route-state:VPS-FALLBACK",
                succeeded=True,
                latency_ms=None,
                endpoint=None,
                outbound="HY2-NL",
                status="selected",
                status_code=None,
                error_text=None,
                detail=None,
            )
        )

    assert client.get("/api/overview").json()["fallback"]["selected"] == "WG-IMP"
    assert client.get("/api/routes").json()["fallback"]["selected"] == "WG-IMP"


def test_invalid_direct_rule_is_a_safe_validation_failure(route_parts) -> None:
    client, factory = route_parts
    client.app.state.runtime.rule_service = InvalidRules()

    response = client.post("/api/rules/apply", json={"text": "not a direct rule"}, headers=csrf(client))
    with factory() as session:
        actions = [event.action for event in session.scalars(select(AuditEvent)).all()]

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid direct rule"}
    assert "not a direct rule" not in response.text
    assert actions.count("direct_rules_validation_failed") == 1


def test_direct_rule_apply_is_audited_once_by_the_rule_service(route_parts, tmp_path) -> None:
    client, factory = route_parts
    direct_path = tmp_path / "direct.txt"
    direct_path.write_text("DOMAIN,example.org,DIRECT\n", encoding="utf-8")
    client.app.state.runtime.rule_service = RuleService(
        direct_path,
        revision_dir=tmp_path / "revisions",
        mihomo=FakeMihomo(),
        audit_session_factory=factory,
    )

    response = client.post("/api/rules/apply", json={"text": "DOMAIN,new.example,DIRECT\n"}, headers=csrf(client))
    with factory() as session:
        actions = [event.action for event in session.scalars(select(AuditEvent)).all()]

    assert response.status_code == 200
    assert actions.count("direct_rules_apply") == 1


def test_overview_and_routes_serialize_collected_state_without_details(route_parts) -> None:
    client, factory = route_parts
    observed_at = datetime(2026, 7, 13, 12, tzinfo=UTC)
    with factory.begin() as session:
        session.add_all(
            [
                ProbeEvent(
                    observed_at=observed_at,
                    target="service:wg-easy",
                    succeeded=True,
                    latency_ms=7,
                    endpoint=None,
                    outbound=None,
                    status="ok",
                    status_code=200,
                    error_text="must not be returned",
                    detail="must not be returned",
                ),
                ProbeEvent(
                    observed_at=observed_at,
                    target="service:mihomo",
                    succeeded=False,
                    latency_ms=None,
                    endpoint=None,
                    outbound=None,
                    status="unavailable",
                    status_code=503,
                    error_text="must not be returned",
                    detail="must not be returned",
                ),
                ProbeEvent(
                    observed_at=observed_at,
                    target="route-state:AUTO",
                    succeeded=True,
                    latency_ms=None,
                    endpoint=None,
                    outbound="HY2-NL",
                    status="selected",
                    status_code=None,
                    error_text="must not be returned",
                    detail="must not be returned",
                ),
                ProbeEvent(
                    observed_at=observed_at,
                    target="HY2-NL",
                    succeeded=True,
                    latency_ms=42,
                    endpoint="https://www.google.com/generate_204",
                    outbound="HY2-NL",
                    status="ok",
                    status_code=None,
                    error_text="must not be returned",
                    detail="must not be returned",
                ),
                RouteEvent(
                    observed_at=observed_at,
                    route="AUTO",
                    action="selected_outbound_changed",
                    previous_outbound="WG-IMP",
                    new_outbound="HY2-NL",
                    detail="must not be returned",
                ),
            ]
        )

    overview = client.get("/api/overview")
    routes = client.get("/api/routes")

    assert overview.status_code == 200
    assert {item["name"]: item["succeeded"] for item in overview.json()["services"]} == {
        "mihomo": False,
        "wg-easy": True,
    }
    assert overview.json()["fallback"]["selected"] == "HY2-NL"
    assert routes.status_code == 200
    payload = routes.json()
    assert payload["fallback"] == {
        "primary": "WG-IMP",
        "reserve": "HY2-NL",
        "selected": "HY2-NL",
    }
    assert payload["probes"] == [
        {
            "observed_at": "2026-07-13T12:00:00Z",
            "target": "HY2-NL",
            "succeeded": True,
            "latency_ms": 42,
            "endpoint": "https://www.google.com/generate_204",
            "outbound": "HY2-NL",
                "status": "ok",
                "status_code": None,
                "reason": None,
            }
        ]
    assert payload["last_switch"] == {
        "observed_at": "2026-07-13T12:00:00Z",
        "route": "AUTO",
        "action": "selected_outbound_changed",
        "previous_outbound": "WG-IMP",
        "new_outbound": "HY2-NL",
    }
    assert "must not be returned" not in f"{overview.text}{routes.text}"


def test_traffic_usage_uses_stored_monthly_deltas_for_the_requested_period(route_parts) -> None:
    client, factory = route_parts
    current_period = datetime.now(UTC).strftime("%Y-%m")
    with factory.begin() as session:
        session.add(
            TrafficMonthly(
                peer_key="id:42",
                peer_name="laptop",
                peer_id="42",
                period=current_period,
                received_bytes=200,
                transmitted_bytes=100,
            )
        )

    response = client.get("/api/traffic", params={"period": "month"})

    assert response.status_code == 200
    assert response.json() == {
        "period": "month",
        "usage": [
            {
                "peer_id": "42",
                "peer_name": "laptop",
                "received_bytes": 200,
                "transmitted_bytes": 100,
            }
        ],
    }


def test_realtime_traffic_reads_only_minutely_stored_gateway_samples(route_parts) -> None:
    client, factory = route_parts
    observed_at = datetime.now(UTC).replace(second=0, microsecond=0)
    with factory.begin() as session:
        session.add_all(
            [
                GatewayTrafficSample(
                    observed_at=observed_at - timedelta(minutes=offset),
                    up_bps=100 + offset,
                    down_bps=200 + offset,
                )
                for offset in reversed(range(5))
            ]
        )

    response = client.get("/api/traffic/realtime", params={"period": "5m"})

    assert response.status_code == 200
    assert response.json() == {
        "period": "5m",
        "sample_interval_seconds": 60,
        "points": [
            {
                "observed_at": (observed_at - timedelta(minutes=offset)).isoformat().replace("+00:00", "Z"),
                "up_bps": 100 + offset,
                "down_bps": 200 + offset,
            }
            for offset in reversed(range(5))
        ],
    }


def test_journal_filters_real_probe_and_route_fields_on_the_server(route_parts) -> None:
    client, factory = route_parts
    observed_at = datetime(2026, 7, 13, 12, tzinfo=UTC)
    with factory.begin() as session:
        session.add_all(
            [
                AuditEvent(
                    observed_at=observed_at,
                    actor="owner",
                    action="client_create",
                    succeeded=True,
                    status_code=None,
                    detail="must not be returned",
                    error_text=None,
                    restored=None,
                    revision_number=None,
                ),
                ProbeEvent(
                    observed_at=observed_at,
                    target="HY2-NL",
                    succeeded=True,
                    latency_ms=33,
                    endpoint="https://api.github.com/",
                    outbound="HY2-NL",
                    status="ok",
                    status_code=200,
                    detail="must not be returned",
                    error_text="must not be returned",
                ),
                RouteEvent(
                    observed_at=observed_at,
                    route="AUTO",
                    action="selected_outbound_changed",
                    previous_outbound="WG-IMP",
                    new_outbound="HY2-NL",
                    detail="must not be returned",
                ),
            ]
        )

    response = client.get("/api/journal", params={"outbound": "HY2-NL", "endpoint": "https://api.github.com/"})

    assert response.status_code == 200
    assert response.json()["events"] == [
        {
            "id": 1,
            "kind": "probe",
            "observed_at": "2026-07-13T12:00:00Z",
            "actor": None,
            "action": "probe",
            "outbound": "HY2-NL",
            "endpoint": "https://api.github.com/",
            "revision_number": None,
            "succeeded": True,
            "status_code": 200,
            "restored": None,
            "latency_ms": 33,
        }
    ]
    assert "must not be returned" not in response.text


def test_updates_returns_current_geodata_files_with_their_last_observed_time(route_parts) -> None:
    client, factory = route_parts
    observed_at = datetime(2026, 7, 14, 10, tzinfo=UTC)
    client.app.state.runtime.collector.current_geo_metadata = lambda: (
        (
            GeoFileSnapshot("geoip.dat", 10, observed_at, "a" * 64),
            GeoFileSnapshot("geosite.dat", 12, observed_at, "b" * 64),
        ),
        None,
    )
    with factory.begin() as session:
        update = GeoUpdate(
            observed_at=observed_at,
            source="daily",
            operation="metadata_snapshot",
            succeeded=True,
            status_code=None,
            error_text=None,
            version=None,
            detail=None,
        )
        session.add(update)
        session.flush()
        session.add_all(
            [
                GeoFileMetadata(
                    geo_update_id=update.id,
                    phase="daily",
                    filename="geoip.dat",
                    size_bytes=10,
                    modified_at=observed_at,
                    sha256="a" * 64,
                ),
                GeoFileMetadata(
                    geo_update_id=update.id,
                    phase="daily",
                    filename="geosite.dat",
                    size_bytes=12,
                    modified_at=observed_at,
                    sha256="b" * 64,
                ),
            ]
        )

    response = client.get("/api/updates")

    assert response.status_code == 200
    assert response.json()["assets"] == [
        {
            "filename": "geoip.dat",
            "kind": "GeoIP",
            "size_bytes": 10,
            "modified_at": "2026-07-14T10:00:00Z",
            "sha256": "a" * 64,
            "last_observed_at": "2026-07-14T10:00:00Z",
        },
        {
            "filename": "geosite.dat",
            "kind": "GeoSite",
            "size_bytes": 12,
            "modified_at": "2026-07-14T10:00:00Z",
            "sha256": "b" * 64,
            "last_observed_at": "2026-07-14T10:00:00Z",
        },
    ]


def test_manual_geo_update_returns_a_verified_file_outcome(route_parts) -> None:
    client, factory = route_parts
    observed_at = datetime(2026, 7, 21, 19, 59, tzinfo=UTC)
    with factory.begin() as session:
        update = GeoUpdate(
            observed_at=observed_at,
            source="manual",
            operation="geo_upgrade",
            succeeded=True,
            status_code=204,
            error_text=None,
            version=None,
            detail=None,
        )
        session.add(update)
        session.flush()
        session.add_all(
            [
                GeoFileMetadata(
                    geo_update_id=update.id,
                    phase="before",
                    filename="GeoIP.dat",
                    size_bytes=10,
                    modified_at=observed_at,
                    sha256="a" * 64,
                ),
                GeoFileMetadata(
                    geo_update_id=update.id,
                    phase="after",
                    filename="GeoIP.dat",
                    size_bytes=12,
                    modified_at=observed_at,
                    sha256="b" * 64,
                ),
            ]
        )
        update_id = update.id

    class CompletedGeoCollector:
        async def manual_geo_upgrade(self, actor: str) -> int:
            assert actor == "owner"
            return update_id

    client.app.state.runtime.collector = CompletedGeoCollector()

    response = client.post("/api/updates/geo", headers=csrf(client))

    assert response.status_code == 200
    assert response.json() == {
        "id": update_id,
        "observed_at": "2026-07-21T19:59:00Z",
        "source": "manual",
        "operation": "geo_upgrade",
        "succeeded": True,
        "status_code": 204,
        "version": None,
        "verification": "changed",
        "checked_files": ["GeoIP.dat"],
        "changed_files": ["GeoIP.dat"],
    }


def test_updates_includes_direct_and_managed_rule_audit(route_parts) -> None:
    client, factory = route_parts
    observed_at = datetime(2026, 7, 14, 10, tzinfo=UTC)
    with factory.begin() as session:
        session.add_all(
            [
                AuditEvent(
                    observed_at=observed_at,
                    actor="admin",
                    action="direct_rules_apply",
                    revision_number=7,
                    succeeded=True,
                    status_code=None,
                    error_text=None,
                    restored=None,
                    detail=None,
                ),
                AuditEvent(
                    observed_at=observed_at,
                    actor="admin",
                    action="policy_rule_create",
                    revision_number=None,
                    succeeded=True,
                    status_code=None,
                    error_text=None,
                    restored=None,
                    detail='{"kind":"GEOSITE","category":"openai"}',
                ),
            ]
        )

    response = client.get("/api/updates")

    assert response.status_code == 200
    assert response.json()["rule_changes"] == [
        {
            "observed_at": "2026-07-14T10:00:00Z",
            "actor": "admin",
            "action": "policy_rule_create",
            "subject": "GEOSITE: openai",
            "succeeded": True,
            "revision_number": None,
        },
        {
            "observed_at": "2026-07-14T10:00:00Z",
            "actor": "admin",
            "action": "direct_rules_apply",
            "subject": "revision 7",
            "succeeded": True,
            "revision_number": 7,
        },
    ]


def test_rules_catalogue_contains_current_ai_russian_p2p_and_streaming_categories(route_parts, tmp_path) -> None:
    client, factory = route_parts

    response = client.get("/api/rules")

    assert response.status_code == 200
    categories = {(item["kind"], item["category"]) for item in response.json()["policy_catalog"]}
    required = {
        ("GEOSITE", "category-ai-!cn"), ("GEOSITE", "microsoft"),
        ("GEOSITE", "category-bank-ru"), ("GEOSITE", "category-ecommerce-ru"),
        ("GEOSITE", "category-gov-ru"), ("GEOSITE", "ozon"), ("GEOSITE", "kinopoisk"),
        ("GEOSITE", "sber"), ("GEOIP", "cloudfront"),
        ("GEOSITE", "category-public-tracker"), ("GEOSITE", "category-pt"),
        ("GEOSITE", "tracker"), ("GEOSITE", "category-entertainment"),
        ("GEOSITE", "category-media"), ("GEOSITE", "disney"),
        ("GEOSITE", "hbo"), ("GEOSITE", "primevideo"),
        ("GEOSITE", "twitch"), ("GEOSITE", "dazn"),
        ("GEOSITE", "bilibili"), ("GEOSITE", "biliintl"),
        ("GEOSITE", "anime"), ("GEOSITE", "ehentai"),
        ("GEOSITE", "category-porn"), ("GEOSITE", "category-games"),
        ("GEOSITE", "category-game-platforms-download"),
        ("GEOSITE", "category-android-app-download"), ("GEOSITE", "steam"),
        ("GEOSITE", "category-ads-all"), ("GEOSITE", "speedtest"),
    }
    assert required <= categories

    _prepare_managed_rule_files(tmp_path / "rules")
    client.app.state.runtime.policy_rule_service = ManagedRuleService(tmp_path / "rules", FakePolicyMihomo(), factory)
    created = client.post(
        "/api/rules/policies",
        json={"kind": "GEOSITE", "category": "category-public-tracker", "action": "VPS-FALLBACK", "enabled": True},
    )
    repeated = client.post(
        "/api/rules/policies",
        json={"kind": "GEOSITE", "category": "category-public-tracker", "action": "VPS-FALLBACK", "enabled": True},
    )
    rejected = client.post(
        "/api/rules/policies",
        json={"kind": "GEOSITE", "category": "category-p2p", "action": "VPS-FALLBACK", "enabled": True},
    )

    assert created.status_code == 201
    assert created.json() == {
        "id": 1,
        "kind": "GEOSITE",
        "category": "category-public-tracker",
        "label": "Публичные торрент-трекеры",
        "action": "VPS-FALLBACK",
        "enabled": True,
    }
    assert repeated.status_code == 201
    assert repeated.json() == created.json()
    with factory() as session:
        assert len(session.scalars(select(ManagedRulePolicy)).all()) == 1
    assert rejected.status_code == 422
    assert rejected.json() == {"detail": "invalid managed rule"}


def test_openai_uses_the_shared_fallback_by_default_without_overwriting_an_admin_rule(route_parts, tmp_path) -> None:
    _client, factory = route_parts
    rules_dir = tmp_path / "rules"
    _prepare_managed_rule_files(rules_dir)
    # Empty file providers are intentionally absent from Mihomo's controller
    # response until they contain a rule. The first policy must still apply.
    service = ManagedRuleService(rules_dir, FakeMihomo(), factory)

    assert asyncio.run(service.ensure_default_openai_fallback()) is True
    assert asyncio.run(service.ensure_default_openai_fallback()) is False

    assert [(item.kind, item.category, item.action) for item in service.list_policies()] == [
        ("GEOSITE", "openai", "VPS-FALLBACK"),
    ]
    assert (rules_dir / "managed-fallback.txt").read_text(encoding="utf-8") == "GEOSITE,openai\n"
    with factory() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.action == "policy_default_openai"))
    assert event is not None
    assert event.actor == "system"


def _prepare_managed_rule_files(rules_dir) -> None:
    rules_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("managed-direct.txt", "managed-wg-imp.txt", "managed-hy2-nl.txt", "managed-fallback.txt"):
        (rules_dir / filename).write_text("", encoding="utf-8")


def test_manual_probe_reports_busy_state_and_safe_failure_reason(route_parts) -> None:
    client, factory = route_parts
    collector: FakeCollector = client.app.state.runtime.collector

    started = client.post("/api/probes/run")

    assert started.status_code == 202
    assert collector.probe_calls == 1

    collector.probe_result = False
    busy = client.post("/api/probes/run")
    assert busy.status_code == 409
    assert busy.json()["detail"] == "A probe run is already in progress"

    with factory.begin() as session:
        session.add(
            ProbeEvent(
                observed_at=datetime.now(UTC),
                target="WG-IMP",
                succeeded=False,
                latency_ms=None,
                endpoint="https://www.google.com/generate_204",
                outbound="WG-IMP",
                status="failed",
                status_code=None,
                error_text="unexpected controller response",
                detail=None,
            )
        )

    payload = client.get("/api/routes").json()

    assert payload["probes"][0]["reason"] == "unexpected controller response"


def test_probe_diagnosis_reports_controller_dns_and_selected_exit_for_an_enabled_target(route_parts) -> None:
    client, factory = route_parts
    client.app.state.runtime.probe_targets = ProbeTargetService(
        factory,
        resolver=lambda _hostname: (ipaddress.ip_address("104.18.33.45"),),
    )
    client.put("/api/probes/targets", json={"targets": [{"key": item["key"], "enabled": item["key"] == "openai"} for item in client.get("/api/probes/targets").json()]})

    response = client.post("/api/probes/diagnose", json={"target_key": "openai", "outbound": "WG-IMP"})

    assert response.status_code == 200
    assert response.json() == {
        "observed_at": "2026-07-14T12:00:00Z",
        "outbound": "WG-IMP",
        "endpoint": "https://www.openai.com/",
        "conclusion": "exit_failure",
        "conclusion_text": "Контроллер и DNS отвечают; проверка через выбранный выход не завершилась.",
        "controller": {"succeeded": True, "latency_ms": 4, "reason": None},
        "dns": {"succeeded": True, "latency_ms": 9, "reason": None, "hostname": "www.openai.com", "addresses": ["104.18.33.45"]},
        "exit": {"succeeded": False, "latency_ms": None, "reason": "controller request timed out"},
    }


def test_probe_diagnosis_rejects_an_disabled_target(route_parts) -> None:
    client, factory = route_parts
    client.app.state.runtime.probe_targets = ProbeTargetService(
        factory,
        resolver=lambda _hostname: (ipaddress.ip_address("104.18.33.45"),),
    )

    response = client.post("/api/probes/diagnose", json={"target_key": "openai", "outbound": "WG-IMP"})

    assert response.status_code == 422


def test_custom_probe_target_api_creates_updates_and_deletes_only_public_https_targets(route_parts) -> None:
    client, factory = route_parts
    client.app.state.runtime.probe_targets = ProbeTargetService(
        factory,
        resolver=lambda _hostname: (ipaddress.ip_address("93.184.216.34"),),
    )

    created = client.post("/api/probes/targets", json={"label": "Status", "url": "https://example.com/health"})

    assert created.status_code == 201
    target = created.json()
    assert target["is_custom"] is True

    changed = client.put(
        f"/api/probes/targets/{target['key']}",
        json={"label": "Status 2", "url": "https://example.com/ready", "enabled": False},
    )
    assert changed.status_code == 200
    assert changed.json()["label"] == "Status 2"

    invalid = client.post("/api/probes/targets", json={"label": "NAS", "url": "https://192.168.2.103/"})
    assert invalid.status_code == 422

    deleted = client.delete(f"/api/probes/targets/{target['key']}")
    assert deleted.status_code == 204
