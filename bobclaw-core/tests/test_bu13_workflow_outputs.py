"""BU13 contracts and repairs through the real stage loop and charged call guard."""
from copy import deepcopy
import json
import time

import pytest
from langchain_core.messages import AIMessage

from core.execution.models import ExecutionError, canonical
from core.execution.workflow_output import output_example, output_instructions, read_object
from core.execution.workflow_schema import validate_units, validate_verdict
from core.execution.workflow_context import current
from core.execution.workflows import LIMITS
from test_workflow_controller import harness, mock_redis, ScriptedModel, drive


def verdict():
    return {'verdict':'pass','summary':'Observed the frozen files.','findings':[],
            'question':None,'snapshot_digest':'a'*64}


@pytest.mark.parametrize('kind', ['verdict','units'])
def test_stated_example_validates_and_fields_are_generated(kind):
    message=output_instructions(kind,snapshot_digest='a'*64,max_units=16)
    example=read_object(message.split('Filled-in example (use your actual judgment and evidence):\n')[1])
    assert example==output_example(kind,'a'*64)
    if kind=='verdict':
        validate_verdict(example,'a'*64)
        for field in verdict(): assert field in message
        assert 'pass =' in message and 'changes =' in message and 'blocked =' in message
        assert 'null unless verdict is blocked' in message and 'a'*64 in message
        assert 'tool receipts' in message and 'never invent' in message
    else:
        validate_units(example,16)
        assert '1 to 16 items' in message and 'depth at most 16' in message


@pytest.mark.parametrize('wrap', [
    lambda x:x, lambda x:'```json\n'+x+'\n```',
    lambda x:'I reviewed the files.\n'+x+'\nThat is the observed result.'])
def test_generous_reader(wrap):
    assert validate_verdict(read_object(wrap(canonical(verdict()))))==verdict()


@pytest.mark.parametrize('answer',[
    '{} {}', '```json\n{}\n``` and {}', '[{}]', '{"outer":{}', 'It passed.',
    '{"verdict":"pass","verdict":"changes"}', '{"x":NaN}',
])
def test_ambiguous_or_non_json_answers_are_refused(answer):
    with pytest.raises(ExecutionError): read_object(answer)


def test_unknown_extras_are_ignored_without_filling_missing_contract_keys():
    value=verdict()
    value.update(criteria=[{'verdict':'met'}],passed_checks=True)
    value['findings']=[{'id':'issue','detail':'Observed','evidence_ids':[],'extra':True}]
    result=validate_verdict(value)
    assert set(result)==set(verdict()) and 'extra' not in result['findings'][0]
    del value['summary']
    with pytest.raises(ExecutionError) as error: validate_verdict(value)
    assert "key 'summary' is missing" in error.value.detail


def script_answers(monkeypatch,h,kind,answers):
    original=ScriptedModel.ainvoke
    counts={}
    async def invoke(model,messages):
        ctx=current.get()
        response=await original(model,messages)
        if not ctx.summary and ctx.spec['output']==kind:
            number=counts.get(ctx.stage['key'],0)
            counts[ctx.stage['key']]=number+1
            if ctx.repairing:
                assert not getattr(model,'tools',None), 'Repair model was offered tools.'
                assert ctx.stage['conversation_id'] in h.store.pool.conversations
            if number<len(answers):
                replacement=answers[number](ctx,json.loads(response.content)) if callable(answers[number]) else answers[number]
                return replacement if isinstance(replacement,AIMessage) else AIMessage(content=replacement)
        return response
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    return counts


def events(h,row,kind=None):
    result=h.store.events['lane','owner',row['id']]
    return [e for e in result if kind is None or e['kind']==kind]


