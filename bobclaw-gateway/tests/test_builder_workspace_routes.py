"""Workspace browse stays on the normal authenticated read route."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_builder_owner_routes import builder_h, tmp_path
from tests.test_owner_sessions import owner_h


async def test_workspace_browse_needs_no_owner_stepup(builder_h):
    h,client=builder_h
    args={'project_id':'owned','path':'src'}
    response=await client.post('/api/workspace/browse',headers=h.headers,json={'contract_version':2,**args})
    assert response.status==200,await response.text()
    envelope=h.hits[-1]['envelope']
    assert envelope['operation']=='workspace.browse' and envelope['args']==args
    assert envelope['authority']['token_type']=='portal' and not envelope['authority']['session_kind']
    for version in (None,True,1):
        response=await client.post('/api/workspace/browse',headers=h.headers,json={'contract_version':version,**args})
        assert response.status==422


def test_workspace_browse_broker_closed_schema_normal_transport(monkeypatch):
    from tests.test_day_one_broker_sync import load_broker
    module=load_broker(Path(__file__).parents[2]/'portal-broker/portal-broker.py',monkeypatch)
    broker=module.Broker(SimpleNamespace(),{'sub':'owner','token_type':'portal'},SimpleNamespace())
    hits=[]
    broker._rest=lambda *a,**k: hits.append((a,k)) or {'entries':[]}
    assert broker.op_workspace_browse({'project_id':'p','path':''})=={'entries':[]}
    assert hits[-1][0]==('POST','/api/workspace/browse')
    for path in ('../host','/absolute','C:/host','a/../b','a\\b','%2e%2e'):
        with pytest.raises(module.BrokerError):
            broker.op_workspace_browse({'project_id':'p','path':path})
    for extra in ({'owner':'other'},{'root':'/host'}):
        with pytest.raises(module.BrokerError):
            broker.op_workspace_browse({'project_id':'p',**extra})
    assert len(hits)==1
