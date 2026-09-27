"""Bounded, argv-only Docker discovery. Never return full inspect or env."""

import json
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .catalog import ComponentDefinition, DIGEST_RE

DOCKER_PATHS = ("/usr/local/bin/docker", "/var/packages/ContainerManager/target/usr/bin/docker", "/usr/bin/docker")
CONTAINER_FORMAT = '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},"container":{{json .Name}},"running":{{json .State.Running}},"image_id":{{json .Image}},"image":{{json .Config.Image}}}'
IMAGE_FORMAT = '{"digests":{{json .RepoDigests}},"version":{{json (index .Config.Labels "org.opencontainers.image.version")}},"os":{{json .Os}},"arch":{{json .Architecture}}}'


class DockerError(RuntimeError):
    pass


def bounded_run(argv, timeout=60, limit=1048576):
    """A draining reader bounds memory even when Docker produces excessive output."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
    except OSError:
        raise DockerError("docker_unavailable") from None
    chunks = []
    failure = []
    def read():
        size = 0
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                return
            size += len(chunk)
            if size > limit:
                failure.append("docker_output_limit")
                process.kill()
                return
            chunks.append(chunk)
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise DockerError("docker_timeout") from None
    finally:
        thread.join(timeout=5)
        process.stdout.close()
    if failure or process.returncode != 0:
        raise DockerError(failure[0] if failure else "docker_command_failed")
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeError:
        raise DockerError("docker_invalid_output") from None


class DockerAdapter:
    def __init__(self, catalog: Tuple[ComponentDefinition, ...], executable: Optional[Path] = None, runner: Callable = bounded_run, expected_images: Optional[Dict[str, str]] = None):
        self.catalog = {item.id: item for item in catalog}
        if executable is None:
            executable = next((Path(item) for item in DOCKER_PATHS if Path(item).is_file()), None)
        if executable is None or str(executable).replace("\\", "/") not in DOCKER_PATHS:
            raise DockerError("docker_path_unavailable")
        self.executable, self.runner = str(executable), runner
        self.expected_images = expected_images or {}

    def _run(self, *args):
        return self.runner([self.executable, *args], timeout=60, limit=1048576)

    def inspect_component(self, component: ComponentDefinition):
        if self.catalog.get(component.id) != component:
            raise DockerError("component_not_allowed")
        rows = []
        for service in component.services:
            name = component.container_name(service)
            found = self._run("container", "ls", "--all", "--format", "{{.Names}}", "--filter", "name=^/" + name + "$").splitlines()
            if not found:
                continue
            if found != [name]:
                raise DockerError("ambiguous_container")
            row = json.loads(self._run("container", "inspect", "--format", CONTAINER_FORMAT, name))
            if row.get("project") != component.compose_project or row.get("service") != service:
                raise DockerError("container_identity_mismatch")
            image_id = row.get("image_id", "")
            if not isinstance(image_id, str) or not DIGEST_RE.fullmatch(image_id):
                raise DockerError("invalid_image_id")
            image = json.loads(self._run("image", "inspect", "--format", IMAGE_FORMAT, image_id))
            if image.get("os") != "linux" or image.get("arch") != "amd64":
                raise DockerError("platform_mismatch")
            references = image.get("digests") or []
            digests = {ref.split("@", 1)[1] for ref in references if isinstance(ref, str) and "@" in ref and ref.split("@", 1)[0] in component.image_repositories and DIGEST_RE.fullmatch(ref.split("@", 1)[1])}
            row["digest"] = next(iter(digests)) if len(digests) == 1 else None
            if component.source_kind == "local" and not references:
                row["digest"] = image_id  # Local build: content-addressed image ID, no registry manifest.
            row["version"] = image.get("version")
            expected = self.expected_images.get(service, "")
            row["expected_digest"] = expected.rsplit("@", 1)[-1] if "@" in expected else None
            rows.append(row)
        return tuple(rows)
