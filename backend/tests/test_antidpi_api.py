import copy
from pathlib import Path
import sys

import pytest

from app.maintenance_client import MaintenanceClient
from test_maintenance_api import system, grant, PASSWORD, JOB

OP = dict(action='strategy_check',service_id='youtube',expected_revision='a'*64,settings={})


@pytest.fixture
def antidpi(system,tmp_path):
    client,sessions,frames,state,runtime=system
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy'))
    from maintenance.store import JobStore
    from maintenance.strategy_state import StrategyStore
    from maintenance.strategy_runner import StrategyRunner
    strategies=StrategyStore(JobStore(tmp_path/'private/worker.sqlite3'))
    strategies.register('a'*64,{'youtube':'tlsrec-sni','discord':'tlsrec-sni','telegram':'tlsrec-sni','instagram':'disorder-sni'},1000)
    snapshot=StrategyRunner(strategies,clock=lambda:1000).snapshot()
    snapshot['capabilities']=dict(can_check=True,can_apply=False,can_configure=True,blockers=['dns_renewal_unverified'])
    original=runtime.maintenance_client._transport
    async def transport(frame):
        if frame['method']=='strategy_snapshot':
            frames.append(frame)
            return dict(ok=True,result=copy.deepcopy(snapshot))
        return await original(frame)
    runtime.maintenance_client=MaintenanceClient(enabled=True,transport=transport)
    yield client,frames,state,runtime,snapshot


def test_snapshot_has_real_scope_and_owner_capabilities_without_probes(antidpi):
    client,frames,_,_,_=antidpi
    response=client.get('/api/antidpi/strategies')
    assert response.status_code==200, response.text
    value=response.json()
    assert value['catalog']['scope']=='https_tcp_443_only'
    assert value['can_manage'] and not value['capabilities']['can_apply']
    assert value['services'][0]['strategy_id']=='tlsrec-sni'
    assert [frame['method'] for frame in frames]==['strategy_snapshot']


def test_anonymous_denied_admin_readonly_and_csrf_required(antidpi):
    client,frames,_,_,_=antidpi
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/maintenance/authorize',json={'operation':OP,'password':PASSWORD}).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.post('/api/auth/admins',json={'username':'reader','password':PASSWORD}).status_code==201
    login=client.post('/api/auth/login',json={'username':'reader','password':PASSWORD})
    client.headers['X-CSRF-Token']=login.json()['csrf_token']
    assert client.get('/api/antidpi/strategies').json()['can_manage'] is False
    assert client.post('/api/maintenance/authorize',json={'operation':OP,'password':PASSWORD}).status_code==403
    client.cookies.clear()
    assert client.get('/api/antidpi/strategies').status_code==401


def test_operation_bound_grant_and_lost_response_do_not_repeat_mutation(antidpi):
    client,frames,state,_,_=antidpi
    token=grant(client,OP)
    other={**OP,'service_id':'instagram'}
    assert client.post('/api/maintenance/jobs',json={'operation':other,'job_id':JOB,'grant':token}).status_code==403
    state['lost']=True
    body={'operation':OP,'job_id':JOB,'grant':token}
    assert client.post('/api/maintenance/jobs',json=body).json()['phase']=='unknown'
    assert client.post('/api/maintenance/jobs',json=body).json()['phase']=='queued'
    assert sum(frame['method']=='submit' for frame in frames)==1
    assert PASSWORD not in str(frames) and token not in str(frames)


def test_arbitrary_urls_commands_fields_and_bad_policies_rejected(antidpi):
    client,frames,_,_,_=antidpi
    cases=[{**OP,'service_id':'http://127.0.0.1'}, {**OP,'command':'id'}, {**OP,'settings':{'strategy_id':'split-1'}},
        {**OP,'action':'strategy_apply','settings':{'strategy_id':'--shell','mode':'auto'}},
        {**OP,'action':'strategy_configure','settings':dict(enabled=True,mode='auto',interval_minutes=1,daily_enabled=True)},
        {**OP,'action':'strategy_configure','settings':dict(enabled='false',mode='auto',interval_minutes=30,daily_enabled=True)}]
    for op in cases:
        response=client.post('/api/maintenance/authorize',json={'operation':op,'password':PASSWORD})
        assert response.status_code==422, response.text
        assert PASSWORD not in response.text
    assert not frames


def test_unavailable_worker_and_malformed_snapshot_are_explicit_readonly(antidpi):
    client,_,_,runtime,snapshot=antidpi
    snapshot['secret']='must-not-appear'
    response=client.get('/api/antidpi/strategies')
    assert response.status_code==200 and response.json()['available'] is False
    assert 'must-not-appear' not in response.text
    runtime.maintenance_client.enabled=False
    value=client.get('/api/antidpi/strategies').json()
    assert value['available'] is False and value['can_manage'] is False
    assert value['capabilities']['blockers']==['worker_unavailable']
    assert client.get('/api/auth/csrf').status_code==200
