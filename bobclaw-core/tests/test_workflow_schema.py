import copy
import pytest
from core.execution.models import ExecutionError
from core.execution.workflow_schema import validate, bounds, validate_inputs, stages
from core.execution.workflows import PREMADES


@pytest.fixture(autouse=True)
def mock_redis():
    # This pure data validator never imports the interactive model/tool loop.
    yield


def premade():
    return copy.deepcopy(next(iter(PREMADES.values())))


@pytest.mark.parametrize('d',PREMADES.values())
def test_premades(d):
    assert validate(d)==d
    assert d['authority']['checks']==[]
    assert [k for k,v in d['inputs'].items() if v['required']]==['task']
    validate_inputs(d,{'task':'Build the thing'})
    assert 0<bounds(d)<=4096


@pytest.mark.parametrize('path',[
    (),('agents','builder'),('inputs','task'),('workspace',),('authority',),('budget',),
    ('stages',0),('stages',0,'inputs','task'),('stages',1,'build'),
])
def test_unknown_keys_at_each_object(path):
    d=premade(); target=d
    for k in path: target=target[k]
    target['surprise']=True
    with pytest.raises(ExecutionError): validate(d)


@pytest.mark.parametrize('change',[
    lambda d:d['stages'][0].update(depends_on=['report']),
    lambda d:d['stages'][0].update(depends_on=['missing']),
    lambda d:d['stages'][1]['inputs'].update(oops={'stage':'owner','output':'text'}),
    lambda d:d['stages'][1]['inputs'].update(oops={'stage':'plan','output':'items'}),
    lambda d:d['stages'][1]['inputs'].update(oops={'input':'not_declared'}),
    lambda d:d['stages'][1]['inputs'].update(oops={'item':True}),
    lambda d:d['stages'][1]['inputs'].update(oops={'literal':'x'*8193}),
    lambda d:d['stages'][1].update(check_ids=['missing']),
    lambda d:d['stages'][1]['build'].update(agent='missing'),
    lambda d:d['stages'][1]['build'].update(skills=[{'skill_id':'s','version':True,'sha256':None}]),
    lambda d:d.update(extensions={'future':True}),
    lambda d:d['budget'].update(calls=True),
    lambda d:d['authority'].update(workspace_ops=['write']),
    lambda d:d['stages'][2].update(kind='report'),
    lambda d:d['stages'][2].update(depends_on=['plan']),
])
def test_reject_invalid_graph_reference_or_shape(change):
    d=premade(); change(d)
    with pytest.raises(ExecutionError): validate(d)


def test_expansion_bound_and_no_nested_foreach():
    d=copy.deepcopy(PREMADES['premade-sprint-board']); s=d['stages'][1]
    s['max_units']=64
    s['body']*=1
    # 3 repair leaves * 11 attempts * 16 body stages * 64 units >4096.
    original=copy.deepcopy(s['body'][0]); s['body']=[]
    for i in range(16):
        stage=copy.deepcopy(original); stage.update(id='work'+str(i),max_fixes=10)
        s['body'].append(stage)
    with pytest.raises(ExecutionError,match='invalid_definition'): validate(d)
    d=copy.deepcopy(PREMADES['premade-sprint-board'])
    d['stages'][1]['body']=[copy.deepcopy(d['stages'][1])]
    with pytest.raises(ExecutionError): validate(d)


def test_check_empty_rejected_but_repair_empty_allowed():
    d=premade(); validate(d)
    d['stages'].insert(2,{'id':'checks','kind':'check','depends_on':['work'],'check_ids':[],
                           'snapshot':{'stage':'work','output':'snapshot'}})
    d['stages'][3]['depends_on']=['checks']
    with pytest.raises(ExecutionError): validate(d)


@pytest.mark.parametrize('value',[{'task':'x','unknown':1},{'evidence_ids':[]},{'task':'x','evidence_ids':['x'*257]}, {'task':'x'*30001}])
def test_inputs(value):
    with pytest.raises(ExecutionError): validate_inputs(premade(),value)


def test_deterministic_topological_order():
    d=premade(); assert [k for k,_ in stages(d)]==['plan','work','owner','report']


def test_units_cycle_depth_extra_keys_and_bounds():
    from core.execution.workflow_schema import validate_units
    units=[{'id':'u'+str(i),'title':'Unit','brief':'Build','depends_on':['u'+str(i-1)] if i else []} for i in range(16)]
    assert validate_units({'units':units},16)
    units[0]['depends_on']=['u15']
    with pytest.raises(ExecutionError): validate_units({'units':units},16)
    units[0]['depends_on']=[]
    units.append({'id':'u16','title':'Unit','brief':'Build','depends_on':['u15']})
    with pytest.raises(ExecutionError): validate_units({'units':units},17)
    with pytest.raises(ExecutionError): validate_units({'units':units[:2]},1)
    units[0]['tools']=['shell']
    normalized=validate_units({'units':units[:1],'extra':True},16)
    assert 'tools' not in normalized['units'][0] and 'extra' not in normalized


def test_verdict_is_typed_and_requires_real_blocking_question():
    from core.execution.workflow_schema import validate_verdict
    value={'verdict':'pass','summary':'Inspected supplied snapshot','findings':[],'question':None,'snapshot_digest':'a'*64}
    assert validate_verdict(value)==value
    assert validate_verdict({**value,'passed_checks':True})==value
    for bad in ({**value,'question':'Approve?'},{**value,'verdict':'blocked'},{**value,'snapshot_digest':'guessed'},
                {**value,'verdict':'met'}):
        with pytest.raises(ExecutionError): validate_verdict(bad)
