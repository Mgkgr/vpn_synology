"""Root-owned component identity, not a user supplied Compose/command catalogue."""

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional, Tuple

ComponentId = Literal["mihomo", "wireguard", "uptime-kuma", "metacubexd", "dashboard", "antidpi"]
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
IDENTITIES = {
    "mihomo": ("vpn-gateway", ("mihomo",), ("MetaCubeX/mihomo",), ("metacubex/mihomo",)),
    "wireguard": ("vpn-gateway", ("wireguard",), ("wg-easy/wg-easy",), ("ghcr.io/wg-easy/wg-easy",)),
    "uptime-kuma": ("vpn-gateway", ("uptime-kuma",), ("louislam/uptime-kuma",), ("louislam/uptime-kuma",)),
    "metacubexd": ("vpn-gateway", ("metacubexd",), ("MetaCubeX/metacubexd",), ("ghcr.io/metacubex/metacubexd",)),
    "dashboard": ("vpn-dashboard", ("dashboard",), (None,), ("vpn-dashboard-dashboard",)),
    "antidpi": ("vpn-antidpi", ("antidpi", "socks"), (None, "bol-van/zapret2", "hufrea/byedpi"), ("vpn-antidpi-engine", "vpn-antidpi-socks")),
}


class CatalogError(ValueError):
    pass


def read_root_json(path: Path, limit: int = 65536) -> Any:
    """No symlink or writable root data; installer also owns the parent tree."""
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise CatalogError("unsafe_catalog_path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise CatalogError("invalid_catalog_file")
        if os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o022):
            raise CatalogError("catalog_not_root_owned")
        raw = os.read(fd, limit + 1)
        if len(raw) > limit:
            raise CatalogError("catalog_too_large")
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise CatalogError("invalid_catalog_json") from None
    finally:
        os.close(fd)


@dataclass(frozen=True)
class ComponentDefinition:
    id: ComponentId
    compose_project: str
    services: Tuple[str, ...]
    repository: Optional[str]
    source_kind: str
    dependencies: Tuple[ComponentId, ...]

    @property
    def image_repositories(self) -> Tuple[str, ...]:
        return IDENTITIES[self.id][3]

    def container_name(self, service: str) -> str:
        if service not in self.services:
            raise CatalogError("unknown_service")
        return "vpn-antidpi-socks" if service == "socks" else "vpn-" + service


@dataclass(frozen=True)
class ArtifactSnapshot:
    service: str
    purpose: str
    actual_version: Optional[str]
    actual_digest: Optional[str]
    expected_digest: Optional[str]
    image_id: Optional[str] = None


@dataclass(frozen=True)
class ComponentSnapshot:
    component: ComponentId
    installed: bool
    running: bool
    actual_version: Optional[str]
    actual_digest: Optional[str]
    expected_digest: Optional[str]
    checked_at: Optional[datetime]
    release: Any
    freshness_error: Optional[str]
    artifacts: Tuple[ArtifactSnapshot, ...]

    @property
    def drift(self) -> Optional[bool]:
        known = [item for item in self.artifacts if item.actual_digest and item.expected_digest]
        if any(item.actual_digest != item.expected_digest for item in known):
            return True
        return False if known and len(known) == len(self.artifacts) else None


def load_catalog(path: Path) -> Tuple[ComponentDefinition, ...]:
    data = read_root_json(path)
    if not isinstance(data, dict) or set(data) != {"version", "components"} or data["version"] != 1 or not isinstance(data["components"], list):
        raise CatalogError("invalid_catalog_schema")
    output = []
    for row in data["components"]:
        keys = {"id", "compose_project", "services", "repository", "source_kind", "dependencies"}
        if not isinstance(row, dict) or set(row) != keys or row["id"] not in IDENTITIES:
            raise CatalogError("invalid_component")
        project, services, repositories, _ = IDENTITIES[row["id"]]
        expected_deps = ["wireguard"] if row["id"] == "mihomo" else []
        source_kind = "local" if row["id"] == "dashboard" else "disabled" if row["repository"] is None else "github"
        if (row["compose_project"] != project or row["services"] != list(services)
                or row["repository"] not in repositories or row["dependencies"] != expected_deps
                or row["source_kind"] != source_kind):
            raise CatalogError("untrusted_component_identity")
        output.append(ComponentDefinition(row["id"], project, services, row["repository"], source_kind, tuple(expected_deps)))
    if len(output) != len(IDENTITIES) or {item.id for item in output} != set(IDENTITIES):
        raise CatalogError("incomplete_or_duplicate_catalog")
    return tuple(output)


def _digest(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and DIGEST_RE.fullmatch(value) else None


def discover_components(docker, catalog: Tuple[ComponentDefinition, ...]) -> Tuple[ComponentSnapshot, ...]:
    output = []
    for component in catalog:
        artifacts = []
        error = None
        running = True
        try:
            rows = docker.inspect_component(component)
        except (OSError, RuntimeError, ValueError):
            rows, error = (), "inventory_unavailable"
        seen = set()
        for row in rows:
            if row.get("project") != component.compose_project:
                error = "project_mismatch"
                break
            service = row.get("service")
            if service not in component.services or service in seen or row.get("container", "").lstrip("/") != component.container_name(service):
                error = "service_mismatch"
                break
            seen.add(service)
            running = running and row.get("running") is True
            version = row.get("version")
            if not isinstance(version, str) or len(version) > 128 or not re.fullmatch(r"[A-Za-z0-9._+/-]+", version):
                version = None
            purpose = "engine" if service == "antidpi" else "socks_auth" if service == "socks" else service
            artifacts.append(ArtifactSnapshot(service, purpose, version, _digest(row.get("digest")), _digest(row.get("expected_digest")), _digest(row.get("image_id"))))
        if not error and rows and seen != set(component.services):
            error = "incomplete_component"
        if not error and artifacts and any(item.actual_digest is None for item in artifacts):
            error = "digest_unavailable"
        first = artifacts[0] if artifacts else None
        output.append(ComponentSnapshot(component.id, bool(rows), bool(rows) and running and error not in {"project_mismatch", "service_mismatch", "incomplete_component"}, first.actual_version if first else None, first.actual_digest if first else None, first.expected_digest if first else None, None, None, error, tuple(artifacts)))
    return tuple(output)
