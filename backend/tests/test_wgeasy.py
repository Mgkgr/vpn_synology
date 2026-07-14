from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.models import AuditEvent, WgEasyCredential
from app.settings import Settings
from app.wgeasy import (
    ProfileMutationDisabled,
    WgEasyAdapter,
    WgEasyCredentialVault,
    WgEasyIntegrationError,
)


TEST_FERNET_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


class MockWgEasy:
    """A local httpx transport that isolates the adapter from the network."""

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], list[httpx.Response | BaseException]] = {}
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def add_json(self, method: str, url: str, payload: Any, *, status_code: int = 200) -> None:
        self.add_response(method, url, httpx.Response(status_code, json=payload))

    def add_content(
        self,
        method: str,
        url: str,
        content: str,
        *,
        content_type: str,
        status_code: int = 200,
    ) -> None:
        self.add_response(method, url, httpx.Response(status_code, content=content.encode(), headers={"Content-Type": content_type}))

    def add_response(self, method: str, url: str, response: httpx.Response) -> None:
        self.responses.setdefault((method, url), []).append(response)

    def add_exception(self, method: str, url: str, error: BaseException) -> None:
        self.responses.setdefault((method, url), []).append(error)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, str(request.url))
        queued = self.responses.get(key, [])
        response = queued.pop(0) if queued else httpx.Response(500, request=request)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def adapter_parts(tmp_path: Any) -> tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any]:
    settings = Settings(
        dashboard_bind="192.168.2.103:8088",
        mihomo_url="http://mihomo:9091",
        wgeasy_url="http://vpn-wireguard:51821",
        metacubexd_url="http://metacubexd:9090",
        kuma_url="http://kuma:3001",
        dashboard_encryption_key=TEST_FERNET_KEY,
        database_path=tmp_path / "dashboard.sqlite3",
        direct_rules_path=tmp_path / "direct.txt",
        geodata_dir=tmp_path / "geodata",
    )
    engine = create_sqlite_engine(settings.database_path)
    create_all(engine)
    session_factory = create_session_factory(engine)
    vault = WgEasyCredentialVault(session_factory, settings.dashboard_encryption_key.get_secret_value())
    vault.store("dashboard-user", "dashboard-password")
    return settings, vault, MockWgEasy(), session_factory


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def client_payload(*, client_id: int = 8, name: str = "laptop") -> dict[str, Any]:
    return {
        "id": client_id,
        "name": name,
        "enabled": True,
        "ipv4Address": "10.8.0.8",
        "latestHandshakeAt": None,
        "transferRx": 0,
        "transferTx": 0,
    }


def test_mutation_is_blocked_if_v15_contract_shape_changes(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    settings, vault, upstream, _session_factory = adapter_parts
    upstream.add_json("GET", "http://vpn-wireguard:51821/api/client", {"unexpected": True})
    adapter = WgEasyAdapter(settings, credential_vault=vault, transport=upstream.transport)

    assert not run(adapter.verify_contract()).ready

    upstream.add_json("GET", "http://vpn-wireguard:51821/api/client", {"unexpected": True})
    with pytest.raises(ProfileMutationDisabled):
        run(adapter.create_client("laptop"))

    assert [request.method for request in upstream.requests] == ["GET", "GET"]


def test_contract_normalizes_only_the_v15_list_fields(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    settings, vault, upstream, _session_factory = adapter_parts
    aliased = client_payload()
    aliased.pop("id")
    aliased["clientId"] = 9
    upstream.add_json("GET", "http://vpn-wireguard:51821/api/client", [aliased])
    adapter = WgEasyAdapter(settings, credential_vault=vault, transport=upstream.transport)

    status = run(adapter.verify_contract())

    assert status.ready is True
    assert status.clients[0].id == 9
    assert status.clients[0].ipv4_address == "10.8.0.8"
    assert status.clients[0].latest_handshake_at is None
    assert (status.clients[0].received_bytes, status.clients[0].transmitted_bytes) == (0, 0)
    assert upstream.requests[0].headers["Authorization"].startswith("Basic ")


def test_contract_normalizes_null_transfer_counters_for_unused_wg_easy_clients(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    settings, vault, upstream, _session_factory = adapter_parts
    upstream.add_json(
        "GET",
        "http://vpn-wireguard:51821/api/client",
        [dict(client_payload(), transferRx=None, transferTx=None)],
    )
    adapter = WgEasyAdapter(settings, credential_vault=vault, transport=upstream.transport)

    status = run(adapter.verify_contract())

    assert status.ready is True
    assert (status.clients[0].received_bytes, status.clients[0].transmitted_bytes) == (0, 0)


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": True},
        [dict(client_payload(), id=0)],
        [dict(client_payload(), enabled=1)],
        [dict(client_payload(), ipv4Address="not-an-ip")],
        [{key: value for key, value in client_payload().items() if key != "latestHandshakeAt"}],
        [dict(client_payload(), transferTx=-1)],
    ],
)
def test_contract_rejects_missing_or_invalid_required_client_fields(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any], payload: Any
) -> None:
    settings, vault, upstream, _session_factory = adapter_parts
    upstream.add_json("GET", "http://vpn-wireguard:51821/api/client", payload)
    adapter = WgEasyAdapter(settings, credential_vault=vault, transport=upstream.transport)

    status = run(adapter.verify_contract())

    assert status.ready is False
    assert status.reason == "client-list contract mismatch"


