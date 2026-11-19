"""Real temporary Postgres, deterministic text models and filesystem adapters.

No SQLite/in-memory substitute. Missing S4B_TEST_POSTGRES_DSN is a skip, not a pass.
Installed Linux process/cleanup proof lives in --checked-edits acceptance.
"""
import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from tests.test_tool_jobs_postgres import jobs_api
from tests.test_durable_runs_v2 import pg, TEAM
from core.execution.checked import CONTRACT
from core.execution.executor import Executor
from core.execution.models import ExecutionError, canonical
from core.execution.store import decoded
from core.execution.job_store import JobStore
from core.execution.worker_queue import WorkerQueue
from core.execution.workspaces import snapshot
from core.worker.contract import profile_revision
from core.tool_jobs.checked_runner import execute
from core.tool_jobs import service as domain
from unittest.mock import AsyncMock


def typed_plan(base):
    common={'instruction':'bounded edit','criteria_ids':[]}
    job={'proposed_seconds':900,'rationale':'small deterministic fixture'}
    return {'tasks':[
        {**common,**job,'id':'read','kind':'workspace_read','dependencies':[],'snapshot':base,'operation_id':'workspace.read','paths':['file.txt']},
        {**common,'id':'model','kind':'model_task','dependencies':['read'],'snapshot':'read','operation_id':'edits.plan'},
        {**common,**job,'id':'edit','kind':'workspace_edit','dependencies':['model'],'snapshot':'model','operation_id':'workspace.edit','edits_from':'model'},
        {**common,**job,'id':'check','kind':'registered_check','dependencies':['edit'],'snapshot':'edit','check_id':'files.nonempty'},
        {**common,'id':'publish','kind':'proposal_publish','dependencies':['check'],'snapshot':'check','criteria_ids':['deliverable']},
    ]}


@pytest.fixture
async def checked_api(jobs_api,monkeypatch):
    h=jobs_api
    registry=h.project.parents[1]/'project-folders.json'
    folders=json.loads(registry.read_text())
    folders[0]['checked_edits']={'checks':['files.nonempty'],'write_paths':['file.txt']}
    registry.write_text(json.dumps(folders))
    h.cfg['profiles']['checked']={'argv':['/installed/python','-m','core.tool_jobs.checked_runner'],'network':False,'adapter_revision':CONTRACT}
    h.revision=profile_revision(h.cfg['profiles']['checked'])
    monkeypatch.setattr(domain,'capabilities',AsyncMock(return_value={'operations':{'jobs.start':{'available':True}},'coder':{'available':True}}))
    team={**TEAM,'id':'checked-team','topology':'agent-loop/v1','max_parallelism':1}
    await h.store.save_team(h.auth,team,None,'save-checked',{})
    h.args={**h.args,'team':'checked-team','role':'coder','worker_contract':CONTRACT,
            'acceptance':{'criteria':[{'id':'deliverable','output_kind':'proposal','checks':{'required_check_ids':['files.nonempty']}}]},
            'budget':{**h.args['budget'],'start_calls':2,'max_job_attempts':8},
            'job_coverage':{**h.args['job_coverage'],'profiles':{'checked':h.revision},'max_job_attempts':8}}
    h.calls=[];h.stale=False;h.delete=False
    async def model(binding,prompt):
        h.calls.append(prompt)
        if 'tasks\":[typed steps]' in prompt:
            value=typed_plan(snapshot(h.project)['digest'])
        else:
            pre='0'*64 if h.stale else hashlib.sha256(b'registered base').hexdigest()
            value={'edits':[{'operation':'delete' if h.delete else 'replace','path':'file.txt','pre_sha256':pre,
                             **({} if h.delete else {'content':'checked output'})}]}
        return {'text':canonical(value),'usage':{}}
    h.model=model
    return h


class CheckedTransport:
    """Filesystem oracle is installed deterministic code, never model prose."""
    def __init__(self,h): self.h=h;self.receipts={};self.launches=[]
    def hello(self): return {'profiles':{'checked':self.h.revision},'lane_generation':0,'active_execution':None,'max_timeout':3600}
    def control(self,value):
        if value['op']=='inspect': return {'receipt':self.receipts.get(value['execution_id'])}
        return {'type':'idle_barrier','cleanup':'idle_proved','daemon_epoch':'fixture',
                'lane_generation':value['lane_generation'],'controller_session':value['controller_session']}
    def submit(self,request,stop):
        self.launches.append(request['execution_id'])
        target=Path(self.h.cfg['run_workspace_root'])/request['workspace'][4:]
        error=''
        try: execute(json.loads(request['input']),target)
        except (ExecutionError,OSError,ValueError) as exc: error=str(exc)
        receipt={k:request[k] for k in ('execution_id','request_digest','lane_generation','controller_session')}
        receipt.update(type='result',daemon_epoch='fixture',outcome='failed' if error else 'succeeded',cleanup='idle_proved',
                       returncode=1 if error else 0,execution_seconds=.01,cleanup_seconds=0,
                       stdout_b64='',stderr_b64=base64.b64encode(error.encode()).decode())
        self.receipts[request['execution_id']]=receipt
        return receipt


async def drive(h,*,after_checkpoint=None):
    worker=JobStore(h.pg,'lane');await worker.initialize()
    transport=CheckedTransport(h);queue=WorkerQueue(worker,transport,poll=.01);queue.start()
    executor=Executor(h.store,h.admission,h.model,poll=.01,after_checkpoint=after_checkpoint)
    try:
        run=await h.store.claim(executor.controller)
        assert run is not None
        await asyncio.wait_for(executor.execute(run),10)
    finally: await queue.close()
    return transport


