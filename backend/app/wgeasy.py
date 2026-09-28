"""Guarded adapter for the unstable wg-easy v15.2.2 profile API."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from ipaddress import IPv4Address
from typing import Any
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent, WgEasyCredential
from app.settings import Settings
from app.maintenance_client import guarded_write, mark_mutation


class WgEasyIntegrationError(RuntimeError):
    """A safe adapter error that never carries credentials, URLs, or vendor payloads."""

    def __init__(self, operation: str, reason: str, status_code: int | None = None) -> None:
        self.operation = operation
        self.reason = reason
        self.status_code = status_code
        super().__init__(self.__str__())

    def __str__(self) -> str:
        status = f" (HTTP {self.status_code})" if self.status_code is not None else ""
        return f"wg-easy {self.operation} failed: {self.reason}{status}"


class ProfileMutationDisabled(RuntimeError):
    """Raised before every profile write when the deployed list contract is not trusted."""

    def __init__(self) -> None:
        super().__init__("wg-easy profile mutation disabled until the v15.2.2 client-list contract verifies")


class CredentialVaultError(RuntimeError):
    """Raised when encrypted credentials are absent, corrupt, or unusable."""


@dataclass(frozen=True, slots=True, repr=False)
class WgEasyCredentials:
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class WireGuardClient:
    """Browser-safe normalized client data; no keys, configuration, or raw vendor fields."""

    id: int
    name: str
    enabled: bool
    ipv4_address: str
    latest_handshake_at: str | None
    received_bytes: int
    transmitted_bytes: int


@dataclass(frozen=True, slots=True)
class ContractStatus:
    ready: bool
    reason: str | None
    clients: tuple[WireGuardClient, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileMutationResult:
    client_id: int
    name: str
    operation: str


# This is intentionally the only field-name compatibility map.  It mirrors the
# pinned v15.2.2 response and permits only the documented `clientId` ID alias.
V15_CLIENT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "clientId"),
    "name": ("name",),
    "enabled": ("enabled",),
    "ipv4_address": ("ipv4Address",),
    "latest_handshake_at": ("latestHandshakeAt",),
    "received_bytes": ("transferRx",),
    "transmitted_bytes": ("transferTx",),
}


class WgEasyCredentialVault:
    """Persist one wg-easy Basic-auth credential pair as Fernet ciphertext only."""

    _RECORD_ID = 1

    def __init__(self, session_factory: sessionmaker[Session], encryption_key: str) -> None:
        self._session_factory = session_factory
        try:
            self._fernet = Fernet(encryption_key.encode("ascii"))
        except (TypeError, ValueError) as error:
            raise CredentialVaultError("dashboard encryption key is invalid") from error

    def store(self, username: str, password: str) -> None:
        """Encrypt and upsert setup-supplied credentials without creating audit data."""

        try:
            payload = json.dumps(
                {"username": _required_text(username), "password": _required_text(password)},
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise CredentialVaultError("credentials must contain a username and password") from error
        ciphertext = self._fernet.encrypt(payload).decode("ascii")
        with self._session_factory.begin() as session:
            record = session.get(WgEasyCredential, self._RECORD_ID)
            if record is None:
                session.add(WgEasyCredential(id=self._RECORD_ID, ciphertext=ciphertext))
            else:
                record.ciphertext = ciphertext

    def load(self) -> WgEasyCredentials:
        """Decrypt credentials only in backend memory immediately before a request."""

        with self._session_factory() as session:
            record = session.get(WgEasyCredential, self._RECORD_ID)
        if record is None:
            raise CredentialVaultError("wg-easy credentials are not configured")
        try:
            payload = json.loads(self._fernet.decrypt(record.ciphertext.encode("ascii")).decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("credential payload must be an object")
            return WgEasyCredentials(
                username=_required_text(payload.get("username")),
                password=_required_text(payload.get("password")),
            )
        except (InvalidToken, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise CredentialVaultError("stored wg-easy credentials are unavailable") from error

    def is_configured(self) -> bool:
        """Report local credential availability without contacting wg-easy."""

        try:
            self.load()
        except CredentialVaultError:
            return False
        return True


class WgEasyAdapter:
    """Use only the pinned v15.2.2 endpoints and block writes on any list-contract drift."""

    def __init__(
        self,
        settings: Settings,
        *,
        credential_vault: WgEasyCredentialVault | None = None,
        audit_session_factory: sessionmaker[Session] | None = None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        maintenance_client=None,
    ) -> None:
        self._base_url = _validated_base_url(str(settings.wgeasy_url))
        self._credential_vault = credential_vault
        self._audit_session_factory = audit_session_factory
        self._timeout = timeout
        self._transport = transport
        self._maintenance_client = maintenance_client

    async def verify_contract(self) -> ContractStatus:
        """Fetch and validate the entire client-list shape without exposing vendor JSON."""

        try:
            return ContractStatus(True, None, tuple(await self._clients("verify_contract")))
        except ValueError:
            return ContractStatus(False, "client-list contract mismatch")
        except WgEasyIntegrationError:
            return ContractStatus(False, "client-list contract unavailable")

    def credentials_configured(self) -> bool:
        """Return local credential availability without making an upstream request."""

        return self._credential_vault is not None and self._credential_vault.is_configured()

    @guarded_write("dashboard_config")
    async def configure_credentials(self, username: str, password: str) -> None:
        """Verify a candidate pair against the pinned list contract before replacing stored credentials."""

        if self._credential_vault is None:
            raise WgEasyIntegrationError("configure_credentials", "wg-easy credentials are unavailable")
        try:
            candidate = WgEasyCredentials(username=_required_text(username), password=_required_text(password))
            response = await self._request_with_credentials("configure_credentials", "GET", "api/client", candidate)
            _normalize_client_list(response.json())
        except WgEasyIntegrationError:
            self._audit_credentials_configured(succeeded=False)
            raise
        except (TypeError, ValueError) as error:
            self._audit_credentials_configured(succeeded=False)
            raise WgEasyIntegrationError("configure_credentials", "invalid client-list response") from error
        except CredentialVaultError as error:
            self._audit_credentials_configured(succeeded=False)
            raise WgEasyIntegrationError("configure_credentials", "wg-easy credentials are unavailable") from error

        mark_mutation()
        self._credential_vault.store(candidate.username, candidate.password)
        self._audit_credentials_configured(succeeded=True)

    async def list_clients(self) -> list[WireGuardClient]:
        """Return only normalized, browser-safe client fields."""

        try:
            return await self._clients("list_clients")
        except ValueError as error:
            raise WgEasyIntegrationError("list_clients", "invalid client-list response") from error

    @guarded_write("clients")
    async def create_client(self, name: str) -> WireGuardClient:
        """Create a profile and confirm it from the authoritative client list.

        wg-easy documents this API as unstable.  A successful state change must
        not be presented as a dashboard error merely because a future response
        adds or changes an acknowledgement field.
        """

        before = await self._require_verified_contract()
        try:
            normalized_name = _required_text(name)
        except (TypeError, ValueError) as error:
            raise WgEasyIntegrationError("create_client", "invalid client name") from error
        mark_mutation()
        response = await self._request(
            "create_client",
            "POST",
            "api/client",
            json_body={"name": normalized_name, "expiresAt": None},
        )
        try:
            payload = response.json()
            created_id = _positive_id(payload.get("clientId")) if isinstance(payload, Mapping) and payload.get("success") is True else None
        except (TypeError, ValueError):
            created_id = None

        created = await self._confirm_created_client(before.clients, created_id, normalized_name)
        if created is None:
            raise WgEasyIntegrationError("create_client", "created client was not confirmed by the client list")
        self._audit("profile_create", created.id, created.name)
        return created

    async def _confirm_created_client(
        self,
        before: tuple[WireGuardClient, ...],
        created_id: int | None,
        name: str,
    ) -> WireGuardClient | None:
        """Confirm one POST through fresh list reads without ever issuing it again.

        wg-easy can briefly keep its client-list endpoint busy while persisting a
        new peer.  A retry here is safe because it is GET-only; repeating the
        original POST would risk creating a duplicate profile.
        """

        previous_ids = {client.id for client in before}
        for _attempt in range(2):
            try:
                after = await self.list_clients()
            except WgEasyIntegrationError:
                continue
            created = next((client for client in after if created_id is not None and client.id == created_id), None)
            if created is not None:
                return created
            candidates = [client for client in after if client.id not in previous_ids and client.name == name]
            if len(candidates) == 1:
                return candidates[0]
        return None

    @guarded_write("clients")
    async def disable_client(self, client_id: int) -> ProfileMutationResult:
        """Disable a profile after re-verifying the list contract immediately before the write."""

        client_id, client = await self._guarded_client(client_id)
        mark_mutation()
        await self._request("disable_client", "POST", self._client_path(client_id, "/disable"))
        updated = next((item for item in await self.list_clients() if item.id == client_id), None)
        if updated is None or updated.enabled:
            raise WgEasyIntegrationError("disable_client", "disabled client was not confirmed by the client list")
        self._audit("profile_disable", client_id, client.name)
        return ProfileMutationResult(client_id, client.name, "disable")

    @guarded_write("clients")
    async def enable_client(self, client_id: int) -> ProfileMutationResult:
        """Enable a profile and verify the enabled bit after the upstream write."""

        client_id, client = await self._guarded_client(client_id)
        mark_mutation()
        await self._request("enable_client", "POST", self._client_path(client_id, "/enable"))
        updated = next((item for item in await self.list_clients() if item.id == client_id), None)
        if updated is None or not updated.enabled:
            raise WgEasyIntegrationError("enable_client", "enabled client was not confirmed by the client list")
        self._audit("profile_enable", client_id, client.name)
        return ProfileMutationResult(client_id, client.name, "enable")

    @guarded_write("clients")
    async def rename_client(self, client_id: int, name: str) -> ProfileMutationResult:
        """Rename using the documented v15 full update route and verify it."""

        client_id, client = await self._guarded_client(client_id)
        try:
            normalized_name = _required_text(name)
        except (TypeError, ValueError) as error:
            raise WgEasyIntegrationError("rename_client", "invalid client name") from error
        raw_client = await self._json("rename_client", "GET", self._client_path(client_id))
        payload = _client_update_payload(raw_client, normalized_name)
        mark_mutation()
        await self._request("rename_client", "POST", self._client_path(client_id), json_body=payload)
        updated = next((item for item in await self.list_clients() if item.id == client_id), None)
        if updated is None or updated.name != normalized_name:
            raise WgEasyIntegrationError("rename_client", "renamed client was not confirmed by the client list")
        self._audit("profile_rename", client_id, normalized_name)
        return ProfileMutationResult(client_id, client.name, "rename")

    @guarded_write("clients")
    async def delete_client(self, client_id: int) -> ProfileMutationResult:
        """Delete a profile after re-verifying the list contract immediately before the write."""

        client_id, client = await self._guarded_client(client_id)
        mark_mutation()
        await self._request("delete_client", "DELETE", self._client_path(client_id))
        if any(item.id == client_id for item in await self.list_clients()):
            raise WgEasyIntegrationError("delete_client", "deleted client is still present in the client list")
        self._audit("profile_delete", client_id, client.name)
        return ProfileMutationResult(client_id, client.name, "delete")

    async def config(self, client_id: int) -> str:
        """Return valid configuration text in memory only; never persist or audit it."""

        response = await self._request("config", "GET", self._client_path(_positive_id(client_id), "/configuration"))
        try:
            configuration = response.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WgEasyIntegrationError("config", "invalid configuration response") from error
        if not _is_wireguard_configuration(configuration):
            raise WgEasyIntegrationError("config", "invalid configuration response")
        return configuration

    async def qrcode(self, client_id: int) -> str:
        """Return a safe, in-memory SVG only; it is never persisted or audited."""

        response = await self._request("qrcode", "GET", self._client_path(_positive_id(client_id), "/qrcode.svg"))
        if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "image/svg+xml":
            raise WgEasyIntegrationError("qrcode", "invalid SVG response")
        try:
            svg = response.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WgEasyIntegrationError("qrcode", "invalid SVG response") from error
        if not _is_safe_svg(svg):
            raise WgEasyIntegrationError("qrcode", "invalid SVG response")
        return svg

    async def _guarded_client(self, client_id: int) -> tuple[int, WireGuardClient]:
        status = await self._require_verified_contract()
        normalized_id = _positive_id(client_id)
        client = next((item for item in status.clients if item.id == normalized_id), None)
        if client is None:
            raise WgEasyIntegrationError("profile", "client is not present in the verified client list")
        return normalized_id, client

    async def _require_verified_contract(self) -> ContractStatus:
        status = await self.verify_contract()
        if not status.ready:
            raise ProfileMutationDisabled()
        return status

    async def _clients(self, operation: str) -> list[WireGuardClient]:
        response = await self._request(operation, "GET", "api/client")
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise WgEasyIntegrationError(operation, "invalid JSON response", response.status_code) from error
        return _normalize_client_list(payload)

    async def _json(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        response = await self._request(operation, method, path, json_body=json_body)
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise WgEasyIntegrationError(operation, "invalid JSON response", response.status_code) from error
        if not isinstance(payload, Mapping):
            raise WgEasyIntegrationError(operation, "invalid JSON response", response.status_code)
        return payload

    async def _request(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        if self._credential_vault is None:
            raise WgEasyIntegrationError(operation, "wg-easy credentials are not configured")
        try:
            credentials = self._credential_vault.load()
        except CredentialVaultError as error:
            raise WgEasyIntegrationError(operation, "wg-easy credentials are unavailable") from error
        return await self._request_with_credentials(operation, method, path, credentials, json_body=json_body)

    async def _request_with_credentials(
        self,
        operation: str,
        method: str,
        path: str,
        credentials: WgEasyCredentials,
        *,
        json_body: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=json_body,
                    auth=httpx.BasicAuth(credentials.username, credentials.password),
                )
        except httpx.TimeoutException as error:
            raise WgEasyIntegrationError(operation, "wg-easy request timed out") from error
        except httpx.HTTPError as error:
            raise WgEasyIntegrationError(operation, "wg-easy request failed") from error

        if 200 <= response.status_code < 300:
            return response
        if response.status_code == 401:
            raise WgEasyIntegrationError(operation, "authentication rejected", response.status_code)
        raise WgEasyIntegrationError(operation, "unexpected wg-easy response", response.status_code)

    def _client_path(self, client_id: int, suffix: str = "") -> str:
        return f"api/client/{quote(str(_positive_id(client_id)), safe='')}{suffix}"

    def _audit(self, action: str, client_id: int, name: str) -> None:
        if self._audit_session_factory is None:
            return
        detail = json.dumps({"client_id": client_id, "name": name}, separators=(",", ":"), sort_keys=True)
        with self._audit_session_factory.begin() as session:
            session.add(
                AuditEvent(
                    observed_at=datetime.now(UTC),
                    actor="backend",
                    action=action,
                    detail=detail,
                )
            )

    def _audit_credentials_configured(self, *, succeeded: bool) -> None:
        if self._audit_session_factory is None:
            return
        with self._audit_session_factory.begin() as session:
            session.add(
                AuditEvent(
                    observed_at=datetime.now(UTC),
                    actor="backend",
                    action="wgeasy_credentials_configured" if succeeded else "wgeasy_credentials_configure_failed",
                    succeeded=succeeded,
                )
            )


def _validated_base_url(value: str) -> httpx.URL:
    try:
        parsed = urlsplit(value)
        base_url = httpx.URL(value)
    except (TypeError, ValueError) as error:
        raise WgEasyIntegrationError("configuration", "invalid wg-easy URL") from error
    if (
        base_url.scheme not in {"http", "https"}
        or base_url.host is None
        or bool(base_url.query)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise WgEasyIntegrationError("configuration", "invalid wg-easy URL")
    return base_url.copy_with(path=base_url.path.rstrip("/") + "/")


def _normalize_client_list(payload: object) -> list[WireGuardClient]:
    if not isinstance(payload, list):
        raise ValueError("client list must be an array")
    clients: list[WireGuardClient] = []
    for raw_client in payload:
        if not isinstance(raw_client, Mapping):
            raise ValueError("client must be an object")
        clients.append(_normalize_client(raw_client))
    return clients


def _normalize_client(raw_client: Mapping[str, object]) -> WireGuardClient:
    client_id = _positive_id(_required_alias(raw_client, "id"))
    name = _required_text(_required_alias(raw_client, "name"))
    enabled = _required_alias(raw_client, "enabled")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a bool")
    try:
        ipv4_address = str(IPv4Address(_required_text(_required_alias(raw_client, "ipv4_address"))))
    except ValueError as error:
        raise ValueError("ipv4 address is invalid") from error
    latest_handshake = _required_alias(raw_client, "latest_handshake_at")
    if latest_handshake is not None:
        latest_handshake = _required_text(latest_handshake)
    # wg-easy v15 emits null counters until a client has exchanged traffic.
    # Treat that documented empty state as zero rather than rejecting the
    # complete client-list contract.
    received_bytes = _nullable_nonnegative_int(_required_alias(raw_client, "received_bytes"))
    transmitted_bytes = _nullable_nonnegative_int(_required_alias(raw_client, "transmitted_bytes"))
    return WireGuardClient(
        id=client_id,
        name=name,
        enabled=enabled,
        ipv4_address=ipv4_address,
        latest_handshake_at=latest_handshake,
        received_bytes=received_bytes,
        transmitted_bytes=transmitted_bytes,
    )


_CLIENT_UPDATE_FIELDS = (
    "enabled",
    "expiresAt",
    "ipv4Address",
    "ipv6Address",
    "preUp",
    "postUp",
    "preDown",
    "postDown",
    "allowedIps",
    "serverAllowedIps",
    "mtu",
    "jC",
    "jMin",
    "jMax",
    "i1",
    "i2",
    "i3",
    "i4",
    "i5",
    "persistentKeepalive",
    "serverEndpoint",
    "dns",
)


def _client_update_payload(raw_client: Mapping[str, Any], name: str) -> dict[str, object]:
    """Copy only the documented editable v15 fields; never replay keys or IDs."""

    payload: dict[str, object] = {"name": _required_text(name)}
    for field in _CLIENT_UPDATE_FIELDS:
        if field not in raw_client or not _json_primitive_or_text_list(raw_client[field]):
            raise WgEasyIntegrationError("rename_client", "invalid client detail response")
        payload[field] = raw_client[field]
    if not isinstance(payload["enabled"], bool):
        raise WgEasyIntegrationError("rename_client", "invalid client detail response")
    for field in ("mtu", "jC", "jMin", "jMax", "persistentKeepalive"):
        if isinstance(payload[field], bool) or not isinstance(payload[field], int):
            raise WgEasyIntegrationError("rename_client", "invalid client detail response")
    if not isinstance(payload["serverAllowedIps"], list):
        raise WgEasyIntegrationError("rename_client", "invalid client detail response")
    return payload


def _json_primitive_or_text_list(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _required_alias(payload: Mapping[str, object], field: str) -> object:
    aliases = V15_CLIENT_FIELD_ALIASES[field]
    present = [alias for alias in aliases if alias in payload]
    if len(present) != 1:
        raise ValueError(f"{field} is missing or ambiguous")
    return payload[present[0]]


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("non-empty text required")
    return value.strip()


def _positive_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("positive integer required")
    return value


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("nonnegative integer required")
    return value


def _nullable_nonnegative_int(value: object) -> int:
    return 0 if value is None else _nonnegative_int(value)


def _is_wireguard_configuration(value: str) -> bool:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines or lines[0] != "[Interface]" or "[Peer]" not in lines:
        return False
    try:
        peer_index = lines.index("[Peer]")
    except ValueError:
        return False
    return any(line.startswith("PrivateKey =") and line.partition("=")[2].strip() for line in lines[1:peer_index]) and any(
        line.startswith("PublicKey =") and line.partition("=")[2].strip() for line in lines[peer_index + 1 :]
    )


_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_SVG_ALLOWED_CHILDREN: dict[str, frozenset[str]] = {
    "svg": frozenset({"g", "path", "rect"}),
    "g": frozenset({"g", "path", "rect"}),
    "path": frozenset(),
    "rect": frozenset(),
}
_SVG_ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "svg": frozenset({"width", "height", "viewBox", "fill", "stroke"}),
    "g": frozenset({"fill", "stroke"}),
    "path": frozenset({"d", "fill", "stroke"}),
    "rect": frozenset({"x", "y", "width", "height", "fill", "stroke"}),
}
_SVG_STATIC_COLOR = re.compile(r"(?:black|white|none|#[0-9A-Fa-f]{3,8})\Z")
_SVG_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_SVG_NUMBER_TOKEN = re.compile(_SVG_NUMBER)
_SVG_LENGTH = re.compile(rf"{_SVG_NUMBER}(?:mm)?\Z")
_SVG_PATH_TOKEN = re.compile(rf"[AaCcHhLlMmQqSsTtVvZz]|{_SVG_NUMBER}")
_SVG_PATH_COMMANDS = frozenset("AaCcHhLlMmQqSsTtVvZz")
_SVG_PATH_COMMAND_ARITY = {
    "A": 7,
    "C": 6,
    "H": 1,
    "L": 2,
    "M": 2,
    "Q": 4,
    "S": 4,
    "T": 2,
    "V": 1,
}


def _is_safe_svg(value: str) -> bool:
    """Accept only static, default-namespace SVG primitives used by QR codes."""

    # QR SVGs do not need XML directives or entity references.  Reject before
    # parsing so ElementTree never handles a DTD or entity expansion.
    if "<!" in value or "<?" in value or "&" in value:
        return False
    try:
        # ElementTree removes xmlns attributes from the element tree, so inspect
        # declarations while parsing and permit only the default SVG namespace.
        parser = ElementTree.iterparse(StringIO(value), events=("start-ns",))
        for _event, namespace in parser:
            prefix, namespace_url = namespace
            if prefix != "" or namespace_url != _SVG_NAMESPACE:
                return False
        root = parser.root
    except (ElementTree.ParseError, TypeError, ValueError):
        return False
    if root is None:
        return False

    return _is_safe_svg_element(root, parent_tag=None)


def _is_safe_svg_element(element: ElementTree.Element, *, parent_tag: str | None) -> bool:
    tag = element.tag
    if not isinstance(tag, str) or not tag.startswith(f"{{{_SVG_NAMESPACE}}}"):
        return False
    local_tag = tag.removeprefix(f"{{{_SVG_NAMESPACE}}}")
    if not local_tag or tag != f"{{{_SVG_NAMESPACE}}}{local_tag}":
        return False
    if parent_tag is None:
        if local_tag != "svg":
            return False
    elif local_tag not in _SVG_ALLOWED_CHILDREN.get(parent_tag, frozenset()):
        return False

    allowed_attributes = _SVG_ALLOWED_ATTRIBUTES.get(local_tag)
    if allowed_attributes is None:
        return False
    if element.text and element.text.strip():
        return False
    for attribute, attribute_value in element.attrib.items():
        if not isinstance(attribute, str) or "}" in attribute or attribute not in allowed_attributes:
            return False
        if not _is_safe_svg_attribute(local_tag, attribute, attribute_value):
            return False

    for child in element:
        if not _is_safe_svg_element(child, parent_tag=local_tag):
            return False
        if child.tail and child.tail.strip():
            return False
    return True


def _is_safe_svg_attribute(element_tag: str, attribute: str, value: str) -> bool:
    if attribute in {"fill", "stroke"}:
        return bool(_SVG_STATIC_COLOR.fullmatch(value))
    if attribute == "d":
        return element_tag == "path" and _is_svg_path(value)
    if attribute == "viewBox":
        return element_tag == "svg" and _is_svg_viewbox(value)
    if attribute in {"x", "y"}:
        return element_tag == "rect" and _parse_svg_number(value) is not None
    if attribute in {"width", "height"}:
        if element_tag == "svg":
            return _is_svg_length(value)
        if element_tag == "rect":
            number = _parse_svg_number(value)
            return number is not None and number >= 0
    return False


def _parse_svg_number(value: str) -> float | None:
    if not _SVG_NUMBER_TOKEN.fullmatch(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _is_svg_length(value: str) -> bool:
    if not _SVG_LENGTH.fullmatch(value):
        return False
    number_text = value[:-2] if value.endswith("mm") else value
    number = _parse_svg_number(number_text)
    return number is not None and number >= 0


def _is_svg_viewbox(value: str) -> bool:
    numbers = _parse_svg_number_list(value)
    return numbers is not None and len(numbers) == 4 and numbers[2] >= 0 and numbers[3] >= 0


def _parse_svg_number_list(value: str) -> list[float] | None:
    matches = list(_SVG_NUMBER_TOKEN.finditer(value))
    if not matches:
        return None
    numbers: list[float] = []
    position = 0
    for index, match in enumerate(matches):
        separator = value[position : match.start()]
        separator_pattern = r"\s*" if index == 0 else r"[\s,]+"
        if re.fullmatch(separator_pattern, separator) is None:
            return None
        number = _parse_svg_number(match.group())
        if number is None:
            return None
        numbers.append(number)
        position = match.end()
    if re.fullmatch(r"\s*", value[position:]) is None:
        return None
    return numbers


def _is_svg_path(value: str) -> bool:
    tokens = _tokenize_svg_path(value)
    if not tokens or tokens[0] not in {"M", "m"}:
        return False

    position = 0
    while position < len(tokens):
        command = tokens[position]
        if command not in _SVG_PATH_COMMANDS:
            return False
        position += 1
        if command in {"Z", "z"}:
            continue

        parameter_start = position
        while position < len(tokens) and tokens[position] not in _SVG_PATH_COMMANDS:
            position += 1
        parameters = tokens[parameter_start:position]
        arity = _SVG_PATH_COMMAND_ARITY[command.upper()]
        if not parameters or len(parameters) % arity:
            return False
        if command.upper() == "A" and any(
            parameters[index + flag_offset] not in {"0", "1"}
            for index in range(0, len(parameters), arity)
            for flag_offset in (3, 4)
        ):
            return False
    return True


def _tokenize_svg_path(value: str) -> list[str] | None:
    tokens: list[str] = []
    position = 0
    for match in _SVG_PATH_TOKEN.finditer(value):
        if re.fullmatch(r"[\s,]*", value[position : match.start()]) is None:
            return None
        token = match.group()
        if token not in _SVG_PATH_COMMANDS and _parse_svg_number(token) is None:
            return None
        tokens.append(token)
        position = match.end()
    if re.fullmatch(r"\s*", value[position:]) is None:
        return None
    return tokens
