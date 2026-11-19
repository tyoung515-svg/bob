"""Real temporary Postgres gate; skips are explicitly not storage passes.

Models and worker transport are deterministic fixtures. Real Landlock/process
acceptance is separate; this suite checks transactional authority and recovery.
"""
import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from tests.test_durable_runs_v2 import pg,MODEL,TEAM
from core.execution.admission import Admission
from core.execution.executor import Executor
from core.execution.models import Authority,ExecutionError,canonical,digest
from core.execution.store import Store
from core.execution.workspaces import snapshot,proposal
from core.execution.worker_queue import WorkerQueue
from core.execution.job_store import JobStore
from core.worker.contract import profile_revision
from core.tool_jobs import service as domain


@pytest.fixture
async def jobs_api(pg,tmp_path,monkeypatch):
    monkeypatch.setenv('BOB_LANE_ROOT',str(tmp_path))
    monkeypatch.delenv('BOB_DEFAULT_OWNER',raising=False)
    monkeypatch.delenv('BOB_SETTINGS_ID',raising=False)
    project=tmp_path/'projects'/'source';project.mkdir(parents=True);(project/'file.txt').write_text('registered base')
    runs=tmp_path/'runs';runs.mkdir()
    from core.portal_resources import register_project_folder
    pid=str(uuid.uuid4())
    await pg.execute('INSERT INTO projects(id,user_id) VALUES($1,$2)',uuid.UUID(pid),'owner')
    folder=register_project_folder(tmp_path,'owner',pid,'projects/source','Fixture')
    cfg={'run_workspace_root':str(runs),'worker_gid':os.getgid() if hasattr(os,'getgid') else 0,
         'profiles':{'proposal':{'argv':['/installed/python','-m','core.tool_jobs.runner'],'network':False}}}
    revision=profile_revision(cfg['profiles']['proposal'])
    from core.worker import client
    monkeypatch.setattr(client,'configuration',lambda:cfg)
    monkeypatch.setattr(domain,'capabilities',AsyncMock(return_value={'operations':{'jobs.start':{'available':True}}}))
    store=Store(pg,'lane');await store.initialize()
    admission=Admission(store,AsyncMock(return_value=MODEL))
    auth=Authority('lane','owner','owner',{'may_touch':[str(tmp_path)+'*']},'human')
    await store.save_team(auth,TEAM,None,'save',{})
    scope={'profiles':{'proposal':revision},'workspaces':{'projects':['source']},'attachments':[folder['attachment_id']],
           'max_job_attempts':4,'job_seconds_budget':1800,'allow_cli':False}
    args={'contract_version':2,'role':'proposer','task':'Inspect the registered evidence','attachment':folder['attachment_id'],
          'request_key':'job-start','team':TEAM['id'],'team_revision':1,
          'budget':{'start_calls':10,'wall_mode':'fixed','wall_seconds':120,'max_job_attempts':4,'job_seconds_budget':1800},
          'job_policy':{'proposed_seconds':900,'rationale':'bounded report','auto_escalate':False},'job_coverage':scope}
    jobs=domain.Jobs(admission)
    return SimpleNamespace(pg=pg,jobs=jobs,store=store,admission=admission,auth=auth,args=args,project=project,cfg=cfg,revision=revision)


async def freeze_fixture(h,started):
    row=await h.jobs.row(h.auth,started['id'])
    async with h.store.transaction() as c:
        base=await h.jobs.frozen(c,row,'base')
        target=Path(h.cfg['run_workspace_root'])/row['id']
        (target/'BOB-PROPOSAL.md').write_text('immutable proposal')
        out=snapshot(target)
        prop=proposal(base,out,{'job_id':row['id'],'project_id':row['project_id']})
        await h.jobs.save_snapshot(c,row,'output',out)
        await c.execute('INSERT INTO job_proposals VALUES($1,$2,$3,$4,$5::jsonb)',h.auth.lane,h.auth.owner,row['id'],prop['digest'],canonical(prop))
        sha=await h.jobs.blob(c,row,canonical(prop).encode())
        filename='proposal-'+row['id']+'.json'
        await c.execute('INSERT INTO tool_job_artifacts VALUES($1,$2,$3,$4,$5)',h.auth.lane,h.auth.owner,row['id'],filename,sha)
        await c.execute('UPDATE tool_jobs SET output_digest=$4 WHERE lane=$1 AND owner=$2 AND id=$3',h.auth.lane,h.auth.owner,row['id'],out['digest'])
    return row,prop,filename


