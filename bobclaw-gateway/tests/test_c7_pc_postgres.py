"""Real PC request persistence with a fixture verifier, no host/provider calls.

Native CNG and Python RSA interoperability is tested separately with public data.
"""
import asyncio
import time
import uuid
import pytest
from core.execution.models import Authority,ExecutionError
from core.execution.store import Store,decoded
from core.execution.pc import PC
from tests.test_durable_runs_v2 import pg
from tests.test_builder_postgres import builder


@pytest.fixture
async def pc_harness(pg,monkeypatch):
    await Store(pg,'lane').initialize()
    conversation=uuid.uuid4();await pg.execute('INSERT INTO conversations(id,user_id) VALUES($1,$2)',conversation,'owner')
    auth=Authority('lane','owner','owner',{},'portal',credential_epoch=3)
    context={'authority':auth.as_dict(),'face_id':'assistant','conversation_id':str(conversation)}
    # This fixture establishes database semantics, not cryptographic provenance.
    monkeypatch.setattr('core.execution.pc.verify_native',lambda payload,signature:signature=='fixture-native')
    return PC(pg),auth,context,pg


async def pending(h,*,key='one',face='assistant',operation='pc.write'):
    service,auth,context,pg=h
    frames=[]
    args = {} if operation == 'pc.info' else {'path':r'C:\fixture\hello.txt','content':'hello','preimage_sha256':None}
    future=asyncio.create_task(service.request({**context,'face_id':face},operation,args,key,frames.append))
    async with asyncio.timeout(5):
        while not frames:
            if future.done():raise AssertionError(str(await future))
            await asyncio.sleep(.01)
    row=(await service.command(auth,'pc.pending',{}))['requests'][0]
    return future,row


def signed(row,kind,result=None):
    tick=int(time.time());value={'kind':kind,'request_id':row['id'],'task_id':row['task_id'],
        'args_sha256':row['args_sha256'],'host_id':'fixture-host','iat':tick,'exp':tick+60,'nonce':uuid.uuid4().hex}
    if result is not None:value['result']=result
    return {'payload':value,'signature':'fixture-native'}


@pytest.mark.parametrize('outcome',['done','failed','denied','unknown','expired','stopped'])
@pytest.mark.parametrize('operation',['pc.write','pc.info'])
async def test_native_results_are_distinct_signed_and_terminal_without_replay(pc_harness,outcome,operation):
    h=pc_harness;service,auth,context,pg=h;future,row=await pending(h,operation=operation)
    try:
        assert row['operation'] == operation
        if operation == 'pc.info':
            from core.execution.pc import digest
            assert row['args'] == {} and row['args_sha256'] == digest(['pc.info', {}])
        await service.command(auth,'pc.claim',signed(row,'pc.claim'))
        frame=signed(row,'pc.receipt',{'status':outcome,'exit_code':7 if outcome=='failed' else 0 if outcome=='done' else None})
        await service.command(auth,'pc.receipt',frame)
        result=await asyncio.wait_for(future,3);assert result['status']==outcome
        assert result['result']['signature']=='fixture-native'
        assert (await service.command(auth,'pc.receipt',frame))['recorded']
        with pytest.raises(ExecutionError,match='pc_request_not_pending'):
            await service.command(auth,'pc.claim',signed(row,'pc.claim'))
        replay=await service.request(context,operation,row['args'],'one')
        assert replay['request_id']==row['id'] and replay['status']==outcome
        assert await pg.fetchval('SELECT count(*) FROM pc_requests')==1
    finally:
        if not future.done():future.cancel()


async def test_forged_approval_and_receipt_do_not_dispatch_or_change_state(pc_harness):
    h=pc_harness;service,auth,context,pg=h;future,row=await pending(h)
    try:
        with pytest.raises(ExecutionError,match='native_approval_required'):
            await service.command(auth,'pc.approve',{'approved':True})
        forged=signed(row,'pc.claim');forged['signature']='forged'
        with pytest.raises(ExecutionError,match='native_signature_required'):
            await service.command(auth,'pc.claim',forged)
        assert await pg.fetchval('SELECT state FROM pc_requests WHERE id=$1',row['id'])=='pending'
        await service.command(auth,'pc.end',{'conversation_id':context['conversation_id']})
        assert (await asyncio.wait_for(future,3))['status']=='stopped'
        assert row['task_id'] in (await service.command(auth,'pc.pending',{}))['ended_tasks']
        next_task=await service.task(auth,'assistant',context['conversation_id']);assert next_task['id']!=row['task_id']
    finally:
        if not future.done():future.cancel()


async def test_pc_tasks_are_per_agent_epoch_and_conversation(pc_harness):
    service,auth,context,pg=pc_harness
    from dataclasses import replace
    original=await service.task(auth,'assistant',context['conversation_id'])
    assert (await service.task(auth,'assistant',context['conversation_id']))['id']==original['id']
    assert (await service.task(auth,'builder',context['conversation_id']))['id']!=original['id']
    assert (await service.task(replace(auth,credential_epoch=4),'assistant',context['conversation_id']))['id']!=original['id']
    assert original['id'] in (await service.command(replace(auth,credential_epoch=4),'pc.pending',{}))['ended_tasks']


async def test_library_result_is_latest_successful_html_for_owned_project(builder):
    from tests.test_c6_builder_postgres import parked,allow
    from dataclasses import replace
    h=builder
    task,card,_=await parked(h,'fs_write',{'path':'first.html','content':'<h1>first</h1>'})
    await allow(h,card);await task
    await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'app/latest.html','content':'<h1>latest</h1>'})
    await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'last.txt','content':'not HTML'})
    result=await h.service.command(h.auth,'builder.project.result',{'project_id':str(h.project)})
    assert result=={'project_id':str(h.project),'conversation_id':h.conversation,'path':'app/latest.html'}
    with pytest.raises(ExecutionError,match='project_not_found'):
        await h.service.command(replace(h.auth,owner='other'),'builder.project.result',{'project_id':str(h.project)})
