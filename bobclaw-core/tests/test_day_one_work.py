import copy
import hashlib
import json
from pathlib import Path

import pytest

from core.execution.chat_context import valid, signature, OPERATIONS
from core.execution.chat_records import valid_answer, answer_signature
from core.execution.checked import registration, direct_plan, edit_scope
from core.execution.models import Authority, ExecutionError, canonical, start_request
from core.execution.publication import publication_edits
from core.execution.workbench import recipe
from core.execution.workspaces import snapshot, proposal
from core.tool_jobs.checked_runner import execute


def context():
    return {'authority':Authority('lane','owner','owner',{},'portal').as_dict(),
            'conversation_id':'conversation','face_id':'builder','iat':100,'exp':200}


def test_context_binds_verified_caller_conversation_and_face():
    value=context(); proof=signature(value,'secret')
    assert valid(value,proof,'secret',owner='owner',conversation='conversation',face='builder',now=110)
    for field, args in [('owner',{'owner':'other'}),('conversation',{'conversation':'other'}),('face',{'face':'fixer'})]:
        supplied=dict(owner='owner',conversation='conversation',face='builder',now=110);supplied.update(args)
        assert not valid(value,proof,'secret',**supplied),field
    changed=copy.deepcopy(value);changed['authority']['scope']={'may_touch':['*']}
    assert not valid(changed,proof,'secret',owner='owner',conversation='conversation',face='builder',now=110)
    assert not valid(value,proof,'secret',owner='owner',conversation='conversation',face='builder',now=200)
    changed=copy.deepcopy(value);changed['authority']['session_kind']='owner_stepup'
    assert not valid(changed,signature(changed,'secret'),'secret',owner='owner',conversation='conversation',face='builder',now=110)
    assert not {'grants.issue','work.workspace.create','work.publish'} & OPERATIONS


def test_answer_vouch_prevents_cross_question_or_user_forgery():
    value={'question_id':'q','user_id':'owner','answer':'yes','iat':100,'exp':130}
    proof=answer_signature(value,'secret')
    assert valid_answer(value,proof,'secret','q',110)
    assert not valid_answer(value,proof,'secret','other',110)
    assert not valid_answer({**value,'user_id':'other'},proof,'secret','q',110)
    assert not valid_answer(value,proof,'secret','q',130)
    assert not valid_answer({**value,'exp':1000},proof,'secret','q',110)


def test_direct_recipes_are_typed_single_model_and_fixer_has_baseline(tmp_path):
    frozen=snapshot(tmp_path)
    folder={'checked_edits':{'checks':['python.compile','python.unittest'],'write_paths':['app']}}
    contract={'criteria':[{'id':'deliverable','output_kind':'proposal','checks':{'required_check_ids':['python.unittest']}}]}
    for mode in ('builder','fixer'):
        ctx=registration(folder,frozen,'rev',mode)
        plan=direct_plan(contract,ctx)['tasks']
        assert sum(t['kind']=='model_task' for t in plan)==1
        assert sum(t['kind']=='registered_check' for t in plan)==2
        assert (plan[0].get('operation_id')=='python.baseline')==(mode=='fixer')
        assert plan[-1]['kind']=='proposal_publish'
        assert all(t['snapshot']=='edit' for t in plan if t['kind']=='registered_check')


def test_existing_tests_cannot_be_replaced_but_regression_tests_can_be_created():
    create={'operation':'create','path':'app/test_regression.py','pre_sha256':None,'content':'test'}
    edit_scope({'edits':[create]},['app'],True)
    from core.execution.failure import validate_output
    row={'snapshot':{'team':{'topology':'agent-loop/v1'}},'request':{'worker_contract':'checked-edits/1'},
         'checked_context':{'write_paths':['app'],'allow_new_tests':True}}
    assert validate_output(row,{'spec':{'phase':'task'}},canonical({'edits':[create]}))['passed']
    for item,enabled in [(create,False),({**create,'operation':'replace','pre_sha256':'a'*64},True),
                         ({**create,'path':'tests/test_bad.py'},True),({**create,'path':'app/../escape'},True)]:
        with pytest.raises(ExecutionError):edit_scope({'edits':[item]},['app'],enabled)


def test_actual_python_checks_and_failing_baseline(tmp_path, capfd):
    app=tmp_path/'app';app.mkdir()
    (app/'calc.py').write_text('def add(a,b): return a-b\n')
    (app/'test_calc.py').write_text('import unittest\nfrom calc import add\nclass Cases(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n')
    before=snapshot(tmp_path)
    baseline=execute({'operation':'python.baseline','input_digest':before['digest']},tmp_path)
    assert baseline['exit_status']!=0 and baseline['tests_run']==1 and not baseline['passed']
    assert 'FAIL' in baseline['output_tail']
    edits={'edits':[{'operation':'replace','path':'app/calc.py','pre_sha256':hashlib.sha256(b'def add(a,b): return a-b\n').hexdigest(),
                     'content':'def add(a,b): return a+b\n'}]}
    execute({'operation':'workspace.edit','input_digest':before['digest'],'write_paths':['app'],
             'operation_key':'fix','edits':edits['edits']},tmp_path)
    fixed=snapshot(tmp_path)
    for check in ('python.compile','python.unittest'):
        assert execute({'operation':check,'input_digest':fixed['digest']},tmp_path)['exit_status']==0
    assert snapshot(tmp_path)['digest']==fixed['digest']


def test_zero_test_suite_is_not_success(tmp_path,capfd):
    (tmp_path/'app').mkdir();(tmp_path/'app/code.py').write_text('x=1\n')
    for operation in ('python.unittest','python.baseline'):
        with pytest.raises(ExecutionError,match='no_tests_executed'):
            execute({'operation':operation,'input_digest':snapshot(tmp_path)['digest']},tmp_path)


def test_publication_uses_frozen_bytes_and_stale_batch_writes_nothing(tmp_path):
    (tmp_path/'app').mkdir();p=tmp_path/'app/a.py';p.write_text('old')
    base=snapshot(tmp_path);p.write_text('new');out=snapshot(tmp_path);p.write_text('old')
    prop=proposal(base,out,{})
    edits=publication_edits(base,out,prop['files'])
    assert edits[0]['content']=='new'
    p.write_text('concurrent user change')
    with pytest.raises(ExecutionError,match='stale_snapshot'):
        execute({'operation':'workspace.edit','input_digest':base['digest'],'edits':edits,
                 'write_paths':['app'],'operation_key':'publish'},tmp_path)
    assert p.read_text()=='concurrent user change'


def test_recipe_saves_registered_steps_only():
    args={'name':'my-python','checks':['python.compile','python.unittest'],'expected_revision':None,'request_key':'save'}
    assert recipe(args)['target']=='bob-lane-workspace'
    for changes in [{'checks':['shell']},{'command':'rm -rf /'},{'checks':[]},{'checks':['python.compile']*2}]:
        with pytest.raises(ExecutionError):recipe({**args,**changes})


def test_all_three_profiles_load_with_real_tools_and_shared_model_defaults():
    import yaml
    from core.faces.registry import Face, FaceRegistry
    root=Path(__file__).resolve().parents[1]/'core/faces/templates'
    for identifier in ('orchestrator','builder','fixer'):
        face=Face.model_validate(yaml.safe_load((root/(identifier+'.yaml')).read_text()))
        FaceRegistry._validate_registered_tools(face)
        assert face.preferred_backend==face.escalation_backend==''
        assert face.allowed_skills==(['bob.builder'] if identifier=='builder' else ['bob.work'])  # product guide is bob_guide now