@pytest.mark.parametrize('wrong_count',[1,2])
async def test_wrong_once_or_twice_then_right_completes_in_same_stage(harness,monkeypatch,wrong_count):
    h=harness
    script_answers(monkeypatch,h,'verdict',['It looks good.']*wrong_count)
    row=await h.new()
    w=await drive(h,row)
    assert w['status']=='completed',w['reason']
    stage=next(s for s in w['stages'] if '/review-' in s['key'])
    assert stage['calls']==wrong_count+1
    assert len(stage['format_repairs'])==wrong_count
    assert w['budget']['consumed_calls']==len(h.calls)
    calls=[c for c in h.calls if c['stage']==stage['key']]
    assert len(calls)==wrong_count+1
    assert 'It looks good.' in str(calls[-1]['messages'])
    history=[m for m in h.store.pool.history if m['conversation']==stage['conversation_id']]
    assert len([m for m in history if m['role']=='user' and 'Answer-format repair' in m['content']])==wrong_count
    assert any(e['payload'].get('type')=='format_repaired' for e in events(h,row,'activity'))
    assert len([s for s in w['stages'] if '/review-' in s['key']])==1


@pytest.mark.parametrize('answer',['It passed.',canonical({**verdict(),'verdict':'met'})])
async def test_three_wrong_answers_fail_and_leave_built_files(harness,monkeypatch,answer):
    h=harness
    script_answers(monkeypatch,h,'verdict',[answer]*3)
    row=await h.new()
    w=await drive(h,row)
    assert w['status']=='failed' and w['reason_code']=='invalid_review_verdict'
    assert 'could not be read after two requests' in w['reason']
    assert 'files are as the build stage left them' in w['reason']
    stage=next(s for s in w['stages'] if '/review-' in s['key'])
    assert stage['calls']==3 and len(stage['format_repairs'])==2
    assert w['budget']['consumed_calls']==len(h.calls)
    assert (h.roots[w['project_id']]/'app.py').read_text()=='value = "ready"\n'
    assert not w['report']['checks']
    assert not any('/fix-' in s['key'] for s in w['stages'])
    assert all(s['state']=='skipped' for s in w['stages'] if s['kind']=='gate')


BAD_VERDICTS=[
    (lambda v:{k:x for k,x in v.items() if k!='summary'}, "key 'summary' is missing"),
    (lambda v:{**v,'verdict':'met'}, "'verdict' must be one of pass, changes, blocked"),
    (lambda v:{**v,'summary':False}, "'summary' must be a string"),
    (lambda v:{**v,'question':'Approve?'}, "'question' must be null"),
    (lambda v:{**v,'verdict':'blocked'}, 'blocked review needs a nonblank'),
    (lambda v:{**v,'snapshot_digest':'oops'}, "'snapshot_digest' must contain 64 to 64"),
    (lambda v:{**v,'findings':{}}, "'findings' must be an array"),
    (lambda v:{**v,'findings':[{'id':'bad ID','detail':'x','evidence_ids':[]}]}, "findings[0].id"),
    (lambda v:{**v,'findings':[{'id':'issue','detail':'x'}]}, "findings[0].evidence_ids"),
    (lambda v:{**v,'findings':[{'id':'issue','detail':'x','evidence_ids':['']} ]}, "findings[0].evidence_ids[0]"),
]


@pytest.mark.parametrize('change,expected',BAD_VERDICTS)
async def test_specific_validation_problem_reaches_repair_message(harness,monkeypatch,change,expected):
    h=harness
    script_answers(monkeypatch,h,'verdict',[lambda ctx,v:canonical(change(v))])
    row=await h.new()
    w=await drive(h,row)
    assert w['status']=='completed',w['reason']
    stage=next(s for s in w['stages'] if '/review-' in s['key'])
    assert expected in stage['format_repairs'][0]['message']


@pytest.mark.parametrize('wrapper',[
    lambda s:'```json\n'+s+'\n```',lambda s:'Review result:\n'+s+'\nInspected.'])
async def test_fenced_wrapped_answers_complete_without_repair(harness,monkeypatch,wrapper):
    h=harness
    script_answers(monkeypatch,h,'verdict',[lambda ctx,v:wrapper(canonical(v))])
    row=await h.new()
    w=await drive(h,row)
    assert w['status']=='completed'
    assert not any(s.get('format_repairs') for s in w['stages'])


