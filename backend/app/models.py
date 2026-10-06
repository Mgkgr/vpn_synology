"""SQLAlchemy records retained by the dashboard."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db import Base


class UtcDateTime(TypeDecorator[datetime]):
    """Persist datetimes as UTC ISO-8601 strings and always restore aware values."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Any) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def process_result_value(self, value: str | None, _dialect: Any) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class SiteProbeSchedule(Base):
    """Retained watermark: pruning results must not enable clock-rollback replay."""
    __tablename__ = "site_probe_schedule"
    __table_args__ = (CheckConstraint("id = 1", name="ck_site_probe_schedule_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_claimed_date: Mapped[str] = mapped_column(String(10), nullable=False)


class SiteProbeRun(Base):
    __tablename__ = "site_probe_runs"
    __table_args__ = (CheckConstraint("state IN ('running','completed','interrupted')", name="ck_site_probe_run_state"),)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)


class SiteProbeResult(Base):
    __tablename__ = "site_probe_results"
    __table_args__ = (
        CheckConstraint("state IN ('responded','http_rejected','failed','unknown')", name="ck_site_probe_result_state"),
        Index("ix_site_probe_result_service_route_day", "service_key", "route_id", "day"),
    )
    day: Mapped[str] = mapped_column(ForeignKey("site_probe_runs.day", ondelete="CASCADE"), primary_key=True)
    service_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    route_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    route_label: Mapped[str] = mapped_column(String(100), nullable=False)
    url: Mapped[str] = mapped_column(String(255), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    delay_ms: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(String(48))


class MaintenanceGrant(Base):
    """A hashed owner step-up, bound to one session and canonical operation."""

    __tablename__ = "maintenance_grants"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("dashboard_sessions.id", ondelete="CASCADE"), index=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    consumed_job_id: Mapped[str | None] = mapped_column(String(32))


class MaintenanceStepUpThrottle(Base):
    __tablename__ = "maintenance_stepup_throttles"
    session_id: Mapped[int] = mapped_column(ForeignKey("dashboard_sessions.id", ondelete="CASCADE"), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(UtcDateTime())


class MaintenanceSubmitIntent(Base):
    """Persist before IPC; an uncertain request is queried, never resubmitted."""

    __tablename__ = "maintenance_submit_intents"
    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class MaintenanceSubmissionReceipt(Base):
    """A definite worker acceptance/refusal, distinct from a lost response."""

    __tablename__ = "maintenance_submission_receipts"
    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class PeerSnapshotRecord(Base):
    __tablename__ = "peer_snapshots"
    __table_args__ = (
        CheckConstraint("received_bytes >= 0", name="ck_peer_snapshots_received_nonnegative"),
        CheckConstraint("transmitted_bytes >= 0", name="ck_peer_snapshots_transmitted_nonnegative"),
        Index("ix_peer_snapshots_peer_key_observed_at", "peer_key", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    peer_key: Mapped[str] = mapped_column(String(255), nullable=False)
    peer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    peer_id: Mapped[str | None] = mapped_column(String(255))
    received_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    transmitted_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    latest_handshake_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class PeerBaseline(Base):
    """Current counter state retained independently from raw snapshot history."""

    __tablename__ = "peer_baselines"
    __table_args__ = (
        CheckConstraint("received_bytes >= 0", name="ck_peer_baselines_received_nonnegative"),
        CheckConstraint("transmitted_bytes >= 0", name="ck_peer_baselines_transmitted_nonnegative"),
    )

    peer_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    peer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    peer_id: Mapped[str | None] = mapped_column(String(255))
    received_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    transmitted_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class TrafficHourly(Base):
    __tablename__ = "traffic_hourly"
    __table_args__ = (
        UniqueConstraint("peer_key", "hour_start", name="uq_traffic_hourly_peer_hour"),
        CheckConstraint("received_bytes >= 0", name="ck_traffic_hourly_received_nonnegative"),
        CheckConstraint("transmitted_bytes >= 0", name="ck_traffic_hourly_transmitted_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    peer_key: Mapped[str] = mapped_column(String(255), nullable=False)
    peer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    peer_id: Mapped[str | None] = mapped_column(String(255))
    hour_start: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    received_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transmitted_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TrafficMonthly(Base):
    __tablename__ = "traffic_monthly"
    __table_args__ = (
        UniqueConstraint("peer_key", "period", name="uq_traffic_monthly_peer_period"),
        CheckConstraint("received_bytes >= 0", name="ck_traffic_monthly_received_nonnegative"),
        CheckConstraint("transmitted_bytes >= 0", name="ck_traffic_monthly_transmitted_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    peer_key: Mapped[str] = mapped_column(String(255), nullable=False)
    peer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    peer_id: Mapped[str | None] = mapped_column(String(255))
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    received_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transmitted_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class GatewayTrafficSample(Base):
    """One lightweight, minute-level throughput observation from Mihomo."""

    __tablename__ = "gateway_traffic_samples"
    __table_args__ = (
        CheckConstraint("up_bps >= 0", name="ck_gateway_traffic_up_nonnegative"),
        CheckConstraint("down_bps >= 0", name="ck_gateway_traffic_down_nonnegative"),
        Index("ix_gateway_traffic_samples_observed_at", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    up_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    down_bps: Mapped[int] = mapped_column(Integer, nullable=False)


class ProbeEvent(Base):
    __tablename__ = "probe_events"
    __table_args__ = (Index("ix_probe_events_target_observed_at", "target", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    endpoint: Mapped[str | None] = mapped_column(String(255))
    outbound: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str | None] = mapped_column(String(64))
    status_code: Mapped[int | None] = mapped_column(Integer)
    error_text: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)


class ProbeTarget(Base):
    """One dashboard-approved target selectable for independent delay checks."""

    __tablename__ = "probe_targets"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class ManagedRulePolicy(Base):
    """A category-to-outbound policy materialized into managed Mihomo providers."""

    __tablename__ = "managed_rule_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class RouteEvent(Base):
    __tablename__ = "route_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    route: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_outbound: Mapped[str | None] = mapped_column(String(255))
    new_outbound: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[str | None] = mapped_column(Text)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_number: Mapped[int | None] = mapped_column(Integer)
    succeeded: Mapped[bool | None] = mapped_column(Boolean)
    status_code: Mapped[int | None] = mapped_column(Integer)
    error_text: Mapped[str | None] = mapped_column(Text)
    restored: Mapped[bool | None] = mapped_column(Boolean)
    detail: Mapped[str | None] = mapped_column(Text)


class DashboardOwner(Base):
    """The single local dashboard administrator, stored only as an Argon2id hash."""

    __tablename__ = "dashboard_owners"
    __table_args__ = (
        CheckConstraint("singleton_marker = 1", name="ck_dashboard_owners_singleton_marker"),
        UniqueConstraint("singleton_marker", name="uq_dashboard_owners_singleton_marker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    singleton_marker: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class DashboardAdmin(Base):
    """Additional local dashboard administrators, separate from bootstrap ownership."""

    __tablename__ = "dashboard_admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class DashboardSession(Base):
    """Opaque browser sessions; raw session and CSRF tokens are never persisted."""

    __tablename__ = "dashboard_sessions"
    __table_args__ = (Index("ix_dashboard_sessions_expires_at", "expires_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("dashboard_owners.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    actor_username: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class LoginThrottleRecord(Base):
    """Persistent login-failure window keyed by the effective client address."""

    __tablename__ = "login_throttle_records"

    ip_address: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(UtcDateTime())


class WgEasyCredential(Base):
    """The sole encrypted-at-rest credential payload used to call wg-easy."""

    __tablename__ = "wgeasy_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)


class GeoUpdate(Base):
    __tablename__ = "geo_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str | None] = mapped_column(String(64))
    succeeded: Mapped[bool | None] = mapped_column(Boolean)
    status_code: Mapped[int | None] = mapped_column(Integer)
    error_text: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(255))
    detail: Mapped[str | None] = mapped_column(Text)


class GeoFileMetadata(Base):
    """A structured, read-only metadata snapshot of one mounted GeoData file."""

    __tablename__ = "geo_file_metadata"
    __table_args__ = (UniqueConstraint("geo_update_id", "phase", "filename", name="uq_geo_metadata_update_phase_file"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    geo_update_id: Mapped[int] = mapped_column(ForeignKey("geo_updates.id", ondelete="CASCADE"), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    modified_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class OutboundIncident(Base):
    """One confirmed outage; start/recovery also serve as durable journal events."""

    __tablename__ = "outbound_incidents"
    __table_args__ = (Index("ix_outbound_incidents_outbound_confirmed", "outbound", "confirmed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outbound: Mapped[str] = mapped_column(String(32), nullable=False)
    first_failed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    recovered_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class OutboundControlCycle(Base):
    __tablename__ = "outbound_control_cycles"
    __table_args__ = (
        UniqueConstraint("outbound", "cycle_id", name="uq_outbound_control_cycle"),
        CheckConstraint("successes >= 0 AND successes <= 3", name="ck_control_successes"),
        Index("ix_outbound_control_completed", "completed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outbound: Mapped[str] = mapped_column(String(32), nullable=False)
    cycle_id: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    successes: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_fallback: Mapped[str | None] = mapped_column(String(32))
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False)
    checks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")


class OutboundHealthState(Base):
    __tablename__ = "outbound_health_states"

    outbound: Mapped[str] = mapped_column(String(32), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    pending_since: Mapped[datetime | None] = mapped_column(UtcDateTime())
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("outbound_incidents.id"))
    incident_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    recovery_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    last_cycle_id: Mapped[str | None] = mapped_column(String(32))
    successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_fallback: Mapped[str | None] = mapped_column(String(32))
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class NotificationDeliveryState(Base):
    """Bounded coalescing state. Acceptance by Kuma is not Telegram delivery."""

    __tablename__ = "notification_delivery_states"

    monitor_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    last_accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    pending_revision: Mapped[str | None] = mapped_column(String(64))
    accepted_revision: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(32))
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
