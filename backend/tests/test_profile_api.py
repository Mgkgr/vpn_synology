import copy
from pathlib import Path
import sys

import pytest
from sqlalchemy import select

from app.maintenance_client import MaintenanceClient
from app.models import MaintenanceSubmitIntent
from test_maintenance_api import system, grant, PASSWORD, JOB

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy/tests'))
from test_profile_links import VLESS

OP=dict(action='profile_check',draft_id='d'*32,expected_revision='a'*64)


@pytest.fixture
def profiles(system,tmp_path):
    client,sessions,frames,state,runtime=system
    from maintenance.profile_state import ProfileDrafts
    draft=ProfileDrafts(tmp_path/'drafts').stage(VLESS,'owner','a'*64,1000)
    draft['draft_id']='d'*32
    snapshot=dict(revision='a'*64,current=[dict(target='WG-IMP',protocol='vless',server='vpn.example',port=443)],drafts=[draft])
    original=runtime.maintenance_client._transport
    async def transport(frame):
        if frame['method'] in ('profile_stage','profile_snapshot'):
            frames.append(frame)
            return dict(ok=True,result=copy.deepcopy(draft if frame['method']=='profile_stage' else snapshot))
        return await original(frame)
    runtime.maintenance_client=MaintenanceClient(enabled=True,transport=transport)
    return client,sessions,frames,state,runtime,draft,snapshot


def test_preview_is_owner_csrf_only_and_never_echoes_link(profiles):
    client,_,frames,_,_,_,_=profiles
    response=client.post('/api/outbound-profiles/preview',json={'uri':VLESS})
    assert response.status_code==200,response.text
    assert response.json()['target']=='WG-IMP'
    assert '11111111-2222-4333-8444-555555555555' not in response.text
    assert 'no-store' in response.headers['cache-control']
    assert [frame['method'] for frame in frames]==['profile_stage']
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/outbound-profiles/preview',json={'uri':VLESS}).status_code==403
    client.headers['X-CSRF-Token']=csrf
    client.post('/api/auth/admins',json={'username':'reader','password':PASSWORD})
    login=client.post('/api/auth/login',json={'username':'reader','password':PASSWORD})
    client.headers['X-CSRF-Token']=login.json()['csrf_token']
    assert client.post('/api/outbound-profiles/preview',json={'uri':VLESS}).status_code==403
    assert client.get('/api/outbound-profiles').json()['can_manage'] is False
    client.cookies.clear()
    assert client.get('/api/outbound-profiles').status_code==401


def test_get_never_checks_or_mutates_and_malformed_secret_response_is_dropped(profiles):
    client,_,frames,_,_,draft,snapshot=profiles
    value=client.get('/api/outbound-profiles')
    assert value.status_code==200 and value.json()['available'] is True
    assert [frame['method'] for frame in frames]==['profile_snapshot']
    draft['uuid']='private-value'
    response=client.post('/api/outbound-profiles/preview',json={'uri':VLESS})
    assert response.status_code==503 and 'private-value' not in response.text
    snapshot['private_key']='private-value'
    response=client.get('/api/outbound-profiles')
    assert response.json()['available'] is False and 'private-value' not in response.text


def test_grant_is_bound_to_opaque_draft_and_lost_response_is_not_replayed(profiles):
    client,sessions,frames,state,_,_,_=profiles
    token=grant(client,OP)
    assert client.post('/api/maintenance/jobs',json=dict(job_id=JOB,grant=token,operation=dict(OP,draft_id='e'*32))).status_code==403
    state['lost']=True
    body=dict(job_id=JOB,grant=token,operation=OP)
    assert client.post('/api/maintenance/jobs',json=body).json()['phase']=='unknown'
    assert client.post('/api/maintenance/jobs',json=body).json()['phase']=='queued'
    assert sum(frame['method']=='submit' for frame in frames)==1
    with sessions() as session:
        saved=session.scalar(select(MaintenanceSubmitIntent)).request_json
    assert '11111111-2222-4333-8444-555555555555' not in saved
    assert 'vless://' not in saved and PASSWORD not in saved
    assert 'draft_id' in saved


def test_unsafe_fields_and_oversized_links_do_not_leak_validation_values(profiles):
    client,_,frames,_,_,_,_=profiles
    for payload in ({'uri':VLESS,'command':'private-secret'},{'uri':VLESS*100},{'uri':{'secret':'private-secret'}}):
        response=client.post('/api/outbound-profiles/preview',json=payload)
        assert response.status_code==422 and 'private-secret' not in response.text
        assert '11111111-2222-4333-8444-555555555555' not in response.text
    response=client.post('/api/maintenance/authorize',json=dict(operation=dict(OP,uri=VLESS),password=PASSWORD))
    assert response.status_code==422 and PASSWORD not in response.text
    assert frames==[]