async def test_two_objects_trigger_repair_instead_of_choosing_pass(harness,monkeypatch):
    h=harness
    script_answers(monkeypatch,h,'verdict',[lambda ctx,v:canonical(v)+' '+canonical({**v,'verdict':'changes'})])
    row=await h.new()
    w=await drive(h,row)
    stage=next(s for s in w['stages'] if '/review-' in s['key'])
    assert w['status']=='completed' and 'multiple JSON' in stage['format_repairs'][0]['problem']


async def test_supplied_live_answer_shape_requests_correction(harness,monkeypatch):
    # The brief quotes an abbreviated object with ellipses, not the full DB row.
    # Replay its described keys/values without claiming to possess the raw row.
    described={'verdict':'met','criteria':[{'id':str(i),'verdict':'met','evidence':'Read hello.txt'} for i in range(3)],
               'plan_review':{},'defects':[],'limits':'Only the supplied workspace was inspected.'}
    h=harness
    script_answers(monkeypatch,h,'verdict',[canonical(described)])
    row=await h.new()
    w=await drive(h,row)
    stage=next(s for s in w['stages'] if '/review-' in s['key'])
    assert w['status']=='completed'
    assert "'verdict' must be one of pass, changes, blocked" in stage['format_repairs'][0]['problem']
    assert stage['calls']==2


async def test_copied_digest_is_repairable(harness,monkeypatch):
    h=harness
    script_answers(monkeypatch,h,'verdict',[lambda ctx,v:canonical({**v,'snapshot_digest':'b'*64})])
    row=await h.new()
    w=await drive(h,row)
    stage=next(s for s in w['stages'] if '/review-' in s['key'])
    assert w['status']=='completed'
    assert 'snapshot digest does not match; use '+stage['base_snapshot']['digest'] in stage['format_repairs'][0]['problem']


@pytest.mark.parametrize('during_repair',[False,True])
async def test_actual_workspace_change_is_hard_stop(harness,monkeypatch,during_repair):
    h=harness
    def changed(ctx,v):
        (h.roots[ctx.record['run']['project_id']]/'app.py').write_text('external change\n')
        return canonical(v)
    script_answers(monkeypatch,h,'verdict',(['bad answer'] if during_repair else [])+[changed])
    row=await h.new()
    w=await drive(h,row)
    assert w['status']=='failed' and w['reason_code']=='review_snapshot_mismatch'
    stage=next(s for s in w['stages'] if '/review-' in s['key'])
    assert len(stage.get('format_repairs',[]))==int(during_repair)
    assert not w['report']['checks']


@pytest.mark.parametrize('limit',['calls','rounds','time'])
async def test_repairs_obey_existing_call_round_and_time_limits(harness,monkeypatch,limit):
    h=harness
    def wrong(ctx,v):
        if limit=='rounds': ctx.round_limit=ctx.model_calls
        if limit=='time': ctx.started=time.monotonic()-ctx.record['coverage']['limits']['workflow_turn_seconds']-1
        return 'Wrong format.'
    script_answers(monkeypatch,h,'verdict',[wrong])
    row=await h.new(budget=4 if limit=='calls' else None)
    w=await drive(h,row)
    expected={'calls':'call_budget_exhausted','rounds':'tool_round_limit','time':'stage_time_budget_exhausted'}[limit]
    assert w['status']=='expired' and w['reason_code']==expected
    assert len(h.calls)==4 and w['budget']['consumed_calls']==4


async def test_tools_requested_during_repair_never_run(harness,monkeypatch):
    h=harness
    def tool_attempt(ctx,v):
        return AIMessage(content=canonical(v),tool_calls=[{'name':'fs_write','args':{'path':'forbidden.txt','content':'no'},'id':'no'}])
    script_answers(monkeypatch,h,'verdict',['bad answer',tool_attempt])
    row=await h.new()
    w=await drive(h,row)
    assert w['status']=='completed'
    assert not (h.roots[w['project_id']]/'forbidden.txt').exists()
    stage=next(s for s in w['stages'] if '/review-' in s['key'])
    assert stage['calls']==3 and 'tools are unavailable' in stage['format_repairs'][1]['problem']


