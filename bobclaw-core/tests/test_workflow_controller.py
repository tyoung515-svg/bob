"""Fake models, real face tool loop/Builder/file operations and controller.

The DB and isolated-worker transports are deterministic test adapters. These
tests establish controller behavior, not PostgreSQL or Linux confinement.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from langchain_core.messages import AIMessage

from core.execution.models import Authority,ExecutionError,canonical,digest
from core.execution.executor import Executor
from core.execution.workflow_store import WorkflowStore
from core.execution.workflow_context import current
from core.execution.workflows import WorkflowService,WorkflowRuntime,PREMADES,LIMITS
from core.builder.service import Builder
from core.worker.builder_runner import execute as execute_file_operation
from test_workflow_authority import MemoryStore,call,start


@pytest.fixture(autouse=True)
def mock_redis(): yield


class Database:
    """Only persistence is substituted. Unknown SQL fails the test."""
    def __init__(self,store):
        self.store=store; self.modes={}; self.approvals={}; self.conversations={}; self.history=[]
        self.consumed={}; self.attempts={}; self.hold_by_run={}; self.leases={}; self.questions={}

    @property
    def holds(self): return sum(self.hold_by_run.values())

    @asynccontextmanager
    async def acquire(self): yield self
    @asynccontextmanager
    async def transaction(self): yield self

    async def execute(self,sql,*a):
        sql=' '.join(sql.split())
        if sql.startswith('SELECT pg_advisory'): return 'SELECT 1'
        if sql.startswith('INSERT INTO conversations'):
            self.conversations[str(a[0])]={'project_id':a[4],'owner':a[1]}; return 'INSERT 1'
        if sql.startswith('INSERT INTO messages'):
            self.history.append({'id':str(a[0]),'conversation':str(a[1]),'role':a[2],'content':a[3]}); return 'INSERT 1'
        if sql.startswith('INSERT INTO builder_modes'):
            if 'workflow_run_id' in sql:
                self.modes[str(a[2])]=dict(conversation_id=a[2],project_id=a[3],session_id=a[4],session_until=a[5],mode=a[6],
                    authority_scope=a[7],credential_epoch=a[8],workflow_run_id=a[9],workflow_generation=a[10],expires_at=None,revision=1)
            elif str(a[2]) not in self.modes:
                self.modes[str(a[2])]=dict(conversation_id=a[2],project_id=a[3],session_id=a[4],
                    session_until=a[5],mode='approve',authority_scope=None,credential_epoch=None,expires_at=None,revision=1)
            return 'INSERT 1'
        if sql.startswith('INSERT INTO builder_approvals'):
            keys=['id','lane','owner','conversation_id','project_id','session_id','turn_id','kind','mode','summary','detail','payload','expires_at','state','session_until']
            self.approvals[a[0]]={**dict(zip(keys,a)), 'execution_state':'pending','result':None,'decision_key':None,'decision_hash':None}
            return 'INSERT 1'
        if sql.startswith('UPDATE builder_approvals SET execution_state=$3'):
            self.approvals[a[0]].update(result=a[1],execution_state=a[2]); return 'UPDATE 1'
        if sql.startswith('UPDATE builder_approvals SET state=$2'):
            self.approvals[a[0]].update(state=a[1],decision_key=a[2],decision_hash=a[3]); return 'UPDATE 1'
        if sql.startswith("UPDATE builder_approvals SET state='expired'"):
            for v in self.approvals.values():
                if v['state']=='pending' and ('expires_at<=$4' not in sql or v['expires_at']<=a[3] or v['session_id']!=a[4]): v['state']='expired'
            return 'UPDATE 1'
        if sql.startswith("UPDATE builder_approvals SET execution_state='unknown'"): return 'UPDATE 0'
        if sql.startswith("UPDATE builder_approvals SET execution_state='stopped'"): return 'UPDATE 0'
        if sql.startswith('UPDATE builder_approvals SET task_authorized'): return 'UPDATE 1'
        if sql.startswith(('UPDATE pc_tasks','UPDATE pc_requests')): return 'UPDATE 1'
        if sql.startswith("UPDATE builder_approvals SET state='stopped'"): return 'UPDATE 1'
        if sql.startswith('INSERT INTO agent_attempts'):
            self.attempts[a[3]]={'id':a[3],'run_id':a[2],'state':'dispatched','binding':json.loads(a[6])}; return 'INSERT 1'
        if sql.startswith('UPDATE agent_attempts'):
            self.attempts[a[3]]['state']='failed' if "state='failed'" in sql else 'completed'
            if "state='failed'" in sql: self.attempts[a[3]]['failure_detail']=json.loads(a[4])
            return 'UPDATE 1'
        if sql.startswith('UPDATE agent_budget_allocations'):
            self.consumed[a[2]]=self.consumed.get(a[2],0)+1; return 'UPDATE 1'
        if sql.startswith('UPDATE agent_budget_accounts'): return 'UPDATE 1'
        if sql.startswith('UPDATE agent_pool_holds'): self.hold_by_run[a[2]]-=1; return 'UPDATE 1'
        if sql.startswith('UPDATE agent_call_pools'): return 'UPDATE 1'
        if sql.startswith('INSERT INTO workflow_workspace_leases'):
            self.leases[a[2]]={'run_id':a[3],'stage_key':a[4],'generation':a[5],'quarantined':False}; return 'INSERT 1'
        if sql.startswith('DELETE FROM workflow_workspace_leases'):
            self.leases={k:v for k,v in self.leases.items() if v['run_id']!=a[2]}; return 'DELETE 1'
        if sql.startswith('UPDATE agent_execution_grants'): self.store.revoked.add(a[2]); return 'UPDATE 1'
        if sql.startswith('INSERT INTO chat_questions'):
            self.questions[a[0]]={'id':a[0],'conversation_id':a[1],'user_id':a[2],'question':a[3],'choices':a[4],'expires_at':a[5],'answer':None}; return 'INSERT 1'
        if sql.startswith('UPDATE chat_questions'): self.questions[a[0]]['answer']=a[1]; return 'UPDATE 1'
        if sql.startswith(('INSERT INTO agent_run_steps','UPDATE agent_runs','UPDATE workflow_workspace_leases',
                           'INSERT INTO workflow_waits','INSERT INTO builder_allowances','UPDATE builder_modes')): return 'UPDATE 1'
        if sql.startswith('UPDATE workflow_waits'): return 'UPDATE 1'
        raise AssertionError('Unexpected test SQL: '+sql)

    async def fetchrow(self,sql,*a):
        sql=' '.join(sql.split())
        if 'FROM conversations c' in sql and 'SELECT c.project_id' in sql:
            return self.conversations.get(str(a[0]))
        if 'FROM builder_modes' in sql:
            conversation=a[2] if len(a)>2 else a[0]
            return deepcopy(self.modes.get(str(conversation)))
        if 'FROM builder_approvals' in sql: return deepcopy(self.approvals.get(a[0]))
        if 'FROM workflow_workspace_leases' in sql:
            return next((v for v in self.leases.values() if self.store.project_for(v['run_id'])==a[2]),None)
        if 'FROM chat_questions' in sql: return deepcopy(self.questions.get(a[0]))
        raise AssertionError('Unexpected test SQL: '+sql)

    async def fetchval(self,sql,*a):
        sql=' '.join(sql.split())
        if sql.startswith("UPDATE builder_approvals SET execution_state='running'"):
            r=self.approvals[a[0]]
            if r['state']=='allowed' and r['execution_state']=='pending': r['execution_state']='running'; return a[0]
            return None
        if 'FROM builder_checkpoints' in sql: return None
        if sql.startswith('INSERT INTO builder_checkpoints'): return a[0]
        if 'SELECT EXISTS' in sql and 'worker_job_attempts' in sql: return self.store.unresolved
        if 'SELECT EXISTS' in sql and 'work_publications' in sql: return False  # BU14: registered workspaces
        if 'SELECT EXISTS' in sql and 'builder_approvals' in sql:
            return any(v['execution_state'] in {'running','unknown'} and v['project_id']==a[2] for v in self.approvals.values())
        if 'FROM conversations' in sql: return a[0] if str(a[0]) in self.conversations else None
        if sql=='SELECT clock_timestamp()': return datetime.now(timezone.utc)
        if 'SELECT count(*) FROM agent_runs' in sql: return 0
        raise AssertionError('Unexpected test SQL: '+sql)

    async def fetch(self,sql,*a):
        if 'FROM builder_allowances' in sql: return []
        if 'FROM agent_pool_holds' in sql:
            return [{'kind':'project','pool_id':'fixture','remaining':self.hold_by_run[a[2]]}]
        raise AssertionError('Unexpected test SQL: '+sql)


class ControllerStore(MemoryStore):
    wf_clock=WorkflowStore.wf_clock
    wf_stage=WorkflowStore.wf_stage
    wf_open_wait=WorkflowStore.wf_open_wait
    wf_charge=WorkflowStore.wf_charge
    wf_model_result=WorkflowStore.wf_model_result
    wf_model_failure=WorkflowStore.wf_model_failure
    wf_conversation=WorkflowStore.wf_conversation
    wf_history=WorkflowStore.wf_history
    wf_writer=WorkflowStore.wf_writer
    wf_cleanup=WorkflowStore.wf_cleanup
    wf_recovery_ready=WorkflowStore.wf_recovery_ready
    wf_wait=WorkflowStore.wf_wait

    def __init__(self):
        super().__init__(); self.pool=Database(self); self.revoked=set(); self.unresolved=False; self.generations={}

    @asynccontextmanager
    async def transaction(self,**kw):
        async with self.lock: yield self.pool

    async def _fenced(self,c,row):
        if row['generation']!=self.generations.get(row['id'],row['generation']): raise ExecutionError('stale_controller',409)
        return row
    async def check_policy(self,*args): pass
    async def _grant(self,c,row): return None if row['id'] in self.revoked else {'id':'grant'}
    async def wf_grant_active(self,c,auth,id):
        return not any(r['run']['id'] in self.revoked for (lane,owner,kind,key),r in self.data.items() if kind=='run' and r['run']['grant']['id']==id)
    async def _lock_pools(self,*args): pass
    async def wf_start(self,c,auth,r,proposal):
        self.pool.hold_by_run[r['run']['id']]=r['run']['budget']['limit']['calls']
        await super().wf_start(c,auth,r,proposal)
    async def wf_interrupted_calls(self,c,row):
        attempts=[v for v in self.pool.attempts.values() if v['state']=='dispatched' and v['run_id']==row['id']]
        for v in attempts: v['state']='unknown'
        return len(attempts)
    async def wf_job_usage(self,c,auth,run_id):
        r=self.record(run_id)
        receipts=[x['result'] for s in r['run']['stages'] for x in s.get('receipts',[]) if x['result'].get('execution_id')]
        receipts.extend(x for s in r['run']['stages'] for x in s.get('checks',[]))
        return {'job_seconds':sum(x.get('duration',0) for x in receipts),'job_attempts':len(receipts)}
    async def wf_take_messages(self,c,auth,run_id,stage): return []
    async def _chain(self,c,keys,id):
        r=self.record(keys[2])
        return [{'allowance':r['run']['budget']['limit']['calls'],'consumed':self.pool.consumed.get(keys[2],0),'reserved':0,'delegated':0}]
    async def _release_holds(self,c,keys): self.pool.hold_by_run[keys[2]]=0
    async def wf_release(self,*args): pass
    async def wf_stop(self,c,auth,r,**kw):
        self.revoked.add(r['run']['id'])
        if kw.get('release_holds',True): self.pool.hold_by_run[r['run']['id']]=0
    async def wf_request_stop(self,c,auth,r): self.revoked.add(r['run']['id'])
    def record(self,id): return next(deepcopy(v) for (lane,owner,kind,key),v in self.data.items() if kind=='run' and key==id)
    def project_for(self,id): return self.record(id)['run']['project_id']


class Runtime(WorkflowRuntime):
    def __init__(self,store,root,registry):
        self.store,self.root,self.registry=store,root,registry
    async def bindings(self,auth):
        return [{'binding_id':str(i),'backend':'deepseek_v4_flash','model':'frozen-'+str(i),'model_identity':'canonical-'+str(i),
                 'family':'family-'+str(i),'face_ids':['assistant','builder','orchestrator','reviewer'],
                 'tool_capable':True,'runnable':True,'reasons':[]} for i in range(6)]
    async def resolve(self,c,auth,args,d,planned_project):
        faces={}
        for face in {s['face_id'] for s in d['agents'].values()}|{'assistant'}:
            data=self.registry.get_face(face).model_dump(mode='json'); faces[face]={'definition':data,'digest':digest(data)}
        return {'workspace':{'registration_digest':digest('registration'),'path_label':'Test workspace'},
                'registration':{},'evidence':{},'skills':{},'faces':faces,
                **({'assistant_binding':(await self.bindings(auth))[5]} if args.get('origin_conversation_id') else {})}
    async def create_workspace(self,*args): pass


class ScriptedModel:
    def __init__(self,harness,model): self.harness,self.model=harness,model; self.round=0
    def bind_tools(self,tools): self.tools=tools; return self
    async def ainvoke(self,messages):
        ctx=current.get(); h=self.harness; self.round+=1
        assert self.model==ctx.binding['model'], 'The lane default replaced the frozen model.'
        h.calls.append({'stage':ctx.stage['key'],'model':self.model,'messages':messages})
        h.model_started.set()
        if (h.hang is True and 'build' in ctx.stage['key'] or h.hang=='all' or
                h.hang=='after-write' and ctx.face=='builder' and self.round>=2):
            await h.release.wait()
        if ctx.summary: return AIMessage(content='The workflow report records the files, checks and remaining uncertainty.')
        if ctx.spec['output']=='units':
            return AIMessage(content=canonical({'units':[{'id':id,'title':id,'brief':'Build '+id,'acceptance':'Create the file.', 'depends_on':deps}
                for id,deps in [('one',[]),('two',[])]]}))
        if ctx.spec['output']=='verdict':
            evidence=ctx.stage['envelope']['review_evidence']; h.reviews.append(evidence)
            verdict='changes' if h.fail_review and '/review-0' in ctx.stage['key'] else 'pass'
            if h.bad_review: return AIMessage(content='It looks good to me.')
            return AIMessage(content=canonical({'verdict':verdict,'summary':'Review '+verdict,
                'findings':[{'id':'fix','detail':'Replace the broken content.','evidence_ids':[]}] if verdict=='changes' else [],
                'question':None,'snapshot_digest':evidence['snapshot']['digest']}))
        if ctx.face=='builder':
            if h.ask and self.round==1:
                return AIMessage(content='I need the file label before writing.',tool_calls=[{'name':'ask_operator','args':{'question':'Which label should I use?','choices':['Ready','Done']},'id':'ask'}])
            write_round=2 if h.ask else 1
            if self.round==write_round:
                path=(ctx.stage.get('unit_id') or 'app')+'.py'
                content='value = "ready"\n' if '/fix-' in ctx.stage['key'] or not h.fail_review else 'value = "broken"\n'
                return AIMessage(content='I will write the file.',tool_calls=[{'name':'fs_write','args':{'path':path,'content':content},'id':'write'}])
            return AIMessage(content='',tool_calls=[{'name':'task_finish','args':{'report':'The file was written. See deterministic check receipts for verification.'},'id':'finish'}])
        return AIMessage(content='The plan preserves the supplied task and acceptance criteria.')


@pytest.fixture
def harness(monkeypatch,tmp_path):
    import core.faces.registry as registry_module
    import core.nodes.execute as execute_module
    import core.worker.client as worker_client
    from core.execution import chat_records
    registry=registry_module.FaceRegistry(Path(__file__).parents[1]/'core/faces/templates')
    monkeypatch.setattr(registry_module,'get_default_registry',lambda:registry)
    monkeypatch.setattr(registry_module,'get_registry',lambda:registry)
    store=ControllerStore()
    h=SimpleNamespace(store=store,calls=[],reviews=[],ask=False,hang=False,fail_review=False,bad_review=False,
                      model_started=asyncio.Event(),release=asyncio.Event(),roots={})
    runtime=Runtime(store,tmp_path,registry)
    admission=SimpleNamespace(store=store)
    executor=Executor(store,admission,None,poll=.005)
    executor.workflow.running=True
    executor.workflow.service.runtime=runtime
    h.controller=executor.workflow; h.executor=executor
    service=WorkflowService(admission,runtime=runtime)
    h.service=service
    h.auth=Authority('lane','owner','owner',{'auto_actions':['read','write','model_call'],
        'escalate_actions':[],'may_touch':[str(tmp_path)+'/**'],'may_not_touch':[]},'human')
    h.click=replace(h.auth,session_kind='owner_stepup',allowed_operations=('workflows.grant_start','workflows.resume','workflows.gate','builder.approve'))
    async def require(auth,op):
        if auth.session_kind!='owner_stepup' or op not in auth.allowed_operations: raise ExecutionError('not_found',404)
    monkeypatch.setattr('core.execution.owner.require_owner',require)
    monkeypatch.setattr('core.execution.owner.check_epoch',lambda auth:asyncio.sleep(0))
    monkeypatch.setattr(execute_module,'_build_tool_model',lambda backend,model_override=None: ScriptedModel(h,model_override or 'lane-default'))
    monkeypatch.setattr(execute_module,'require_backend',lambda *args:None)
    monkeypatch.setattr(chat_records,'POOL',store.pool)
    monkeypatch.setattr(Builder,'workspace',lambda self,auth,row:h.roots.get(str(row['project_id'])))
    monkeypatch.setattr(h.controller,'root',lambda r:h.roots.get(r['run']['project_id']))
    run_root=tmp_path/'run-workspaces'; run_root.mkdir()
    monkeypatch.setattr(worker_client,'configuration',lambda:{'workspaces':{'projects':str(tmp_path)},'run_workspace_root':str(run_root),'worker_gid':None})
    async def worker(profile,*,cwd,input,**kwargs):
        payload=json.loads(input)
        if payload['operation']=='shell_run':
            # Real deterministic compileall work in the frozen scratch copy.
            # The production Linux shell/queue boundary is separately gated.
            import compileall
            passed=compileall.compile_dir(cwd,quiet=2)
            return {'returncode':0 if passed else 1,'stdout':b'compileall completed','stderr':b'',
                    'cleanup':'idle_proved','execution_id':uuid.uuid4().hex,'execution_seconds':.01}
        result=execute_file_operation(payload,cwd)
        return {'returncode':0,'stdout':canonical({'adapter':'builder-command/1','result':result}).encode(),'stderr':b'',
                'cleanup':'idle_proved','execution_id':uuid.uuid4().hex,'execution_seconds':.01}
    monkeypatch.setattr(worker_client,'execute_async',worker)
    async def new(id='premade-plan-build-review-fix',*,mode='auto',budget=None,origin=False,checks=True,same=False):
        d=deepcopy(PREMADES[id])
        if budget: d['budget']['calls']=budget
        if checks:
            d['authority']['checks']=[{'id':'compile','command':'python -m compileall -q .','cwd':'','timeout_seconds':120}]
            for _,s in __import__('core.execution.workflow_schema',fromlist=['stages']).stages(d):
                if s['kind']=='repair': s['check_ids']=['compile']
        project=str(uuid.uuid4()); directory=tmp_path/project; directory.mkdir()
        h.roots[project]=directory
        bindings={alias:str(i) for i,alias in enumerate(d['agents'])}
        if same:
            for alias in bindings: bindings[alias]='0'
            d['distinct_models']=[]
        origin_id=str(uuid.uuid4()) if origin else None
        if origin_id: store.pool.conversations[origin_id]={'project_id':uuid.UUID(project),'owner':'owner'}
        p=(await call(service,h.auth,'propose',definition=d,inputs={'task':'Build the granted files.'},bindings=bindings,project_id=project,
            origin_conversation_id=origin_id,expires_at=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),
            run_mode=mode,request_key=uuid.uuid4().hex))['proposal']
        w=(await start(service,h.click,p,key=uuid.uuid4().hex,allow_same_model_review=same))['run']
        r=store.record(w['id']); row={'lane':'lane','owner':'owner','id':w['id'],'generation':1,'controller':'test',
            'run_authority':r['initiator'],'boundary':r['boundary'],'snapshot':{'team':{'topology':'workflow/v1'}},'topology':'workflow/v1'}
        store.generations[row['id']]=1
        return row
    h.new=new
    return h


async def wait_for(h,row,predicate):
    async with asyncio.timeout(5):
        while not predicate(h.store.record(row['id'])['run']): await asyncio.sleep(.005)
    return h.store.record(row['id'])['run']


async def drive(h,row):
    from core.execution.chat_records import accept
    task=asyncio.create_task(h.controller.tick(row))
    async with asyncio.timeout(10):
        while not task.done():
            w=h.store.record(row['id'])['run']
            for wait in w['needs_you']:
                if getattr(h,'on_wait',None): await h.on_wait(row,w,wait)
                if wait['kind']=='approval':
                    await Builder(h.store.pool).approve(h.click,{'approval_id':wait['record_id'],'decision':'allow_once','request_key':wait['wait_id']})
                elif wait['kind']=='question':
                    await accept({'question_id':wait['record_id'],'user_id':'owner','answer':'Ready'})
            await asyncio.sleep(.005)
        await task
    w=h.store.record(row['id'])['run']
    if w['status']=='needs_you' and any(x['kind']=='gate' for x in w['needs_you']):
        wait=next(x for x in w['needs_you'] if x['kind']=='gate')
        await call(h.service,h.click,'gate',run_id=row['id'],wait_id=wait['wait_id'],decision='approve',expected_revision=w['revision'],request_key=uuid.uuid4().hex)
        row['generation']+=1; h.store.generations[row['id']]=row['generation']
        return await drive(h,row)
    return h.store.record(row['id'])['run']


@pytest.mark.parametrize('premade',list(PREMADES))
async def test_each_premade_executes_real_tools_checks_and_report(harness,premade):
    h=harness; h.fail_review=True
    row=await h.new(premade)
    w=await drive(h,row)
    assert w['status']=='completed',w.get('reason')
    assert w['report']['checks'] and all(c['passed'] for c in w['report']['checks'])
    assert w['budget']['consumed_calls']==len(h.calls)
    assert any('/fix-1' in s['key'] for s in w['stages'])
    assert h.reviews and any('content' in f for f in h.reviews[-1]['files'])
    assert all('ready' in p.read_text() for p in h.roots[w['project_id']].glob('*.py'))
    assert not list(h.roots[w['project_id']].glob('__pycache__/*')), 'Checks modified the live workspace.'
    assert w['report']['files'] and h.store.pool.holds==0


async def test_approve_and_question_use_existing_in_process_paths(harness):
    h=harness; h.ask=True
    row=await h.new(mode='approve')
    w=await drive(h,row)
    assert w['status']=='completed',w['reason']
    events=h.store.events['lane','owner',row['id']]
    kinds=[e['payload']['wait']['kind'] for e in events if e['kind']=='wait']
    assert 'question' in kinds and 'approval' in kinds
    assert any(q['answer']=='Ready' for q in h.store.pool.questions.values())
    assert all(a['mode']=='approve' for a in h.store.pool.approvals.values())
    assert all(a['expires_at']>__import__('time').time()+600 for a in h.store.pool.approvals.values())


async def test_frozen_model_reaches_builder_not_lane_default(harness):
    h=harness; row=await h.new()
    w=await drive(h,row)
    assert w['status']=='completed'
    assert all(c['model'].startswith('frozen-') for c in h.calls)
    builder=next(c for c in h.calls if '/build' in c['stage'])
    assert builder['model']=='frozen-1'


def script_reported_names(h,monkeypatch,report,declared=None,metadata_key='model_name'):
    original=ScriptedModel.ainvoke
    async def invoke(model,messages):
        response=await original(model,messages)
        actual=report(current.get(),model.round)
        response.response_metadata={} if actual is None else {metadata_key:actual}
        return response
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    if declared is not None:
        runtime=h.service.runtime
        bindings=runtime.bindings
        async def with_names(auth):
            return [{**b,'reported_model_names':list(declared)} for b in await bindings(auth)]
        monkeypatch.setattr(runtime,'bindings',with_names)


@pytest.mark.parametrize('reported,declared',[
    ('requested',None),('requested',['alias']),('alias',['alias']), (None,None), (None,['alias'])])
async def test_reported_name_equal_declared_alias_or_missing(harness,monkeypatch,reported,declared):
    h=harness
    script_reported_names(h,monkeypatch,lambda ctx,round:ctx.binding['model'] if reported=='requested' else reported,declared)
    row=await h.new(); w=await drive(h,row)
    assert w['status']=='completed',w['reason']
    for stage in w['stages']:
        for model in stage['actual_models']:
            assert model['provider_reported_model']==(model['model'] if reported=='requested' else reported)
    assert not any('could not confirm' in c for c in w['report']['caveats'])
    if reported is None: assert not w.get('model_coverage')
    if declared:
        assert all(b['reported_model_names']==declared for b in h.store.record(row['id'])['coverage']['bindings'].values())


async def test_declared_alias_rejects_third_name_with_owner_reason(harness,monkeypatch):
    h=harness
    script_reported_names(h,monkeypatch,lambda ctx,round:'third-name',['alias'])
    row=await h.new(); w=await drive(h,row)
    assert w['status']=='failed' and w['reason_code']=='binding_mismatch'
    assert w['reason']=="The provider answered as 'third-name' but this run was set up for 'frozen-0'."
    stage=next(s for s in w['stages'] if s['key']=='plan')
    assert stage['reason']==w['reason'] and stage['actual_models'][0]['provider_reported_model']=='third-name'
    assert w['report']['reason']==w['reason'] and w['report']['reason_code']=='binding_mismatch'
    assert all(a['state']=='completed' for a in h.store.pool.attempts.values())


async def test_declared_names_can_alternate_and_model_metadata_is_checked(harness,monkeypatch):
    h=harness
    script_reported_names(h,monkeypatch,lambda ctx,round:ctx.binding['model'] if round==1 else 'alias',
                          ['alias'],metadata_key='model')
    row=await h.new(); w=await drive(h,row)
    assert w['status']=='completed',w['reason']
    assert any(m['provider_reported_model']=='alias' for s in w['stages'] for m in s['actual_models'])
    assert not w.get('model_coverage')


async def test_parallel_unit_mismatch_keeps_names_and_failure_code(harness,monkeypatch):
    h=harness
    script_reported_names(h,monkeypatch,
        lambda ctx,round:'third-name' if ctx.stage.get('unit_id') else ctx.binding['model'], ['alias'])
    row=await h.new('premade-sprint-board')
    w=await drive(h,row)
    assert w['status']=='failed' and w['reason_code']=='binding_mismatch'
    failed=[s for s in w['stages'] if s['state']=='failed' and s.get('unit_id') and s['actual_models']]
    assert failed
    assert all(s['reason']==w['reason'] for s in failed)
    assert 'third-name' in w['reason'] and failed[0]['binding']['model'] in w['reason']
    assert w['report']['reason']==w['reason']


async def test_parallel_units_share_undeclared_binding_observation(harness,monkeypatch):
    h=harness
    script_reported_names(h,monkeypatch,
        lambda ctx,round:ctx.stage['unit_id'] if ctx.stage.get('unit_id') else ctx.binding['model'])
    row=await h.new('premade-sprint-board')
    w=await drive(h,row)
    assert w['status']=='failed' and w['reason_code']=='binding_mismatch'
    assert w['model_coverage']['1']['provider_reported_model']=='one'
    assert all(name in w['reason'] for name in ('one','two','frozen-1'))
    assert w['report']['model_coverage']==w['model_coverage']


@pytest.mark.parametrize('reported,declared,summary_state',[
    ('alias',['alias'],'delivered'),('third-name',['alias'],'skipped'),
    ('provider-name',None,'delivered')])
async def test_assistant_summary_uses_frozen_reported_name_rule(harness,monkeypatch,reported,declared,summary_state):
    h=harness
    script_reported_names(h,monkeypatch,
        lambda ctx,round:reported if ctx.summary else ctx.binding['model'],declared)
    row=await h.new(origin=True); w=await drive(h,row)
    assert w['status']=='completed' and w['report']['assistant_summary']==summary_state
    stage=next(s for s in w['stages'] if s['key']=='assistant-summary')
    assert stage['actual_models'][0]['provider_reported_model']==reported
    if summary_state=='skipped':
        assert all(name in stage['reason'] for name in (reported,'frozen-5'))
        assert stage['reason'] in ' '.join(w['report']['caveats'])
    elif declared is None:
        assert w['model_coverage']['5']['provider_reported_model']==reported
        assert any('could not confirm' in caveat and reported in caveat for caveat in w['report']['caveats'])


async def test_undeclared_name_change_in_tool_loop_is_refused(harness,monkeypatch):
    h=harness
    script_reported_names(h,monkeypatch,lambda ctx,round:'first' if round==1 else 'second')
    row=await h.new(); w=await drive(h,row)
    assert w['status']=='failed' and w['reason_code']=='binding_mismatch'
    stage=next(s for s in w['stages'] if s['key'].endswith('/build'))
    assert stage['calls']==2
    assert [m['provider_reported_model'] for m in stage['actual_models']]==['first','second']
    assert stage['reason']==w['reason'] and all(name in w['reason'] for name in ('first','second',stage['binding']['model']))


async def test_undeclared_name_is_recorded_shared_across_stages_and_reported(harness,monkeypatch):
    h=harness
    script_reported_names(h,monkeypatch,lambda ctx,round:'provider-name')
    row=await h.new(same=True); w=await drive(h,row)
    assert w['status']=='completed',w['reason']
    observed=w['model_coverage']['0']
    sentence="The provider reported the model as 'provider-name' for the requested model 'frozen-0', and BoB could not confirm it is the same model."
    assert observed=={'model':'frozen-0','provider_reported_model':'provider-name','note':sentence}
    assert w['report']['model_coverage']==w['model_coverage'] and sentence in w['report']['caveats']
    artifact=h.store.artifacts['lane','owner',row['id'],w['report']['artifact_id']]
    assert sentence in json.loads(artifact['bytes'])['caveats']
    assert 'model_coverage' not in h.store.record(row['id'])['coverage'], 'The frozen grant must not change.'


@pytest.mark.parametrize('first,later', [('provider-name','other-name'),('provider-name','requested'),('requested','other-name')])
async def test_undeclared_name_cannot_change_on_a_later_call(harness,monkeypatch,first,later):
    h=harness
    def report(ctx,round):
        name=first if ctx.stage['key']=='plan' else later
        return ctx.binding['model'] if name=='requested' else name
    script_reported_names(h,monkeypatch,report)
    row=await h.new(same=True); w=await drive(h,row)
    assert w['status']=='failed' and w['reason_code']=='binding_mismatch'
    expected_first='frozen-0' if first=='requested' else first
    expected_later='frozen-0' if later=='requested' else later
    assert w['model_coverage']['0']['provider_reported_model']==expected_first
    assert all(name in w['reason'] for name in (expected_first,expected_later,'frozen-0'))
    assert any(s['state']=='failed' and s['reason']==w['reason'] for s in w['stages'])


async def test_missing_name_does_not_clear_observation_and_runs_are_isolated(harness,monkeypatch):
    h=harness
    script_reported_names(h,monkeypatch,lambda ctx,round:'first-name' if ctx.stage['key']=='plan' else None)
    row=await h.new(same=True); w=await drive(h,row)
    assert w['status']=='completed' and w['model_coverage']['0']['provider_reported_model']=='first-name'
    script_reported_names(h,monkeypatch,lambda ctx,round:'new-run-name')
    other=await h.new(same=True); second=await drive(h,other)
    assert second['status']=='completed' and second['model_coverage']['0']['provider_reported_model']=='new-run-name'


async def test_stop_cancels_inflight_and_revokes_and_releases(harness):
    h=harness; h.hang='after-write'
    row=await h.new(); task=asyncio.create_task(h.controller.tick(row))
    await wait_for(h,row,lambda w:any(s['state']=='running' and '/build' in s['key'] and s.get('receipts') for s in w['stages']))
    await call(h.service,h.auth,'stop',run_id=row['id'],request_key='stop')
    await asyncio.wait_for(task,5)
    w=h.store.record(row['id'])['run']
    assert w['status']=='stopped' and not w['grant']['active']
    assert row['id'] in h.store.revoked and h.store.pool.holds==0
    assert not h.store.pool.leases and w['report']
    assert w['report']['files'] and w['budget']['unknown_calls']==1


async def test_stop_during_question_closes_wait_and_future(harness):
    h=harness; h.ask=True
    row=await h.new(); task=asyncio.create_task(h.controller.tick(row))
    w=await wait_for(h,row,lambda w:any(x['kind']=='question' for x in w['needs_you']))
    wait=next(x for x in w['needs_you'] if x['kind']=='question')
    await call(h.service,h.auth,'stop',run_id=row['id'],request_key='stop-question')
    await asyncio.wait_for(task,5)
    w=h.store.record(row['id'])['run']
    from core.tools.ask import pending_questions
    assert w['status']=='stopped' and not w['needs_you']
    assert wait['record_id'] not in pending_questions()
    assert any(e['kind']=='wait' and e['payload']['wait']['wait_id']==wait['wait_id'] and e['payload']['wait']['state']=='expired'
        for e in h.store.events['lane','owner',row['id']])


async def test_pause_then_resume_at_stage_boundary(harness):
    h=harness; h.hang='all'
    row=await h.new(); task=asyncio.create_task(h.controller.tick(row))
    await h.model_started.wait()
    w=h.store.record(row['id'])['run']
    await call(h.service,h.auth,'pause',run_id=row['id'],expected_revision=w['revision'],request_key='pause')
    h.hang=False; h.release.set(); await asyncio.wait_for(task,5)
    w=h.store.record(row['id'])['run']; assert w['status']=='paused'
    assert len(h.calls)==1
    await call(h.service,h.click,'resume',run_id=row['id'],expected_revision=w['revision'],request_key='resume')
    row['generation']+=1; h.store.generations[row['id']]=row['generation']
    assert (await drive(h,row))['status']=='completed'


async def test_restart_interrupts_then_owner_resumes_without_refund(harness):
    h=harness; h.hang=True
    row=await h.new(); task=asyncio.create_task(h.controller.tick(row))
    await wait_for(h,row,lambda w:any(s['state']=='running' and '/build' in s['key'] for s in w['stages']))
    await asyncio.sleep(.02); task.cancel(); await asyncio.gather(task,return_exceptions=True)
    spent=h.store.record(row['id'])['run']['budget']['consumed_calls']
    row['generation']+=1; h.store.generations[row['id']]=row['generation']
    await h.controller.tick(row)
    w=h.store.record(row['id'])['run']; assert w['status']=='needs_you'
    wait=next(x for x in w['needs_you'] if x['kind']=='recovery')
    assert any(s['state']=='interrupted' for s in w['stages'])
    assert w['budget']['consumed_calls']==spent
    await call(h.service,h.click,'resume',run_id=row['id'],wait_id=wait['wait_id'],expected_revision=w['revision'],request_key='resume-stage')
    h.hang=False; h.release.set()
    w=await drive(h,row)
    assert w['status']=='completed',w['reason']
    assert w['budget']['consumed_calls']>spent
    assert any('earlier attempt was interrupted' in str(c['messages']) for c in h.calls)


async def test_exhausted_budget_and_invalid_review_are_distinct(harness):
    h=harness; row=await h.new(budget=1)
    w=await drive(h,row)
    assert w['status']=='expired' and w['reason_code']=='call_budget_exhausted'
    assert w['budget']['consumed_calls']==1 and w['report']


async def test_invalid_review_never_passes(harness):
    h=harness; h.bad_review=True; row=await h.new()
    w=await drive(h,row)
    assert w['status']=='failed' and w['reason_code']=='invalid_review_verdict'
    assert not w['report']['checks']


async def test_same_model_downgrade_and_no_checks_reported(harness):
    h=harness; row=await h.new(same=True,checks=False)
    w=await drive(h,row)
    assert w['status']=='completed' and w['report']['review_downgraded']
    assert 'No deterministic check ran.' in w['report']['caveats']


async def test_busy_workspace_waits_with_plain_reason(harness):
    h=harness; h.hang=True
    first=await h.new(); task=asyncio.create_task(h.controller.tick(first))
    await wait_for(h,first,lambda w:any('/build' in s['key'] and s['state']=='running' for s in w['stages']))
    second=await h.new(); record=h.store.record(second['id'])
    first_record=h.store.record(first['id'])
    record['run']['project_id']=first_record['run']['project_id']
    record['coverage']['project_id']=record['run']['project_id']
    await h.store.wf_run(None,h.auth,record)
    await asyncio.wait_for(h.controller.tick(second),5)
    w=h.store.record(second['id'])['run']
    assert w['status']=='queued' and 'busy' in w['reason']
    await call(h.service,h.auth,'stop',run_id=first['id'],request_key='stop')
    await asyncio.wait_for(task,5)


async def test_assistant_summary_is_after_deterministic_report_and_charged(harness):
    h=harness; row=await h.new(origin=True)
    w=await drive(h,row)
    assert w['status']=='completed' and w['report']['assistant_summary']=='delivered'
    assert h.calls[-1]['stage']=='assistant-summary'
    assert w['report']['calls_used']==len(h.calls)
    assert 'workflow_controller' in str(h.calls[-1]['messages'])


async def test_running_catalog_tracks_process_lifecycle(harness):
    h=harness
    assert (await call(h.service,h.auth,'catalog'))['available']
    h.controller.running=False
    result=await call(h.service,h.auth,'catalog')
    assert not result['available'] and result['reasons']


async def test_unknown_cleanup_retains_workspace_exclusion(harness):
    h=harness; row=await h.new(); h.store.unresolved=True
    w=await drive(h,row)
    assert w['status']=='unknown' and h.store.pool.leases
    assert 'could not be proved' in w['reason']


async def test_summary_skips_when_budget_spent_and_report_still_exists(harness):
    h=harness; row=await h.new(origin=True,budget=1)
    w=await drive(h,row)
    assert w['status']=='expired' and w['report']['assistant_summary']=='skipped'
    assert w['report']['artifact_id'] and len(h.calls)==1


async def test_last_allowed_call_can_finish_assistant_summary(harness):
    h=harness; row=await h.new(origin=True,budget=5)
    w=await drive(h,row)
    assert w['status']=='completed' and w['budget']['consumed_calls']==5
    assert w['report']['assistant_summary']=='delivered'


async def test_database_clock_excludes_human_wait_time(harness):
    h=harness; h.ask=True
    now=[datetime.now(timezone.utc)]
    async def database_clock(c): return now[0]
    h.store.wf_clock=database_clock
    row=await h.new(); task=asyncio.create_task(h.controller.tick(row))
    w=await wait_for(h,row,lambda w:any(x['kind']=='question' for x in w['needs_you']))
    before=w['budget']['active_seconds']
    now[0]+=timedelta(minutes=20)
    assert (await h.controller.load(row))['run']['budget']['active_seconds']==before
    question=next(x for x in w['needs_you'] if x['kind']=='question')
    from core.execution.chat_records import accept
    await accept({'question_id':question['record_id'],'user_id':'owner','answer':'Ready'})
    await asyncio.wait_for(task,5)
    w=await drive(h,row)
    assert w['status']=='completed' and w['budget']['active_seconds']==before


def test_real_tool_model_builder_uses_frozen_model_and_zero_retries(monkeypatch):
    import core.backends._lc_openai as wrapper
    captured={}
    monkeypatch.setitem(wrapper._LC_OPENAI_BACKENDS,'deepseek_v4_flash',lambda:{'base_url':'http://fixture.invalid','api_key':'fixture','model':'lane-default'})
    monkeypatch.setattr(wrapper,'ChatOpenAI',lambda **kwargs:captured.update(kwargs) or captured)
    wrapper.build_chat_openai('deepseek_v4_flash',model_override='frozen-model')
    assert captured['model']=='frozen-model' and captured['max_retries']==0


def test_builder_checks_use_protected_run_root_and_reject_escape(tmp_path,monkeypatch):
    from core.worker.builder_profile import project_root
    from core.worker.client import request
    import core.worker.client as client
    parent=tmp_path/'run-workspaces'; parent.mkdir()
    identifier=uuid.uuid4().hex; target=parent/identifier; target.mkdir()
    cfg={'run_workspace_root':str(parent),'workspaces':{'projects':str(tmp_path/'projects'),'scratch':str(tmp_path/'scratch')}}
    monkeypatch.setattr(client,'configuration',lambda:cfg)
    value=request('builder',cwd=target,input='{}')
    assert value['workspace']=='run_'+identifier and value['cwd']==''
    assert project_root(cfg,value)==target
    for invalid in ['run_../escape','run_'+identifier+'/child','run_'+'f'*32]:
        with pytest.raises(Exception): project_root(cfg,{'workspace':invalid,'cwd':''})
    with pytest.raises(Exception): project_root(cfg,{'workspace':'run_'+identifier,'cwd':'child'})


async def test_controller_golden_fixtures(harness):
    """Extend the WB1 golden manifest with execution-produced wire records."""
    h=harness; fixtures={}
    async def capture(label,row):
        request={'contract_version':1,'run_id':row['id']}
        result=await h.service.command(h.auth,'workflows.run',request)
        fixtures[label]={'operation':'workflows.run','request':request,'result':result}
        event_request={**request,'after_seq':0,'limit':100}
        fixtures[label+'-events']={'operation':'workflows.events','request':event_request,
            'result':await h.service.command(h.auth,'workflows.events',event_request)}
        return result['run']
    async def question_and_approval(row,w,wait):
        label='run-executed-'+wait['kind']
        if label not in fixtures: await capture(label,row)
    h.on_wait=question_and_approval
    h.ask=True
    completed=await h.new(mode='approve')
    w=await drive(h,completed)
    assert w['status']=='completed'
    await capture('run-executed-completed',completed)
    request={'contract_version':1,'run_id':completed['id'],'artifact_id':w['report']['artifact_id']}
    fixtures['report-executed']={'operation':'workflows.artifact','request':request,
        'result':await h.service.command(h.auth,'workflows.artifact',request)}
    h.ask=False; h.hang=True
    interrupted=await h.new()
    task=asyncio.create_task(h.controller.tick(interrupted))
    await wait_for(h,interrupted,lambda w:any('/build' in s['key'] and s['state']=='running' for s in w['stages']))
    await capture('run-executed-running',interrupted)
    task.cancel(); await asyncio.gather(task,return_exceptions=True)
    interrupted['generation']+=1; h.store.generations[interrupted['id']]=interrupted['generation']
    await h.controller.tick(interrupted)
    w=await capture('run-executed-interrupted',interrupted)
    wait=next(x for x in w['needs_you'] if x['kind']=='recovery')
    request={'contract_version':1,'run_id':interrupted['id'],'wait_id':wait['wait_id'],
        'expected_revision':w['revision'],'request_key':'fixture-resume-interrupted'}
    fixtures['resume-executed-stage']={'operation':'workflows.resume','request':request,
        'result':await h.service.command(h.click,'workflows.resume',request)}
    h.hang=False; h.release.set()
    assert (await drive(h,interrupted))['status']=='completed'
    h.hang='after-write'; h.release.clear()
    stopped=await h.new(); task=asyncio.create_task(h.controller.tick(stopped))
    await wait_for(h,stopped,lambda w:any('/build' in s['key'] and s['state']=='running' and s.get('receipts') for s in w['stages']))
    request={'contract_version':1,'run_id':stopped['id'],'request_key':'fixture-stop-inflight'}
    fixtures['stop-executed-stage']={'operation':'workflows.stop','request':request,
        'result':await h.service.command(h.auth,'workflows.stop',request)}
    await asyncio.wait_for(task,5)
    assert (await capture('run-executed-stopped-mid-stage',stopped))['status']=='stopped'
    h.hang=False
    expired=await h.new(budget=1)
    assert (await drive(h,expired))['status']=='expired'
    await capture('run-executed-over-budget',expired)
    if os.environ.get('WORKFLOW_WRITE_FIXTURES')=='1':
        destination=Path(__file__).resolve().parents[3]/'runs/golive/workflow-fixtures'
        manifest_path=destination/'manifest.json'
        manifest=json.loads(manifest_path.read_text(encoding='utf8'))
        for name,fixture in fixtures.items():
            (destination/(name+'.request.json')).write_text(json.dumps(fixture['request'],indent=2,ensure_ascii=False)+'\n',encoding='utf8')
            (destination/(name+'.result.json')).write_text(json.dumps(fixture['result'],indent=2,ensure_ascii=False)+'\n',encoding='utf8')
            manifest[name]={'operation':fixture['operation'],'status':200}
        manifest_path.write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf8')
    assert {'run-executed-running','run-executed-question','run-executed-approval','run-executed-interrupted',
            'run-executed-completed','run-executed-stopped-mid-stage','run-executed-over-budget'}<=fixtures.keys()
