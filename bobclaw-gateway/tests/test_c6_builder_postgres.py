"""C6 task lifetime, once-only dispatch and truthful outcomes with real Postgres.

Worker transport is a local fixture. Installed sandbox/chat proof is separate.
"""
import asyncio
from dataclasses import replace
import json
import time
import uuid

import pytest

from tests.test_builder_workspace_postgres import builder, base_builder
from tests.test_builder_postgres import mode
from tests.test_durable_runs_v2 import pg
from core.builder.service import Builder
from core.execution.models import Authority, ExecutionError, digest
from core.execution.store import EXECUTION_MIGRATIONS, MIGRATION, Store


@pytest.mark.parametrize('expired', [False, True])
async def test_c5f_mode_migration_keeps_missing_binding_errors_structured(pg, monkeypatch, expired):
    # Construct the actual pre-C6 schema before inserting a historical mode.
    # PostgreSQL adds nullable columns to existing rows; SELECT * cannot omit
    # those keys after Store.initialize completes at core startup.
    for name in EXECUTION_MIGRATIONS:
        if name == '2026-11-09_builder_task.sql':
            break
        await pg.execute(MIGRATION.with_name(name).read_text(encoding='utf-8'))
    project, conversation = uuid.uuid4(), uuid.uuid4()
    await pg.execute('INSERT INTO projects(id,user_id) VALUES($1,$2)', project, 'owner')
    await pg.execute('INSERT INTO conversations(id,user_id,project_id) VALUES($1,$2,$3)',
                     conversation, 'owner', project)
    await pg.execute('''INSERT INTO builder_modes
        (lane,owner,conversation_id,project_id,mode,session_id,session_until,expires_at)
        VALUES('lane','owner',$1,$2,'yolo','c5f-session',$3,$3)''',
        conversation, project, 1 if expired else time.time() + 3600)
    before = await pg.fetchrow('SELECT * FROM builder_modes WHERE conversation_id=$1', conversation)
    assert before['conversation_id'] == conversation
    assert 'authority_scope' not in before and 'credential_epoch' not in before

    await Store(pg, 'lane').initialize()
    migrated = await pg.fetchrow('SELECT * FROM builder_modes WHERE conversation_id=$1', conversation)
    assert migrated['conversation_id'] == conversation
    assert migrated['authority_scope'] is None and migrated['credential_epoch'] is None
    auth = Authority('lane', 'owner', 'owner', {}, 'portal', credential_epoch=7)
    builder = Builder(pg)
    monkeypatch.setattr(builder, 'workspace', lambda *a: None)
    row = await builder.store.mode(auth, str(conversation))
    assert row['authority_scope'] == digest(auth.scope) and row['credential_epoch'] == 7
    assert row['mode'] == ('auto' if expired else 'yolo')
    assert (row['session_id'] != 'c5f-session') == expired
    result = await builder.tool(auth, str(conversation), 'fs_write', {'path': 'unused', 'content': 'unused'})
    assert result['error']['code'] == 'workspace_missing'
    # A stale mode row never supplies a disappeared conversation's authority.
    await pg.execute('DELETE FROM conversations WHERE id=$1', conversation)
    with pytest.raises(ExecutionError, match='^conversation_project_required$'):
        await builder.tool(auth, str(conversation), 'fs_write', {'path': 'unused', 'content': 'unused'})


async def parked(h, operation, args):
    frames=[]
    task=asyncio.create_task(h.service.tool(h.auth,h.conversation,operation,args,writer=frames.append))
    async with asyncio.timeout(5):
        while not frames:
            if task.done():
                raise AssertionError('Tool ended before required approval: '+str(await task))
            await asyncio.sleep(.01)
    card=json.loads(frames[0]['content'].split('\n')[1])
    return task,card,frames


async def allow(h, card, decision='allow_session', key=None):
    return await Builder(h.pg).approve(h.owner,{'approval_id':card['approval_id'],
        'decision':decision,'request_key':key or uuid.uuid4().hex})


