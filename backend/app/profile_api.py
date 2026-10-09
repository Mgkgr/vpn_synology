"""Owner-only private import; checking/applying reuse durable maintenance grants."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.auth import AuthenticatedSession, get_auth_service, require_admin
from app.maintenance_api import _client, _owner_write
from app.maintenance_client import MaintenanceConflict, MaintenanceUnavailable
from app.profile_schema import ProfileDraft, ProfilePreview, ProfileSnapshot


def build_profile_router():
    router=APIRouter(prefix='/api/outbound-profiles')

    @router.get('')
    async def snapshot(request: Request,principal: AuthenticatedSession=Depends(require_admin)):
        try:
            raw=await _client(request).request('profile_snapshot',actor=principal.username)
            value=ProfileSnapshot.model_validate(raw).model_dump(mode='json')
        except (MaintenanceUnavailable,ValidationError):
            return JSONResponse(dict(available=False,can_manage=False,revision=None,current=[],drafts=[]),
                                headers={'Cache-Control':'no-store'})
        owner=get_auth_service(request).is_owner(principal)
        if not owner: value['drafts']=[]
        return JSONResponse(dict(value,available=True,can_manage=owner),headers={'Cache-Control':'no-store'})

    @router.post('/preview')
    async def preview(payload: ProfilePreview,request: Request,principal: AuthenticatedSession=Depends(_owner_write)):
        try:
            raw=await _client(request).request('profile_stage',uri=payload.uri.get_secret_value(),actor=principal.username)
            value=ProfileDraft.model_validate(raw).model_dump(mode='json')
        except MaintenanceConflict:
            raise HTTPException(422,'Ссылка не поддерживается или лимит черновиков исчерпан. Нужна одна ссылка VLESS TCP/REALITY или Hysteria2/salamander.') from None
        except ValidationError:
            raise MaintenanceUnavailable() from None
        return JSONResponse(value,headers={'Cache-Control':'no-store'})

    return router