def test_lifecycle_is_guarded_and_audit_never_contains_configuration_or_credentials(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    settings, vault, upstream, session_factory = adapter_parts
    list_url = "http://vpn-wireguard:51821/api/client"
    upstream.add_json("GET", list_url, [client_payload(client_id=1, name="existing")])
    upstream.add_json("POST", list_url, {"success": True, "clientId": 8})
    upstream.add_json("GET", list_url, [client_payload(client_id=8, name="laptop")])
    upstream.add_content(
        "GET",
        "http://vpn-wireguard:51821/api/client/8/configuration",
        "[Interface]\nPrivateKey = fake-private-key\n[Peer]\nPublicKey = fake-public-key\n",
        content_type="application/octet-stream",
    )
    upstream.add_content(
        "GET",
        "http://vpn-wireguard:51821/api/client/8/qrcode.svg",
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z"/></svg>',
        content_type="image/svg+xml",
    )
    upstream.add_json("GET", list_url, [client_payload(client_id=8, name="laptop")])
    upstream.add_json("POST", "http://vpn-wireguard:51821/api/client/8/disable", {"success": True})
    upstream.add_json("GET", list_url, [dict(client_payload(client_id=8, name="laptop"), enabled=False)])
    upstream.add_json("GET", list_url, [dict(client_payload(client_id=8, name="laptop"), enabled=False)])
    upstream.add_json("DELETE", "http://vpn-wireguard:51821/api/client/8", {"success": True})
    upstream.add_json("GET", list_url, [])
    adapter = WgEasyAdapter(
        settings,
        credential_vault=vault,
        audit_session_factory=session_factory,
        transport=upstream.transport,
    )

    created = run(adapter.create_client("laptop"))
    configuration = run(adapter.config(8))
    qrcode = run(adapter.qrcode(8))
    disabled = run(adapter.disable_client(8))
    deleted = run(adapter.delete_client(8))

    assert (created.id, created.name) == (8, "laptop")
    assert configuration.startswith("[Interface]\n")
    assert qrcode.startswith("<svg")
    assert (disabled.client_id, disabled.name, deleted.client_id, deleted.name) == (8, "laptop", 8, "laptop")
    assert [request.method for request in upstream.requests] == ["GET", "POST", "GET", "GET", "GET", "GET", "POST", "GET", "GET", "DELETE", "GET"]
    assert all(request.headers["Authorization"].startswith("Basic ") for request in upstream.requests)
    assert json.loads(upstream.requests[1].content) == {"name": "laptop", "expiresAt": None}

    with session_factory() as session:
        audit_values = "\n".join(event.detail or "" for event in session.scalars(select(AuditEvent)).all())
    assert "laptop" in audit_values
    assert '"client_id":8' in audit_values
    for secret in ("dashboard-user", "dashboard-password", "fake-private-key", "fake-public-key", configuration, qrcode, "Basic "):
        assert secret not in audit_values


def test_create_recovers_when_the_first_confirmation_list_read_times_out(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    settings, vault, upstream, _session_factory = adapter_parts
    list_url = "http://vpn-wireguard:51821/api/client"
    upstream.add_json("GET", list_url, [client_payload(client_id=1, name="existing")])
    upstream.add_json("POST", list_url, {"success": True, "clientId": 8})
    upstream.add_exception("GET", list_url, httpx.ReadTimeout("slow list response"))
    upstream.add_json("GET", list_url, [client_payload(client_id=8, name="laptop")])
    adapter = WgEasyAdapter(settings, credential_vault=vault, transport=upstream.transport)

    created = run(adapter.create_client("laptop"))

    assert (created.id, created.name) == (8, "laptop")
    assert [request.method for request in upstream.requests] == ["GET", "POST", "GET", "GET"]


def test_vault_persists_only_fernet_ciphertext(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    _settings, vault, _upstream, session_factory = adapter_parts

    credentials = vault.load()
    with session_factory() as session:
        stored = session.scalars(select(WgEasyCredential)).one()

    assert (credentials.username, credentials.password) == ("dashboard-user", "dashboard-password")
    assert stored.ciphertext != "dashboard-user:dashboard-password"
    assert "dashboard-user" not in stored.ciphertext
    assert "dashboard-password" not in stored.ciphertext


def test_configuring_credentials_verifies_the_v15_list_before_encrypted_rotation_and_audits(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    settings, vault, upstream, session_factory = adapter_parts
    username = "rotated-user"
    password = "rotated-password"
    upstream.add_json("GET", "http://vpn-wireguard:51821/api/client", [client_payload()])
    adapter = WgEasyAdapter(
        settings,
        credential_vault=vault,
        audit_session_factory=session_factory,
        transport=upstream.transport,
    )

    run(adapter.configure_credentials(username, password))

    with session_factory() as session:
        stored = session.scalars(select(WgEasyCredential)).one()
        audit = session.scalars(select(AuditEvent).where(AuditEvent.action == "wgeasy_credentials_configured")).one()
    assert (vault.load().username, vault.load().password) == (username, password)
    assert username not in stored.ciphertext
    assert password not in stored.ciphertext
    assert audit.detail is None
    assert audit.error_text is None
    assert upstream.requests[0].headers["Authorization"].startswith("Basic ")


def test_failed_credential_verification_keeps_the_previous_encrypted_pair(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    settings, vault, upstream, session_factory = adapter_parts
    candidate_username = "rejected-user"
    candidate_password = "rejected-password"
    upstream.add_json("GET", "http://vpn-wireguard:51821/api/client", {"unexpected": True})
    adapter = WgEasyAdapter(
        settings,
        credential_vault=vault,
        audit_session_factory=session_factory,
        transport=upstream.transport,
    )

    with pytest.raises(WgEasyIntegrationError) as raised:
        run(adapter.configure_credentials(candidate_username, candidate_password))

    assert (vault.load().username, vault.load().password) == ("dashboard-user", "dashboard-password")
    assert candidate_username not in str(raised.value)
    assert candidate_password not in str(raised.value)
    with session_factory() as session:
        audit = session.scalars(select(AuditEvent).where(AuditEvent.action == "wgeasy_credentials_configure_failed")).one()
    assert audit.succeeded is False
    assert audit.detail is None
    assert audit.error_text is None


def test_invalid_configuration_and_svg_are_rejected_without_echoing_payloads(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    settings, vault, upstream, _session_factory = adapter_parts
    malformed_config = "raw-config-secret"
    malformed_svg = "<script>raw-svg-secret</script>"
    upstream.add_content(
        "GET",
        "http://vpn-wireguard:51821/api/client/8/configuration",
        malformed_config,
        content_type="application/octet-stream",
    )
    upstream.add_content(
        "GET",
        "http://vpn-wireguard:51821/api/client/8/qrcode.svg",
        malformed_svg,
        content_type="image/svg+xml",
    )
    adapter = WgEasyAdapter(settings, credential_vault=vault, transport=upstream.transport)

    with pytest.raises(WgEasyIntegrationError) as config_error:
        run(adapter.config(8))
    with pytest.raises(WgEasyIntegrationError) as qrcode_error:
        run(adapter.qrcode(8))

    assert "invalid configuration response" in str(config_error.value)
    assert "invalid SVG response" in str(qrcode_error.value)
    assert malformed_config not in str(config_error.value)
    assert malformed_svg not in str(qrcode_error.value)


@pytest.mark.parametrize(
    "svg",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import url("https://attacker.invalid/qr.css");</style><path d="M0 0h1v1H0z"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z" style="fill:url(https://attacker.invalid/qr.css)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z" fill="url(https://attacker.invalid/qr.css)"/></svg>',
        r'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z" fill="u\72l(https://attacker.invalid/qr.css)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z" href="https://attacker.invalid/qr.svg"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><path d="M0 0h1v1H0z" xlink:href="https://attacker.invalid/qr.svg"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z" src="https://attacker.invalid/qr.svg"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z" onload="alert(1)"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="1" cy="1" r="1"/></svg>',
        '<svg><path d="M0 0h1v1H0z"/></svg>',
        '<!DOCTYPE svg [<!ENTITY payload "M0 0h1v1H0z">]><svg xmlns="http://www.w3.org/2000/svg"><path d="&payload;"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><rect x="0" y="0" width="1" height="1" data-source="unexpected"/></svg>',
    ],
)
def test_qrcode_rejects_nonstatic_or_external_svg(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any], svg: str
) -> None:
    settings, vault, upstream, _session_factory = adapter_parts
    upstream.add_content(
        "GET",
        "http://vpn-wireguard:51821/api/client/8/qrcode.svg",
        svg,
        content_type="image/svg+xml",
    )
    adapter = WgEasyAdapter(settings, credential_vault=vault, transport=upstream.transport)

    with pytest.raises(WgEasyIntegrationError, match="invalid SVG response"):
        run(adapter.qrcode(8))


def test_qrcode_allows_static_svg_with_allowlisted_namespace_tags_and_attributes(
    adapter_parts: tuple[Settings, WgEasyCredentialVault, MockWgEasy, Any],
) -> None:
    settings, vault, upstream, _session_factory = adapter_parts
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="21mm" height="21mm" viewBox="0 0 21 21">'
        '<g fill="#000"><rect x="0" y="0" width="1" height="1"/><path d="M2 2h1v1H2z"/></g></svg>'
    )
    upstream.add_content(
        "GET",
        "http://vpn-wireguard:51821/api/client/8/qrcode.svg",
        svg,
        content_type="image/svg+xml",
    )
    adapter = WgEasyAdapter(settings, credential_vault=vault, transport=upstream.transport)

    assert run(adapter.qrcode(8)) == svg
