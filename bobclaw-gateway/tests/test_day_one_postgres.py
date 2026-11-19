"""Real disposable PostgreSQL + actual edits/tests. Model and daemon transport are fixtures."""
import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import time
from pathlib import Path
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_durable_runs_v2 import pg
from tests.test_tool_jobs_postgres import jobs_api
from tests.test_checked_edits_postgres import checked_api
from core.execution.models import ExecutionError, canonical
from core.execution.workbench import Workbench
from core.execution.workspaces import snapshot
from core.execution.executor import Executor
from core.execution.job_store import JobStore
from core.execution.worker_queue import WorkerQueue
from core.tool_jobs.checked_runner import execute


@pytest.fixture
async def work(checked_api,monkeypatch):
    h=checked_api
    folders=json.loads((h.project.parents[1]/'project-folders.json').read_text())
    h.pid=folders[0]['project_id']
    target=h.project.parent/h.pid;h.project.rename(target);h.project=target
    folders[0]['path']='projects/'+h.pid
    folders[0]['checked_edits']={'checks':['python.compile','python.unittest'],'write_paths':['app']}
    (target.parents[1]/'project-folders.json').write_text(json.dumps(folders))
    h.cfg['workspaces']={'projects':str(target.parent)}
    import core.lane_config as lane
    monkeypatch.setattr(lane,'get_lane',lambda:SimpleNamespace(default_backend='deepseek_v4_flash'))
    from core.execution import owner
    async def require_owner(auth,op):
        if auth.session_kind!='owner_stepup' or op not in auth.allowed_operations:raise ExecutionError('not_found',404)
    monkeypatch.setattr(owner,'require_owner',require_owner)
    monkeypatch.setattr(owner,'job_time_capabilities',AsyncMock(return_value={'available':True,'profile_revisions':{'checked':h.revision}}))
    h.owner=replace(h.auth,session_kind='owner_stepup',audience='bob-owner-controls',allowed_operations=('work.publish','work.workspace.create'))
    h.work=Workbench(h.admission)
    await h.pg.execute("ALTER TABLE projects ADD COLUMN name TEXT DEFAULT 'Fixture', ADD COLUMN updated_at TIMESTAMPTZ DEFAULT now()")
    h.calls=[]
    h.content='def add(a,b): return a+b\n'
    return h


class Transport:
    def __init__(self,h):self.h=h;self.receipts={};self.launches=[]
    def hello(self):return {'profiles':{'checked':self.h.revision},'lane_generation':0,'active_execution':None,'max_timeout':3600}
    def control(self,value):
        if value['op']=='inspect':return {'receipt':self.receipts.get(value['execution_id'])}
        return {'type':'idle_barrier','cleanup':'idle_proved','daemon_epoch':'fixture','lane_generation':value['lane_generation'],'controller_session':value['controller_session']}
    def submit(self,request,stop):
        self.launches.append(request['execution_id'])
        target=(Path(self.h.cfg['run_workspace_root'])/request['workspace'][4:] if request['workspace'].startswith('run_')
                else Path(self.h.cfg['workspaces'][request['workspace']])/request['cwd'])
        error='';out=''
        try:
            result=execute(json.loads(request['input']),target)
            out=canonical({'adapter':'checked-edits/1','result':result})+'\n'
        except Exception as exc:error=str(exc)
        receipt={k:request[k] for k in ('execution_id','request_digest','lane_generation','controller_session')}
        receipt.update(type='result',daemon_epoch='fixture',outcome='failed' if error else 'succeeded',cleanup='idle_proved',
            returncode=1 if error else 0,execution_seconds=.01,cleanup_seconds=0,stdout_b64=base64.b64encode(out.encode()).decode(),stderr_b64=base64.b64encode(error.encode()).decode())
        self.receipts[request['execution_id']]=receipt;return receipt


