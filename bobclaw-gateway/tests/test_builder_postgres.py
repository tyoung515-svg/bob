"""Real disposable Postgres, actual file edits; worker transport is a fixture.

No providers are called. Linux sandbox/process proof requires installed acceptance.
"""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

import pytest
from tests.test_durable_runs_v2 import pg
from core.builder.service import Builder, turn_id
from core.builder.network import DEFAULTS
from core.execution.models import Authority, ExecutionError, canonical
from core.execution.store import Store
from core.worker.builder_runner import execute


@pytest.fixture
def tmp_path(tmp_path_factory):
    import os
    if os.name != 'nt':return tmp_path_factory.mktemp('builder-pg')
    path=Path(__file__).parents[1]/'test-db'/('builder-'+uuid.uuid4().hex)
    path.mkdir(mode=0o777)
    return path


@pytest.fixture
async def builder(pg,tmp_path,monkeypatch):
    store=Store(pg,'lane');await store.initialize()
    project=uuid.uuid4();conversation=uuid.uuid4()
    await pg.execute('INSERT INTO projects(id,user_id) VALUES($1,$2)',project,'owner')
    await pg.execute('INSERT INTO conversations(id,user_id,project_id) VALUES($1,$2,$3)',conversation,'owner',project)
    root=tmp_path/'projects'/str(project);root.mkdir(parents=True)
    (tmp_path/'project-folders.json').write_text(json.dumps([{'attachment_id':str(uuid.uuid4()),'owner':'owner',
        'project_id':str(project),'path':'projects/'+str(project),'label':'Fixture'}]),encoding='utf-8')
    monkeypatch.setenv('BOB_LANE_ROOT',str(tmp_path))
    from core.execution import owner
    async def require(auth,op):
        if auth.session_kind!='owner_stepup' or op not in auth.allowed_operations:
            raise ExecutionError('owner_required',403)
    monkeypatch.setattr(owner,'require_owner',require)
    auth=Authority('lane','owner','owner',{},'portal')
    elevated=replace(auth,token_type='human',session_kind='owner_stepup',audience='bob-owner-controls',
                     allowed_operations=('builder.mode.set','builder.approve'))
    calls=[]
    async def worker(profile,**kwargs):
        assert profile=='builder' and kwargs['cwd']==root
        payload=json.loads(kwargs['input']);calls.append(payload)
        result=execute(payload,root)
        return {'stdout':canonical({'adapter':'builder-command/1','result':result}).encode(), 'stderr':b'',
                'returncode':0,'cleanup':'idle_proved','execution_id':uuid.uuid4().hex,'execution_seconds':.01}
    import core.worker.client as client
    monkeypatch.setattr(client,'execute_async',worker)
    return SimpleNamespace(pg=pg,service=Builder(pg),auth=auth,owner=elevated,root=root,
                           conversation=str(conversation),project=project,calls=calls)


async def mode(h,target,**extra):
    row=await h.service.command(h.auth,'builder.mode.get',{'conversation_id':h.conversation})
    return await h.service.command(h.owner,'builder.mode.set',{'conversation_id':h.conversation,'mode':target,
        'expected_revision':row['revision'],'request_key':uuid.uuid4().hex,**extra})


async def test_default_owner_only_modes_revision_replay_and_yolo_expiry(builder):
    h=builder
    row=await h.service.command(h.auth,'builder.mode.get',{'conversation_id':h.conversation})
    assert row['mode']=='approve' and row['revision']==1
    assert row['network']=={'allowlist':DEFAULTS['allowlist'],'project_extra':[],'revision':0}
    args={'conversation_id':h.conversation,'mode':'yolo','expected_revision':1,'request_key':'yolo'}
    with pytest.raises(ExecutionError,match='owner_required'):
        await h.service.command(h.auth,'builder.mode.set',args)
    with pytest.raises(ExecutionError,match='yolo_ack_required'):
        await h.service.command(h.owner,'builder.mode.set',args)
    args['yolo_acknowledged']=True
    result=await h.service.command(h.owner,'builder.mode.set',args)
    assert result['mode']=='yolo' and time.time()<result['expires_at']<=time.time()+7200
    assert result==await h.service.command(h.owner,'builder.mode.set',args)
    with pytest.raises(ExecutionError,match='revision_conflict'):
        await h.service.command(h.owner,'builder.mode.set',{**args,'request_key':'stale'})
    await h.pg.execute("UPDATE builder_modes SET expires_at=1 WHERE conversation_id=$1",uuid.UUID(h.conversation))
    assert (await h.service.store.mode(h.auth,h.conversation))['mode']=='auto'


