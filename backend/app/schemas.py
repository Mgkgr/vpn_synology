"""Deliberately small response models for the authenticated dashboard API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapRequest(_StrictModel):
    username: str = Field(default="owner", min_length=1, max_length=255)
    password: str = Field(min_length=12, max_length=1024)


class LoginRequest(_StrictModel):
    username: str = Field(default="owner", min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class DashboardAdminCreateRequest(_StrictModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=12, max_length=1024)


class DashboardAdminResponse(BaseModel):
    username: str
    bootstrap_owner: bool


class SessionRevokeRequest(_StrictModel):
    username: str = Field(min_length=1, max_length=255)


class AuthSessionResponse(BaseModel):
    csrf_token: str = Field(min_length=32, max_length=256)


class ClientCreateRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=255)


class ClientRenameRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=255)


class WgEasyCredentialSetupRequest(_StrictModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("username", "password")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("non-empty text required")
        return normalized


class WgEasyCredentialStatusResponse(BaseModel):
    configured: bool


class HostContainerResponse(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    state: str = Field(min_length=1, max_length=64)
    restart_count: int = Field(ge=0)
    health: str | None = Field(default=None, max_length=64)


class HostHealthResponse(BaseModel):
    observed_at: datetime
    cpu_usage_percent: float | None = Field(default=None, ge=0, le=100)
    load_one: float = Field(ge=0)
    load_five: float = Field(ge=0)
    load_fifteen: float = Field(ge=0)
    memory_total_bytes: int = Field(ge=0)
    memory_available_bytes: int = Field(ge=0)
    swap_total_bytes: int = Field(ge=0)
    swap_free_bytes: int = Field(ge=0)
    volume_total_bytes: int = Field(ge=0)
    volume_available_bytes: int = Field(ge=0)
    network_rx_errors: int = Field(ge=0)
    network_rx_dropped: int = Field(ge=0)
    network_tx_errors: int = Field(ge=0)
    network_tx_dropped: int = Field(ge=0)
    containers: list[HostContainerResponse]


class ClientResponse(BaseModel):
    id: int
    name: str
    enabled: bool
    ipv4_address: str
    latest_handshake_at: str | None
    received_bytes: int = Field(ge=0)
    transmitted_bytes: int = Field(ge=0)


class TrafficResponse(BaseModel):
    up: int = Field(ge=0)
    down: int = Field(ge=0)


class GatewayTrafficPointResponse(BaseModel):
    observed_at: datetime
    up_bps: int = Field(ge=0)
    down_bps: int = Field(ge=0)


class RealtimeTrafficResponse(BaseModel):
    period: Literal["5m", "30m", "6h"]
    sample_interval_seconds: int = Field(60, ge=60, le=60)
    points: list[GatewayTrafficPointResponse]


class OverviewResponse(BaseModel):
    client_count: int = Field(ge=0)
    clients: list[ClientResponse]
    mihomo_version: str | None
    traffic: TrafficResponse | None
    services: list["ServiceStatusResponse"]
    fallback: "FallbackStateResponse"
    exit_health: list["ExitHealthResponse"]


class ServiceStatusResponse(BaseModel):
    name: str
    observed_at: datetime
    succeeded: bool
    latency_ms: int | None = Field(default=None, ge=0)
    status: str | None
    status_code: int | None


class FallbackStateResponse(BaseModel):
    primary: Literal["WG-IMP"] = "WG-IMP"
    reserve: Literal["HY2-USA"] = "HY2-USA"
    selected: Literal["WG-IMP", "HY2-USA"] | None


class ExitHealthResponse(BaseModel):
    """One complete independently measured health-check cycle for an exit."""

    name: Literal["WG-IMP", "HY2-USA"]
    observed_at: datetime | None
    succeeded: bool | None
    succeeded_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    last_success_at: datetime | None
    unavailable_since: datetime | None


class RouteGroupResponse(BaseModel):
    name: str
    kind: str
    choices: list[str]
    selected: str | None


class RoutesResponse(BaseModel):
    groups: list[RouteGroupResponse]
    fallback: FallbackStateResponse
    probes: list["RouteProbeResponse"]
    last_switch: "RouteSwitchResponse | None"


class ProbeTargetResponse(BaseModel):
    key: str
    label: str
    url: str
    enabled: bool
    position: int
    is_custom: bool


class ProbeTargetUpdateItem(_StrictModel):
    key: str = Field(min_length=1, max_length=64)
    enabled: bool


class ProbeTargetUpdateRequest(_StrictModel):
    targets: list[ProbeTargetUpdateItem] = Field(min_length=1, max_length=64)


class ProbeTargetCreateRequest(_StrictModel):
    label: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=255)


class ProbeTargetEditRequest(_StrictModel):
    label: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=255)
    enabled: bool


class ProbeDiagnosisRequest(_StrictModel):
    target_key: str = Field(min_length=1, max_length=64)
    outbound: Literal["WG-IMP", "HY2-USA"]


class ProbeDiagnosticStepResponse(BaseModel):
    succeeded: bool | None
    latency_ms: int | None = Field(default=None, ge=0)
    reason: str | None


class ProbeDiagnosticDnsResponse(ProbeDiagnosticStepResponse):
    hostname: str
    addresses: list[str] = Field(default_factory=list, max_length=8)


class ProbeDiagnosisResponse(BaseModel):
    observed_at: datetime
    outbound: Literal["WG-IMP", "HY2-USA"]
    endpoint: str
    conclusion: Literal["ok", "controller_unavailable", "dns_failure", "dns_no_address", "exit_failure"]
    conclusion_text: str
    controller: ProbeDiagnosticStepResponse
    dns: ProbeDiagnosticDnsResponse
    exit: ProbeDiagnosticStepResponse


class RouteProbeResponse(BaseModel):
    observed_at: datetime
    target: Literal["WG-IMP", "HY2-USA"]
    succeeded: bool
    latency_ms: int | None = Field(default=None, ge=0)
    endpoint: str | None
    outbound: Literal["WG-IMP", "HY2-USA"] | None
    status: str | None
    status_code: int | None
    reason: str | None


class RouteSwitchResponse(BaseModel):
    observed_at: datetime
    route: str
    action: str
    previous_outbound: Literal["WG-IMP", "HY2-USA"] | None
    new_outbound: Literal["WG-IMP", "HY2-USA"] | None


class ControllerRuleResponse(BaseModel):
    type: str
    payload: str
    proxy: str


class RuleProviderResponse(BaseModel):
    name: str
    behavior: str


class RulesResponse(BaseModel):
    rules: list[ControllerRuleResponse]
    providers: list[RuleProviderResponse]
    direct_text: str
    policies: list["ManagedRulePolicyResponse"] = Field(default_factory=list)
    policy_catalog: list["ManagedRuleCategoryResponse"] = Field(default_factory=list)


class DirectRuleApplyRequest(_StrictModel):
    text: str = Field(max_length=262_144)


class DirectRuleApplyResponse(BaseModel):
    revision_number: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)


class ManagedRulePolicyResponse(BaseModel):
    id: int
    kind: Literal["GEOSITE", "GEOIP"]
    category: str
    label: str
    action: Literal["DIRECT", "VPS-FALLBACK", "WG-IMP", "HY2-USA"]
    enabled: bool


class ManagedRuleCategoryResponse(BaseModel):
    kind: Literal["GEOSITE", "GEOIP"]
    category: str
    label: str
    description: str = ""


class ManagedRulePolicyRequest(_StrictModel):
    kind: Literal["GEOSITE", "GEOIP"]
    category: str = Field(min_length=1, max_length=128)
    action: Literal["DIRECT", "VPS-FALLBACK", "WG-IMP", "HY2-USA"]
    enabled: bool = True


class GeoUpdateResponse(BaseModel):
    id: int
    observed_at: datetime
    source: str
    operation: str | None
    succeeded: bool | None
    status_code: int | None
    version: str | None
    verification: str
    checked_files: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)


class GeoAssetResponse(BaseModel):
    filename: str
    kind: str
    size_bytes: int = Field(ge=0)
    modified_at: datetime
    sha256: str = Field(min_length=64, max_length=64)
    last_observed_at: datetime | None


class RuleChangeResponse(BaseModel):
    observed_at: datetime
    actor: str
    action: str
    subject: str
    succeeded: bool | None
    revision_number: int | None


class UpdatesResponse(BaseModel):
    assets: list[GeoAssetResponse] = Field(default_factory=list)
    assets_error: str | None = None
    updates: list[GeoUpdateResponse]
    rule_changes: list[RuleChangeResponse] = Field(default_factory=list)


class JournalEventResponse(BaseModel):
    id: int
    kind: Literal["audit", "route", "probe", "outbound_health"]
    target: str | None = None
    observed_at: datetime
    actor: str | None
    action: str
    outbound: str | None
    endpoint: str | None
    revision_number: int | None
    succeeded: bool | None
    status_code: int | None
    restored: bool | None
    latency_ms: int | None = Field(default=None, ge=0)


class JournalResponse(BaseModel):
    events: list[JournalEventResponse]
    page: int = Field(ge=1)
    page_size: int = Field(ge=10, le=200)
    has_more: bool


class OutboundHealthResponse(BaseModel):
    id: Literal["WG-IMP", "HY2-USA", "ANTIDPI"]
    label: str
    engine: str
    state: Literal["healthy", "degraded", "pending", "down", "unknown"]
    observed_at: datetime | None
    pending_since: datetime | None
    incident_id: int | None
    incident_started_at: datetime | None
    last_success_at: datetime | None
    recovery_streak: int
    successes: int
    total: int = 3
    reasons: list[str]
    selected_fallback: Literal["WG-IMP", "HY2-USA"] | None


class HealthDeliveryMonitorResponse(BaseModel):
    key: str
    state: Literal["pending", "accepted", "error"]
    last_attempt_at: datetime | None
    last_accepted_at: datetime | None
    error_code: str | None


class HealthDeliveryResponse(BaseModel):
    state: Literal["disabled", "pending", "accepted", "error"]
    error_code: str | None = None
    telegram_delivery: Literal["unconfirmed"] = "unconfirmed"
    monitors: list[HealthDeliveryMonitorResponse] = Field(default_factory=list)


class OutboundHealthStatusResponse(BaseModel):
    enabled: bool
    observed_at: datetime | None
    collector_state: Literal["disabled", "healthy", "unknown"]
    outbounds: list[OutboundHealthResponse]
    delivery: HealthDeliveryResponse


class TrafficUsageResponse(BaseModel):
    peer_id: str | None
    peer_name: str
    received_bytes: int = Field(ge=0)
    transmitted_bytes: int = Field(ge=0)


class TrafficUsagePeriodResponse(BaseModel):
    period: Literal["month", "year"]
    usage: list[TrafficUsageResponse]