@pytest.mark.asyncio
async def test_start_replay_conflict_and_base_digest_binding(jobs_api):
    h=jobs_api
    a=await h.jobs.command(h.auth,'jobs.start',h.args)
    b=await h.jobs.command(h.auth,'jobs.start',h.args)
    assert a['id']==b['id'] and a['operation_id']==b['operation_id'] and b['replayed']
    assert a['base_digest']==snapshot(h.project)['digest']
    with pytest.raises(ExecutionError,match='idempotency_conflict'):
        await h.jobs.start(h.auth,{**h.args,'task':'different'})
    assert await h.pg.fetchval('SELECT count(*) FROM tool_jobs')==1
    assert await h.pg.fetchval('SELECT count(*) FROM agent_runs')==1


@pytest.mark.asyncio
async def test_immutable_artifact_bounded_read_and_stale_base(jobs_api):
    h=jobs_api;started=await h.jobs.start(h.auth,h.args)
    row,prop,name=await freeze_fixture(h,started)
    first=await h.jobs.command(h.auth,'jobs.artifact',{'job_id':row['id'],'filename':name,'max_bytes':10})
    assert first['truncated'] and len(base64.b64decode(first['content']))==10 and first['next_offset']==10
    data=await h.pg.fetchval('SELECT content FROM tool_job_blobs WHERE sha256=$1',first['sha256'])
    assert hashlib.sha256(data).hexdigest()==first['sha256']
    (Path(h.cfg['run_workspace_root'])/row['id']/'BOB-PROPOSAL.md').write_text('changed after freeze')
    assert (await h.jobs.command(h.auth,'jobs.artifact',{'job_id':row['id'],'filename':name,'max_bytes':10}))==first
    for table in ['tool_job_blobs','tool_job_snapshots','tool_job_artifacts','job_proposals']:
        with pytest.raises(Exception,match='immutable tool job evidence'):
            await h.pg.execute('DELETE FROM '+table)
    (h.project/'file.txt').write_text('changed registered base')
    view=await h.jobs.command(h.auth,'jobs.proposal',{'job_id':row['id']})
    assert view['digest']==prop['digest'] and 'base' in view['stale_paths'] and not view['applicable']


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['jobs.audit','jobs.cancel','jobs.proposal','jobs.artifact','jobs.decide'])
@pytest.mark.parametrize('change',[{'owner':'other'},{'lane':'other'},{'actor':'other','token_type':'agent'}])
async def test_all_nested_operations_hide_foreign_jobs(jobs_api,operation,change):
    h=jobs_api;started=await h.jobs.start(h.auth,h.args)
    foreign=replace(h.auth,**change)
    with pytest.raises(ExecutionError) as exc:
        await h.jobs.command(foreign,operation,{'job_id':started['id'],'filename':'proposal-'+started['id']+'.json'})
    assert exc.value.status==404


@pytest.mark.asyncio
async def test_foreign_project_and_artifact_denials(jobs_api):
    h=jobs_api;started=await h.jobs.start(h.auth,h.args)
    row,_,name=await freeze_fixture(h,started)
    second=await h.jobs.start(h.auth,{**h.args,'request_key':'second'})
    with pytest.raises(ExecutionError) as exc:
        await h.jobs.command(h.auth,'jobs.artifact',{'job_id':second['id'],'filename':name})
    assert exc.value.status==404
    await h.pg.execute('UPDATE projects SET user_id=$1','other')
    for op,args in [('jobs.audit',{'job_id':row['id']}),('jobs.start',{**h.args,'request_key':'third'})]:
        with pytest.raises(ExecutionError) as exc: await h.jobs.command(h.auth,op,args)
        assert exc.value.status==404


