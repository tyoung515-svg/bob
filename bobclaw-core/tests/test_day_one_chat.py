"""Chat-hop recovery and signed work-face wiring, without provider calls."""
import asyncio
import copy
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.config import config
from core.execution import api, chat_context, chat_records, owner, workbench
from core.execution.models import Authority, canonical
from core.permissions import execution_vouch, verify_execution_vouch


@pytest.fixture
def mock_redis():
    # These isolated service tests do not import or run the model-loop node.
    return None


@pytest.fixture
def chat(monkeypatch):
    now=int(time.time())
    context={'authority':Authority('lane','owner','owner',{},'portal').as_dict(),
             'conversation_id':str(uuid.uuid4()),'face_id':'builder','iat':now,'exp':now+300}
    token=chat_context.current.set(context)
    monkeypatch.setattr(config,'BOBCLAW_SECRET','fixture-secret')
    receipt=AsyncMock()
    monkeypatch.setattr(chat_records,'work_receipt',receipt)
    reader=AsyncMock(return_value=[{'tool':'work.start','payload':{'id':'prior-job'}}])
    monkeypatch.setattr(chat_records,'conversation_receipts',reader)
    transport=SimpleNamespace(context=context,receipt=receipt,reader=reader,sent=[],timeout=None,
                              data={'preparation_id':'prepared'},status=200,error=None)

    class Response:
        @property
        def status(self):return transport.status
        async def json(self):return copy.deepcopy(transport.data)
        async def __aenter__(self):
            if transport.error:raise transport.error
            return self
        async def __aexit__(self,*exc):return False

    class Session:
        def __init__(self,*,timeout):transport.timeout=timeout.total
        async def __aenter__(self):return self
        async def __aexit__(self,*exc):return False
        def post(self,url,*,json):
            transport.sent.append((url,json))
            return Response()

    import aiohttp
    monkeypatch.setattr(aiohttp,'ClientSession',Session)
    yield transport
    chat_context.current.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,data',[(200,{'preparation_id':'prepared'}),(409,{'error':{'code':'revision_conflict'}})])
async def test_known_http_result_survives_receipt_failure(chat,status,data):
    chat.receipt.side_effect=[None,RuntimeError('database unavailable')]
    chat.status,chat.data=status,data
    result=await chat_context.call('work.prepare',{'request_key':'same-key'})
    assert result=={'ok':status<400,'http_status':status,**data,'receipt_saved':False}
    assert chat.timeout==40
    assert chat.receipt.await_count==2


@pytest.mark.asyncio
async def test_unknown_call_replays_identical_arguments_and_key(chat):
    chat.error=asyncio.TimeoutError()
    args={'preparation_id':'prepared','request_key':'same-key'}
    result=await chat_context.call('work.start',args)
    assert result['error']['code']=='work_outcome_unknown'
    assert 'same tool call' in result['error']['detail'] and 'request_key' in result['error']['detail']
    chat.error=None
    result=await chat_context.call('work.start',args)
    assert result['ok'] and result['receipt_saved']
    assert [v['envelope']['args'] for _,v in chat.sent]==[args,args]


@pytest.mark.asyncio
async def test_initial_receipt_failure_refuses_before_http(chat):
    chat.receipt.side_effect=RuntimeError('database unavailable')
    result=await chat_context.call('work.start',{})
    assert result['error']['code']=='work_receipt_storage_unavailable'
    assert not chat.sent


@pytest.mark.asyncio
async def test_catalog_reads_verified_conversation_receipts(chat):
    chat.data={'projects':[]}
    stored=[]
    async def save(context,operation,result):stored.append(copy.deepcopy(result))
    chat.receipt.side_effect=save
    result=await chat_context.call('work.catalog',{})
    assert result['conversation_receipts']==chat.reader.return_value
    assert result['conversation_receipts_available']
    chat.reader.assert_awaited_once_with(chat.context)
    # Store the service result before enrichment, avoiding nested receipt histories.
    assert 'conversation_receipts' not in stored[-1]


@pytest.mark.asyncio
async def test_catalog_read_failure_preserves_known_catalog(chat):
    chat.data={'projects':[]}
    chat.reader.side_effect=RuntimeError('database unavailable')
    result=await chat_context.call('work.catalog',{})
    assert result['ok'] and result['projects']==[]
    assert result['conversation_receipts']==[] and not result['conversation_receipts_available']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['work.prepare','work.recipe.save'])
async def test_agents_cannot_prepare_or_save_recipes(chat,operation):
    chat.context['authority']['token_type']='agent'
    result=await chat_context.call(operation,{})
    assert result['error']['code']=='not_found'
    assert not chat.sent and not chat.receipt.called


@pytest.mark.asyncio
async def test_agent_catalog_remains_available(chat):
    chat.context['authority']['token_type']='agent'
    assert (await chat_context.call('work.catalog',{}))['ok']


