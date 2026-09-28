"""Persistent update orchestration over root-verified artifact/schema adapters.

No adapter is synthesized from browser data. An installer must provide the
version-pinned NAS implementation and approvals; without it the service keeps
updates unavailable. The runner never guesses a database migration path.
"""

import re
from dataclasses import asdict, dataclass

from .catalog import DIGEST_RE
from .protocol import REVISION, request_hash
from .store import JobConflict, TERMINAL

COMPONENT_ORDER = ("wireguard", "antidpi", "mihomo", "uptime-kuma", "metacubexd", "dashboard")


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class RollbackPlan:
    component: str
    snapshot_id: str
    verified: bool
    previous_digest: str
    candidate_digest: str
    previous_schemas: dict
    candidate_schemas: dict
    schema_mode: str
    validation_id: str

    def validate(self, snapshot, release=None):
        if (self.verified is not True or self.snapshot_id != snapshot.snapshot_id or self.component not in snapshot.component_ids
                or self.previous_digest != snapshot.image_digests.get(self.component)
                or not DIGEST_RE.fullmatch(self.previous_digest) or not DIGEST_RE.fullmatch(self.candidate_digest)
                or self.previous_schemas != snapshot.database_schemas
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.validation_id)):
            raise UpdateError("rollback_path_unverified")
        for schemas in (self.previous_schemas, self.candidate_schemas):
            if not isinstance(schemas, dict) or any(not isinstance(name, str) or not isinstance(value, str) or not REVISION.fullmatch(value) for name, value in schemas.items()):
                raise UpdateError("schema_migration_unverified")
        if self.schema_mode not in ("unchanged", "restore_snapshot") or (self.schema_mode == "unchanged" and self.previous_schemas != self.candidate_schemas):
            raise UpdateError("schema_migration_unverified")
        if release is not None and self.candidate_digest != release.digest:
            raise UpdateError("rollback_artifact_mismatch")