@pytest.mark.asyncio
async def test_owner_decision_is_digest_revision_bound_never_applies(jobs_api,monkeypatch):
    h=jobs_api;started=await h.jobs.start(h.auth,h.args)
    row,prop,_=await freeze_fixture(h,started)
    owner=replace(h.auth,session_kind='owner_stepup',audience='bob-owner-controls',allowed_operations=('jobs.decide',))
    from core.execution import owner as controls
    original=controls.require_owner
    async def check(auth,op):
        if auth!=owner: raise ExecutionError('not_found',404)
    monkeypatch.setattr(controls,'require_owner',check)
    args={'job_id':row['id'],'decision':'approve','proposal_digest':prop['digest'],'expected_revision':1,'request_key':'decision'}
    with pytest.raises(ExecutionError): await h.jobs.command(h.auth,'jobs.decide',args)
    with pytest.raises(ExecutionError,match='proposal_digest_conflict'):
        await h.jobs.command(owner,'jobs.decide',{**args,'proposal_digest':'f'*64})
    result=await h.jobs.command(owner,'jobs.decide',args)
    assert result['decision']['state']=='approved' and result['decision']['applied'] is False
    assert '4.3e' in result['decision']['apply_reason']
    replay=await h.jobs.command(owner,'jobs.decide',args)
    assert replay['operation_id']==result['operation_id'] and replay['replayed']
    with pytest.raises(ExecutionError,match='idempotency_conflict'):
        await h.jobs.command(owner,'jobs.decide',{**args,'decision':'deny'})
    with pytest.raises(ExecutionError,match='revision_conflict'):
        await h.jobs.command(owner,'jobs.decide',{**args,'request_key':'another'})
    assert (h.project/'file.txt').read_text()=='registered base' and not (h.project/'BOB-PROPOSAL.md').exists()


class ReportTransport:
    def __init__(self,h): self.h=h;self.receipts={};self.launches=0
    def hello(self): return {'profiles':{'proposal':self.h.revision},'lane_generation':0,'active_execution':None,'max_timeout':3600}
    def control(self,value):
        if value['op']=='inspect': return {'receipt':self.receipts.get(value['execution_id'])}
        return {'type':'idle_barrier','cleanup':'idle_proved','daemon_epoch':'fixture',
                'lane_generation':value['lane_generation'],'controller_session':value['controller_session']}
    def submit(self,request,stop):
        self.launches+=1
        value=json.loads(request['input'])
        target=Path(self.h.cfg['run_workspace_root'])/request['workspace'][4:]
        if value['role']=='proposer': (target/'BOB-PROPOSAL.md').write_text(value['content'])
        receipt={k:request[k] for k in ('execution_id','request_digest','lane_generation','controller_session')}
        receipt.update(type='result',daemon_epoch='fixture',outcome='succeeded',cleanup='idle_proved',returncode=0,
                       execution_seconds=.01,cleanup_seconds=0,stdout_b64='',stderr_b64='')
        self.receipts[request['execution_id']]=receipt
        return receipt


@pytest.mark.asyncio
async def test_model_run_and_worker_share_existing_queue_and_survive_readback(jobs_api):
    h=jobs_api;started=await h.jobs.start(h.auth,h.args)
    worker=JobStore(h.pg,'lane');await worker.initialize()
    transport=ReportTransport(h);queue=WorkerQueue(worker,transport,poll=.01);queue.start()
    async def model(binding,prompt):
        assert 'registered base' in prompt
        return {'text':'{"tasks":["review base"]}' if 'Return only JSON' in prompt else 'bounded proposal report','usage':{}}
    executor=Executor(h.store,h.admission,model,poll=.01)
    try:
        run=await h.store.claim(executor.controller)
        await asyncio.wait_for(executor.execute(run),10)
        audit=await h.jobs.command(h.auth,'jobs.audit',{'job_id':started['id']})
        assert audit['status']=='completed',audit
        assert audit['receipts']['model_attempts']==4 and audit['receipts']['tool_calls']==1
        assert transport.launches==1 and audit['base_digest']!=audit['output_digest']
        assert audit['proposal']['files'][0]['path']=='BOB-PROPOSAL.md'
        assert not (h.project/'BOB-PROPOSAL.md').exists()
        assert (await h.jobs.command(h.auth,'jobs.list',{}))['items'][0]['id']==started['id']
        assert (await h.jobs.start(h.auth,h.args))['id']==started['id'] and transport.launches==1
    finally: await queue.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('claimed', [False, True], ids=['queued', 'claimed'])