@pytest.mark.asyncio
@pytest.mark.parametrize('face',['builder','fixer','orchestrator'])
async def test_verified_face_is_separate_from_model_arguments(chat,face):
    chat.context['face_id']=face
    args={'face_id':'forged','request_key':'key'}
    await chat_context.call('work.prepare',args)
    envelope=chat.sent[0][1]['envelope']
    assert envelope['face_id']==face and envelope['args']==args
    proof=chat.sent[0][1]['vouch']
    assert verify_execution_vouch(envelope,proof,'fixture-secret')
    assert not verify_execution_vouch({**envelope,'face_id':'fixer' if face!='fixer' else 'builder'},proof,'fixture-secret')
    for change in ({'face_id':'assistant'},{'face_id':None},{'extra':'unsigned'}):
        changed={**envelope,**change}
        assert not verify_execution_vouch(changed,execution_vouch(changed,'fixture-secret'),'fixture-secret')
    legacy={k:v for k,v in envelope.items() if k!='face_id'}
    assert verify_execution_vouch(legacy,execution_vouch(legacy,'fixture-secret'),'fixture-secret')


@pytest.mark.asyncio
async def test_receipt_reader_caps_count_and_encoded_size(monkeypatch):
    context={'conversation_id':str(uuid.uuid4()),'authority':{'owner':'owner'}}
    pool=SimpleNamespace(fetch=AsyncMock(return_value=[
        {'id':n,'tool':'work.prepare','created_at':'now','payload':canonical({'text':'x'*8000})}
        for n in range(20,0,-1)]))
    monkeypatch.setattr(chat_records,'POOL',pool)
    result=await chat_records.conversation_receipts(context,limit=10000)
    sql,*args=pool.fetch.call_args.args
    assert args==[uuid.UUID(context['conversation_id']),'owner',20,8192]
    assert 'c.user_id=r.user_id' in sql and 'r.conversation_id=$1 AND r.user_id=$2' in sql
    assert 'octet_length' in sql and 'ORDER BY r.created_at DESC,r.id DESC' in sql
    assert 0<len(result)<20 and len(canonical(result).encode())<=65536
    assert result[0]['id']=='20'


@pytest.mark.asyncio
@pytest.mark.parametrize('face',[None,'builder','fixer'])
async def test_core_dispatch_passes_signed_face(monkeypatch,face):
    monkeypatch.setattr(config,'BOBCLAW_SECRET','fixture-secret')
    monkeypatch.setattr(api,'lane_id',lambda:'lane')
    monkeypatch.setattr(owner,'owner_mapping',lambda _:True)
    monkeypatch.setattr(owner,'check_epoch',AsyncMock())
    dispatch=AsyncMock(return_value={'status':'prepared'})
    monkeypatch.setattr(workbench.Workbench,'command',dispatch)
    auth=Authority('lane','owner','owner',{},'portal',policy_epoch=api.policy_epoch('fixture-secret'))
    now=int(time.time())
    envelope={'operation':'work.prepare','args':{'mode':'builder'},'authority':auth.as_dict(),'iat':now,'exp':now+30}
    if face is not None:envelope['face_id']=face
    request=SimpleNamespace(json=AsyncMock(return_value={'envelope':envelope,'vouch':execution_vouch(envelope,'fixture-secret')}),
                            match_info={'operation':'work.prepare'},app={api.SERVICE_KEY:SimpleNamespace(store=None)})
    response=await api.command(request)
    assert response.status==200
    assert dispatch.call_args.kwargs=={'face':face}


@pytest.mark.asyncio
async def test_workspace_owner_summary_names_owned_project(monkeypatch):
    import json
    monkeypatch.setattr(config,'BOBCLAW_SECRET','fixture-secret')
    monkeypatch.setattr(api,'lane_id',lambda:'lane')
    monkeypatch.setattr(owner,'owner_mapping',lambda _:True)
    monkeypatch.setattr(owner,'check_epoch',AsyncMock())
    pid=str(uuid.uuid4())
    pool=SimpleNamespace(fetchrow=AsyncMock(side_effect=[{'id':pid},{'name':'Canonical project'}]))
    auth=Authority('lane','owner','owner',{},'portal',policy_epoch=api.policy_epoch('fixture-secret'))
    now=int(time.time())
    envelope={'operation':'owner.summary','args':{'operation':'work.workspace.create','resource':{'project_id':pid}},
              'authority':auth.as_dict(),'iat':now,'exp':now+30}
    request=SimpleNamespace(json=AsyncMock(return_value={'envelope':envelope,'vouch':execution_vouch(envelope,'fixture-secret')}),
                            match_info={'operation':'owner.summary'},app={api.SERVICE_KEY:SimpleNamespace(store=SimpleNamespace(pool=pool))})
    response=await api.command(request)
    assert response.status==200 and json.loads(response.text)['project_name']=='Canonical project'
    assert pool.fetchrow.call_args.args[1:]==(uuid.UUID(pid),'owner')
    pool.fetchrow.side_effect=[None]
    assert (await api.command(request)).status==404
