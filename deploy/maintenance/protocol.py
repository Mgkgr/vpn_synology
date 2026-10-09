"""Closed NDJSON v1 protocol; no shell/path/image controls. Profile links are private input."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Dict, Optional, Tuple, Union

from .strategy_catalog import StrategyRequest, parse_strategy_request
from .profile_catalog import ProfileRequest, parse_profile_request

MAX_FRAME = 65536
COMPONENTS = frozenset(("mihomo", "wireguard", "uptime-kuma", "metacubexd", "dashboard", "antidpi"))
RESOURCES = frozenset(("rules", "geodata", "clients", "strategies", "dashboard_config"))
ID = re.compile(r"[0-9a-f]{32}\Z")
REVISION = re.compile(r"[0-9a-f]{64}\Z")
RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class MaintenanceRequest:
    action: str
    components: Tuple[str, ...]
    expected_revision: str
    release_ids: Dict[str, str]
    enable_stopped: Tuple[str, ...]
    snapshot_id: Optional[str]
    accept_data_loss: bool


@dataclass(frozen=True)
class CancelRequest:
    action: str
    job_id: str


def require_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ProtocolError("invalid_id")
    return value


def _keys(value, required):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ProtocolError("invalid_fields")


def _ids(value):
    if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) or item not in COMPONENTS for item in value) or len(set(value)) != len(value):
        raise ProtocolError("invalid_components")
    return tuple(value)


def parse_request(value) -> Union[MaintenanceRequest, CancelRequest]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_request")
    if isinstance(value.get('action'), str) and value['action'].startswith('profile_'):
        try:
            return parse_profile_request(value)
        except ValueError:
            raise ProtocolError('invalid_profile_request') from None
    if isinstance(value.get('action'), str) and value['action'].startswith('strategy_'):
        try:
            return parse_strategy_request(value)
        except ValueError:
            raise ProtocolError('invalid_strategy_request') from None
    if value.get("action") == "cancel":
        _keys(value, ("action", "job_id"))
        return CancelRequest("cancel", require_id(value["job_id"]))
    _keys(value, ("action", "components", "expected_revision", "release_ids", "enable_stopped", "snapshot_id", "accept_data_loss"))
    action = value["action"]
    if action not in ("restart", "update", "rollback"):
        raise ProtocolError("invalid_action")
    components, enable_stopped = _ids(value["components"]), _ids(value["enable_stopped"])
    if not components or not set(enable_stopped) <= set(components):
        raise ProtocolError("invalid_selection")
    revision = value["expected_revision"]
    releases = value["release_ids"]
    if not isinstance(revision, str) or not REVISION.fullmatch(revision) or not isinstance(releases, dict):
        raise ProtocolError("invalid_revision_or_releases")
    if set(releases) != (set(components) if action == "update" else set()):
        raise ProtocolError("invalid_releases")
    if any(not isinstance(item, str) or not RELEASE_ID.fullmatch(item) for item in releases.values()):
        raise ProtocolError("invalid_release_id")
    snapshot = value["snapshot_id"]
    if (action == "rollback" and (not isinstance(snapshot, str) or not REVISION.fullmatch(snapshot))) or (action != "rollback" and snapshot is not None):
        raise ProtocolError("invalid_snapshot")
    loss = value["accept_data_loss"]
    if type(loss) is not bool or (loss and action != "rollback"):
        raise ProtocolError("invalid_data_loss_confirmation")
    return MaintenanceRequest(action, components, revision, dict(releases), enable_stopped, snapshot, loss)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def request_hash(request) -> str:
    return hashlib.sha256(canonical(asdict(request))).hexdigest()


def compute_revision(inventory: dict) -> str:
    """Caller supplies configuration, not raw SQLite. Ignore known volatile observations."""
    volatile = {"telemetry", "probes", "sessions", "traffic", "uptime", "observed_at"}
    value = {key: item for key, item in inventory.items() if key not in volatile}
    if "clients" in value:
        client_volatile = {"latest_handshake_at", "latest_handshake", "received_bytes", "transmitted_bytes", "transfer_rx", "transfer_tx", "last_seen_at", "endpoint"}
        clients = [{key: item for key, item in client.items() if key not in client_volatile} for client in value["clients"]]
        value["clients"] = sorted(clients, key=canonical)
    return hashlib.sha256(canonical(value)).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate_field")
        result[key] = value
    return result


def decode_frame(raw: bytes, peer_uid: int) -> dict:
    if peer_uid not in (0, 10001):
        raise ProtocolError("peer_not_allowed")
    if len(raw) > MAX_FRAME or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ProtocolError("invalid_frame")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("invalid_number")))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ProtocolError("invalid_json") from None
    _keys(value, ("version", "method", "params"))
    if type(value["version"]) is not int or value["version"] != 1:
        raise ProtocolError("unsupported_protocol")
    method, params = value["method"], value["params"]
    methods = {"submit": ("job_id", "actor", "request"), "cancel": ("job_id", "actor"), "job": ("job_id",), "jobs": (), "components": (), "strategy_snapshot": (), "profile_stage": ("uri", "actor"), "profile_snapshot": ("actor",), "check_releases": (), "writer_acquire": ("resource",), "writer_renew": ("lease_id",), "writer_release": ("lease_id", "outcome")}
    if not isinstance(method, str) or method not in methods:
        raise ProtocolError("method_not_allowed")
    _keys(params, methods[method])
    if "job_id" in params:
        require_id(params["job_id"])
    if "lease_id" in params:
        require_id(params["lease_id"])
    if "actor" in params:
        actor = params["actor"]
        if not isinstance(actor, str) or not 1 <= len(actor) <= 64 or not all(char.isprintable() for char in actor):
            raise ProtocolError("invalid_actor")
    if "resource" in params and params["resource"] not in RESOURCES:
        raise ProtocolError("invalid_resource")
    if "outcome" in params and params["outcome"] not in ("complete", "uncertain"):
        raise ProtocolError("invalid_outcome")
    if method == 'profile_stage' and (not isinstance(params['uri'],str) or not 1<=len(params['uri'])<=8192):
        raise ProtocolError('invalid_profile_link')
    if method == "submit" and not isinstance(parse_request(params["request"]), (MaintenanceRequest, StrategyRequest, ProfileRequest)):
        raise ProtocolError("invalid_submit")
    return value