async def test_agent_requires_reviewed_grant_and_cannot_decide(jobs_api, claimed):
    h=jobs_api
    agent=replace(h.auth,actor='agent-1',token_type='agent')
    team={**TEAM,'id':'agent-team'}
    await h.store.save_team(agent,team,None,'agent-team',{})
    args={**h.args,'team':team['id']}
    with pytest.raises(ExecutionError) as exc: await h.jobs.start(agent,args)
    assert exc.value.status==404 and await h.pg.fetchval('SELECT count(*) FROM tool_jobs')==0
    from core.execution.models import start_request
    request=start_request({k:v for k,v in args.items() if k in {'contract_version','team','team_revision','task','request_key','budget','job_coverage'}} | {'project_id':str(await h.pg.fetchval('SELECT id FROM projects'))})
    pinned=await h.store.team(agent,team['id'],1)
    model_snapshot={'team':pinned,'models':MODEL,'review_independence':'same_model'}
    prepared=await h.store.prepare_grant(agent,{'request_key':'prepare-agent'},request,model_snapshot)
    await h.store.issue_grant(h.auth,{'proposal_id':prepared['proposal_id'],'proposal_revision':1,'expected_revision':None,
        'request_key':'issue-agent','coverage_kind':'project','allow_long_runs':False,'auto_escalate':False,'call_ceiling':10,'expires_at':None})
    started=await h.jobs.start(agent,args)
    assert (await h.jobs.command(agent,'jobs.audit',{'job_id':started['id']}))['id']==started['id']
    assert (await h.jobs.command(agent,'jobs.list',{}))['items'][0]['id']==started['id']
    assert (await h.jobs.command(agent,'jobs.attachments',{}))['items'][0]['id']==args['attachment']
    with pytest.raises(ExecutionError) as exc:
        await h.jobs.command(agent,'jobs.decide',{'job_id':started['id'],'decision':'approve'})
    assert exc.value.status==404
    if claimed:
        run=await h.store.claim('test-controller')
        assert run is not None and run['id']==started['run_id']
    # Unclaimed work has no transport/cleanup to await. Claimed work must keep
    # cancellation intent until its controller acknowledges it.
    expected='cancelling' if claimed else 'cancelled'
    first=await h.jobs.command(agent,'jobs.cancel',{'job_id':started['id']})
    second=await h.jobs.command(agent,'jobs.cancel',{'job_id':started['id']})
    assert first['operation_id']==second['operation_id']
    assert first['status']==second['status']==expected
    assert first['terminal']==second['terminal']==(not claimed)
    audit=await h.jobs.command(agent,'jobs.audit',{'job_id':started['id']})
    assert audit['status']==expected and audit['terminal']==(not claimed)
    assert await h.pg.fetchval('SELECT status FROM agent_runs WHERE id=$1',started['run_id'])==expected
    await h.pg.execute('UPDATE agent_execution_grants SET revoked=TRUE')
    with pytest.raises(ExecutionError) as exc: await h.jobs.command(agent,'jobs.audit',{'job_id':started['id']})
    assert exc.value.status==404


@pytest.mark.asyncio
async def test_stale_approval_and_changed_identity_are_rejected(jobs_api,monkeypatch):
    h=jobs_api;started=await h.jobs.start(h.auth,h.args);row,prop,_=await freeze_fixture(h,started)
    from core.execution import owner as controls
    monkeypatch.setattr(controls,'require_owner',AsyncMock())
    (h.project/'file.txt').write_text('new base')
    with pytest.raises(ExecutionError,match='proposal_stale'):
        await h.jobs.command(h.auth,'jobs.decide',{'job_id':row['id'],'decision':'approve','proposal_digest':prop['digest'],
                            'expected_revision':1,'request_key':'stale'})
    with pytest.raises(Exception,match='immutable tool job identity'):
        await h.pg.execute('UPDATE tool_jobs SET base_digest=$1','f'*64)
    assert await h.pg.fetchval('SELECT count(*) FROM job_decisions')==0
