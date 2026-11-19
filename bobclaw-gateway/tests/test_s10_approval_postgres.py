"""Real server persistence/scope integration; opt-in disposable Postgres only."""
import uuid
import pytest

from tests.test_builder_workspace_postgres import builder, base_builder, create, allow
from tests.test_builder_postgres import mode
from tests.test_durable_runs_v2 import pg
from core.builder.service import Builder
import asyncio
import json


async def grant(h,operation,args):
    frames=[]
    task=asyncio.create_task(h.service.tool(h.auth,h.conversation,operation,args,writer=frames.append))
    try:
        async with asyncio.timeout(5):
            while not frames:
                await asyncio.sleep(.01)
        card=json.loads(frames[0]['content'].split('\n')[1])
        await h.service.approve(h.owner,{'approval_id':card['approval_id'],'decision':'allow_session','request_key':uuid.uuid4().hex})
        return await task
    finally:
        if not task.done():task.cancel()


async def test_three_different_writes_survive_service_reconnect_not_other_conversation_or_mount(builder):
    h=builder
    await grant(h,'fs_write',{'path':'one.txt','content':'first'})
    service=Builder(h.pg)
    for n in range(3):
        result=await service.tool(h.auth,h.conversation,'fs_write',{'path':f'file{n}.txt','content':str(n)})
        assert result['exit_code']==0 and (h.root/f'file{n}.txt').read_text()==str(n)
        row=await service.command(h.auth,'builder.approval.get',{'approval_id':result['approval_id']})
        assert row['state']=='allowed' and row['execution_state']=='completed'
    conversation=uuid.uuid4()
    await h.pg.execute('INSERT INTO conversations(id,user_id,project_id) VALUES($1,$2,$3)',conversation,'owner',h.project)
    other=await service.store.mode(h.auth,str(conversation))
    assert not await service.store.allowances(h.auth,other)
    proposal=await create(h,'Replacement workspace')
    await allow(h,proposal)
    current=await service.store.mode(h.auth,h.conversation)
    assert not await service.store.allowances(h.auth,current)
    assert current['project_id']!=h.project


async def test_edit_allowance_is_kind_scoped_and_session_expiry_revokes_it(builder):
    h=builder
    (h.root/'one.txt').write_text('version 0')
    await grant(h,'fs_edit',{'path':'one.txt','old':'version 0','new':'version 1'})
    for n in range(2,5):
        result=await Builder(h.pg).tool(h.auth,h.conversation,'fs_edit',{'path':'one.txt','old':f'version {n-1}','new':f'version {n}'})
        assert result['exit_code']==0
    assert (h.root/'one.txt').read_text()=='version 4'
    await h.pg.execute('UPDATE builder_modes SET session_until=0 WHERE conversation_id=$1',uuid.UUID(h.conversation))
    current=await h.service.store.mode(h.auth,h.conversation)
    assert not await h.service.store.allowances(h.auth,current)


async def test_compound_program_allowances_are_durable_and_read_only_commands_are_receipted(builder,monkeypatch):
    from unittest.mock import AsyncMock
    h=builder
    monkeypatch.setattr(Builder,'worker',AsyncMock(return_value={'exit_code':0,'stdout':'fixture','cleanup':'idle_proved'}))
    await grant(h,'shell_run',{'command':'npm test; npm run build'})
    for command in ['npm run build && npm test', 'pwd; ls -la | head -20']:
        result=await Builder(h.pg).tool(h.auth,h.conversation,'shell_run',{'command':command})
        record=await Builder(h.pg).command(h.auth,'builder.approval.get',{'approval_id':result['approval_id']})
        assert record['state']=='allowed' and record['execution_state']=='completed'
    assert await h.pg.fetchval('SELECT count(*) FROM builder_allowances WHERE conversation_id=$1',uuid.UUID(h.conversation))==2