async def build(h,mode):
    if mode=='fixer':
        (h.project/'app').mkdir();(h.project/'app/calc.py').write_text('def add(a,b): return a-b\n')
        (h.project/'app/test_calc.py').write_text('import unittest\nfrom calc import add\nclass Cases(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n')
    args={'project_id':h.pid,'task':'Implement correct addition, add(2,3)=5, with a unittest.',
          'mode':mode,'recipe':'python-basic','request_key':'prepare-'+mode}
    prepared=await h.work.prepare(h.auth,args)
    assert prepared==await h.work.prepare(h.auth,args)
    # Session proof is a fixture; use the real grant matcher and exact intent.
    proposal=prepared['proposal']
    await h.store.issue_grant(h.owner,{'proposal_id':proposal['proposal_id'],'proposal_revision':proposal['revision'],
        'expected_revision':None,'request_key':'owner-grant-'+mode,'coverage_kind':'task','allow_long_runs':True,
        'auto_escalate':False,'call_ceiling':10,'expires_at':None,'allow_extended_jobs':False,
        'allow_custom_job_limit':False,'job_seconds_ceiling':900,'job_auto_escalate':False})
    started=await h.work.start(h.auth,{'preparation_id':prepared['preparation_id'],'request_key':'start-'+mode})
    async def model(binding,prompt):
        h.calls.append(prompt)
        files=snapshot(h.project);by={x['path']:x for x in files['manifest']}
        edits=[{'operation':'replace' if 'app/calc.py' in by else 'create','path':'app/calc.py',
                'pre_sha256':by.get('app/calc.py',{}).get('sha256'),'content':h.content}]
        if mode=='builder':edits.append({'operation':'create','path':'app/test_calc.py','pre_sha256':None,
            'content':'import unittest\nfrom calc import add\nclass Cases(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n'})
        return {'text':canonical({'edits':edits}),'usage':{}}
    worker=JobStore(h.pg,'lane');await worker.initialize()
    transport=Transport(h);queue=WorkerQueue(worker,transport,poll=.01);queue.start()
    executor=Executor(h.store,h.admission,model,poll=.01)
    try:
        run=await h.store.claim(executor.controller);assert run
        await asyncio.wait_for(executor.execute(run),20)
    finally:await queue.close()
    return started,transport


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['builder','fixer'])
async def test_direct_single_model_job_runs_real_checks_and_leaves_source_unpublished(work,mode):
    h=work;started,transport=await build(h,mode)
    view=await h.store.get(h.auth,started['run_id'])
    detail=await h.pg.fetch('SELECT payload FROM checked_step_receipts WHERE run_id=$1 ORDER BY ordinal',started['run_id'])
    assert view['run']['status']=='completed',(view['run'].get('reason'),[dict(r) for r in detail])
    assert len(h.calls)==1 and view['run']['calls_used']==1
    assert len(transport.launches)==(4 if mode=='fixer' else 3)
    prop=await h.jobs.command(h.auth,'jobs.proposal',{'job_id':started['id']})
    assert prop['checks_passed'] and len(prop['check_runs'])==2
    status=await h.work.command(h.auth,'work.status',{'job_id':started['id']})
    assert status['checked_proposal']['digest']==prop['digest'] and status['publication'] is None
    assert 'app/calc.py' in status['checked_proposal']['diff']
    assert not (h.project/'app/calc.py').exists() if mode=='builder' else 'a-b' in (h.project/'app/calc.py').read_text()
    if mode=='fixer':assert 'FAIL' in h.calls[0] and '"exit_status":1' in h.calls[0]


@pytest.mark.asyncio
async def test_recipe_revision_idempotency_and_foreign_preparation(work):
    h=work;args={'name':'my-pipe','checks':['python.compile','python.unittest'],'expected_revision':None,'request_key':'save-pipe'}
    result=await h.work.save_recipe(h.auth,args)
    assert result['recipe']['revision']==1 and not result['started']
    assert (await h.work.save_recipe(h.auth,args))['recipe']==result['recipe']
    with pytest.raises(ExecutionError,match='revision_conflict'):
        await h.work.save_recipe(h.auth,{**args,'request_key':'other'})
    p=await h.work.prepare(h.auth,{'project_id':h.pid,'task':'task','mode':'builder','recipe':'my-pipe','request_key':'prep'})
    catalog=await h.work.catalog(h.auth,{})
    assert catalog['recent_preparations'][0]['preparation_id']==p['preparation_id']
    assert any(r['id']=='my-pipe' and r['revision']==1 for r in catalog['recipes'])
    with pytest.raises(ExecutionError):await h.work.preparation(replace(h.auth,owner='foreign'),p['preparation_id'])
    with pytest.raises(ExecutionError):await h.work.preparation(replace(h.auth,actor='foreign',token_type='agent'),p['preparation_id'])


