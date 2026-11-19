"""Actual stage loop with scripted models; no providers or PostgreSQL claims."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from core.execution.agent_turn import StageContext, ToolRefusal, READ_TOOLS, BUILDER_EFFECTS, REVIEWER_READ_TOOLS
from core.execution.models import ExecutionError
from core.execution.workflow_context import current
from test_workflow_controller import harness, drive, ScriptedModel, mock_redis


@pytest.mark.parametrize('tool,args',[
    ('work_catalog',{}), ('fs_write',{'path':'forbidden.py','content':'bad'}),
    ('skill_view',{'skill_id':'bob.work'}),
    ('ask_operator',{}),
    ('pc_list',{'path':'.'}),  # BU17: refused with advice, never a wait nobody can answer
])
async def test_refusal_is_named_no_execution_and_stage_can_finish(harness,monkeypatch,tool,args):
    h=harness
    original=ScriptedModel.ainvoke
    seen=[]
    async def invoke(model,messages):
        ctx=current.get()
        if ctx.stage['key']=='plan':
            seen.append(messages[:])
            if not any(isinstance(m,ToolMessage) for m in messages):
                model.round+=1
                return AIMessage(content='',tool_calls=[{'name':tool,'args':args,'id':'refused'}])
            result=next(m for m in messages if isinstance(m,ToolMessage))
            assert result.name==tool and tool in result.content
            assert json.loads(result.content)['status']=='refused'
            return AIMessage(content='The bounded plan is ready.')
        return await original(model,messages)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await h.new(); w=await drive(h,row)
    assert w['status']=='completed',w['reason']
    stage=next(s for s in w['stages'] if s['key']=='plan')
    refusal=stage['receipts'][0]
    assert refusal['tool']==tool and refusal['result']['status']=='refused'
    assert stage['calls']==2
    events=h.store.events['lane','owner',row['id']]
    assert any(e['kind']=='tool_result' and e['payload']['tool']==tool and
               e['payload']['result'].get('status')=='refused' for e in events)
    assert not any(p.name=='forbidden.py' for p in h.roots[w['project_id']].rglob('*'))
    assert all('forbidden.py' not in a['payload'] for a in h.store.pool.approvals.values())
    assert len(seen)==2


async def test_repeated_refusals_fail_with_tool_in_stage_and_report(harness,monkeypatch):
    original=ScriptedModel.ainvoke
    async def invoke(model,messages):
        if current.get().stage['key']=='plan':
            return AIMessage(content='',tool_calls=[{'name':'work_catalog','args':{},'id':'refused'}])
        return await original(model,messages)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await harness.new(); w=await drive(harness,row)
    assert w['status']=='failed' and w['reason_code']=='tool_refusal_limit'
    assert 'planning stage kept asking for work_catalog' in w['reason']
    plan=next(s for s in w['stages'] if s['key']=='plan')
    assert plan['calls']==3 and len(plan['receipts'])==3
    assert plan['reason']==w['reason']==w['report']['reason']
    assert all(s['state']=='skipped' for s in w['stages'] if s['key']!='plan')


async def test_bound_list_prompt_and_gate_agree_and_skills_are_filtered(harness,monkeypatch):
    original=ScriptedModel.ainvoke
    async def invoke(model,messages):
        ctx=current.get()
        names={t.name for t in getattr(model,'tools',[])}
        card=next(m.content for m in messages if 'Available tools: ' in str(m.content))
        assert set(card.split('Available tools: ',1)[1].split(', '))==names
        assert names==ctx.bound_names
        assert not names & {'work_catalog','work_prepare','work_start','workspace_create','workspace_mount'}
        assert all(ctx.permits_tool(n) for n in names)
        if ctx.spec.get('access')=='read': assert not names & BUILDER_EFFECTS
        if ctx.face=='builder': assert BUILDER_EFFECTS <= names
        if ctx.face=='reviewer':
            assert names == {'bob_guide'} | REVIEWER_READ_TOOLS
            assert names == {n for n in READ_TOOLS | BUILDER_EFFECTS if ctx.permits_tool(n)}
        from core.faces.registry import get_default_registry
        assert ctx.skill_summaries(get_default_registry())==[]
        assert not any('Available skill summaries' in str(m.content) for m in messages)
        assert any('chat procedure' in str(m.content).lower() for m in messages)
        return await original(model,messages)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await harness.new(); w=await drive(harness,row)
    assert w['status']=='completed',w['reason']


@pytest.mark.parametrize('code',[
    'binding_changed','binding_mismatch','grant_expired','authorization_revoked',
    'call_budget_exhausted','job_attempt_budget_exhausted','job_time_budget_exhausted',
    'active_time_budget_exhausted','owner_stopped','run_terminal','stale_controller',
])
async def test_validation_errors_are_never_recoverable(harness,monkeypatch,code):
    original=harness.controller.validate
    async def validate(ctx,*,tools=False):
        if tools: raise ExecutionError(code,409)
        return await original(ctx,tools=tools)
    monkeypatch.setattr(harness.controller,'validate',validate)
    original_model=ScriptedModel.ainvoke
    async def invoke(model,messages):
        if current.get().stage['key']=='plan':
            return AIMessage(content='',tool_calls=[{'name':'work_catalog','args':{},'id':'refused'}])
        return await original_model(model,messages)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await harness.new(); w=await drive(harness,row)
    assert w['status']!='completed' and w['reason_code']==code
    assert not any(s.get('receipts') for s in w['stages'])


@pytest.mark.parametrize('summary,name,args,code',[
    (True,'get_server_time',{},'summary_tools_forbidden'),
    (False,'fs_read',{'path':'outside.txt'},'scope_denied'),
])
async def test_argument_and_summary_denials_are_refusals(harness,monkeypatch,summary,name,args,code):
    from core.execution.models import Authority
    controller=harness.controller
    record={'run':{'grant':{'expires_at':'2099-01-01T00:00:00Z'}},
            'coverage':{'limits':{'workflow_tool_rounds':10},
                        'authority':{'workspace_ops':['read','write','edit','check']},'checks':[]}}
    ctx=StageContext(controller,{},record,{'key':'test'},{'access':'write','summary':summary},{},'builder')
    monkeypatch.setattr(controller,'validate',AsyncMock())
    monkeypatch.setattr(controller,'root',lambda r:__import__('pathlib').Path('/workspace'))
    monkeypatch.setattr(controller,'auth',lambda r:Authority('lane','owner','owner',
        {'may_touch':['/workspace/inside/**'],'may_not_touch':[]},'human'))
    with pytest.raises(ToolRefusal) as exc: await ctx.before_tool(name,args)
    assert exc.value.code==code


@pytest.mark.parametrize('face,access',[('orchestrator','read'),('builder','write'),('reviewer','read')])
async def test_pc_tools_are_not_offered_and_refused_with_workspace_advice(harness,monkeypatch,face,access):
    from core.tools.pc_tools import PC_TOOL_NAMES
    controller=harness.controller
    record={'run':{'grant':{'expires_at':'2099-01-01T00:00:00Z'}},
            'coverage':{'limits':{'workflow_tool_rounds':10},
                        'authority':{'workspace_ops':['read','write','edit','check']},'checks':[]}}
    ctx=StageContext(controller,{},record,{'key':'plan'},{'access':access},{},face)
    monkeypatch.setattr(controller,'validate',AsyncMock())
    park=AsyncMock(); monkeypatch.setattr(controller,'park',park)
    monkeypatch.setattr(controller,'receipt',AsyncMock())
    assert not any(ctx.permits_tool(n) for n in PC_TOOL_NAMES)
    tools=ctx.filter_tools([SimpleNamespace(name=n) for n in [*PC_TOOL_NAMES,'fs_list','fs_read','bob_guide']])
    assert {t.name for t in tools}=={'fs_list','fs_read','bob_guide'}
    with pytest.raises(ToolRefusal) as exc: await ctx.before_tool('pc_list',{'path':'.'})
    assert exc.value.code=='workflow_effect_denied' and 'no PC access' in exc.value.detail
    park.assert_not_awaited()
    told=json.loads(await ctx.refuse('pc_list',exc.value,{'path':'.'}))['error']['detail']
    assert 'Use the available tools: fs_list, fs_read.' in told and 'project workspace' in told


@pytest.mark.parametrize('excluded,refused',[
    ([],False),
    (['/other-lane/**','/workspace-2/**'],False),  # a shared machine: other lanes' folders
    (['/workspace/secret/**'],True),(['/workspace/notes.txt'],True),(['/workspace'],True),
    (['/**'],True),(['**/private'],True),(['/work*'],True),
])
async def test_shell_scope_ignores_exclusions_outside_the_workspace(harness,monkeypatch,excluded,refused):
    """Owner's live run 2026-11-18: the Builder's shell was refused in every workflow
    because the owner scope listed other lanes' folders as exclusions."""
    from core.execution.models import Authority
    from core.execution.agent_turn import exclusion_reaches
    controller=harness.controller
    record={'run':{'grant':{'expires_at':'2099-01-01T00:00:00Z'}},
            'coverage':{'limits':{'workflow_tool_rounds':10},
                        'authority':{'workspace_ops':['read','write','edit','check']},'checks':[]}}
    ctx=StageContext(controller,{},record,{'key':'test'},{'access':'write','summary':False},{},'builder')
    monkeypatch.setattr(controller,'validate',AsyncMock())
    monkeypatch.setattr(controller,'root',lambda r:__import__('pathlib').Path('/workspace'))
    monkeypatch.setattr(controller,'auth',lambda r:Authority('lane','owner','owner',
        {'may_touch':['/workspace/**'],'may_not_touch':excluded},'human'))
    assert any(exclusion_reaches('/workspace',p) for p in excluded) is refused
    if refused:
        with pytest.raises(ToolRefusal) as exc:
            await ctx.before_tool('shell_run',{'command':'python -m compileall -q .'})
        assert exc.value.code=='scope_denied'
    else:
        await ctx.before_tool('shell_run',{'command':'python -m compileall -q .'})


