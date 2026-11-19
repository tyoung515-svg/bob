"""Work routes through actual JWT/owner middleware, not a bypassed service call."""
import pytest
from core.execution.models import Authority
from core.execution.owner import require_owner
from tests.test_owner_sessions import owner_h,stepup


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,resource,args',[
    ('work.workspace.create',{'project_id':'owned-project'},{'project_id':'owned-project','request_key':'setup'}),
    ('work.publish',{'job_id':'a'*32},{'job_id':'a'*32,'proposal_digest':'b'*64,'expected_revision':1,'request_key':'publish'})])
async def test_owner_work_routes_preserve_exact_session_authority(owner_h,operation,resource,args):
    h=owner_h
    headers,_=await stepup(h.client,h.headers,operation,resource)
    path='/api/work/'+operation.removeprefix('work.')
    response=await h.client.post(path,headers=headers,json={'contract_version':2,**args})
    assert response.status==200,await response.text()
    envelope=h.hits[-1]['envelope']
    assert envelope['operation']==operation and envelope['args']==args
    await require_owner(Authority(**envelope['authority']),operation)
    # An owner bearer never becomes general chat/work authority.
    response=await h.client.post('/api/work/catalog',headers=headers,json={'contract_version':2})
    assert response.status==403
    response=await h.client.post(path,headers=h.headers,json={'contract_version':2,**args})
    assert response.status in (403,404)


@pytest.mark.asyncio
async def test_portal_work_reads_use_original_scoped_authority(owner_h):
    h=owner_h
    response=await h.client.post('/api/work/status',headers=h.headers,json={'contract_version':2,'job_id':'a'*32})
    assert response.status==200,await response.text()
    envelope=h.hits[-1]['envelope']
    assert envelope['operation']=='work.status' and envelope['authority']['token_type']=='portal'
    assert envelope['authority']['session_kind']==''
