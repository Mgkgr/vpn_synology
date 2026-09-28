"""Dependency-ordered restart. No NAS/daemon restart, Compose down or volume removal."""

from dataclasses import dataclass

from .protocol import REVISION, request_hash
from .store import JobConflict, TERMINAL

SERVICES = {"wireguard": ("wireguard",), "mihomo": ("mihomo",), "antidpi": ("antidpi", "socks"),
    "uptime-kuma": ("uptime-kuma",), "metacubexd": ("metacubexd",), "dashboard": ("dashboard",)}


class RestartError(RuntimeError):
    pass


@dataclass(frozen=True)
class RestartStep:
    operation: str
    service: str
    component: str


def restart_plan(request, inventory):
    selected = set(request.components)
    if not isinstance(inventory, dict) or not set(inventory) <= {s for values in SERVICES.values() for s in values}:
        raise RestartError("inventory_unavailable")
    for component in selected:
        for service in SERVICES[component]:
            state = inventory.get(service)
            if not isinstance(state, dict) or type(state.get("running")) is not bool:
                raise RestartError("component_not_installed")
            if not state["running"] and component not in request.enable_stopped:
                raise RestartError("stopped_service_needs_choice")
    if "wireguard" in selected and inventory.get("mihomo", {}).get("running") is True:
        selected.add("mihomo")  # The owner's live dependent must be stopped first.
    if "mihomo" in selected:
        owner = inventory.get("wireguard")
        if not owner or (not owner["running"] and "wireguard" not in selected):
            raise RestartError("stopped_dependency_needs_choice")
    steps = []
    def stop(service, component):
        if inventory[service]["running"]:
            steps.append(RestartStep("stop", service, component))
    def start(service, component):
        operation = "recreate" if service in ("mihomo", "socks") and inventory[service].get("namespace_stale") else "start"
        steps.append(RestartStep(operation, service, component))
    def verify(component):
        steps.append(RestartStep("verify", component, component))
    if "mihomo" in selected:
        stop("mihomo", "mihomo")
    if "wireguard" in selected:
        stop("wireguard", "wireguard")
        start("wireguard", "wireguard")
        verify("wireguard")
    if "antidpi" in selected:
        stop("socks", "antidpi")
        stop("antidpi", "antidpi")
        start("antidpi", "antidpi")
        start("socks", "antidpi")
        verify("antidpi")
    if "mihomo" in selected:
        start("mihomo", "mihomo")
        verify("mihomo")
    for component in ("uptime-kuma", "metacubexd", "dashboard"):
        if component in selected:
            stop(component, component)
            start(component, component)
            verify(component)
    return tuple(steps)


class RestartRunner:
    def __init__(self, store, docker, readiness, *, revision, identity_digest):
        self.store, self.docker, self.readiness = store, docker, readiness
        self.revision, self.identity_digest = revision, identity_digest

    def _effect(self, job_id, step, callback):
        if self.store.begin_effect(job_id, step):
            callback()
            self.store.finish_effect(job_id, step)

    def run(self, request, job_id):
        job = self.store.get_job(job_id)
        if request.action != "restart" or job.request_hash != request_hash(request):
            raise JobConflict("job_request_mismatch")
        if job.phase != "queued":
            return job  # Reconnect/reboot never replays side effects.
        if not self.store.claim_job(job_id):
            return self.store.get_job(job_id)
        touched, inventory = set(), {}
        applied = False
        try:
            if self.revision() != request.expected_revision:
                raise RestartError("revision_changed")
            inventory = self.docker.inventory()  # Adapter validates labels/mounts/caps/namespace.
            steps = restart_plan(request, inventory)
            self.readiness.preflight(tuple(sorted({step.component for step in steps})))
            identity = self.identity_digest()
            if not isinstance(identity, str) or not REVISION.fullmatch(identity):
                raise RestartError("identity_unavailable")
            if self.store.get_job(job_id).cancel_requested:
                return self.store.set_phase(job_id, "cancelled")
            if self.revision() != request.expected_revision:
                raise RestartError("revision_changed")
            self.store.set_phase(job_id, "apply")
            self.store.begin_maintenance(job_id, 15)
            applied = True
            for step in steps:
                self.store.set_component(job_id, step.component)
                if step.operation == "verify":
                    self.readiness.wait(step.component)
                else:
                    touched.add(step.service)
                    operation = step.operation
                    if operation == "start" and step.service in ("mihomo", "socks") and self.docker.inventory()[step.service].get("namespace_stale"):
                        operation = "recreate"
                    self._effect(job_id, operation + "." + step.service,
                        lambda step=step, operation=operation: getattr(self.docker, operation)(step.service))
            self.store.set_phase(job_id, "verify")
            if self.identity_digest() != identity:
                raise RestartError("identity_changed")
            return self.store.set_phase(job_id, "completed", revision_after=self.revision())
        except Exception as error:
            current = self.store.get_job(job_id)
            if current.phase in TERMINAL:
                return current
            if not applied:
                if current.cancel_requested:
                    return self.store.set_phase(job_id, "cancelled")
                code = str(error) if isinstance(error, RestartError) else "restart_preflight_failed"
                return self.store.set_phase(job_id, "failed", error_code=code)
            # One best-effort recovery, retaining the lock until root reconciliation.
            # An uncertain Docker response is not proof that nothing changed.
            self.store.set_phase(job_id, "rollback")
            self._recover(job_id, touched, inventory)
            return self.store.needs_reconcile(job_id, "identity_changed" if isinstance(error, RestartError) and str(error) == "identity_changed" else "restart_failed")

    def _recover(self, job_id, touched, inventory):
        for service in ("wireguard", "antidpi", "socks", "mihomo", "uptime-kuma", "metacubexd", "dashboard"):
            if service not in touched:
                continue
            try:
                owner = "wireguard" if service == "mihomo" else "antidpi" if service == "socks" else None
                if owner is not None and not self.docker.inventory().get(owner, {}).get("running"):
                    continue
                operation = "start" if inventory[service]["running"] else "stop"
                self._effect(job_id, "recovery." + operation + "." + service,
                    lambda operation=operation, service=service: getattr(self.docker, operation)(service))
                if operation == "start" and service not in ("antidpi", "socks"):
                    self.readiness.wait(service)
            except Exception:
                continue  # No retries and no raw Docker error in public status.