@pytest.mark.parametrize('encoded',[False,True])
async def test_tool_path_refusal_recovers_without_retrying_effect(harness,monkeypatch,encoded):
    from langchain_core.tools import tool
    from core.nodes import execute
    original_tools=execute.get_all_tools
    invoked=[]
    @tool
    async def get_server_time() -> str:
        """Return a fixed refusal for the tool-loop regression."""
        invoked.append(True)
        if not encoded: raise ExecutionError('scope_denied',403)
        return json.dumps({'status':'refused','error':{'code':'scope_denied'}})
    async def tools(names):
        return [get_server_time if t.name=='get_server_time' else t for t in await original_tools(names)]
    monkeypatch.setattr(execute,'get_all_tools',tools)
    original=ScriptedModel.ainvoke
    async def invoke(model,messages):
        if current.get().stage['key']=='plan':
            if not any(isinstance(m,ToolMessage) for m in messages):
                return AIMessage(content='',tool_calls=[{'name':'get_server_time','args':{},'id':'refused'}])
            return AIMessage(content='The supplied evidence is enough to plan.')
        return await original(model,messages)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await harness.new(); w=await drive(harness,row)
    assert w['status']=='completed',w['reason']
    assert invoked==[True]
    plan=next(s for s in w['stages'] if s['key']=='plan')
    assert plan['receipts'][0]['result']['status']=='refused'
    assert plan['receipts'][0]['tool']=='get_server_time'


