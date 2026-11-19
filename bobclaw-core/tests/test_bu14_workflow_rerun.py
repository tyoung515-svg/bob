"""BU14: ended-run renewal, real stage loop and registered workspace checks.

Models and PostgreSQL transport use the existing deterministic harness. Workspace
admission/creation below uses WorkflowRuntime and real temporary lane files.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest

from core.builder.service import Builder
from core.execution.models import ExecutionError
from core.execution.workflows import WorkflowRuntime, TERMINAL, WORKSPACE_REUSE_NOTICE, utc
from test_workflow_authority import call, start
from test_workflow_controller import harness, drive, wait_for


REAL_WORKSPACE = Builder.workspace


@pytest.fixture(autouse=True)
def mock_redis():
    yield


def source(h, row):
    p_id = h.store.record(row['id'])['run']['proposal_id']
    return deepcopy(h.store.data[h.auth.lane,h.auth.owner,'proposal',p_id])


async def again(h, row, auth=None):
    return (await call(h.service,auth or h.auth,'propose_again',
        proposal_id=source(h,row)['proposal']['id']))['proposal']


@pytest.mark.parametrize('outcome',['completed','failed','stopped','cancelled','expired','blocked','unknown'])
async def test_ended_run_renews_with_lineage_and_separate_start(harness,outcome):
    h = harness
    h.bad_review = outcome == 'failed'
    if outcome == 'stopped': h.hang = 'after-write'
    row = await h.new()
    if outcome in {'completed','failed'}:
        ended = await drive(h,row)
    elif outcome == 'stopped':
        task = asyncio.create_task(h.controller.tick(row))
        try:
            await wait_for(h,row,lambda w:any(s['state']=='running' and '/build' in s['key']
                and s.get('receipts') for s in w['stages']))
            await call(h.service,h.auth,'stop',run_id=row['id'],request_key='bu14-stop')
            await asyncio.wait_for(task,5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
        ended = h.store.record(row['id'])['run']
    else:
        # Explicit cancelled records are supported by the existing SQL enum.
        # A cancelled controller task itself is recovery, not a terminal run.
        await h.controller.terminal(row,outcome,'owner_stopped' if outcome=='cancelled' else None)
        await h.controller.tick(row)
        ended = h.store.record(row['id'])['run']
    assert ended['status'] == outcome and outcome in TERMINAL
    old = source(h,row)
    before_run = h.store.record(row['id'])
    before_events = deepcopy(h.store.events)
    before_starts, before_calls = h.store.starts, len(h.calls)
    original_clock = h.service.clock()
    h.service.clock = lambda:original_clock+timedelta(days=2)
    fresh = await again(h,row)
    assert fresh['id'] != old['proposal']['id']
    assert fresh['planned_run_id'] != row['id']
    assert fresh['state']=='proposed' and fresh['run_id'] is None
    assert fresh['previous_proposal_id']==old['proposal']['id']
    assert utc(fresh['expires_at'])==h.service.clock()+timedelta(minutes=15)
    assert utc(fresh['created_at'])==h.service.clock()
    assert fresh['coverage']['inputs']==old['args']['inputs']
    assert fresh['coverage']['bindings']==old['proposal']['coverage']['bindings']
    assert fresh['coverage']['run_mode']==old['proposal']['coverage']['run_mode']
    assert fresh['coverage']['authority']==old['proposal']['coverage']['authority']
    assert fresh['coverage']['budget']==old['proposal']['coverage']['budget']
    assert fresh['coverage']['review']==old['proposal']['coverage']['review']
    assert WORKSPACE_REUSE_NOTICE in fresh['reasons']
    saved = h.store.data[h.auth.lane,h.auth.owner,'proposal',fresh['id']]
    assert 'request_key' not in saved['args'] and 'grant_id' not in saved
    assert utc(saved['args']['expires_at'])-h.service.clock()==utc(old['args']['expires_at'])-utc(old['proposal']['created_at'])
    after = source(h,row)
    assert after['proposal']['state']=='granted'
    assert after['proposal']['run_id']==row['id'] and after['grant_id']==old['grant_id']
    assert after['proposal']['proposed_again_ids']==[fresh['id']]
    assert after['frozen']==old['frozen']
    assert h.store.record(row['id'])==before_run and h.store.events==before_events
    assert (h.store.starts,len(h.calls))==(before_starts,before_calls)
    # The unchanged owner gate is still required and issues a distinct grant.
    with pytest.raises(ExecutionError,match='not_found'):
        await start(h.service,h.auth,fresh,key='bu14-no-owner-gate')
    started = (await start(h.service,h.click,fresh,key='bu14-fresh-start'))['run']
    assert started['id']==fresh['planned_run_id'] and started['proposal_id']==fresh['id']
    assert started['grant']['id'] != ended['grant']['id']
    assert h.store.starts==before_starts+1


@pytest.mark.parametrize('status',['accepted','queued','running','recovering','pausing','paused','needs_you','stopping','cancelling'])
async def test_active_run_is_refused_with_plain_sentence(harness,status):
    h = harness; row = await h.new()
    async def state(c,r):
        r['run']['status'] = status
    await h.controller.change(row,state)
    before = deepcopy(h.store.data)
    with pytest.raises(ExecutionError,match='run_still_active') as exc:
        await again(h,row)
    assert exc.value.detail=='This run is still going. Wait for it to end before asking to run it again.'
    assert h.store.data==before and h.store.starts==1 and not h.calls


async def test_owner_lane_and_source_run_scope(harness):
    h = harness; row = await h.new()
    await h.controller.terminal(row,'failed')
    p_id = source(h,row)['proposal']['id']
    for other,code in [(replace(h.auth,owner='other',actor='other'),'proposal_not_found'),
                       (replace(h.auth,lane='other'),'not_found'),
                       (replace(h.auth,token_type='agent'),'proposal_again_not_permitted'),
                       (replace(h.auth,actor='other'),'proposal_again_not_permitted')]:
        with pytest.raises(ExecutionError,match=code):
            await call(h.service,other,'propose_again',proposal_id=p_id)
    for op,args in [('run',{'run_id':row['id']}),('runs',{})]:
        other = replace(h.auth,owner='other',actor='other')
        if op=='run':
            with pytest.raises(ExecutionError,match='not_found'):
                await call(h.service,other,op,**args)
        else:
            assert (await call(h.service,other,op,**args))['items']==[]
    # Even a visible source proposal cannot authorize another owner's run.
    foreign = h.store.data.pop((h.auth.lane,h.auth.owner,'run',row['id']))
    h.store.data[h.auth.lane,'other','run',row['id']] = foreign
    with pytest.raises(ExecutionError,match='run_not_found') as exc:
        await call(h.service,h.auth,'propose_again',proposal_id=p_id)
    assert exc.value.detail=='The earlier run was not found for the signed-in owner.'


async def test_repeated_and_concurrent_clicks_share_only_an_open_proposal(harness):
    h = harness; row = await h.new()
    await h.controller.terminal(row,'failed')
    replies = await asyncio.gather(again(h,row),again(h,row),again(h,row))
    fresh = replies[0]
    assert all(p==fresh for p in replies)
    assert source(h,row)['proposal']['proposed_again_ids']==[fresh['id']]
    assert h.store.starts==1 and not h.calls
    now = h.service.clock()
    h.service.clock = lambda:now+timedelta(minutes=16)
    second = await again(h,row)
    assert second['id']!=fresh['id']
    await start(h.service,h.click,second,key='bu14-second-start')
    third = await again(h,row)
    assert third['id'] not in {fresh['id'],second['id']}
    assert source(h,row)['proposal']['proposed_again_ids']==[fresh['id'],second['id'],third['id']]
    assert h.store.starts==2


async def test_rerun_rechecks_models_and_requires_same_model_choice(harness):
    h = harness; row = await h.new(same=True)
    await h.controller.terminal(row,'failed')
    fresh = await again(h,row)
    assert not fresh['runnable'] and fresh['coverage']['review']['same_model_choice_required']
    assert WORKSPACE_REUSE_NOTICE in fresh['reasons']
    with pytest.raises(ExecutionError,match='same_model_review_choice_required'):
        await start(h.service,h.click,fresh,key='bu14-choice-required')
    result = await start(h.service,h.click,fresh,key='bu14-choice',allow_same_model_review=True)
    assert result['run']['review']['downgraded']
    bindings = h.service.runtime.bindings
    async def unavailable(auth):
        return [{**b,'runnable':False} for b in await bindings(auth)]
    h.service.runtime.bindings = unavailable
    before = deepcopy(h.store.data)
    with pytest.raises(ExecutionError,match='proposal_recheck_failed') as exc:
        await again(h,row)
    assert 'selected model is unavailable' in exc.value.detail
    assert h.store.data==before and h.store.starts==2


async def test_run_reads_expose_durable_source_without_proposal_cache(harness):
    h = harness; row = await h.new()
    p_id = source(h,row)['proposal']['id']
    assert (await call(h.service,h.auth,'run',run_id=row['id']))['run']['proposal_id']==p_id
    assert (await call(h.service,h.auth,'runs'))['items'][0]['proposal_id']==p_id


@pytest.fixture
def registered(harness,monkeypatch,tmp_path):
    h = harness; db = h.store.pool
    projects = {}
    (tmp_path/'projects').mkdir()
    monkeypatch.setattr('core.tool_jobs.service.lane_root',lambda:tmp_path)
    monkeypatch.setattr(Builder,'workspace',REAL_WORKSPACE)
    execute, fetchrow = db.execute, db.fetchrow
    async def with_projects(sql,*args):
        if 'INSERT INTO projects' in sql:
            # Catch an accidental repeat creation under the same display name.
            assert not any(p['name']==args[2] for p in projects.values())
            projects[str(args[0])] = {'owner':args[1],'name':args[2]}
            return 'INSERT 1'
        return await execute(sql,*args)
    async def read_projects(sql,*args):
        if 'SELECT id FROM projects' in sql:
            p = projects.get(str(args[0]))
            return {'id':args[0]} if p and p['owner']==args[1] else None
        return await fetchrow(sql,*args)
    monkeypatch.setattr(db,'execute',with_projects)
    monkeypatch.setattr(db,'fetchrow',read_projects)
    class RegisteredRuntime(type(h.service.runtime)):
        resolve = WorkflowRuntime.resolve
        async def create_workspace(self,c,auth,record):
            await WorkflowRuntime.create_workspace(self,c,auth,record)
            if record['args'].get('workspace'):
                h.roots[record['planned_project']] = tmp_path/'projects'/record['planned_project']
    runtime = RegisteredRuntime(h.store,tmp_path,h.service.runtime.registry)
    runtime.admission = SimpleNamespace(store=h.store)
    h.service.runtime = h.controller.service.runtime = runtime
    async def new():
        from core.execution.workflows import PREMADES
        d = deepcopy(PREMADES['premade-plan-build-review-fix'])
        p = (await call(h.service,h.auth,'propose',definition=d,inputs={'task':'Keep earlier files.'},
            bindings={alias:str(i) for i,alias in enumerate(d['agents'])},workspace={'create':'Same workspace name'},
            expires_at=(h.service.clock()+timedelta(hours=2)).isoformat(),request_key=uuid.uuid4().hex))['proposal']
        w = (await start(h.service,h.click,p,key=uuid.uuid4().hex))['run']
        record = h.store.record(w['id'])
        row = {'lane':'lane','owner':'owner','id':w['id'],'generation':1,'controller':'test',
            'run_authority':record['initiator'],'boundary':record['boundary'],
            'snapshot':{'team':{'topology':'workflow/v1'}},'topology':'workflow/v1'}
        h.store.generations[w['id']] = 1
        return row
    return h,projects,new


@pytest.mark.parametrize('legacy',[False,True])
async def test_created_workspace_reused_with_real_registration_and_files(registered,legacy):
    h,projects,new = registered
    row = await new()
    ended = await drive(h,row)
    assert ended['status']=='completed',ended['reason']
    folder = h.roots[ended['project_id']]
    files = {p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    assert files
    if legacy:
        # All pre-BU14 runs were also persisted after synchronous creation.
        h.store.data[h.auth.lane,h.auth.owner,'run',row['id']].pop('workspace_created')
    fresh = await again(h,row)
    assert fresh['coverage']['project_id']==ended['project_id']
    assert set(fresh['coverage']['workspace'])=={'registration_digest','path_label'}
    assert 'create' not in fresh['coverage']['workspace']
    saved = h.store.data[h.auth.lane,h.auth.owner,'proposal',fresh['id']]
    assert 'workspace' not in saved['args'] and saved['planned_project'] is None
    assert saved['args']['project_id']==ended['project_id']
    assert WORKSPACE_REUSE_NOTICE in fresh['reasons'] and fresh['runnable']
    assert {p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()}==files
    await start(h.service,h.click,fresh,key='bu14-reuse-start')
    assert len(projects)==1 and len(h.roots)==1


@pytest.mark.parametrize('deleted',['project','registration','directory','foreign_project','foreign_registration'])
async def test_deleted_or_reassigned_workspace_refused(registered,deleted):
    from core.portal_resources import load_folders
    h,projects,new = registered; row = await new()
    await h.controller.terminal(row,'failed')
    project = h.store.record(row['id'])['run']['project_id']
    registry = h.service.runtime.root/'project-folders.json'
    if deleted=='project': projects.pop(project)
    elif deleted=='foreign_project': projects[project]['owner']='other'
    elif deleted=='directory': h.roots[project].rmdir()
    elif deleted=='registration': registry.write_text('[]',encoding='utf-8')
    else:
        import json
        folders = load_folders(h.service.runtime.root)
        folders[0]['owner']='other'
        registry.write_text(json.dumps(folders),encoding='utf-8')
    before = deepcopy(h.store.data)
    with pytest.raises(ExecutionError,match='workspace_not_found') as exc:
        await again(h,row)
    assert exc.value.detail=='The workspace from the earlier run is no longer available. Choose another workspace before trying again.'
    assert h.store.data==before and h.store.starts==1 and not h.calls


@pytest.mark.parametrize('marker',[False,None])
async def test_ended_before_creation_proposes_creation_again(registered,marker):
    h,projects,new = registered; row = await new()
    await h.controller.terminal(row,'failed')
    # Start currently creates synchronously, so seed a pre-creation receipt to
    # cover older/imported records rather than claim the stage loop creates it.
    record = h.store.data[h.auth.lane,h.auth.owner,'run',row['id']]
    project = record['run']['project_id']
    h.roots.pop(project).rmdir()
    projects.pop(project)
    (h.service.runtime.root/'project-folders.json').write_text('[]',encoding='utf-8')
    record['run']['project_id'] = None
    if marker is False: record['workspace_created'] = False
    else: record.pop('workspace_created')
    fresh = await again(h,row)
    saved = h.store.data[h.auth.lane,h.auth.owner,'proposal',fresh['id']]
    assert saved['args']['workspace']=={'create':'Same workspace name'}
    assert not saved['args'].get('project_id') and saved['planned_project']!=project
    assert fresh['coverage']['workspace']['create']=='Same workspace name'
    assert fresh['coverage']['workspace']['project_id']==saved['planned_project']
    assert WORKSPACE_REUSE_NOTICE not in fresh['reasons']
    assert not projects and not h.roots and h.store.starts==1 and not h.calls