async def test_one_task_grant_covers_nested_root_writes_edit_and_reconnect(builder):
    h=builder
    before=await h.service.store.mode(h.auth,h.conversation)
    task,card,_=await parked(h,'fs_write',{'path':'app/index.html','content':'<h1>before</h1>'})
    try:
        await allow(h,card)
        assert (await task)['exit_code']==0
        for path in ['app/style.css','index.html']:
            frames=[]
            result=await Builder(h.pg).tool(h.auth,h.conversation,'fs_write',{'path':path,'content':'data'},writer=frames.append)
            assert result['exit_code']==0 and not frames
        result=await Builder(h.pg).tool(h.auth,h.conversation,'fs_edit',{'path':'app/index.html','old':'before','new':'after'})
        assert result['exit_code']==0 and (h.root/'app/index.html').read_text()=='<h1>after</h1>'
        after=await Builder(h.pg).store.mode(h.auth,h.conversation)
        assert after['session_id']==before['session_id'] and after['session_until']==before['session_until']
        from core.builder.policy import decide
        grants=await h.service.store.allowances(h.auth,after)
        for command in ['npm test','python3 -m unittest discover -s app -v','node --check app/script.js']:
            assert decide(after,'command',{'command':command,'workspace':str(h.root),'cwd':'','network':False},grants)=='run'
        for op in ['fs_delete','checkpoint_restore']:
            assert decide(after,'edit',{'operation':op,'workspace':str(h.root),'path':'index.html'},grants)=='ask'
    finally:
        if not task.done():task.cancel()


async def test_workspace_create_can_explicitly_authorize_the_task_in_one_click(builder):
    h=builder
    task,card,_=await parked(h,'workspace_create',{'name':'C6 HTML app'})
    try:
        result=await allow(h,card)
        receipt=await task
        assert result['result']['task_authorized'] and receipt['task_authorized']
        row=await h.service.store.mode(h.auth,h.conversation)
        grants=await h.service.store.allowances(h.auth,row)
        from core.builder.policy import decide
        root=h.service.workspace(h.auth,row)
        assert root!=h.root and decide(row,'write',{'operation':'fs_write','path':'app/index.html','workspace':str(root)},grants)=='run'
        assert row['mode']=='approve'
    finally:
        if not task.done():task.cancel()


@pytest.mark.parametrize('change',['mode','scope','expiry','task_end','stop','logout'])
async def test_task_grant_ends_on_real_boundaries(builder,change):
    h=builder
    task,card,_=await parked(h,'fs_write',{'path':'one','content':'first'})
    await allow(h,card)
    await task
    before=await h.service.store.mode(h.auth,h.conversation)
    auth=h.auth
    if change=='mode':
        await mode(h,'auto');await mode(h,'approve')
    elif change=='scope':
        auth=replace(auth,scope={'may_touch':['narrower']})
    elif change=='expiry':
        await h.pg.execute('UPDATE builder_modes SET session_until=0 WHERE conversation_id=$1',uuid.UUID(h.conversation))
    elif change=='task_end':
        await h.service.tool(auth,h.conversation,'task_finish',{})
    elif change=='logout':
        from core.builder.lifecycle import end_sessions
        await end_sessions(h.pg,auth.lane,auth.owner,reason='owner_logged_out')
    else:
        await h.service.command(auth,'builder.task.end',{'conversation_id':h.conversation})
    row=await h.service.store.mode(auth,h.conversation)
    assert row['session_id']!=before['session_id']
    assert not await h.service.store.allowances(auth,row)


async def test_nonzero_exit_is_failed_separately_from_allowed_and_never_replayed(builder,monkeypatch):
    h=builder
    from core.worker import client
    calls=[]
    async def fail(*args,**kwargs):
        calls.append(1)
        return {'returncode':1,'cleanup':'idle_proved','execution_id':'c6-forced',
            'stdout':b'{"adapter":"builder-command/1","error":"permission_denied","diagnostics":{"stage":"temp-open","errno":13}}',
            'stderr':b''}
    monkeypatch.setattr(client,'execute_async',fail)
    task,card,_=await parked(h,'fs_write',{'path':'app/index.html','content':'never written'})
    key=uuid.uuid4().hex
    approved=await allow(h,card,'allow_once',key)
    result=await task
    record=await h.service.command(h.auth,'builder.approval.get',{'approval_id':card['approval_id']})
    assert record['state']=='allowed' and record['execution_state']=='failed' and record['outcome']=='failed'
    assert result['status']=='failed' and result['diagnostics']=={'stage':'temp-open','errno':13}
    assert (await allow(h,card,'allow_once',key))==approved
    assert len(calls)==1 and not (h.root/'app/index.html').exists()


