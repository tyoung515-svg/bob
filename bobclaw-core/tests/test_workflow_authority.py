"""Service contracts with transactional fake storage; no DB/provider required.

The adapter substitutes persistence and lane catalogs, not WorkflowService logic.
Real PostgreSQL DDL, locks and rollback still require the integration gate.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.execution.models import Authority, ExecutionError, digest, canonical, wire_integers
from core.execution.workflows import WorkflowService, PREMADES, UNAVAILABLE, review_facts


@pytest.fixture(autouse=True)
def mock_redis():
    yield


class MemoryStore:
    lane='lane'
    def __init__(self):
        self.data={}; self.receipts={}; self.events={}; self.artifacts={}; self.messages=[]
        self.starts=0; self.lock=asyncio.Lock()

    @asynccontextmanager
    async def transaction(self, **kwargs):
        async with self.lock:
            before=deepcopy((self.data,self.receipts,self.events,self.artifacts,self.messages,self.starts))
            try: yield None
            except Exception:
                self.data,self.receipts,self.events,self.artifacts,self.messages,self.starts=before
                raise

    async def _mutation(self,c,auth,op,key,payload):
        r=self.receipts.get((auth.lane,auth.owner,op,key))
        if r:
            if r['boundary']!=auth.boundary: raise ExecutionError('not_found',404)
            if r['hash']!=digest(payload): raise ExecutionError('idempotency_conflict',409)
            return deepcopy(r)

    async def _remember(self,c,auth,op,key,payload,resource,result):
        self.receipts[auth.lane,auth.owner,op,key]={'hash':digest(payload),'boundary':auth.boundary,
                                                 'result':{**deepcopy(result),'operation_id':'internal-receipt'}}

    async def wf_lock(self,*args): pass

    async def wf_get(self,c,auth,kind,id,revision=None):
        r=self.data.get((auth.lane,auth.owner,kind,id))
        if not r: return
        r=deepcopy(r)
        if kind=='definition':
            head=r['head']; r=r['versions'].get(revision or head['revision'])
            if not r: return
            r={**r,'deleted':head['deleted']}
            if r['origin']!='premade' and auth.token_type=='agent' and r['boundary']!=auth.boundary: return
        elif auth.token_type=='agent':
            boundary = r.get('boundary') or Authority(**r['frozen']['initiator']).boundary
            if boundary!=auth.boundary: return
        return r

    async def wf_definition(self,c,auth,r):
        key=(auth.lane,auth.owner,'definition',r['id'])
        row=self.data.setdefault(key,{'head':None,'versions':{}})
        row['head']=deepcopy(r); row['versions'][r['revision']]=deepcopy(r)

    async def wf_list(self,c,auth,kind,cursor,limit):
        rows=[]
        for lane,owner,k,id in sorted(self.data):
            if (lane,owner,k)!=(auth.lane,auth.owner,kind) or id<=cursor: continue
            r=await self.wf_get(c,auth,kind,id)
            if r and not r.get('deleted'): rows.append(r)
        return rows[:limit]

    async def wf_proposal(self,c,auth,r,**kwargs):
        self.data[auth.lane,auth.owner,'proposal',r['proposal']['id']]=deepcopy(r)

    async def wf_start(self,c,auth,r,proposal):
        assert (auth.lane,auth.owner,'run',r['run']['id']) not in self.data
        self.starts+=1
        await self.wf_run(c,auth,r)

    async def wf_run(self,c,auth,r):
        self.data[auth.lane,auth.owner,'run',r['run']['id']]=deepcopy(r)

    async def wf_event(self,c,auth,e):
        self.events.setdefault((auth.lane,auth.owner,e['run_id']),[]).append(deepcopy(e))

    async def wf_events(self,c,auth,id,after,limit):
        return deepcopy([e for e in self.events.get((auth.lane,auth.owner,id),[]) if e['seq']>after][:limit])

    async def wf_stop(self,*args): pass
    async def wf_request_stop(self,*args): pass
    async def wf_grant_active(self,*args): return True
    async def wf_report(self,*args): pass
    async def wf_wait(self,*args): pass
    async def wf_stage(self,*args): pass
    async def wf_message(self,c,auth,id,stage,mid,text): self.messages.append((id,stage,mid,text))

    async def wf_artifact(self,c,auth,id,artifact,content=None):
        key=(auth.lane,auth.owner,id,artifact)
        if content is not None:
            self.artifacts[key]={'bytes':content,'digest':hashlib.sha256(content).hexdigest(),'media_type':'application/json'}
        return self.artifacts.get(key)


class Runtime:
    def __init__(self):
        self.epoch=1
        self.rows=[{'binding_id':k,'backend':'fixture','model':k,'model_identity':k,'family':k,
                    'face_ids':['assistant','orchestrator','builder','reviewer'],'tool_capable':True,'runnable':True,'reasons':[]}
                   for k in ('one','two','three','four','five')]
        self.created=[]
    async def bindings(self,auth): return deepcopy(self.rows)
    async def resolve(self,c,auth,a,d,project):
        return {'workspace':{'registration_digest':digest([self.epoch,a.get('workspace'),a.get('project_id')]),'path_label':'Fixture workspace'},
                'evidence':{},'skills':{},'faces':{'fixture':digest(['face',self.epoch])}}
    async def create_workspace(self,c,auth,r): self.created.append(r['planned_project'])


@pytest.fixture
def setup(monkeypatch):
    auth=Authority('lane','owner','owner',{},'human',credential_epoch=1)
    async def require(a,op):
        if a.token_type!='human' or a.actor!=a.owner or a.session_kind!='owner_stepup' or op not in a.allowed_operations:
            raise ExecutionError('not_found',404)
    monkeypatch.setattr('core.execution.owner.require_owner',require)
    store=MemoryStore(); runtime=Runtime(); now=datetime(2026,11,16,12,tzinfo=timezone.utc)
    service=WorkflowService(SimpleNamespace(store=store),runtime=runtime,test_only_runner_available=True,clock=lambda:now)
    click=replace(auth,session_kind='owner_stepup',audience='bob-owner-controls',allowed_operations=('workflows.grant_start','workflows.resume','workflows.gate'))
    return service,auth,click


def proposal_args(id='premade-plan-build-review-fix',key='propose'):
    d=PREMADES[id]
    return {'definition_ref':{'id':id,'revision':1},'inputs':{'task':'Build a bounded fixture'},
            'bindings':{alias:['one','two','three','four','five'][i] for i,alias in enumerate(d['agents'])},
            'project_id':None,'workspace':{'create':'Fixture workspace'},'origin_conversation_id':None,
            'expires_at':'2026-11-17T12:00:00Z','request_key':key}


async def call(s,auth,op,**args):
    return await s.command(auth,'workflows.'+op,{'contract_version':1,**args})


async def proposed(s,auth,**kwargs):
    a=proposal_args(); a.update(kwargs)
    return (await call(s,auth,'propose',**a))['proposal']


async def start(s,click,p,key='start',**kwargs):
    return await call(s,click,'grant_start',proposal_id=p['id'],proposal_revision=p['revision'],proposal_digest=p['digest'],request_key=key,**kwargs)


async def test_copy_cas_read_only_tombstone(setup):
    s,a,_=setup
    await call(s,a,'catalog')
    for op,kwargs in [('save',{'definition':next(iter(PREMADES.values())),'expected_revision':1}),('delete',{'expected_revision':1})]:
        with pytest.raises(ExecutionError,match='premade_read_only'):
            await call(s,a,op,id='premade-plan-build-review-fix',request_key=op,**kwargs)
    r=(await call(s,a,'copy',id='premade-plan-build-review-fix',revision=1,name='Editable',request_key='copy'))['definition']
    assert r['origin']=='user' and r['revision']==1 and not r['read_only']
    edit={**r['definition'],'summary':'edited'}
    result=await call(s,a,'save',id=r['id'],definition=edit,expected_revision=1,request_key='edit')
    assert result['definition']['revision']==2
    assert await call(s,a,'save',id=r['id'],definition=edit,expected_revision=1,request_key='edit')==result
    with pytest.raises(ExecutionError,match='revision_conflict'):
        await call(s,a,'save',id=r['id'],definition=edit,expected_revision=1,request_key='bad-edit')
    await call(s,a,'delete',id=r['id'],expected_revision=2,request_key='delete')
    assert not (await call(s,a,'list'))['items']
    assert (await call(s,a,'get',id=r['id'],revision=1))['definition']['definition']['summary']!=edit['summary']
    with pytest.raises(ExecutionError,match='not_found'): await call(s,a,'get',id=r['id'])


async def test_cross_owner_lane_and_agent_mutations(setup):
    s,a,click=setup; p=await proposed(s,a); w=(await start(s,click,p))['run']
    for other in (replace(a,owner='other',actor='other'),replace(a,lane='other'),replace(a,token_type='agent',actor='outside')):
        for op,args in [('proposal',{'proposal_id':p['id']}),('run',{'run_id':w['id']}),('events',{'run_id':w['id'],'after_seq':0})]:
            with pytest.raises(ExecutionError,match='not_found'): await call(s,other,op,**args)
    agent=replace(a,token_type='agent')
    with pytest.raises(ExecutionError,match='not_found'): await start(s,agent,p)
    for op,args in [('copy',{'id':'premade-plan-build-review-fix','revision':1,'name':'No'}),('pause',{'run_id':w['id'],'expected_revision':w['revision']})]:
        with pytest.raises(ExecutionError,match='not_found'): await call(s,agent,op,request_key=op,**args)


async def test_double_start_replay_and_conflicting_choice(setup):
    s,a,click=setup; p=await proposed(s,a)
    replies=await asyncio.gather(start(s,click,p),start(s,click,p),start(s,click,p,key='double-click'))
    assert len({r['run']['id'] for r in replies})==1 and s.store.starts==1
    assert replies[0]==replies[1]==replies[2]
    assert (await call(s,a,'proposal',proposal_id=p['id']))['proposal']['run_id']==replies[0]['run']['id']
    with pytest.raises(ExecutionError,match='idempotency_conflict'):
        await start(s,click,p,key='different-choice',allow_same_model_review=True)


async def test_scoped_proposer_can_stop_only_its_granted_run(setup):
    s,a,click=setup; agent=replace(a,actor='scoped-agent',token_type='agent',scope={'may_touch':['fixture/**']})
    p=await proposed(s,agent); w=(await start(s,click,p))['run']
    foreign=replace(agent,actor='another-agent')
    with pytest.raises(ExecutionError,match='not_found'):
        await call(s,foreign,'stop',run_id=w['id'],request_key='foreign-stop')
    assert (await call(s,agent,'stop',run_id=w['id'],request_key='own-stop'))['run']['status']=='stopped'


async def test_assistant_steering_is_bound_to_origin(setup):
    s,a,click=setup; p=await proposed(s,a); w=(await start(s,click,p))['run']
    s.assistant_origin='different-conversation'
    for op,args in [('stop',{}),('message',{'stage_key':'plan','text':'untrusted steering'})]:
        with pytest.raises(ExecutionError,match='not_found'):
            await call(s,a,op,run_id=w['id'],request_key=op,**args)


async def test_proposal_stale_catalog_evidence_workspace_or_expiry(setup):
    s,a,click=setup; p=await proposed(s,a); s.runtime.epoch=2
    with pytest.raises(ExecutionError,match='proposal_stale'): await start(s,click,p)
    assert not s.store.starts
    s.runtime.epoch=1; s.clock=lambda:datetime(2026,11,16,12,16,tzinfo=timezone.utc)
    with pytest.raises(ExecutionError,match='proposal_expired'): await start(s,click,p)


async def test_policy_choice_and_unknown_family(setup):
    s,a,click=setup
    for b in s.runtime.rows: b['family']=None
    args=proposal_args('premade-council-build')
    p=(await call(s,a,'propose',**args))['proposal']
    assert not p['coverage']['review']['policy_met']
    with pytest.raises(ExecutionError,match='same_model_review_choice_required'): await start(s,click,p)
    w=(await start(s,click,p,allow_same_model_review=True))['run']
    stopped=(await call(s,a,'stop',run_id=w['id'],request_key='stop'))['run']
    assert any('same-model' in c for c in stopped['report']['caveats'])


async def test_distinct_models_cannot_downgrade(setup):
    s,a,_=setup; args=proposal_args('premade-council-build'); args['bindings']={k:'one' for k in args['bindings']}
    with pytest.raises(ExecutionError,match='distinct_models_required'): await call(s,a,'propose',**args)


async def test_reported_names_do_not_supply_review_identity(setup):
    s,a,_=setup
    for row in s.runtime.rows:
        row['model_identity']='one-model'
        row['reported_model_names']=[row['binding_id']+'-provider-name']
    args=proposal_args('premade-council-build')
    with pytest.raises(ExecutionError,match='distinct_models_required'):
        await call(s,a,'propose',**args)
    p=await proposed(s,a)
    assert not p['coverage']['review']['policy_met']
    assert all(b['reported_model_names'] for b in p['coverage']['bindings'].values())


async def test_default_unavailable_and_epoch_revocation(setup):
    s,a,click=setup; s.test_only_runner_available=False
    catalog=await call(s,a,'catalog'); assert catalog['available'] is False and catalog['reasons']==[UNAVAILABLE]
    p=await proposed(s,a)
    assert UNAVAILABLE in p['reasons']
    w=(await start(s,click,p))['run']
    assert w['status']=='accepted' and w['reason']==UNAVAILABLE and w['budget']['consumed_calls']==0
    assert s.store.starts==1
    p=await proposed(s,a,request_key='new-proposal')
    with pytest.raises(ExecutionError,match='proposal_stale'): await start(s,replace(click,credential_epoch=2),p,key='stale-epoch')


async def test_pause_resume_message_stop_and_artifact(setup):
    s,a,click=setup; p=await proposed(s,a); w=(await start(s,click,p))['run']
    message=await call(s,a,'message',run_id=w['id'],stage_key='plan',text='Keep original scope',request_key='message')
    assert message['state']=='queued'
    w=(await call(s,a,'run',run_id=w['id']))['run']
    w=(await call(s,a,'pause',run_id=w['id'],expected_revision=w['revision'],request_key='pause'))['run']
    assert w['status']=='paused'
    w=(await call(s,click,'resume',run_id=w['id'],expected_revision=w['revision'],request_key='resume'))['run']
    assert w['status']=='queued'
    w=(await call(s,a,'stop',run_id=w['id'],request_key='stop'))['run']
    assert w['status']=='stopped' and w['report']['outcome']=='stopped' and not w['grant']['active']
    assert w['budget']['consumed_calls']==0 and not w['report']['checks']
    artifact=await call(s,a,'artifact',run_id=w['id'],artifact_id=w['report']['artifact_id'])
    assert json.loads(artifact['content'])['outcome']=='stopped'
    page=await call(s,a,'events',run_id=w['id'],after_seq=0,limit=1)
    assert page['has_more'] and page['next_seq']==1
    with pytest.raises(ExecutionError,match='run_terminal'):
        await call(s,a,'message',run_id=w['id'],stage_key='plan',text='Late',request_key='late')


async def test_gate_cas_owner_and_reject(setup):
    s,a,click=setup; p=await proposed(s,a); w=(await start(s,click,p))['run']
    r=await s.store.wf_get(None,a,'run',w['id'])
    r['run']['status']='needs_you'
    r['run']['needs_you']=[{'wait_id':'gate-1','kind':'gate','stage_key':'owner','conversation_id':None,'request_id':None,
                           'record_id':'gate-1','prompt':'Approve?','expires_at':None,'state':'open','requires_owner':True}]
    await s.store.wf_run(None,a,r)
    with pytest.raises(ExecutionError,match='not_found'):
        await call(s,a,'gate',run_id=w['id'],wait_id='gate-1',decision='approve',expected_revision=w['revision'],request_key='denied')
    result=await call(s,click,'gate',run_id=w['id'],wait_id='gate-1',decision='reject',expected_revision=w['revision'],request_key='reject')
    assert result['run']['status']=='blocked' and result['run']['report']['outcome']=='blocked'


async def test_dispatched_stop_does_not_claim_cleanup_or_terminal_success(setup):
    s,a,click=setup; w=(await start(s,click,await proposed(s,a)))['run']
    r=await s.store.wf_get(None,a,'run',w['id']); r['run']['status']='running'
    await s.store.wf_run(None,a,r)
    w=(await call(s,a,'stop',run_id=w['id'],request_key='stop'))['run']
    assert w['status']=='stopping' and not w['grant']['active'] and w['report'] is None


async def test_utf8_artifact_offsets(setup):
    s,a,click=setup; w=(await start(s,click,await proposed(s,a)))['run']
    await s.store.wf_artifact(None,a,w['id'],'unicode','a😀b'.encode())
    first=await call(s,a,'artifact',run_id=w['id'],artifact_id='unicode',max_bytes=4)
    assert first['content']=='a' and first['next_offset']==1
    second=await call(s,a,'artifact',run_id=w['id'],artifact_id='unicode',offset=1,max_bytes=4)
    assert second['content']=='😀' and second['next_offset']==5
    with pytest.raises(ExecutionError,match='invalid_offset'):
        await call(s,a,'artifact',run_id=w['id'],artifact_id='unicode',offset=2)


async def test_pinned_binding_offline_tools_and_same_identity(setup):
    s,a,click=setup
    args=proposal_args(); args.pop('definition_ref'); d=deepcopy(PREMADES['premade-plan-build-review-fix'])
    args['definition']=d; d['agents']['builder']['binding_id']='two'
    args['bindings']['builder']='one'
    with pytest.raises(ExecutionError,match='binding_override_denied'): await call(s,a,'propose',**args)
    args['bindings']['builder']='two'; s.runtime.rows[1]['runnable']=False
    with pytest.raises(ExecutionError,match='binding_unavailable'): await call(s,a,'propose',**args)
    s.runtime.rows[1]['runnable']=True; s.runtime.rows[1]['tool_capable']=False
    with pytest.raises(ExecutionError,match='tool_capable_binding_required'): await call(s,a,'propose',**args)
    s.runtime.rows[1]['tool_capable']=True; s.runtime.rows[2]['model_identity']='two'
    p=(await call(s,a,'propose',**args))['proposal']
    assert p['coverage']['review']['same_model_choice_required']
    with pytest.raises(ExecutionError,match='same_model_review_choice_required'): await start(s,click,p)


async def test_immutable_revision_pins_and_tombstone_retains_existing_run(setup):
    s,a,click=setup
    r=(await call(s,a,'copy',id='premade-plan-build-review-fix',revision=1,name='Mine',request_key='copy'))['definition']
    args=proposal_args(); args['definition_ref']={'id':r['id'],'revision':1}
    p=(await call(s,a,'propose',**args))['proposal']
    await call(s,a,'save',id=r['id'],definition=r['definition'],expected_revision=1,request_key='edit')
    w=(await start(s,click,p))['run']
    assert w['definition_ref']=={'id':r['id'],'revision':1}
    args.update(request_key='again',definition_ref={'id':r['id'],'revision':2})
    p=(await call(s,a,'propose',**args))['proposal']
    await call(s,a,'delete',id=r['id'],expected_revision=2,request_key='delete')
    with pytest.raises(ExecutionError,match='proposal_stale'): await start(s,click,p,key='deleted-start')
    assert (await call(s,a,'run',run_id=w['id']))['run']['definition_ref']=={'id':r['id'],'revision':1}


async def test_failed_transaction_rolls_back_run_and_grant_binding(setup,monkeypatch):
    s,a,click=setup; p=await proposed(s,a)
    monkeypatch.setattr(s.store,'wf_event',AsyncMock(side_effect=RuntimeError('simulated DB commit failure')))
    with pytest.raises(RuntimeError): await start(s,click,p)
    assert s.store.starts==0
    assert (await call(s,a,'proposal',proposal_id=p['id']))['proposal']['state']=='proposed'


def test_matcher4_binds_entire_coverage_and_legacy_stays_separate():
    from core.execution.grants import grant_matches
    coverage={'budget':{'calls':1000},'inputs':{'task':'bounded'},'definition':{},'bindings':{},'resolved_digest':digest({})}
    snapshot={'team':{'topology':'workflow/v1'},'models':{},'resolved':{},'workflow_coverage':coverage,'coverage_digest':digest(coverage)}
    row={'id':'run','topology':'workflow/v1','boundary':'boundary','snapshot':snapshot,'run_authority':{'credential_epoch':1}}
    grant={'matcher_version':4,'run_id':'run','authority_boundary':'boundary','credential_epoch':1,
           'workflow_coverage':coverage,'coverage_digest':digest(coverage),'call_ceiling':1000,
           'team_digest':digest(snapshot['team']),'models_digest':digest({})}
    assert grant_matches(grant,row)
    for changed in ({**row,'id':'other'},{**row,'topology':'legacy'},
                    {**row,'snapshot':{**snapshot,'models':{'substitution':True}}},
                    {**row,'run_authority':{'credential_epoch':2}}):
        assert not grant_matches(grant,changed)


async def test_real_resolver_freezes_allowed_skills_and_refuses_digest_or_tools(setup,monkeypatch):
    from core.execution.workflows import WorkflowRuntime
    _,auth,_=setup; d=deepcopy(PREMADES['premade-plan-build-review-fix'])
    sha=hashlib.sha256(b'bounded skill').hexdigest()
    d['stages'][0]['skills']=[{'skill_id':'fixture','version':1,'sha256':None}]
    registry=SimpleNamespace(get_face=lambda face:SimpleNamespace(model_dump=lambda **kwargs:{'id':face}),
        load_skill=Mock(return_value={'sha256':sha,'body':'bounded skill','operations':[]}))
    monkeypatch.setattr('core.faces.registry.get_registry',lambda:registry)
    runtime=WorkflowRuntime(SimpleNamespace(store=SimpleNamespace(pool=None)))
    result=await runtime.resolve(None,auth,proposal_args(),d,'00000000-0000-0000-0000-000000000001')
    assert result['skills']['orchestrator/fixture']['body']=='bounded skill'
    assert result['faces']['orchestrator']['definition']=={'id':'orchestrator'}
    d['stages'][0]['skills'][0]['sha256']='0'*64
    with pytest.raises(ExecutionError,match='skill_digest_mismatch'):
        await runtime.resolve(None,auth,proposal_args(),d,'00000000-0000-0000-0000-000000000001')
    d['stages'][0]['skills'][0]['sha256']=None
    registry.load_skill.side_effect=PermissionError('required tool forbidden')
    with pytest.raises(ExecutionError,match='face_or_skill_unavailable'):
        await runtime.resolve(None,auth,proposal_args(),d,'00000000-0000-0000-0000-000000000001')


async def test_catalog_uses_enrolled_identity_and_never_guesses_family(setup,monkeypatch):
    import sys
    from types import ModuleType
    from core.execution.workflows import WorkflowRuntime
    _,auth,_=setup
    execute=ModuleType('core.nodes.execute'); execute._is_tool_enabled=lambda face,backend:face=='builder' and backend=='fixture'
    monkeypatch.setitem(sys.modules,'core.nodes.execute',execute)
    registry=SimpleNamespace(all_faces=lambda:[SimpleNamespace(id='builder')])
    monkeypatch.setattr('core.faces.registry.get_registry',lambda:registry)
    runtime=WorkflowRuntime(SimpleNamespace(models=AsyncMock(return_value={'enrolled':{
        'backend':'fixture','model':'display name with no family','model_identity':'actual-identity','enabled':True}})))
    rows=await runtime.bindings(auth)
    assert rows[0]['family'] is None and rows[0]['model_identity']=='actual-identity'
    assert rows[0]['binding_id']=='enrolled' and rows[0]['tool_capable']


async def test_golden_fixtures(setup,monkeypatch):
    """Emit fixtures only on explicit invocation; every result uses real service."""
    import uuid
    sequence=iter(range(1,10000))
    monkeypatch.setattr('core.execution.workflows.uuid.uuid4',lambda:uuid.UUID(int=next(sequence)))
    s,a,click=setup; fixtures={}
    async def fixture(op,auth=None,label=None,**args):
        request={'contract_version':1,**args}
        result=await s.command(auth or a,'workflows.'+op,request)
        fixtures[label or op]={'operation':'workflows.'+op,'request':request,'result':result}
        return result
    s.test_only_runner_available=False
    await fixture('catalog')
    s.test_only_runner_available=True
    await fixture('list')
    await fixture('get',id='premade-plan-build-review-fix',revision=1)
    copied=(await fixture('copy',id='premade-plan-build-review-fix',revision=1,name='Fixture copy',request_key='copy'))['definition']
    await fixture('save',id=copied['id'],definition=copied['definition'],expected_revision=1,request_key='save')
    await fixture('delete',id=copied['id'],expected_revision=2,request_key='delete')
    proposals=[]
    for key in PREMADES:
        proposals.append((await fixture('propose',label='propose-'+key,**proposal_args(key,key)))['proposal'])
    fixtures['propose']=fixtures['propose-premade-plan-build-review-fix']
    p=proposals[0]
    await fixture('proposal',proposal_id=p['id'])
    w=(await fixture('grant_start',auth=click,proposal_id=p['id'],proposal_revision=1,proposal_digest=p['digest'],request_key='start'))['run']
    await fixture('runs')
    await fixture('run',run_id=w['id'])
    fixtures['run-accepted']=deepcopy(fixtures['run'])
    await fixture('events',run_id=w['id'],after_seq=0)
    await fixture('message',run_id=w['id'],stage_key='plan',text='Preserve the task',request_key='message')
    w=(await call(s,a,'run',run_id=w['id']))['run']
    w=(await fixture('pause',run_id=w['id'],expected_revision=w['revision'],request_key='pause'))['run']
    w=(await fixture('resume',auth=click,run_id=w['id'],expected_revision=w['revision'],request_key='resume'))['run']
    w=(await fixture('stop',run_id=w['id'],request_key='stop'))['run']
    await fixture('run',label='run-stopped',run_id=w['id'])
    await fixture('artifact',run_id=w['id'],artifact_id=w['report']['artifact_id'])
    # watch is broker-local, so its fixture comes from that real method.
    import importlib.util
    import sys
    import threading
    from contextlib import nullcontext
    from unittest.mock import patch
    from types import ModuleType
    broker_file=Path(__file__).resolve().parents[2]/'portal-broker/portal-broker.py'
    spec=importlib.util.spec_from_file_location('wb1_fixture_broker',broker_file)
    module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module
    shim=ModuleType('fcntl')
    def unavailable(*args): raise AssertionError('watch fixture must not exercise POSIX locking')
    shim.flock=unavailable
    with patch.dict(sys.modules,{'fcntl':shim}) if os.name=='nt' else nullcontext():
        spec.loader.exec_module(module)
    broker=object.__new__(module.Broker)
    broker._workflow_watch_lock=threading.Lock(); broker._workflow_watches={}
    request={'contract_version':1,'run_id':w['id'],'after_seq':w['event_seq'],'watch':False}
    fixtures['watch']={'operation':'workflows.watch','request':request,'result':broker.op_workflow_watch(request)}
    # WB2 will create owner waits; seed only the missing execution-produced row.
    second=(await start(s,click,proposals[1],key='second-start'))['run']
    r=await s.store.wf_get(None,a,'run',second['id']); r['run']['status']='needs_you'
    r['run']['needs_you']=[{'wait_id':'fixture-gate','kind':'gate','stage_key':'owner','conversation_id':None,'request_id':None,
                           'record_id':'fixture-gate','prompt':'Approve completion?','expires_at':None,'state':'open','requires_owner':True}]
    await s.store.wf_run(None,a,r)
    await fixture('gate',auth=click,run_id=second['id'],wait_id='fixture-gate',decision='approve',expected_revision=second['revision'],request_key='gate')
    same=proposal_args(key='same-model-proposal'); same['bindings']={k:'one' for k in same['bindings']}
    same_p=(await fixture('propose',label='propose-same-model',**same))['proposal']
    choice_request={'contract_version':1,'proposal_id':same_p['id'],'proposal_revision':1,'proposal_digest':same_p['digest'],'request_key':'missing-choice'}
    with pytest.raises(ExecutionError) as denied:
        await s.command(click,'workflows.grant_start',choice_request)
    fixtures['error-same-model-choice']={'operation':'workflows.grant_start','request':choice_request,'status':denied.value.status,
        'result':{'error':{'code':denied.value.code,'detail':denied.value.detail}}}
    await fixture('grant_start',auth=click,label='grant_start-same-model',proposal_id=same_p['id'],proposal_revision=1,
                  proposal_digest=same_p['digest'],allow_same_model_review=True,request_key='same-model-start')
    assistant=proposal_args(key='assistant-proposal'); assistant['origin_conversation_id']='00000000-0000-0000-0000-000000000002'
    await fixture('propose',label='propose-assistant',**assistant)
    s.test_only_runner_available=False
    unavailable_p=(await fixture('propose',label='propose-unavailable',**proposal_args(key='runner-absent')))['proposal']
    unavailable_w=(await fixture('grant_start',auth=click,label='grant_start-unavailable',proposal_id=unavailable_p['id'],
        proposal_revision=1,proposal_digest=unavailable_p['digest'],request_key='accept-without-runner'))['run']
    await fixture('run',label='run-accepted-unavailable',run_id=unavailable_w['id'])
    errors=[('not-found','run',a,{'run_id':'missing'}),('authority','grant_start',replace(a,token_type='agent'),
             {'proposal_id':p['id'],'proposal_revision':1,'proposal_digest':p['digest'],'request_key':'agent'}),
            ('validation','catalog',a,{'unknown':True}),('stale','grant_start',click,
             {'proposal_id':p['id'],'proposal_revision':2,'proposal_digest':p['digest'],'request_key':'stale'})]
    for label,op,auth,args in errors:
        try: await call(s,auth,op,**args)
        except ExecutionError as exc:
            fixtures['error-'+label]={'operation':'workflows.'+op,'request':{'contract_version':1,**args},'status':exc.status,
                                     'result':{'error':{'code':exc.code,'detail':exc.detail}}}
        else: raise AssertionError('error fixture unexpectedly succeeded')
    from core.execution.store import Store
    unavailable=WorkflowService(SimpleNamespace(store=Store(None,'lane')),runtime=s.runtime,clock=s.clock)
    with pytest.raises(ExecutionError) as storage_error:
        await call(unavailable,a,'catalog')
    fixtures['error-unavailable']={'operation':'workflows.catalog','request':{'contract_version':1},'status':storage_error.value.status,
        'result':{'error':{'code':storage_error.value.code,'detail':storage_error.value.detail}}}
    if os.environ.get('WORKFLOW_WRITE_FIXTURES')=='1':
        dest=Path(__file__).resolve().parents[3]/'runs/golive/workflow-fixtures'
        dest.mkdir(parents=True,exist_ok=True)
        for name,record in fixtures.items():
            for part in ('request','result'):
                (dest/(name+'.'+part+'.json')).write_text(json.dumps(wire_integers(record[part]),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        (dest/'manifest.json').write_text(json.dumps({name:{'operation':r['operation'],'status':r.get('status',200)} for name,r in fixtures.items()},indent=2)+'\n')
    assert set(['catalog','list','get','save','copy','delete','propose','proposal','grant_start','runs','run','events','pause','resume','stop','message','gate','artifact']) <= fixtures.keys()