class UpdateRunner:
    """The driver owns fixed Compose identities, probes and artifact provenance.

    apply/restore MUST call the supplied effect callback before every persistent
    change (including image overlays). No direct Docker control is exposed over
    IPC. prove_rollback checks image, schema and WG identity, not 'running'.
    """
    def __init__(self, store, backups, driver):
        self.store, self.backups, self.driver = store, backups, driver

    def _effect(self, job_id, prefix):
        def apply(step, callback):
            if not isinstance(step, str) or not re.fullmatch(r"[a-z0-9.-]{1,96}", step):
                raise UpdateError("invalid_effect_step")
            name = prefix + "." + step
            if self.store.begin_effect(job_id, name):
                callback()
                self.store.finish_effect(job_id, name)
        return apply

    def _snapshot(self, snapshot, components):
        if (snapshot.verified_at is None or not isinstance(snapshot.snapshot_id, str) or not REVISION.fullmatch(snapshot.snapshot_id)
                or not set(components) <= set(snapshot.component_ids)):
            raise UpdateError("verified_backup_required")

    def _revision(self, expected):
        if self.driver.revision() != expected:
            raise UpdateError("revision_changed")

    def run(self, request, job_id):
        job = self.store.get_job(job_id)
        if request.action not in ("update", "rollback") or job.request_hash != request_hash(request):
            raise JobConflict("job_request_mismatch")
        if job.phase != "queued" or not self.store.claim_job(job_id):
            return self.store.get_job(job_id)
        applied, active, snapshot, plans = False, None, None, {}
        try:
            self._revision(request.expected_revision)
            self.driver.preflight(request)  # Full dependency/identity/probe/config graph.
            if self.driver.resources_ok(request.components) is not True:
                raise UpdateError("insufficient_resources")
            if request.action == "rollback":
                snapshot = self.backups.verify(request.snapshot_id)
                self._snapshot(snapshot, request.components)
                if snapshot.captured_revision != request.expected_revision and not request.accept_data_loss:
                    raise UpdateError("rollback_data_loss_confirmation_required")
                releases = {}
            else:
                releases = {}
                for component in request.components:
                    release = self.driver.resolve(component, request.release_ids[component])
                    if (release.component != component or release.release_id != request.release_ids[component]
                            or release.compatibility != "approved" or not isinstance(release.digest, str) or not DIGEST_RE.fullmatch(release.digest)):
                        raise UpdateError("release_unverified")
                    releases[component] = release
                self.store.set_phase(job_id, "backup")
                snapshot = self.backups.create(request.components, job_id)
                self._snapshot(snapshot, request.components)
                if snapshot.captured_revision != request.expected_revision:
                    raise UpdateError("revision_changed")
            for component in request.components:
                plan = self.driver.rollback_plan(component, releases.get(component), snapshot)
                if not isinstance(plan, RollbackPlan):
                    raise UpdateError("rollback_path_unverified")
                plan.validate(snapshot, releases.get(component))
                plans[component] = plan
                self.store.save_recovery_plan(job_id, component, asdict(plan))
            if request.action == "update":
                self.store.set_phase(job_id, "download")
                for component in request.components:
                    self.store.set_component(job_id, component)
                    release = releases[component]
                    self.driver.download(release)  # Verifies digest/platform/signature or raises.
                    if self.driver.validate_candidate(release, snapshot) is not True:
                        raise UpdateError("candidate_validation_failed")
                    if self.store.get_job(job_id).cancel_requested:
                        return self.store.set_phase(job_id, "cancelled")
            self._revision(request.expected_revision)
            if self.store.get_job(job_id).cancel_requested:
                return self.store.set_phase(job_id, "cancelled")
            self.store.set_phase(job_id, "apply")
            self.store.begin_maintenance(job_id, 30)
            applied = True
            if request.action == "rollback":
                self.store.set_phase(job_id, "rollback")
                for component in (item for item in COMPONENT_ORDER if item in request.components):
                    active = component
                    self.store.set_component(job_id, component)
                    self.driver.restore(plans[component], snapshot, job_id, self._effect(job_id, "rollback." + component))
                    if self.driver.prove_rollback(plans[component], snapshot) is not True:
                        raise UpdateError("rollback_verification_failed")
                self.store.set_phase(job_id, "verify")
            else:
                # Respect explicit queue, but shared namespace owners precede their dependents;
                # the dashboard is always last. No unselected component changes version.
                order = [c for c in COMPONENT_ORDER if c in request.components]
                for number, component in enumerate(order):
                    active = component
                    if number:
                        self.store.set_phase(job_id, "apply")
                    self.store.set_component(job_id, component)
                    release = releases[component]
                    effect = self._effect(job_id, "update." + component)
                    self.driver.apply(release, snapshot, job_id, effect)
                    self.store.set_phase(job_id, "verify")
                    self.driver.verify(component)
                    effect("commit", lambda release=release: self.driver.commit(release))
            return self.store.set_phase(job_id, "completed", revision_after=self.driver.revision())
        except Exception as error:
            current = self.store.get_job(job_id)
            if current.phase in TERMINAL:
                return current
            if not applied:
                if current.cancel_requested:
                    return self.store.set_phase(job_id, "cancelled")
                code = str(error) if isinstance(error, UpdateError) else "update_preflight_failed"
                return self.store.set_phase(job_id, "failed", error_code=code)
            if request.action == "update" and active in plans:
                try:
                    self.store.set_phase(job_id, "rollback")
                    self.driver.restore(plans[active], snapshot, job_id, self._effect(job_id, "rollback." + active))
                    if self.driver.prove_rollback(plans[active], snapshot) is not True:
                        raise UpdateError("rollback_verification_failed")
                    self.store.acknowledge_proven_rollback(job_id, active)
                    return self.store.set_phase(job_id, "failed", error_code="update_reverted", revision_after=self.driver.revision())
                except Exception:
                    pass
            return self.store.needs_reconcile(job_id, "rollback_failed" if request.action == "rollback" or active in plans else "update_failed")