@pytest.mark.parametrize('wrong_count',[1,2,3])
async def test_units_get_the_same_bounded_repairs(harness,monkeypatch,wrong_count):
    h=harness
    # Keep this test focused on planner format repairs rather than concurrent IO.
    async def serial_limits(auth): return {**LIMITS,'workflow_parallelism':1}
    monkeypatch.setattr(h.service.runtime,'limits',serial_limits)
    script_answers(monkeypatch,h,'units',['A prose plan.']*wrong_count)
    row=await h.new('premade-sprint-board')
    w=await drive(h,row)
    board=next(s for s in w['stages'] if s['key']=='board')
    assert board['calls']==min(wrong_count+1,3)
    assert len(board['format_repairs'])==min(wrong_count,2)
    assert w['budget']['consumed_calls']==len(h.calls)
    if wrong_count<3:
        assert w['status']=='completed',w['reason']
    else:
        assert w['status']=='failed' and w['reason_code']=='invalid_units'
        assert 'could not be read after two requests' in w['reason']
        assert not any(s.get('unit_id') for s in w['stages'])
        assert all(s['state']=='skipped' for s in w['stages'] if s['kind']=='foreach')


async def test_units_prompt_and_validator_use_foreach_limit(harness,monkeypatch):
    h=harness
    async def serial_limits(auth): return {**LIMITS,'workflow_parallelism':1}
    monkeypatch.setattr(h.service.runtime,'limits',serial_limits)
    def too_many(ctx,v):
        unit=v['units'][0]
        return canonical({'units':[{**unit,'id':'u'+str(i),'depends_on':[]} for i in range(17)]})
    script_answers(monkeypatch,h,'units',[too_many])
    row=await h.new('premade-sprint-board')
    w=await drive(h,row)
    board=next(s for s in w['stages'] if s['key']=='board')
    assert w['status']=='completed'
    assert "'units' must have 1 to 16 items" in board['format_repairs'][0]['problem']
    assert '1 to 16 items' in board['envelope']['output_instructions']


@pytest.mark.parametrize('mutate,expected',[
    (lambda u:u[0].pop('title'),"units[0].title"),
    (lambda u:u[0].update(depends_on=['missing']),'unknown dependency'),
    (lambda u:u.extend(deepcopy(u)),'duplicate id'),
    (lambda u:u[0].update(depends_on=[u[0]['id']]),'dependency cycle'),
])
def test_units_specific_diagnostics(mutate,expected):
    value=output_example('units')
    mutate(value['units'])
    with pytest.raises(ExecutionError) as error: validate_units(value,16)
    assert expected in error.value.detail


async def test_one_final_report_contains_summary_charge_and_is_idempotent(harness):
    h=harness
    row=await h.new(origin=True)
    w=await drive(h,row)
    reports=events(h,row,'report')
    assert len(reports)==1
    assert reports[0]['payload']['report']['assistant_summary']=='delivered'
    assert reports[0]['payload']['report']['calls_used']==len(h.calls)
    await h.controller.tick(row)
    assert len(events(h,row,'report'))==1
    assert w['report']['calls_used']==len(h.calls)


@pytest.mark.parametrize('ending',['invalid-review','budget','summary-failure','no-summary-binding'])
async def test_failure_and_skipped_summary_still_publish_one_final_report(harness,monkeypatch,ending):
    h=harness
    if ending=='invalid-review': h.bad_review=True
    if ending=='summary-failure':
        original=ScriptedModel.ainvoke
        async def invoke(model,messages):
            response=await original(model,messages)
            if current.get().summary: raise RuntimeError('scripted summary failure')
            return response
        monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await h.new(origin=True,budget=1 if ending=='budget' else None)
    if ending=='no-summary-binding':
        async def remove(c,r): r['coverage'].pop('assistant_binding')
        await h.controller.change(row,remove)
    w=await drive(h,row)
    reports=events(h,row,'report')
    assert len(reports)==1
    final=reports[0]['payload']['report']
    assert final['calls_used']==w['budget']['consumed_calls']==len(h.calls)
    assert final['assistant_summary'] in {'delivered','skipped'}
    assert final['outcome']==w['status']
