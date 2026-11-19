"""Transactional S8 acceptance against disposable Postgres (opt-in DSN)."""
import uuid

import pytest

from tests.test_builder_postgres import builder as base_builder, mode
from tests.test_durable_runs_v2 import pg
from core.execution.models import ExecutionError


@pytest.fixture
async def builder(base_builder):
    h=base_builder
    # The shared disposable fixture intentionally creates minimal project/chat
    # tables. Add only the production columns this feature exercises.
    await h.pg.execute('''ALTER TABLE projects ADD COLUMN name TEXT DEFAULT 'Fixture',
        ADD COLUMN description TEXT, ADD COLUMN default_face_id TEXT;
        ALTER TABLE conversations ADD COLUMN face_id TEXT, ADD COLUMN updated_at TIMESTAMPTZ DEFAULT now();
        CREATE TABLE messages(id UUID PRIMARY KEY,conversation_id UUID,role TEXT,content TEXT,metadata JSONB);''')
    return h


async def create(h, name='New project'):
    await mode(h,'suggest')
    return await h.service.tool(h.auth,h.conversation,'workspace_create',{'name':name,'purpose':'Workspace tests'})


async def allow(h, proposal, key=None):
    return await h.service.approve(h.owner,{'approval_id':proposal['approval']['approval_id'],
                                           'decision':'allow_once','request_key':key or uuid.uuid4().hex})


async def test_create_rebinds_atomically_and_replays_after_binding_change(builder):
    h=builder
    (h.root/'old.txt').write_text('untouched')
    proposed=await create(h)
    assert not await h.pg.fetchval("SELECT id FROM projects WHERE name='New project'")
    result=await allow(h,proposed,'same-click')
    target=uuid.UUID(result['result']['project_id'])
    row=await h.service.store.mode(h.auth,h.conversation)
    assert row['project_id']==target and row['mode']=='approve' and row['expires_at'] is None
    assert await h.pg.fetchval('SELECT project_id FROM conversations WHERE id=$1',uuid.UUID(h.conversation))==target
    assert (h.root/'old.txt').read_text()=='untouched'
    assert await allow(h,proposed,'same-click')==result
    assert await h.pg.fetchval("SELECT count(*) FROM projects WHERE name='New project'")==1
    root=h.service.workspace(h.auth,row)
    (root/'new.txt').write_text('new')
    listing=await h.service.command(h.auth,'workspace.browse',{'project_id':str(target)})
    assert [e['name'] for e in listing['entries']]==['new.txt']
    assert (await h.service.command(h.auth,'builder.preview',{'conversation_id':h.conversation,'path':'new.txt'}))['size']==3
    assert not (await h.service.tool(h.auth,h.conversation,'checkpoint_list',{}))['items']
    assert (await h.service.command(h.auth,'builder.mode.get',{'conversation_id':h.conversation}))['workspace_receipt']['project_id']==str(target)


async def test_failed_switch_rolls_back_binding_mode_and_execution_receipt(builder,monkeypatch):
    h=builder
    proposal=await create(h)
    original=h.service.store.mode
    from core.execution import workbench
    async def fail(*_):raise ExecutionError('synthetic_registration_failure')
    monkeypatch.setattr(workbench,'ensure_workspace',fail)
    with pytest.raises(ExecutionError,match='synthetic_registration_failure'):
        await allow(h,proposal)
    assert (await original(h.auth,h.conversation))['project_id']==h.project
    assert not await h.pg.fetchval("SELECT id FROM projects WHERE name='New project'")
    record=await h.service.store.approval(h.auth,proposal['approval']['approval_id'])
    assert record['execution_state']=='pending' and record['result'] is None


async def test_yolo_create_resets_to_approve_and_mount_old_requires_approval(builder):
    h=builder
    await mode(h,'yolo',yolo_acknowledged=True)
    result=await h.service.tool(h.auth,h.conversation,'workspace_create',{'name':'Yolo target'})
    assert result['mode']=='approve'
    current=await h.service.store.mode(h.auth,h.conversation)
    assert current['project_id']!=h.project and current['mode']=='approve'
    await mode(h,'suggest')
    mount=await h.service.tool(h.auth,h.conversation,'workspace_mount',{'project_id':str(h.project)})
    assert mount['approval']['kind']=='workspace' and mount['approval']['detail']['action']=='mount'
    assert (await h.service.store.mode(h.auth,h.conversation))['project_id']==current['project_id']
    await allow(h,mount)
    assert (await h.service.store.mode(h.auth,h.conversation))['project_id']==h.project


async def test_old_approvals_expire_and_cross_lane_mount_browse_refuse(builder):
    h=builder
    await mode(h,'suggest')
    old=await h.service.tool(h.auth,h.conversation,'fs_write',{'path':'old.txt','content':'bad'})
    proposal=await create(h)
    await allow(h,proposal)
    assert (await h.service.store.approval(h.auth,old['approval']['approval_id']))['state']=='expired'
    from dataclasses import replace
    foreign=replace(h.auth,owner='foreign')
    for auth in (foreign, replace(h.auth,lane='foreign')):
        # Another lane cannot load this approval. A registered root is also
        # lane-local; the service's lane is vouched by the execution API.
        with pytest.raises(ExecutionError):
            await h.service.store.approval(auth,proposal['approval']['approval_id'])
    with pytest.raises(ExecutionError):
        await h.service.command(foreign,'workspace.browse',{'project_id':str(h.project)})


async def test_first_workspace_for_unbound_conversation(builder):
    h=builder
    conversation=uuid.uuid4()
    await h.pg.execute("INSERT INTO conversations(id,user_id,face_id) VALUES($1,'owner','builder')",conversation)
    # Suggest avoids waiting for the owner; all four modes share the same admission.
    await h.service.store.mode(h.auth,str(conversation))
    await h.pg.execute("UPDATE builder_modes SET mode='suggest' WHERE conversation_id=$1",conversation)
    result=await h.service.tool(h.auth,str(conversation),'workspace_create',{'name':'First'})
    assert result['approval']['detail']['from_project_id'] is None
    approved=await allow(h,result)
    assert (await h.service.store.mode(h.auth,str(conversation)))['project_id']==uuid.UUID(approved['result']['project_id'])