async def test_suggest_durable_apply_exactly_once_and_undo(builder):
    h=builder;await mode(h,'suggest')
    token=turn_id.set('turn-one')
    try:
        proposal=await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'index.html','content':'Hello'})
    finally:turn_id.reset(token)
    assert proposal['status']=='proposed' and not (h.root/'index.html').exists()
    # New service instance simulates transport reconnection; state is durable.
    reconnected=Builder(h.pg)
    view=await reconnected.command(h.auth,'builder.mode.get',{'conversation_id':h.conversation})
    assert view['approvals'][0]==proposal['approval']
    args={'approval_id':proposal['approval']['approval_id'],'decision':'allow_once','request_key':'apply'}
    result=await reconnected.command(h.owner,'builder.approve',args)
    assert result['state']=='allowed' and (h.root/'index.html').read_text()=='Hello'
    assert (await reconnected.command(h.owner,'builder.approve',args))['state']=='allowed'
    assert len(h.calls)==1
    await mode(h,'auto')
    checkpoints=await h.service.tool(h.auth,h.conversation,'checkpoint_list',{})
    await h.service.tool(h.auth,h.conversation,'checkpoint_restore',{'checkpoint_id':checkpoints['items'][0]['checkpoint_id']})
    assert not (h.root/'index.html').exists()


async def test_approve_pauses_then_resumes_from_owner_record(builder):
    h=builder;frames=[]
    task=asyncio.create_task(h.service.tool(h.auth,h.conversation,'fs_write',{'path':'x.txt','content':'x'},writer=frames.append))
    try:
        for _ in range(100):
            if frames:break
            await asyncio.sleep(.01)
        assert frames and not task.done() and not (h.root/'x.txt').exists()
        card=json.loads(frames[0]['content'].split('\n')[1])
        await h.service.approve(h.owner,{'approval_id':card['approval_id'],'decision':'allow_once','request_key':'allow'})
        result=await asyncio.wait_for(task,3)
        assert result['exit_code']==0 and (h.root/'x.txt').read_text()=='x'
    finally:
        if not task.done():task.cancel()


async def test_foreign_lane_owner_binding_drift_and_expiry_fail_closed(builder):
    h=builder;await mode(h,'suggest')
    prop=await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'x','content':'x'})
    aid=prop['approval']['approval_id']
    for auth in (replace(h.auth,owner='other'),replace(h.auth,lane='other')):
        with pytest.raises(ExecutionError):
            await h.service.store.approval(auth,aid)
    await h.pg.execute('UPDATE builder_approvals SET expires_at=1 WHERE id=$1',aid)
    result=await h.service.approve(h.owner,{'approval_id':aid,'decision':'allow_once','request_key':'expired'})
    assert result['state']=='expired' and not h.calls
    replacement=uuid.uuid4()
    await h.pg.execute('INSERT INTO projects(id,user_id) VALUES($1,$2)',replacement,'owner')
    await h.pg.execute('UPDATE conversations SET project_id=$2 WHERE id=$1',uuid.UUID(h.conversation),replacement)
    with pytest.raises(ExecutionError,match='builder_project_binding_changed'):
        await h.service.store.mode(h.auth,h.conversation)


