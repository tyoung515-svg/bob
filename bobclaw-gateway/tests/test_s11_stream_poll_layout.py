"""S11 batch uses the existing authenticated portal read route, never owner proof."""
from tests.test_builder_owner_routes import builder_h, tmp_path
from tests.test_owner_sessions import owner_h


async def test_batch_route_uses_general_scoped_authority(builder_h):
    h, client = builder_h
    args={'contract_version':2,'conversation_id':'owned','ids':['a','b']}
    response=await client.post('/api/builder/approvals.get',headers=h.headers,json=args)
    assert response.status==200, await response.text()
    envelope=h.hits[-1]['envelope']
    assert envelope['operation']=='builder.approvals.get'
    assert envelope['args']=={'conversation_id':'owned','ids':['a','b']}
    assert envelope['authority']['token_type']=='portal'
    assert not envelope['authority']['session_kind']
    response=await client.post('/api/builder/approvals.get',json=args)
    assert response.status==401