@pytest.mark.asyncio
async def test_durable_answer_is_owned_idempotent_and_in_history(work,monkeypatch):
    h=work
    from core.execution import chat_records
    from core.tools.ask import _PENDING
    monkeypatch.setattr(chat_records,'POOL',h.pg)
    await h.pg.execute('CREATE TABLE messages(id UUID PRIMARY KEY,conversation_id UUID,role TEXT,content TEXT,metadata JSONB)')
    cid=uuid.uuid4();await h.pg.execute('INSERT INTO conversations(id,user_id) VALUES($1,$2)',cid,'owner')
    import time
    await chat_records.question('q','owner',str(cid),'Which project?',[],time.time()+60)
    future=asyncio.get_running_loop().create_future();_PENDING['q']=future
    try:
        with pytest.raises(ExecutionError):await chat_records.accept({'question_id':'q','user_id':'foreign','answer':'yes'})
        first=await chat_records.accept({'question_id':'q','user_id':'owner','answer':'mine'})
        assert first['resolved'] and await future=='mine'
        assert (await chat_records.accept({'question_id':'q','user_id':'owner','answer':'mine'}))['replayed']
        with pytest.raises(ExecutionError):await chat_records.accept({'question_id':'q','user_id':'owner','answer':'changed'})
        assert await h.pg.fetchval('SELECT count(*) FROM messages')==1
        assert 'mine' in await h.pg.fetchval('SELECT content FROM messages')
    finally:_PENDING.pop('q',None)


@pytest.mark.asyncio
async def test_publication_real_queue_exact_files_replay_and_stale_conflict(work,monkeypatch):
    h=work;started,_=await build(h,'builder')
    prop=await h.jobs.command(h.auth,'jobs.proposal',{'job_id':started['id']})
    args={'job_id':started['id'],'proposal_digest':prop['digest'],'expected_revision':prop['revision'],'request_key':'publish'}
    with pytest.raises(ExecutionError):await h.work.command(h.auth,'work.publish',args)
    worker=JobStore(h.pg,'lane');transport=Transport(h);queue=WorkerQueue(worker,transport,poll=.01);queue.start()
    async def managed_execute(profile,*,cwd,input,timeout,output_limit):
        from core.execution.jobs import current_job
        from core.worker.contract import validate
        identifier=uuid.uuid4().hex
        request=validate({'version':1,'job_id':identifier,'profile':profile,'workspace':'projects','cwd':h.pid,
            'args':[],'input':input,'timeout':timeout,'output_limit':output_limit,'expires':time.time()+timeout+20},max_timeout=2147483647)
        await worker.admit(current_job.get(),identifier,request)
        async with asyncio.timeout(10):
            while True:
                row=await worker.get(identifier)
                if row['outcome'] in {'succeeded','failed','cancelled','timed_out','unknown'}:
                    return {**row['receipt'],'outcome':row['outcome'],'execution_id':identifier}
                await asyncio.sleep(.01)
    import core.worker.client as client
    monkeypatch.setattr(client,'execute_async',managed_execute)
    try:
        result=await h.work.command(h.owner,'work.publish',args)
        assert result['state']=='published' and result['tested_snapshot_digest']==snapshot(h.project)['digest']
        assert (h.project/'app/calc.py').read_text()==h.content
        assert (h.project/'file.txt').read_text()=='registered base'
        assert (await h.work.command(h.owner,'work.publish',args))['replayed']
        status=await h.work.command(h.auth,'work.status',{'job_id':started['id']})
        assert status['publication']['state']=='completed'
        assert len(transport.launches)==1
        with pytest.raises(ExecutionError,match='publication_already_requested'):
            await h.work.command(h.owner,'work.publish',{**args,'request_key':'different'})
    finally:await queue.close()


@pytest.mark.asyncio
async def test_stale_source_is_refused_before_any_publication_job(work):
    h=work;started,_=await build(h,'builder')
    prop=await h.jobs.command(h.auth,'jobs.proposal',{'job_id':started['id']})
    (h.project/'file.txt').write_text('concurrent owner edit')
    with pytest.raises(ExecutionError,match='proposal_stale'):
        await h.work.command(h.owner,'work.publish',{'job_id':started['id'],'proposal_digest':prop['digest'],
            'expected_revision':prop['revision'],'request_key':'publish'})
    assert await h.pg.fetchval('SELECT state FROM work_publications')=='conflict'
    assert (h.project/'file.txt').read_text()=='concurrent owner edit'