async def test_unknown_encoded_safety_failure_stops_with_named_tool(harness,monkeypatch):
    from core.execution.agent_turn import ToolStopped
    controller=SimpleNamespace(receipt=AsyncMock())
    ctx=StageContext(controller,{}, {'coverage':{'limits':{'workflow_tool_rounds':10}}},
        {'key':'build'},{},None,'builder')
    with pytest.raises(ToolStopped) as exc:
        await ctx.tool_result('fs_edit',json.dumps({'status':'refused','error':{'code':'builder_session_expired'}}))
    assert exc.value.code=='builder_session_expired'
    assert 'fs_edit' in exc.value.detail
    controller.receipt.assert_awaited_once()


@pytest.mark.parametrize('code',['builder_preimage_changed','preview_changed','checkpoint_not_found'])
async def test_stale_file_or_missing_checkpoint_is_a_recoverable_refusal(code):
    # Nothing was dispatched: the stage reads again and retries instead of ending the run.
    ctx=StageContext(SimpleNamespace(receipt=AsyncMock()),{}, {'coverage':{'limits':{'workflow_tool_rounds':10}}},
        {'key':'build'},{},None,'builder')
    with pytest.raises(ToolRefusal) as exc:
        await ctx.tool_result('fs_edit',json.dumps({'status':'refused','error':{'code':code}}))
    assert exc.value.code==code


@pytest.mark.parametrize('code',['invalid_pc_command','invalid_pc_path','pc_write_too_large','invalid_pc_preimage'])
async def test_pc_argument_validation_is_a_recoverable_refusal(code):
    ctx=StageContext(SimpleNamespace(receipt=AsyncMock()),{},
        {'coverage':{'limits':{'workflow_tool_rounds':10}}},{'key':'plan'},{},None,'orchestrator')
    with pytest.raises(ToolRefusal) as exc:
        await ctx.tool_result('pc_write',json.dumps({'status':'failed','error':{'code':code}}))
    assert exc.value.code==code


