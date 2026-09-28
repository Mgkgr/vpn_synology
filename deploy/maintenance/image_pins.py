"""Root-controlled image-only Compose overlays, with explicit source drift gates."""

import hashlib
import os
from pathlib import Path
import tempfile

from .backups import no_symlink, private_directory
from .catalog import CatalogError, IDENTITIES, DIGEST_RE, read_root_json
from .protocol import REVISION, canonical

PROJECTS = {key: Path("/volume1/docker") / key for key in ("vpn-gateway", "vpn-dashboard", "vpn-antidpi")}


class ImagePinError(RuntimeError):
    pass


def _atomic(path, data):
    no_symlink(path)
    fd, name = tempfile.mkstemp(prefix=".vpn-images-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(name, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
        if os.name == "posix":
            descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ImageOverrides:
    def __init__(self, private_path, *, projects=None):
        self.private = Path(private_path)
        self.projects = dict(PROJECTS if projects is None else projects)
        if set(self.projects) != set(PROJECTS):
            raise ImagePinError("untrusted_compose_project")
        private_directory(self.private)
        self.index = self.private / "image-pins.json"

    def _path(self, project):
        if project not in self.projects:
            raise ImagePinError("untrusted_compose_project")
        path = self.projects[project] / "compose.versions.yaml"
        no_symlink(path)
        return path

    def _load(self):
        if not self.index.exists():
            return {"version": 1, "projects": {}}
        try:
            data = read_root_json(self.index)
        except (OSError, CatalogError):
            raise ImagePinError("image_manifest_unavailable") from None
        if not isinstance(data, dict) or set(data) != {"version", "projects"} or data["version"] != 1 or not isinstance(data["projects"], dict) or not set(data["projects"]) <= set(PROJECTS):
            raise ImagePinError("image_manifest_invalid")
        return data

    @staticmethod
    def _validate_pins(project, pins):
        services = {service: (component, repositories) for component, (owner, members, _, repositories) in IDENTITIES.items() if owner == project for service in members}
        if not isinstance(pins, dict) or not pins or not set(pins) <= set(services):
            raise ImagePinError("invalid_image_selection")
        for service, image in pins.items():
            component, repositories = services[service]
            if not isinstance(image, str):
                raise ImagePinError("unpinned_image")
            if component == "dashboard":
                if not DIGEST_RE.fullmatch(image):
                    raise ImagePinError("local_image_id_required")
            else:
                pieces = image.split("@")
                if len(pieces) != 2 or pieces[0] not in repositories or not DIGEST_RE.fullmatch(pieces[1]):
                    raise ImagePinError("unpinned_image")

    def verify_deploy(self, project, source_revision):
        path = self._path(project)
        record = self._load()["projects"].get(project)
        if record is None:
            if path.exists():
                raise ImagePinError("unmanaged_image_overlay")
            return None
        if not isinstance(record, dict) or set(record) != {"source_revision", "pins", "overlay_sha256"}:
            raise ImagePinError("image_manifest_invalid")
        if record["source_revision"] != source_revision:
            raise ImagePinError("source_revision_conflict")
        self._validate_pins(project, record["pins"])
        try:
            overlay = read_root_json(path)
        except (OSError, CatalogError):
            raise ImagePinError("image_overlay_unavailable") from None
        expected = {"services": {service: {"image": image} for service, image in record["pins"].items()}}
        if overlay != expected or hashlib.sha256(canonical(overlay)).hexdigest() != record["overlay_sha256"]:
            raise ImagePinError("image_overlay_modified")
        return path

    def write(self, project, pins, source_revision):
        path = self._path(project)
        if not isinstance(source_revision, str) or not REVISION.fullmatch(source_revision):
            raise ImagePinError("invalid_source_revision")
        self._validate_pins(project, pins)
        index = self._load()
        previous = index["projects"].get(project)
        if previous is not None:
            self.verify_deploy(project, previous["source_revision"])
        elif path.exists():
            raise ImagePinError("unmanaged_image_overlay")
        merged = {**(previous["pins"] if previous else {}), **pins}
        overlay = canonical({"services": {service: {"image": image} for service, image in merged.items()}})
        # Caller persists an effect intent before these two replacements. A crash
        # between them is a drift/reconcile condition, never a silent success.
        _atomic(path, overlay)
        index["projects"][project] = {"source_revision": source_revision, "pins": merged,
            "overlay_sha256": hashlib.sha256(overlay).hexdigest()}
        _atomic(self.index, canonical(index))
        return path