@pytest.mark.parametrize('resolution',['deny','expired','stopped','orphan'])
async def test_pending_proposals_reconcile_without_dispatch(builder,resolution):
    h=builder
    task,card,_=await parked(h,'fs_write',{'path':'never','content':'never'})
    try:
        aid=card['approval_id']
        if resolution=='deny':await allow(h,card,'deny')
        elif resolution=='expired':await h.pg.execute('UPDATE builder_approvals SET expires_at=0 WHERE id=$1',aid)
        elif resolution=='orphan':await h.pg.execute("UPDATE builder_approvals SET session_id='old-session' WHERE id=$1",aid)
        else:await h.service.command(h.auth,'builder.task.end',{'conversation_id':h.conversation})
        view=await Builder(h.pg).command(h.auth,'builder.mode.get',{'conversation_id':h.conversation})
        record=next(r for r in view['approval_records'] if r['approval_id']==aid)
        assert record['outcome']=={'deny':'denied','expired':'expired','stopped':'stopped','orphan':'expired'}[resolution]
        assert not h.calls and not (h.root/'never').exists()
        assert (await asyncio.wait_for(task,3))['status'] in {'denied','expired','stopped'}
    finally:
        if not task.done():task.cancel()


async def test_lost_worker_receipt_is_unknown_and_a_repeat_click_never_dispatches(builder,monkeypatch):
    h=builder
    from core.worker import client
    calls=[]
    async def lost(*args,**kwargs):
        calls.append(1)
        raise ConnectionResetError('synthetic lost reply')
    monkeypatch.setattr(client,'execute_async',lost)
    task,card,_=await parked(h,'fs_write',{'path':'uncertain','content':'x'})
    key=uuid.uuid4().hex
    await allow(h,card,'allow_once',key)
    assert (await task)['status']=='unknown'
    record=await Builder(h.pg).command(h.auth,'builder.approval.get',{'approval_id':card['approval_id']})
    assert record['state']=='allowed' and record['execution_state']=='unknown'
    await allow(h,card,'allow_once',key)
    assert len(calls)==1


async def test_stop_during_execution_revokes_without_waiting_for_worker_row_lock(builder,monkeypatch):
    h=builder
    from core.worker import client
    started=asyncio.Event()
    async def slow(*args,**kwargs):
        started.set()
        try:await asyncio.sleep(60)
        except asyncio.CancelledError:
            error=asyncio.CancelledError()
            error.worker_cleanup='idle_proved'
            error.execution_ids=['fixture-cancelled']
            raise error
    monkeypatch.setattr(client,'execute_async',slow)
    tool,card,_=await parked(h,'fs_write',{'path':'stopped','content':'x'})
    approval=asyncio.create_task(allow(h,card,'allow_once'))
    try:
        await asyncio.wait_for(started.wait(),3)
        await asyncio.wait_for(h.service.command(h.auth,'builder.task.end',{'conversation_id':h.conversation}),1)
        result=await asyncio.wait_for(approval,3)
        assert result['result']['status']=='stopped' and result['result']['cleanup']=='idle_proved'
        assert (await tool)['status']=='stopped'
        record=await h.service.command(h.auth,'builder.approval.get',{'approval_id':card['approval_id']})
        assert record['state']=='allowed' and record['execution_state']=='stopped' and record['outcome']=='stopped'
        assert not (h.root/'stopped').exists()
    finally:
        for task in (tool,approval):
            if not task.done():task.cancel()


async def test_orphaned_running_record_becomes_unknown_without_replay(builder):
    h=builder
    task,card,_=await parked(h,'fs_write',{'path':'known','content':'known'})
    await allow(h,card,'allow_once')
    await task
    count=len(h.calls)
    await h.pg.execute("UPDATE builder_approvals SET execution_state='running',execution_until=0,result=NULL WHERE id=$1",card['approval_id'])
    view=await Builder(h.pg).command(h.auth,'builder.mode.get',{'conversation_id':h.conversation})
    record=next(r for r in view['approval_records'] if r['approval_id']==card['approval_id'])
    assert record['state']=='allowed' and record['outcome']=='unknown' and record['result']['status']=='unknown'
    assert len(h.calls)==count