async def test_skill_list_has_no_incompatible_skills_and_remains_usable(harness,monkeypatch):
    original=ScriptedModel.ainvoke
    async def invoke(model,messages):
        if current.get().stage['key']=='plan':
            if not any(isinstance(m,ToolMessage) for m in messages):
                return AIMessage(content='',tool_calls=[{'name':'skill_list','args':{},'id':'skills'}])
            assert json.loads(next(m for m in messages if isinstance(m,ToolMessage)).content)==[]
            return AIMessage(content='The plan is complete from the supplied evidence.')
        return await original(model,messages)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await harness.new(); w=await drive(harness,row)
    assert w['status']=='completed',w['reason']


async def test_reviewer_lists_reads_and_searches_real_workspace(harness, monkeypatch):
    original = ScriptedModel.ainvoke
    observed = []
    async def invoke(model, messages):
        ctx = current.get()
        if ctx.face == 'reviewer' and ctx.spec.get('output') == 'verdict':
            replies = [m for m in messages if isinstance(m, ToolMessage)]
            if replies:
                reply = replies[-1]
                data = json.loads(reply.content)
                observed.append(reply.name)
                if reply.name == 'fs_list':
                    assert 'app.py' in data['items']
                elif reply.name == 'fs_read':
                    assert data['content'] == 'value = "ready"\n'
                elif reply.name == 'fs_search':
                    assert 'app.py' in reply.content and 'ready' in reply.content
            requests = [('fs_list', {'path': ''}), ('fs_read', {'path': 'app.py'}),
                        ('fs_search', {'pattern': 'ready', 'glob': '*.py'})]
            if len(replies) < len(requests):
                name, args = requests[len(replies)]
                return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': name}])
        return await original(model, messages)
    monkeypatch.setattr(ScriptedModel, 'ainvoke', invoke)
    row = await harness.new()
    run = await drive(harness, row)
    assert run['status'] == 'completed', run['reason']
    assert observed == ['fs_list', 'fs_read', 'fs_search']
    review = next(s for s in run['stages'] if '/review-' in s['key'])
    assert [r['tool'] for r in review['receipts']] == observed


@pytest.mark.parametrize('name,args', [
    ('fs_write', {'path': 'forbidden.py', 'content': 'bad'}),
    ('fs_edit', {'path': 'app.py', 'old': 'ready', 'new': 'bad'}),
    ('fs_delete', {'path': 'app.py'}),
    ('shell_run', {'command': 'python -m compileall -q .'}),
])
async def test_reviewer_cannot_mutate_or_run_commands(harness, monkeypatch, name, args):
    original = ScriptedModel.ainvoke
    async def invoke(model, messages):
        if current.get().face == 'reviewer':
            replies = [m for m in messages if isinstance(m, ToolMessage)]
            if not replies:
                return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': 'denied'}])
            result = json.loads(replies[-1].content)
            assert result['status'] == 'refused'
            assert result['error']['code'] == 'workflow_effect_denied'
        return await original(model, messages)
    monkeypatch.setattr(ScriptedModel, 'ainvoke', invoke)
    row = await harness.new()
    run = await drive(harness, row)
    assert run['status'] == 'completed', run['reason']
    root = harness.roots[run['project_id']]
    assert not (root / 'forbidden.py').exists()
    assert (root / 'app.py').read_text() == 'value = "ready"\n'


@pytest.mark.parametrize('name,args', [
    ('fs_read', {'path': 'outside.txt'}), ('fs_list', {'path': 'outside'}),
    ('fs_search', {'pattern': 'anything'}),
])
async def test_reviewer_reads_keep_builder_scope_limits(harness, monkeypatch, name, args):
    from core.execution.models import Authority
    controller = harness.controller
    ctx = StageContext(controller, {}, {'coverage': {'limits': {'workflow_tool_rounds': 10},
        'authority': {'workspace_ops': ['read']}}}, {'key': 'review'}, {'access': 'read'}, {}, 'reviewer')
    monkeypatch.setattr(controller, 'validate', AsyncMock())
    monkeypatch.setattr(controller, 'root', lambda r: __import__('pathlib').Path('/workspace'))
    monkeypatch.setattr(controller, 'auth', lambda r: Authority('lane', 'owner', 'owner',
        {'may_touch': ['/workspace/inside/**'], 'may_not_touch': []}, 'human'))
    token = current.set(ctx)
    try:
        with pytest.raises(ToolRefusal) as error:
            await ctx.before_tool(name, args)
        assert error.value.code == 'scope_denied'
    finally:
        current.reset(token)
