"""Strict API contracts, independently packaged from the privileged host worker."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

ServiceId = Literal['youtube','discord','telegram','instagram']
StrategyId = Literal['tlsrec-sni','disorder-1','disorder-sni','split-1','split-sni','oob-sni','disoob-sni','fake-md5']
Digest = Annotated[str,Field(pattern=r'^[0-9a-f]{64}$',min_length=64,max_length=64)]
Timestamp = Annotated[float,Field(ge=0,allow_inf_nan=False)]
Mode = Literal['auto','pinned']
Interval = Literal[5,15,30,60]
Blocker = Literal['runtime_unverified','dns_renewal_unverified','namespace_recovery_unverified',
    'service_isolation_unverified','production_input_unverified','worker_unavailable','backup_unavailable']


class Closed(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,hide_input_in_errors=True,allow_inf_nan=False)


class PolicySettings(Closed):
    enabled: bool
    mode: Mode
    interval_minutes: Interval
    daily_enabled: bool


class ApplySettings(Closed):
    strategy_id: StrategyId
    mode: Mode


class EmptySettings(Closed):
    pass


class StrategyOperation(Closed):
    action: Literal['strategy_check','strategy_tune','strategy_configure','strategy_apply','strategy_rollback']
    service_id: ServiceId
    expected_revision: Digest
    settings: dict

    @model_validator(mode='after')
    def closed_settings(self):
        model=PolicySettings if self.action=='strategy_configure' else ApplySettings if self.action=='strategy_apply' else EmptySettings
        model.model_validate(self.settings)
        return self


class Observation(Closed):
    checked_at: Timestamp
    verdict: Literal['success','transport_error','http_error','unknown','certificate_error']
    reason: Literal['verified','timeout','reset','tls_transport','http_denied','http_status','dns_unavailable',
        'runtime_unavailable','control_failed','certificate_invalid','identity_changed','clock_invalid','stale','invalid_response','probe_failed']
    latency_ms: Annotated[float,Field(ge=0,le=10000)] | None
    http_status: Annotated[int,Field(ge=100,le=599)] | None
    infrastructure_ok: bool
    identity: Digest
    context_id: Digest


class CheckResult(Observation):
    strategy_id: StrategyId
    source: Literal['scheduled','confirmation','manual','daily','candidate','verify']


class Change(Closed):
    changed_at: Timestamp
    previous: StrategyId | None
    strategy: StrategyId
    reason: Literal['automatic','manual','rollback']
    actor: Annotated[str,Field(min_length=1,max_length=64)]


class ServiceState(PolicySettings):
    service_id: ServiceId
    name: Annotated[str,Field(max_length=40)]
    host: Literal['www.youtube.com','discord.com','web.telegram.org','www.instagram.com']
    strategy_id: StrategyId | None
    previous_strategy_id: StrategyId | None
    state: Literal['disabled','pinned','no_data','stale','unknown','healthy','suspect','http_error','cooldown','rate_limit','search']
    failure_count: Annotated[int,Field(ge=0,le=3)]
    first_failure_at: Timestamp | None
    last_success_at: Timestamp | None
    last_search_at: Timestamp | None
    last_check: CheckResult | None
    last_automatic: Observation | None
    history: Annotated[list[Change],Field(max_length=5)]
    results: Annotated[list[CheckResult],Field(max_length=24)]


class Catalog(Closed):
    version: Literal['byedpi-https-v1']
    catalog_id: Digest
    strategies: Annotated[list[StrategyId],Field(max_length=8)]
    intervals: Annotated[list[Interval],Field(max_length=4)]
    daily_time: Literal['05:30']
    timezone: Literal['Asia/Yekaterinburg']
    scope: Literal['https_tcp_443_only']
    freshness_seconds: Literal[180]
    failure_threshold: Literal[3]
    failure_spacing_seconds: Literal[30]
    candidate_successes: Literal[3]
    candidate_spacing_seconds: Literal[10]
    cooldown_seconds: Literal[900]
    max_changes_per_hour: Literal[2]


class Capabilities(Closed):
    can_check: bool
    can_apply: bool
    can_configure: bool
    blockers: Annotated[list[Blocker],Field(max_length=7)]


class StrategySnapshot(Closed):
    revision: Digest
    identity: Digest | None
    catalog: Catalog
    services: Annotated[list[ServiceState],Field(max_length=4)]
    observed_at: Timestamp
    capabilities: Capabilities
