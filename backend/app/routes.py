"""Authenticated, typed FastAPI routes with dependency-injected integrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import inspect
import json
import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.auth import (
    SESSION_COOKIE,
    AuthService,
    AuthenticatedSession,
    BootstrapUnavailable,
    LoginThrottle,
    get_auth_service,
    require_admin,
)
from app.models import AuditEvent, GeoFileMetadata, GeoUpdate, ProbeEvent, RouteEvent, TrafficMonthly
from app.policy_rules import (
    POLICY_CATEGORIES,
    ManagedRuleConfigurationError,
    ManagedRuleValidationError,
)
from app.rules import DirectRuleValidationError
from app.schemas import (
    AuthSessionResponse,
    BootstrapRequest,
    ClientCreateRequest,
    ClientRenameRequest,
    ClientResponse,
    ControllerRuleResponse,
    DirectRuleApplyRequest,
    DirectRuleApplyResponse,
    DashboardAdminCreateRequest,
    DashboardAdminResponse,
    GeoAssetResponse,
    GeoUpdateResponse,
    FallbackStateResponse,
    JournalEventResponse,
    JournalResponse,
    LoginRequest,
    ManagedRuleCategoryResponse,
    ManagedRulePolicyRequest,
    ManagedRulePolicyResponse,
    OverviewResponse,
    RouteProbeResponse,
    RouteGroupResponse,
    RouteSwitchResponse,
    RoutesResponse,
    ProbeTargetCreateRequest,
    ProbeDiagnosisRequest,
    ProbeDiagnosisResponse,
    ProbeDiagnosticDnsResponse,
    ProbeDiagnosticStepResponse,
    ProbeTargetEditRequest,
    ProbeTargetResponse,
    ProbeTargetUpdateRequest,
    RuleProviderResponse,
    RuleChangeResponse,
    RulesResponse,
    TrafficResponse,
    TrafficUsagePeriodResponse,
    TrafficUsageResponse,
    WgEasyCredentialSetupRequest,
    WgEasyCredentialStatusResponse,
    ServiceStatusResponse,
    UpdatesResponse,
)


@dataclass(slots=True)
class RuntimeContainer:
    """All side-effecting adapters are supplied here, so route tests remain offline."""

    session_factory: sessionmaker[Session]
    wgeasy: Any
    mihomo: Any
    rule_service: Any
    collector: Any
    cookie_secure: bool = True
    csrf_encryption_key: str | None = None
    probe_targets: Any | None = None
    policy_rule_service: Any | None = None
    login_throttle: LoginThrottle | None = None


def build_api_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/auth/bootstrap", response_model=AuthSessionResponse, status_code=status.HTTP_201_CREATED)
    async def bootstrap(payload: BootstrapRequest, response: Response, request: Request) -> AuthSessionResponse:
        auth = get_auth_service(request)
        try:
            issued = auth.bootstrap(payload.username, payload.password)
        except BootstrapUnavailable as error:
            raise HTTPException(status.HTTP_409_CONFLICT, "bootstrap is unavailable") from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid bootstrap request") from error
        _set_session_cookie(response, auth, issued.token, issued.expires_at)
        return AuthSessionResponse(csrf_token=issued.csrf_token)

    @router.post("/auth/login", response_model=AuthSessionResponse)
    async def login(payload: LoginRequest, response: Response, request: Request) -> AuthSessionResponse:
        auth = get_auth_service(request)
        throttle = _login_throttle(request)
        client_ip = _client_ip(request)
        now = datetime.now(UTC)
        if throttle.is_blocked(client_ip, now):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        try:
            issued = auth.login(payload.username, payload.password)
        except ValueError as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from error
        if issued is None:
            throttle.record_failure(client_ip, now)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        throttle.record_success(client_ip)
        _set_session_cookie(response, auth, issued.token, issued.expires_at)
        return AuthSessionResponse(csrf_token=issued.csrf_token)

    @router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        response: Response,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> Response:
        _require_csrf(request, principal)
        auth = get_auth_service(request)
        auth.logout(principal)
        response.delete_cookie(SESSION_COOKIE, path="/", secure=auth.cookie_secure, httponly=True, samesite="strict")
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.get("/auth/csrf", response_model=AuthSessionResponse)
    async def csrf(request: Request, principal: AuthenticatedSession = Depends(require_admin)) -> AuthSessionResponse:
        return AuthSessionResponse(csrf_token=get_auth_service(request).current_csrf(principal))

    @router.get("/auth/admins", response_model=list[DashboardAdminResponse])
    async def list_administrators(
        request: Request,
        _: AuthenticatedSession = Depends(require_admin),
    ) -> list[DashboardAdminResponse]:
        return [DashboardAdminResponse(username=item.username, bootstrap_owner=item.bootstrap_owner) for item in get_auth_service(request).list_administrators()]

    @router.post("/auth/admins", response_model=DashboardAdminResponse, status_code=status.HTTP_201_CREATED)
    async def create_administrator(
        payload: DashboardAdminCreateRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> DashboardAdminResponse:
        _require_csrf(request, principal)
        try:
            created = get_auth_service(request).create_administrator(principal.username, payload.username, payload.password)
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, "administrator cannot be created") from error
        return DashboardAdminResponse(username=created.username, bootstrap_owner=created.bootstrap_owner)

    @router.get("/overview", response_model=OverviewResponse)
    async def overview(request: Request, _: AuthenticatedSession = Depends(require_admin)) -> OverviewResponse:
        runtime = _runtime(request)
        try:
            clients = await runtime.wgeasy.list_clients()
            version = await runtime.mihomo.version()
            traffic = await runtime.mihomo.traffic()
            groups = await runtime.mihomo.groups()
        except Exception as error:
            raise _upstream_failure() from error
        service_states, stored_fallback = _load_observability_state(runtime)
        fallback = FallbackStateResponse(selected=_selected_fallback_from_groups(groups) or stored_fallback.selected)
        return OverviewResponse(
            client_count=len(clients),
            clients=[_client_response(client) for client in clients],
            mihomo_version=_safe_text(getattr(version, "version", None)),
            traffic=TrafficResponse(up=_nonnegative(getattr(traffic, "up", 0)), down=_nonnegative(getattr(traffic, "down", 0))),
            services=service_states,
            fallback=fallback,
        )

    @router.get("/routes", response_model=RoutesResponse)
    async def routes(request: Request, _: AuthenticatedSession = Depends(require_admin)) -> RoutesResponse:
        runtime = _runtime(request)
        try:
            groups = await runtime.mihomo.groups()
        except Exception as error:
            raise _upstream_failure() from error
        service_states, stored_fallback = _load_observability_state(runtime)
        del service_states
        # Controller state is live; the stored sample is only a safe fallback
        # while the group is unavailable or does not expose one of our exits.
        selected = _selected_fallback_from_groups(groups) or stored_fallback.selected
        fallback = FallbackStateResponse(selected=selected)
        probes, last_switch = _load_route_activity(runtime)
        return RoutesResponse(
            groups=[
                RouteGroupResponse(
                    name=_safe_required(getattr(group, "name", None)),
                    kind=_safe_required(getattr(group, "kind", None)),
                    choices=[_safe_required(choice) for choice in getattr(group, "proxies", ())],
                    selected=_safe_text(getattr(group, "now", None)),
                )
                for group in groups
            ],
            fallback=fallback,
            probes=probes,
            last_switch=last_switch,
        )

    @router.get("/clients", response_model=list[ClientResponse])
    async def list_clients(request: Request, _: AuthenticatedSession = Depends(require_admin)) -> list[ClientResponse]:
        try:
            clients = await _runtime(request).wgeasy.list_clients()
        except Exception as error:
            raise _upstream_failure() from error
        return [_client_response(client) for client in clients]

    @router.get("/wgeasy/credentials/status", response_model=WgEasyCredentialStatusResponse)
    async def wgeasy_credential_status(
        request: Request,
        _: AuthenticatedSession = Depends(require_admin),
    ) -> WgEasyCredentialStatusResponse:
        return WgEasyCredentialStatusResponse(configured=_runtime(request).wgeasy.credentials_configured())

    @router.post("/wgeasy/credentials", response_model=WgEasyCredentialStatusResponse)
    async def configure_wgeasy_credentials(
        payload: WgEasyCredentialSetupRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> WgEasyCredentialStatusResponse:
        _require_csrf(request, principal)
        try:
            await _runtime(request).wgeasy.configure_credentials(payload.username, payload.password)
        except Exception as error:
            raise _upstream_failure() from error
        return WgEasyCredentialStatusResponse(configured=True)

    @router.post("/clients", response_model=ClientResponse, status_code=status.HTTP_201_CREATED)
    async def create_client(
        payload: ClientCreateRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> ClientResponse:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            client = await runtime.wgeasy.create_client(payload.name)
        except Exception as error:
            _write_audit(runtime, principal.username, "client_create", succeeded=False, error_text=_safe_operation_reason(error))
            raise _upstream_failure() from error
        _write_audit(runtime, principal.username, "client_create", succeeded=True, detail=_client_detail(client))
        return _client_response(client)

    @router.post("/clients/{client_id}/disable", status_code=status.HTTP_204_NO_CONTENT)
    async def disable_client(
        client_id: int,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> Response:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            result = await runtime.wgeasy.disable_client(client_id)
        except Exception as error:
            _write_audit(runtime, principal.username, "client_disable", succeeded=False, error_text=_safe_operation_reason(error))
            raise _upstream_failure() from error
        _write_audit(runtime, principal.username, "client_disable", succeeded=True, detail=_mutation_detail(result))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/clients/{client_id}/enable", status_code=status.HTTP_204_NO_CONTENT)
    async def enable_client(
        client_id: int,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> Response:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            result = await runtime.wgeasy.enable_client(client_id)
        except Exception as error:
            _write_audit(runtime, principal.username, "client_enable", succeeded=False, error_text=_safe_operation_reason(error))
            raise _upstream_failure() from error
        _write_audit(runtime, principal.username, "client_enable", succeeded=True, detail=_mutation_detail(result))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/clients/{client_id}/rename", response_model=ClientResponse)
    async def rename_client(
        client_id: int,
        payload: ClientRenameRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> ClientResponse:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            result = await runtime.wgeasy.rename_client(client_id, payload.name)
            client = next(item for item in await runtime.wgeasy.list_clients() if int(getattr(item, "id")) == client_id)
        except Exception as error:
            _write_audit(runtime, principal.username, "client_rename", succeeded=False, error_text=_safe_operation_reason(error))
            raise _upstream_failure() from error
        _write_audit(runtime, principal.username, "client_rename", succeeded=True, detail=_mutation_detail(result, renamed_to=payload.name))
        return _client_response(client)

    @router.delete("/clients/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_client(
        client_id: int,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> Response:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            result = await runtime.wgeasy.delete_client(client_id)
        except Exception as error:
            _write_audit(runtime, principal.username, "client_delete", succeeded=False, error_text=_safe_operation_reason(error))
            raise _upstream_failure() from error
        _write_audit(runtime, principal.username, "client_delete", succeeded=True, detail=_mutation_detail(result))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/probes/targets", response_model=list[ProbeTargetResponse])
    async def probe_targets(request: Request, _: AuthenticatedSession = Depends(require_admin)) -> list[ProbeTargetResponse]:
        service = _probe_target_service(request)
        return [_probe_target_response(item) for item in service.list_targets()]

    @router.put("/probes/targets", response_model=list[ProbeTargetResponse])
    async def update_probe_targets(
        payload: ProbeTargetUpdateRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> list[ProbeTargetResponse]:
        _require_csrf(request, principal)
        requested = {item.key: item.enabled for item in payload.targets}
        if len(requested) != len(payload.targets):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid probe target selection")
        try:
            targets = _probe_target_service(request).set_enabled(requested)
        except ValueError as error:
            _write_audit(_runtime(request), principal.username, "probe_targets_update", succeeded=False)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid probe target selection") from error
        _write_audit(_runtime(request), principal.username, "probe_targets_update", succeeded=True)
        return [_probe_target_response(item) for item in targets]

    @router.post("/probes/targets", response_model=ProbeTargetResponse, status_code=status.HTTP_201_CREATED)
    async def create_probe_target(
        payload: ProbeTargetCreateRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> ProbeTargetResponse:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            target = _probe_target_service(request).create_custom_target(label=payload.label, url=payload.url)
        except ValueError as error:
            _write_audit(runtime, principal.username, "probe_target_create", succeeded=False)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "custom probe target is invalid") from error
        _write_audit(runtime, principal.username, "probe_target_create", succeeded=True, detail=_probe_target_detail(target))
        return _probe_target_response(target)

    @router.put("/probes/targets/{target_key}", response_model=ProbeTargetResponse)
    async def update_probe_target(
        target_key: str,
        payload: ProbeTargetEditRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> ProbeTargetResponse:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            target = _probe_target_service(request).update_custom_target(
                target_key, label=payload.label, url=payload.url, enabled=payload.enabled
            )
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "custom probe target was not found") from error
        except ValueError as error:
            _write_audit(runtime, principal.username, "probe_target_update", succeeded=False)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "custom probe target is invalid") from error
        _write_audit(runtime, principal.username, "probe_target_update", succeeded=True, detail=_probe_target_detail(target))
        return _probe_target_response(target)

    @router.delete("/probes/targets/{target_key}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_probe_target(
        target_key: str,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> Response:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            _probe_target_service(request).delete_custom_target(target_key)
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "custom probe target was not found") from error
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "custom probe target is invalid") from error
        _write_audit(runtime, principal.username, "probe_target_delete", succeeded=True, detail=_probe_target_key_detail(target_key))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/probes/run", status_code=status.HTTP_202_ACCEPTED)
    async def run_probes(request: Request, principal: AuthenticatedSession = Depends(require_admin)) -> Response:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        start = getattr(runtime.collector, "start_probe_cycle", None)
        started = start() if callable(start) else False
        if not started:
            raise HTTPException(status.HTTP_409_CONFLICT, "A probe run is already in progress")
        _write_audit(runtime, principal.username, "probe_run", succeeded=True)
        return Response(status_code=status.HTTP_202_ACCEPTED)

    @router.post("/probes/diagnose", response_model=ProbeDiagnosisResponse)
    async def diagnose_probe(
        payload: ProbeDiagnosisRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> ProbeDiagnosisResponse:
        """Run a safe, staged diagnosis for one enabled dashboard target."""

        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            target = _probe_target_service(request).enabled_target(payload.target_key)
        except (KeyError, ValueError) as error:
            _write_audit(runtime, principal.username, "probe_diagnose", succeeded=False)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "probe target is unavailable") from error
        try:
            diagnostic = await runtime.collector.diagnose_probe(payload.outbound, target.url)
        except Exception as error:
            _write_audit(runtime, principal.username, "probe_diagnose", succeeded=False)
            raise _upstream_failure() from error
        _write_audit(
            runtime,
            principal.username,
            "probe_diagnose",
            succeeded=True,
            detail=json.dumps(
                {"target": target.key, "outbound": payload.outbound, "conclusion": _safe_required(getattr(diagnostic, "conclusion", None))},
                separators=(",", ":"),
            ),
        )
        return ProbeDiagnosisResponse(
            observed_at=getattr(diagnostic, "observed_at"),
            outbound=_safe_required(getattr(diagnostic, "outbound", None)),
            endpoint=_safe_required(getattr(diagnostic, "endpoint", None)),
            conclusion=_safe_required(getattr(diagnostic, "conclusion", None)),
            conclusion_text=_safe_required(getattr(diagnostic, "conclusion_text", None)),
            controller=_diagnostic_step_response(getattr(diagnostic, "controller", None)),
            dns=_diagnostic_dns_response(getattr(diagnostic, "dns", None)),
            exit=_diagnostic_step_response(getattr(diagnostic, "exit", None)),
        )

    @router.get("/clients/{client_id}/config")
    async def download_client_config(
        client_id: int,
        request: Request,
        _: AuthenticatedSession = Depends(require_admin),
    ) -> Response:
        runtime = _runtime(request)
        try:
            clients = await runtime.wgeasy.list_clients()
        except Exception as error:
            raise _upstream_failure() from error
        profile = next((item for item in clients if str(getattr(item, "id", "")) == str(client_id)), None)
        if profile is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "client profile was not found")
        try:
            configuration = await runtime.wgeasy.config(client_id)
        except Exception as error:
            raise _upstream_failure() from error
        filename = _configuration_filename(_safe_text(getattr(profile, "name", None)) or "wireguard")
        return Response(
            content=configuration,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": _configuration_disposition(filename)},
        )

    @router.get("/clients/{client_id}/qr")
    async def client_qr(
        client_id: int,
        request: Request,
        _: AuthenticatedSession = Depends(require_admin),
    ) -> Response:
        try:
            svg = await _runtime(request).wgeasy.qrcode(client_id)
        except Exception as error:
            raise _upstream_failure() from error
        return Response(content=svg, media_type="image/svg+xml")

    @router.get("/rules", response_model=RulesResponse)
    async def rules(request: Request, _: AuthenticatedSession = Depends(require_admin)) -> RulesResponse:
        runtime = _runtime(request)
        try:
            controller_rules = await runtime.mihomo.rules()
            providers = await runtime.mihomo.rule_providers()
            direct_text = await _await_if_needed(runtime.rule_service.read_direct_rules())
        except Exception as error:
            raise _upstream_failure() from error
        if not isinstance(direct_text, str):
            raise _upstream_failure()
        policy_service = runtime.policy_rule_service
        policies = [] if policy_service is None else policy_service.list_policies()
        return RulesResponse(
            rules=[
                ControllerRuleResponse(
                    type=_safe_required(getattr(rule, "type", None)),
                    payload=_safe_required(getattr(rule, "payload", None)),
                    proxy=_safe_required(getattr(rule, "proxy", None)),
                )
                for rule in controller_rules
            ],
            providers=[
                RuleProviderResponse(
                    name=_safe_required(getattr(provider, "name", None)),
                    behavior=_safe_required(getattr(provider, "behavior", None)),
                )
                for provider in providers
            ],
            direct_text=direct_text,
            policies=[_policy_response(item) for item in policies],
            policy_catalog=[
                ManagedRuleCategoryResponse(
                    kind=item.kind, category=item.category, label=item.label, description=item.description
                )
                for item in POLICY_CATEGORIES
            ],
        )

    @router.post("/rules/apply", response_model=DirectRuleApplyResponse)
    async def apply_direct_rules(
        payload: DirectRuleApplyRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> DirectRuleApplyResponse:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            revision = await runtime.rule_service.apply_direct_rules_async(payload.text, principal.username)
        except DirectRuleValidationError as error:
            _write_audit(runtime, principal.username, "direct_rules_validation_failed", succeeded=False)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid direct rule") from error
        except Exception as error:
            raise _upstream_failure() from error
        return DirectRuleApplyResponse(revision_number=revision.number, sha256=revision.sha256)

    @router.post("/rules/policies", response_model=ManagedRulePolicyResponse, status_code=status.HTTP_201_CREATED)
    async def create_policy_rule(
        payload: ManagedRulePolicyRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> ManagedRulePolicyResponse:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            policy = await _policy_rule_service(request).create(**payload.model_dump())
        except ManagedRuleValidationError as error:
            _write_audit(runtime, principal.username, "policy_rule_create", succeeded=False)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid managed rule") from error
        except ManagedRuleConfigurationError as error:
            _write_audit(runtime, principal.username, "policy_rule_create", succeeded=False)
            raise HTTPException(status.HTTP_409_CONFLICT, "managed rule providers are not configured") from error
        except Exception as error:
            _write_audit(runtime, principal.username, "policy_rule_create", succeeded=False, error_text=_safe_operation_reason(error))
            raise _upstream_failure() from error
        _write_audit(runtime, principal.username, "policy_rule_create", succeeded=True, detail=_policy_detail(policy))
        return _policy_response(policy)

    @router.put("/rules/policies/{policy_id}", response_model=ManagedRulePolicyResponse)
    async def update_policy_rule(
        policy_id: int,
        payload: ManagedRulePolicyRequest,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> ManagedRulePolicyResponse:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            policy = await _policy_rule_service(request).update(policy_id, **payload.model_dump())
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "managed rule was not found") from error
        except ManagedRuleValidationError as error:
            _write_audit(runtime, principal.username, "policy_rule_update", succeeded=False)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid managed rule") from error
        except ManagedRuleConfigurationError as error:
            _write_audit(runtime, principal.username, "policy_rule_update", succeeded=False)
            raise HTTPException(status.HTTP_409_CONFLICT, "managed rule providers are not configured") from error
        except Exception as error:
            _write_audit(runtime, principal.username, "policy_rule_update", succeeded=False, error_text=_safe_operation_reason(error))
            raise _upstream_failure() from error
        _write_audit(runtime, principal.username, "policy_rule_update", succeeded=True, detail=_policy_detail(policy))
        return _policy_response(policy)

    @router.delete("/rules/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_policy_rule(
        policy_id: int,
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> Response:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            await _policy_rule_service(request).delete(policy_id)
        except KeyError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "managed rule was not found") from error
        except ManagedRuleConfigurationError as error:
            _write_audit(runtime, principal.username, "policy_rule_delete", succeeded=False)
            raise HTTPException(status.HTTP_409_CONFLICT, "managed rule providers are not configured") from error
        except Exception as error:
            _write_audit(runtime, principal.username, "policy_rule_delete", succeeded=False, error_text=_safe_operation_reason(error))
            raise _upstream_failure() from error
        _write_audit(runtime, principal.username, "policy_rule_delete", succeeded=True, detail=json.dumps({"policy_id": policy_id}, separators=(",", ":")))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/updates", response_model=UpdatesResponse)
    async def updates(request: Request, _: AuthenticatedSession = Depends(require_admin)) -> UpdatesResponse:
        runtime = _runtime(request)
        metadata_reader = getattr(runtime.collector, "current_geo_metadata", None)
        if callable(metadata_reader):
            current_assets, assets_error = metadata_reader()
        else:
            current_assets, assets_error = (), "GeoData metadata is unavailable"
        with runtime.session_factory() as session:
            rows = session.scalars(select(GeoUpdate).order_by(GeoUpdate.id.desc()).limit(100)).all()
            metadata_rows = session.execute(
                select(GeoFileMetadata.filename, GeoUpdate.observed_at)
                .join(GeoUpdate, GeoUpdate.id == GeoFileMetadata.geo_update_id)
                .order_by(GeoFileMetadata.id.desc())
            ).all()
            audit_rows = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.in_(("direct_rules_apply", "direct_rules_apply_failed", "policy_rule_create", "policy_rule_update", "policy_rule_delete")))
                .order_by(AuditEvent.id.desc())
                .limit(100)
            ).all()
        last_observed = {filename: observed_at for filename, observed_at in reversed(metadata_rows)}
        return UpdatesResponse(
            assets=[
                GeoAssetResponse(
                    filename=item.filename,
                    kind=_geo_asset_kind(item.filename),
                    size_bytes=item.size_bytes,
                    modified_at=item.modified_at,
                    sha256=item.sha256,
                    last_observed_at=last_observed.get(item.filename),
                )
                for item in current_assets
            ],
            assets_error=assets_error,
            updates=[
                GeoUpdateResponse(
                    id=row.id,
                    observed_at=row.observed_at,
                    operation=row.operation,
                    succeeded=row.succeeded,
                    status_code=row.status_code,
                    version=row.version,
                )
                for row in rows
            ],
            rule_changes=[
                RuleChangeResponse(
                    observed_at=row.observed_at,
                    actor=row.actor,
                    action=row.action,
                    subject=_rule_change_subject(row),
                    succeeded=row.succeeded,
                    revision_number=row.revision_number,
                )
                for row in audit_rows
            ],
        )

    @router.post("/updates/geo", status_code=status.HTTP_202_ACCEPTED)
    async def update_geo(
        request: Request,
        principal: AuthenticatedSession = Depends(require_admin),
    ) -> Response:
        _require_csrf(request, principal)
        runtime = _runtime(request)
        try:
            await runtime.collector.manual_geo_upgrade(principal.username)
        except Exception as error:
            _write_audit(runtime, principal.username, "geo_upgrade", succeeded=False)
            raise _upstream_failure() from error
        _write_audit(runtime, principal.username, "geo_upgrade", succeeded=True)
        return Response(status_code=status.HTTP_202_ACCEPTED)

    @router.get("/traffic", response_model=TrafficUsagePeriodResponse)
    async def traffic_usage(
        request: Request,
        period: str = Query(pattern="^(month|year)$"),
        _: AuthenticatedSession = Depends(require_admin),
    ) -> TrafficUsagePeriodResponse:
        runtime = _runtime(request)
        now = datetime.now(UTC)
        with runtime.session_factory() as session:
            records = session.scalars(select(TrafficMonthly).order_by(TrafficMonthly.peer_name, TrafficMonthly.peer_key)).all()
        if period == "month":
            allowed_periods = {now.strftime("%Y-%m")}
        else:
            allowed_periods = {f"{now.year:04d}-{month:02d}" for month in range(1, 13)}
        totals: dict[str, TrafficUsageResponse] = {}
        for record in records:
            if record.period not in allowed_periods:
                continue
            current = totals.get(record.peer_key)
            if current is None:
                totals[record.peer_key] = TrafficUsageResponse(
                    peer_id=record.peer_id,
                    peer_name=record.peer_name,
                    received_bytes=record.received_bytes,
                    transmitted_bytes=record.transmitted_bytes,
                )
            else:
                current.received_bytes += record.received_bytes
                current.transmitted_bytes += record.transmitted_bytes
        return TrafficUsagePeriodResponse(
            period=period,
            usage=sorted(totals.values(), key=lambda item: (item.peer_name, item.peer_id or "")),
        )

    @router.get("/journal", response_model=JournalResponse)
    async def journal(
        request: Request,
        from_value: datetime | None = Query(default=None, alias="from"),
        to_value: datetime | None = Query(default=None, alias="to"),
        action: str | None = Query(default=None, max_length=64),
        outbound: str | None = Query(default=None, max_length=255),
        endpoint: str | None = Query(default=None, max_length=255),
        _: AuthenticatedSession = Depends(require_admin),
    ) -> JournalResponse:
        runtime = _runtime(request)
        filters = _journal_filters(from_value, to_value, action, outbound, endpoint)
        with runtime.session_factory() as session:
            audit_rows = session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200)).all()
            route_rows = session.scalars(select(RouteEvent).order_by(RouteEvent.id.desc()).limit(200)).all()
            probe_rows = session.scalars(select(ProbeEvent).order_by(ProbeEvent.id.desc()).limit(200)).all()
        events = _journal_events(audit_rows, route_rows, probe_rows, filters)
        return JournalResponse(events=events[:200])

    return router


def _runtime(request: Request) -> RuntimeContainer:
    runtime = getattr(request.app.state, "runtime", None)
    if not isinstance(runtime, RuntimeContainer):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "dashboard is unavailable")
    return runtime


def _probe_target_service(request: Request) -> Any:
    service = _runtime(request).probe_targets
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "probe target service is unavailable")
    return service


def _policy_rule_service(request: Request) -> Any:
    service = _runtime(request).policy_rule_service
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "managed rule service is unavailable")
    return service


def _set_session_cookie(response: Response, auth: AuthService, token: str, expires_at: datetime) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max(1, int((expires_at - datetime.now(UTC)).total_seconds())),
        path="/",
        secure=auth.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _require_csrf(request: Request, principal: AuthenticatedSession) -> None:
    if not get_auth_service(request).verify_csrf(principal, request.headers.get("X-CSRF-Token")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "csrf verification failed")


def _login_throttle(request: Request) -> LoginThrottle:
    throttle = getattr(request.app.state, "login_throttle", None)
    if not isinstance(throttle, LoginThrottle):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication temporarily unavailable")
    return throttle


def _client_ip(request: Request) -> str:
    client = request.client
    return client.host if client is not None and client.host else "unknown"


def _client_response(client: Any) -> ClientResponse:
    return ClientResponse(
        id=int(getattr(client, "id")),
        name=_safe_required(getattr(client, "name", None)),
        enabled=bool(getattr(client, "enabled")),
        ipv4_address=_safe_required(getattr(client, "ipv4_address", None)),
        latest_handshake_at=_safe_text(getattr(client, "latest_handshake_at", None)),
        received_bytes=_nonnegative(getattr(client, "received_bytes", 0)),
        transmitted_bytes=_nonnegative(getattr(client, "transmitted_bytes", 0)),
    )


def _geo_asset_kind(filename: str) -> str:
    normalized = filename.casefold()
    if normalized.startswith("geosite"):
        return "GeoSite"
    if normalized.startswith("geoip"):
        return "GeoIP"
    if "asn" in normalized:
        return "ASN"
    if normalized.endswith(".mmdb"):
        return "MMDB"
    return "GeoData"


def _rule_change_subject(row: AuditEvent) -> str:
    if row.revision_number is not None:
        return f"revision {row.revision_number}"
    if row.action.startswith("policy_rule_"):
        try:
            detail = json.loads(row.detail or "{}")
        except (TypeError, json.JSONDecodeError):
            detail = {}
        kind = detail.get("kind") if isinstance(detail, dict) else None
        category = detail.get("category") if isinstance(detail, dict) else None
        if isinstance(kind, str) and isinstance(category, str) and kind in {"GEOSITE", "GEOIP"} and _safe_text(category):
            return f"{kind}: {_safe_text(category)}"
    return "rule change"


def _write_audit(
    runtime: RuntimeContainer,
    actor: str,
    action: str,
    *,
    succeeded: bool,
    revision_number: int | None = None,
    detail: str | None = None,
    error_text: str | None = None,
) -> None:
    with runtime.session_factory.begin() as session:
        session.add(
            AuditEvent(
                observed_at=datetime.now(UTC),
                actor=actor,
                action=action,
                revision_number=revision_number,
                succeeded=succeeded,
                detail=detail,
                error_text=error_text,
            )
        )


def _client_detail(client: Any) -> str:
    return json.dumps({"client_id": int(getattr(client, "id")), "name": _safe_required(getattr(client, "name", None))}, separators=(",", ":"))


def _mutation_detail(result: Any, *, renamed_to: str | None = None) -> str:
    detail = {"client_id": int(getattr(result, "client_id")), "name": _safe_required(getattr(result, "name", None))}
    if renamed_to is not None:
        detail["renamed_to"] = _safe_required(renamed_to)
    return json.dumps(detail, separators=(",", ":"))


def _safe_operation_reason(error: Exception) -> str:
    value = getattr(error, "reason", None)
    return _journal_text(value if isinstance(value, str) else None, 255) or "operation failed"


def _probe_target_response(item: Any) -> ProbeTargetResponse:
    return ProbeTargetResponse(
        key=_safe_required(getattr(item, "key", None)),
        label=_safe_required(getattr(item, "label", None)),
        url=_safe_required(getattr(item, "url", None)),
        enabled=bool(getattr(item, "enabled", False)),
        position=int(getattr(item, "position", 0)),
        is_custom=bool(getattr(item, "is_custom", False)),
    )


def _diagnostic_step_response(value: Any) -> ProbeDiagnosticStepResponse:
    succeeded = getattr(value, "succeeded", None)
    if succeeded is not None and not isinstance(succeeded, bool):
        raise ValueError("invalid diagnostic state")
    latency = getattr(value, "latency_ms", None)
    return ProbeDiagnosticStepResponse(
        succeeded=succeeded,
        latency_ms=None if latency is None else _nonnegative(latency),
        reason=_safe_text(getattr(value, "reason", None)),
    )


def _diagnostic_dns_response(value: Any) -> ProbeDiagnosticDnsResponse:
    step = _diagnostic_step_response(value)
    addresses = getattr(value, "addresses", ())
    if not isinstance(addresses, (tuple, list)):
        raise ValueError("invalid DNS diagnostic")
    return ProbeDiagnosticDnsResponse(
        succeeded=step.succeeded,
        latency_ms=step.latency_ms,
        reason=step.reason,
        hostname=_safe_text(getattr(value, "hostname", None)) or "",
        addresses=[_safe_required(item) for item in addresses],
    )


def _probe_target_detail(item: Any) -> str:
    return json.dumps({"key": _safe_required(getattr(item, "key", None)), "label": _safe_required(getattr(item, "label", None))})


def _probe_target_key_detail(key: str) -> str:
    return json.dumps({"key": _safe_required(key)})


def _policy_response(item: Any) -> ManagedRulePolicyResponse:
    return ManagedRulePolicyResponse(
        id=int(getattr(item, "id")),
        kind=_safe_required(getattr(item, "kind", None)),
        category=_safe_required(getattr(item, "category", None)),
        label=_safe_required(getattr(item, "label", None)),
        action=_safe_required(getattr(item, "action", None)),
        enabled=bool(getattr(item, "enabled", False)),
    )


def _policy_detail(item: Any) -> str:
    return json.dumps(
        {
            "policy_id": int(getattr(item, "id")),
            "kind": _safe_required(getattr(item, "kind", None)),
            "category": _safe_required(getattr(item, "category", None)),
            "action": _safe_required(getattr(item, "action", None)),
            "enabled": bool(getattr(item, "enabled", False)),
        },
        separators=(",", ":"),
    )


_FALLBACK_OUTBOUNDS = frozenset({"WG-IMP", "HY2-NL"})
_SERVICE_NAMES = frozenset({"wg-easy", "mihomo", "metacubexd", "uptime-kuma"})


def _load_observability_state(runtime: RuntimeContainer) -> tuple[list[ServiceStatusResponse], FallbackStateResponse]:
    """Read persisted collector observations without exposing retained diagnostics."""

    with runtime.session_factory() as session:
        rows = session.scalars(select(ProbeEvent).order_by(ProbeEvent.id.desc()).limit(500)).all()
    services: dict[str, ServiceStatusResponse] = {}
    selected: str | None = None
    for row in rows:
        if row.target.startswith("service:"):
            name = row.target.removeprefix("service:")
            if name not in _SERVICE_NAMES or name in services:
                continue
            services[name] = ServiceStatusResponse(
                name=name,
                observed_at=row.observed_at,
                succeeded=row.succeeded,
                latency_ms=_optional_nonnegative(row.latency_ms),
                status=_safe_text(row.status),
                status_code=_optional_status_code(row.status_code),
            )
        elif row.target.startswith("route-state:") and selected is None and row.outbound in _FALLBACK_OUTBOUNDS:
            selected = row.outbound
    return sorted(services.values(), key=lambda item: item.name), FallbackStateResponse(selected=selected)


def _selected_fallback_from_groups(groups: Any) -> str | None:
    """Use a live controller selection only when the persisted collector has no sample yet."""

    for group in groups:
        choices = getattr(group, "proxies", ())
        selected = getattr(group, "now", None)
        if {"WG-IMP", "HY2-NL"}.issubset(choices) and selected in _FALLBACK_OUTBOUNDS:
            return selected
    return None


def _load_route_activity(runtime: RuntimeContainer) -> tuple[list[RouteProbeResponse], RouteSwitchResponse | None]:
    with runtime.session_factory() as session:
        probe_rows = session.scalars(select(ProbeEvent).order_by(ProbeEvent.id.desc()).limit(500)).all()
        switch_rows = session.scalars(select(RouteEvent).order_by(RouteEvent.id.desc()).limit(200)).all()
    probes = [
        RouteProbeResponse(
            observed_at=row.observed_at,
            target=row.target,
            succeeded=row.succeeded,
            latency_ms=_optional_nonnegative(row.latency_ms),
            endpoint=_safe_text(row.endpoint),
            outbound=row.outbound if row.outbound in _FALLBACK_OUTBOUNDS else None,
            status=_safe_text(row.status),
            status_code=_optional_status_code(row.status_code),
            reason=_journal_text(row.error_text, 160) if not row.succeeded else None,
        )
        for row in probe_rows
        if row.target in _FALLBACK_OUTBOUNDS
    ][:40]
    switch = next(
        (
            RouteSwitchResponse(
                observed_at=row.observed_at,
                route=_safe_required(row.route),
                action=_safe_required(row.action),
                previous_outbound=row.previous_outbound if row.previous_outbound in _FALLBACK_OUTBOUNDS else None,
                new_outbound=row.new_outbound if row.new_outbound in _FALLBACK_OUTBOUNDS else None,
            )
            for row in switch_rows
            if row.previous_outbound in _FALLBACK_OUTBOUNDS or row.new_outbound in _FALLBACK_OUTBOUNDS
        ),
        None,
    )
    return probes, switch


@dataclass(frozen=True, slots=True)
class _JournalFilters:
    from_value: datetime | None
    to_value: datetime | None
    action: str | None
    outbound: str | None
    endpoint: str | None


def _journal_filters(
    from_value: datetime | None,
    to_value: datetime | None,
    action: str | None,
    outbound: str | None,
    endpoint: str | None,
) -> _JournalFilters:
    normalized_from = _utc_filter_time(from_value)
    normalized_to = _utc_filter_time(to_value)
    if normalized_from is not None and normalized_to is not None and normalized_from > normalized_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid journal filter")
    return _JournalFilters(
        from_value=normalized_from,
        to_value=normalized_to,
        action=_journal_text(action, 64),
        outbound=_journal_text(outbound, 255),
        endpoint=_journal_text(endpoint, 255),
    )


def _journal_events(
    audit_rows: list[AuditEvent],
    route_rows: list[RouteEvent],
    probe_rows: list[ProbeEvent],
    filters: _JournalFilters,
) -> list[JournalEventResponse]:
    events: list[JournalEventResponse] = []
    for row in audit_rows:
        if not _matches_time(row.observed_at, filters) or not _matches_action(row.action, filters):
            continue
        if filters.outbound is not None or filters.endpoint is not None:
            continue
        events.append(
            JournalEventResponse(
                id=row.id,
                kind="audit",
                observed_at=row.observed_at,
                actor=_safe_text(row.actor),
                action=_safe_required(row.action),
                outbound=None,
                endpoint=None,
                revision_number=row.revision_number,
                succeeded=row.succeeded,
                status_code=_optional_status_code(row.status_code),
                restored=row.restored,
                latency_ms=None,
            )
        )
    for row in route_rows:
        if not _matches_time(row.observed_at, filters) or not _matches_action(row.action, filters):
            continue
        if filters.endpoint is not None or (
            filters.outbound is not None and filters.outbound not in {row.previous_outbound, row.new_outbound}
        ):
            continue
        events.append(
            JournalEventResponse(
                id=row.id,
                kind="route",
                observed_at=row.observed_at,
                actor=None,
                action=_safe_required(row.action),
                outbound=_safe_text(row.new_outbound),
                endpoint=None,
                revision_number=None,
                succeeded=None,
                status_code=None,
                restored=None,
                latency_ms=None,
            )
        )
    for row in probe_rows:
        if not _matches_time(row.observed_at, filters) or not _matches_action("probe", filters):
            continue
        if filters.outbound is not None and row.outbound != filters.outbound:
            continue
        if filters.endpoint is not None and row.endpoint != filters.endpoint:
            continue
        events.append(
            JournalEventResponse(
                id=row.id,
                kind="probe",
                observed_at=row.observed_at,
                actor=None,
                action="probe",
                outbound=_safe_text(row.outbound),
                endpoint=_safe_text(row.endpoint),
                revision_number=None,
                succeeded=row.succeeded,
                status_code=_optional_status_code(row.status_code),
                restored=None,
                latency_ms=_optional_nonnegative(row.latency_ms),
            )
        )
    return sorted(events, key=lambda item: (item.observed_at, item.kind, item.id), reverse=True)


def _matches_time(observed_at: datetime, filters: _JournalFilters) -> bool:
    if filters.from_value is not None and observed_at < filters.from_value:
        return False
    return filters.to_value is None or observed_at <= filters.to_value


def _matches_action(value: str, filters: _JournalFilters) -> bool:
    return filters.action is None or value == filters.action


def _journal_text(value: str | None, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(character.isspace() and character not in {" "} for character in normalized):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid journal filter")
    return normalized


def _utc_filter_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid journal filter")
    return value.astimezone(UTC)


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _configuration_filename(profile_name: str) -> str:
    """Return a filesystem-safe filename while preserving the visible profile name."""

    normalized = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', "_", profile_name).strip(" .")
    return f"{normalized or 'wireguard'}.conf"


def _configuration_disposition(filename: str) -> str:
    """Provide an ASCII fallback and the exact UTF-8 filename for modern browsers."""

    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii").strip(" .")
    if not ascii_fallback or ascii_fallback == ".conf":
        ascii_fallback = "wireguard.conf"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _upstream_failure() -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, "backend operation failed")


def _safe_required(value: Any) -> str:
    result = _safe_text(value)
    if result is None:
        return "unavailable"
    return result


def _safe_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:255] if normalized else None


def _nonnegative(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_nonnegative(value: Any) -> int | None:
    return _nonnegative(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _optional_status_code(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else None
