"""Public profile metadata only; strict schemas reject accidental secret fields."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr

Digest=Annotated[str,Field(pattern=r'^[a-f0-9]{64}$',min_length=64,max_length=64)]
Reference=Annotated[str,Field(pattern=r'^[a-f0-9]{32}$',min_length=32,max_length=32)]
Timestamp=Annotated[float,Field(ge=0,allow_inf_nan=False)]


class Closed(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,hide_input_in_errors=True,allow_inf_nan=False)


class ProfileOperation(Closed):
    action: Literal['profile_check','profile_apply']
    draft_id: Reference
    expected_revision: Digest


class ProfilePreview(Closed):
    uri: Annotated[SecretStr,Field(min_length=1,max_length=8192)]


class ProfileSummary(Closed):
    target: Literal['WG-IMP','HY2-USA']
    protocol: Literal['vless','hysteria2','wireguard']
    server: Annotated[str,Field(min_length=1,max_length=253,pattern=r'^[a-zA-Z0-9.:\-]+$')]
    port: Annotated[int,Field(ge=1,le=65535)]


class ProfileProbe(Closed):
    target: Literal['cloudflare','google','github']
    ok: bool
    latency_ms: Annotated[int,Field(ge=0,le=30000)] | None
    http_status: Annotated[int,Field(ge=100,le=599)] | None
    reason: Literal['timeout','tls_failed','connect_failed','http_status','invalid_response','probe_failed'] | None
    checked_at: Timestamp


class ProfileDraft(ProfileSummary):
    draft_id: Reference
    revision: Digest
    created_at: Timestamp
    expires_at: Timestamp
    checked_at: Timestamp | None
    check_passed: bool
    endpoint_ip: Annotated[str,Field(max_length=45,pattern=r'^[0-9a-fA-F.:]+$')] | None
    results: Annotated[list[ProfileProbe],Field(max_length=9)]


class ProfileSnapshot(Closed):
    revision: Digest
    current: Annotated[list[ProfileSummary],Field(max_length=2)]
    drafts: Annotated[list[ProfileDraft],Field(max_length=8)]


class ProfileProgress(Closed):
    action: Literal['profile_check','profile_apply']
    step: Literal['queued','checking','backup','applying','verifying','verified','rollback']
    results: Annotated[list[ProfileProbe],Field(max_length=9)]
    completed: Annotated[int,Field(ge=0,le=9)]
    limit: Literal[6,9]
