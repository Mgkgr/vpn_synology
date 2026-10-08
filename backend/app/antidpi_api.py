"""Read-only strategy snapshot; all writes reuse the operation-bound owner grants."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.antidpi_schema import StrategySnapshot
from app.auth import AuthenticatedSession, get_auth_service, require_admin
from app.maintenance_api import _client
from app.maintenance_client import MaintenanceUnavailable


def build_antidpi_router():
    router=APIRouter(prefix='/api/antidpi')

    @router.get('/strategies')
    async def strategies(request: Request, principal: AuthenticatedSession=Depends(require_admin)):
        try:
            raw=await _client(request).request('strategy_snapshot')
            snapshot=StrategySnapshot.model_validate(raw).model_dump(mode='json')
        except (MaintenanceUnavailable,ValidationError):
            return JSONResponse(dict(available=False,can_manage=False,revision=None,identity=None,
                catalog=None,services=[],observed_at=None,capabilities=dict(can_check=False,can_apply=False,
                can_configure=False,blockers=['worker_unavailable'])),headers={'Cache-Control':'no-store'})
        owner=get_auth_service(request).is_owner(principal)
        if not owner:
            for key in ('can_check','can_apply','can_configure'):
                snapshot['capabilities'][key]=False
        return JSONResponse(dict(snapshot,available=True,can_manage=owner),headers={'Cache-Control':'no-store'})

    return router