@pytest.mark.asyncio
@pytest.mark.parametrize('wall_mode',['fixed','none'])
async def test_checked_output_uses_job_ledger_after_final_model_credit(checked_api,wall_mode):
    h=checked_api
    if wall_mode=='none':
        h.args={**h.args,'budget':{**h.args['budget'],'wall_mode':'none','wall_seconds':None}}
    started=await h.jobs.start(h.auth,h.args)
    transport=await drive(h)
    view=await h.store.get(h.auth,started['run_id'])
    assert view['run']['status']=='completed',view
    assert view['run']['accepted_output_count']==1
    assert view['run']['calls_used']==2 and len(h.calls)==2
    assert view['run']['job_account']['dispatched']==3 and view['run']['job_account']['held']==0
    assert len(set(transport.launches))==3
    manifest=view['run']['accepted_outputs'][0]['check_receipts']
    assert manifest['kind']=='checked_proposal' and manifest['checks'][0]['input_digest']==manifest['output_digest']
    assert not manifest['independently_verified']
    prop=await h.jobs.command(h.auth,'jobs.proposal',{'job_id':started['id']})
    assert prop['checks_passed'] and prop['files'][0]['operation']=='replace'
    assert (h.project/'file.txt').read_text()=='registered base'
    for table in ('checked_run_contracts','checked_snapshots','checked_step_jobs','checked_step_receipts'):
        with pytest.raises(Exception,match='immutable'): await h.pg.execute('DELETE FROM '+table)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['stale','failed_check'])
async def test_stale_edits_and_failed_check_never_accept_output(checked_api,failure):
    h=checked_api;h.stale=failure=='stale';h.delete=failure=='failed_check'
    started=await h.jobs.start(h.auth,h.args)
    await drive(h)
    view=await h.store.get(h.auth,started['run_id'])
    assert view['run']['status']=='paused' and view['run']['accepted_output_count']==0
    assert view['run']['flags']
    assert view['run']['job_account']['held']==0
    if h.stale:
        assert (Path(h.cfg['run_workspace_root'])/started['id']/'file.txt').read_text()=='registered base'
    else:
        receipts=await h.pg.fetch('SELECT payload FROM checked_step_receipts')
        assert any(decoded(r['payload']).get('check_id')=='files.nonempty' and not decoded(r['payload'])['passed'] for r in receipts)


@pytest.mark.asyncio
async def test_restart_after_edit_reuses_frozen_snapshot_and_never_replays(checked_api):
    h=checked_api;started=await h.jobs.start(h.auth,h.args)
    async def interrupt(row,ordinal):
        if ordinal==3: raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError): await drive(h,after_checkpoint=interrupt)
    original=await h.pg.fetch('SELECT execution_id FROM checked_step_jobs ORDER BY ordinal')
    transport=await drive(h)
    assert len(transport.launches)==1  # only the not-yet-admitted check
    assert len(h.calls)==2
    assert await h.pg.fetchval('SELECT count(*) FROM checked_step_jobs')==3
    assert all(r['execution_id'] not in transport.launches for r in original)
    assert (await h.store.get(h.auth,started['run_id']))['run']['status']=='completed'


@pytest.mark.asyncio
async def test_completion_waits_for_every_worker_receipt_and_plan_step(checked_api):
    h=checked_api;started=await h.jobs.start(h.auth,h.args)
    await drive(h)
    # Completed accepted output cannot make an unsettled worker disappear from
    # completion. The query sees real ledger rows, including conservative holds.
    async with h.store.transaction() as c:
        row=await h.store._row(c,h.auth,started['run_id'])
        await c.execute("UPDATE worker_job_attempts SET outcome='unknown' WHERE execution_id=(SELECT execution_id FROM checked_step_jobs ORDER BY ordinal LIMIT 1)")
        assert not await h.store._complete_loop(c,row)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_kind',['owner','shutdown'])
async def test_cancellation_keeps_intent_until_worker_cleanup(checked_api,cancel_kind):
    import threading
    import time
    h=checked_api;started=await h.jobs.start(h.auth,h.args)
    entered=asyncio.Event();release=threading.Event();loop=asyncio.get_running_loop()
    class Holding(CheckedTransport):
        def control(self,value):
            if value['op']=='cancel': release.set()
            return super().control(value)
        def submit(self,request,stop):
            if json.loads(request['input'])['operation']=='files.nonempty':
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5),'queue never delivered cancellation'
                receipt=super().submit(request,stop)
                receipt.update(outcome='cancelled',returncode=-9)
                return receipt
            return super().submit(request,stop)
    worker=JobStore(h.pg,'lane');await worker.initialize()
    transport=Holding(h);queue=WorkerQueue(worker,transport,poll=.01);queue.start()
    executor=Executor(h.store,h.admission,h.model,poll=.01)
    run=await h.store.claim(executor.controller)
    task=asyncio.create_task(executor.execute(run))
    try:
        await asyncio.wait_for(entered.wait(),5)
        if cancel_kind=='owner':
            reply=await h.jobs.command(h.auth,'jobs.cancel',{'job_id':started['id'],'request_key':'cancel'})
            assert reply['status']=='cancelling' and not reply['terminal']
            await asyncio.wait_for(task,10)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task,10)
            before=len(transport.launches)
            replacement=Executor(h.store,h.admission,h.model,poll=.01)
            reclaimed=await h.store.claim(replacement.controller)
            assert reclaimed is not None
            await asyncio.wait_for(replacement.execute(reclaimed),10)
            assert len(transport.launches)==before and len(h.calls)==2
        view=(await h.store.get(h.auth,started['run_id']))['run']
        assert view['status']==('cancelled' if cancel_kind=='owner' else 'paused') and view['accepted_output_count']==0
        assert view['job_account']['held']==0 and not view['cleanup_pending']
    finally:
        release.set()
        await executor.close();await queue.close()