async def test_allow_session_does_not_upgrade_mode_and_checkpoint_once_per_turn(builder):
    h=builder;await mode(h,'suggest')
    prop=await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'x.txt','content':'x'})
    await h.service.approve(h.owner,{'approval_id':prop['approval']['approval_id'],'decision':'allow_session','request_key':'allow-session'})
    assert (await h.service.store.mode(h.auth,h.conversation))['mode']=='suggest'
    assert (await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'x.txt','content':'x'}))['status']=='proposed'
    await mode(h,'auto')
    before=await h.pg.fetchval('SELECT count(*) FROM builder_checkpoints')
    token=turn_id.set('same-turn')
    try:
        for name in ('one.md','two.css'):
            await h.service.tool(h.auth,h.conversation,'fs_write',{'path':name,'content':'data'})
    finally:turn_id.reset(token)
    assert await h.pg.fetchval('SELECT count(*) FROM builder_checkpoints')==before+1


async def test_proxy_unavailable_before_dispatch_has_durable_refusal(builder,monkeypatch):
    h=builder;await mode(h,'yolo',yolo_acknowledged=True)
    from core.worker import client
    from core.worker.contract import WorkerUnavailable
    def unavailable():
        raise WorkerUnavailable('synthetic missing worker configuration')
    monkeypatch.setattr(client,'configuration',unavailable)
    result=await h.service.tool(h.auth,h.conversation,'shell_run',{'command':'curl example.com','network':True})
    assert result['status']=='refused' and result['error']['code']=='builder_proxy_unavailable' and not h.calls
    record=await Builder(h.pg).command(h.auth,'builder.approval.get',{'approval_id':result['approval_id']})
    assert record['execution_state']=='failed' and record['result']==result


async def test_network_refused_before_dispatch_and_denial_is_durable(builder):
    h=builder
    # A non-allowlisted request now asks; yolo intentionally allows any domain.
    decision=await h.service.network.policy(h.auth,h.conversation,'curl example.com','job-one','example.com','GET')
    assert decision['decision']=='ask' and not h.calls
    card=decision['approval']
    assert card['kind']=='network' and card['detail']['domain']=='example.com'
    reconnected=Builder(h.pg)
    view=await reconnected.command(h.auth,'builder.mode.get',{'conversation_id':h.conversation})
    assert card in view['approvals']
    args={'approval_id':card['approval_id'],'decision':'deny','request_key':'network-deny'}
    assert (await reconnected.approve(h.owner,args))['state']=='denied'
    assert (await Builder(h.pg).approve(h.owner,args))['state']=='denied'
    result=await Builder(h.pg).network.policy(h.auth,h.conversation,'curl example.com','job-retry','example.com','GET')
    assert result=={'decision':'deny','approval_id':card['approval_id']} and not h.calls
    record=await reconnected.command(h.auth,'builder.approval.get',{'approval_id':card['approval_id']})
    assert record['state']=='denied' and record['execution_state']=='pending'
    await mode(h,'suggest')
    prop=await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'x','content':'x'})
    args={'approval_id':prop['approval']['approval_id'],'decision':'deny','request_key':'deny'}
    assert (await h.service.approve(h.owner,args))['state']=='denied'
    assert (await h.service.approve(h.owner,args))['state']=='denied'
    assert not h.calls


async def test_session_end_reverts_yolo_stops_pending_and_stale_diff_refuses(builder):
    h=builder
    from core.builder.lifecycle import end_sessions
    await mode(h,'yolo',yolo_acknowledged=True)
    await end_sessions(h.pg,'lane','owner',[h.conversation])
    assert (await h.service.store.mode(h.auth,h.conversation))['mode']=='auto'
    await mode(h,'suggest')
    prop=await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'index.html','content':'proposed'})
    await end_sessions(h.pg,'lane','owner',[h.conversation])
    assert (await h.service.store.approval(h.auth,prop['approval']['approval_id']))['state']=='stopped'
    assert (await h.service.approve(h.owner,{'approval_id':prop['approval']['approval_id'],'decision':'allow_once','request_key':'old-click'}))['state']=='stopped'
    prop=await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'index.html','content':'proposed'})
    (h.root/'index.html').write_text('changed elsewhere')
    result=await h.service.approve(h.owner,{'approval_id':prop['approval']['approval_id'],'decision':'allow_once','request_key':'stale-apply'})
    assert result['result']['error']['code']=='builder_preimage_changed'
    assert (h.root/'index.html').read_text()=='changed elsewhere' and not h.calls
