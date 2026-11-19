"""Checked-work validation and actual deterministic filesystem adapter tests."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.execution.checked import (CONTRACT, acceptance, accepted_manifest, edits,
                                    parse_plan, registration)
from core.execution.models import ExecutionError, canonical, digest, start_request
from core.execution.planning import parse_plan as text_plan
from core.execution.workspaces import snapshot
from core.tool_jobs.checked_runner import execute


BASE = 'a' * 64
ACCEPTANCE = {'criteria': [{'id':'deliverable', 'output_kind':'proposal',
                          'checks':{'required_check_ids':['files.nonempty']}}]}
CONTEXT = {'base_digest':BASE, 'write_paths':['file.txt'],
           'checks':{'files.nonempty':{'revision':1}}, 'build_contracts':None}


def plan(base=BASE):
    def step(key, kind, deps, source, **extra):
        result = {'id':key,'kind':kind,'instruction':'bounded work','dependencies':deps,'snapshot':source,'criteria_ids':[]}
        if kind not in {'model_task','proposal_publish'}:
            result.update(proposed_seconds=900,rationale='small offline work')
        return {**result, **extra}
    return {'tasks':[
        step('read','workspace_read',[],base,operation_id='workspace.read',paths=['file.txt']),
        step('model','model_task',['read'],'read',operation_id='edits.plan'),
        step('edit','workspace_edit',['model'],'model',operation_id='workspace.edit',edits_from='model'),
        step('check','registered_check',['edit'],'edit',check_id='files.nonempty'),
        step('publish','proposal_publish',['check'],'check',criteria_ids=['deliverable']),
    ]}


def test_negotiation_retains_text_contract_and_typed_depth():
    assert acceptance(copy.deepcopy(ACCEPTANCE)) == ACCEPTANCE
    assert parse_plan(canonical(plan()), ACCEPTANCE, CONTEXT) == plan()
    with pytest.raises(ExecutionError):
        text_plan(canonical(plan()), ACCEPTANCE)
    assert text_plan(canonical({'tasks':[{'id':'t','instruction':'text','dependencies':[],
        'output_kind':'intermediate','criteria_ids':[]}]}), {'criteria':[]})


@pytest.mark.parametrize('index,field,value,error',[
    (0,'operation_id','shell.exec','unregistered_operation'),
    (0,'snapshot','b'*64,'invalid_snapshot_reference'),
    (0,'proposed_seconds',True,'invalid_proposed_tier'),
    (0,'proposed_seconds',901,'invalid_proposed_tier'),
    (0,'dependencies',['missing'],'unknown_dependency'),
    (1,'operation_id','provider.fallback','unregistered_operation'),
    (2,'edits_from','read','edit_source_dependency_required'),
    (3,'check_id','invented','unregistered_check'),
    (3,'check_id',[],'unregistered_check'),
    (3,'criteria_ids',['deliverable'],'unchecked_output_not_deliverable'),
    (4,'criteria_ids',[],'single_complete_publication_required'),
    (4,'dependencies',['edit'],'invalid_snapshot_reference'),
    (4,'kind','await_owner_decision','managed_apply_unavailable'),
    (4,'kind','integrate_approved_proposal','managed_apply_unavailable'),
])
def test_untrusted_plan_cannot_grant_authority(index,field,value,error):
    value_plan=plan();value_plan['tasks'][index][field]=value
    with pytest.raises(ExecutionError,match=error):
        parse_plan(canonical(value_plan),ACCEPTANCE,CONTEXT)


def test_cycle_missing_checks_and_smuggled_profile():
    value=plan();value['tasks'][0]['dependencies']=['publish']
    with pytest.raises(ExecutionError,match='plan_cycle'): parse_plan(canonical(value),ACCEPTANCE,CONTEXT)
    value=plan();value['tasks'].pop(3);value['tasks'][-1].update(dependencies=['edit'],snapshot='edit')
    with pytest.raises(ExecutionError,match='required_check_missing'): parse_plan(canonical(value),ACCEPTANCE,CONTEXT)
    value=plan();value['tasks'][0]['profile']='shell'
    with pytest.raises(ExecutionError,match='unsupported_fields'): parse_plan(canonical(value),ACCEPTANCE,CONTEXT)


@pytest.mark.parametrize('name',['../file','/absolute','a\\b','.env','tests/../file','a.pem','CON','a.','a ','a//b','C:/file','credentials/token'])
def test_edit_path_denials(name):
    with pytest.raises(ExecutionError):
        edits({'edits':[{'path':name,'operation':'create','pre_sha256':None,'content':'value'}]})


def test_exact_create_replace_delete_and_batch_staleness(tmp_path):
    file=tmp_path/'file.txt';file.write_text('before')
    pre=hashlib.sha256(b'before').hexdigest()
    base=snapshot(tmp_path)
    def apply(items):
        return execute({'operation':'workspace.edit','input_digest':snapshot(tmp_path)['digest'],
                        'write_paths':['file.txt','new.txt'],'edits':items},tmp_path)
    create={'operation':'create','path':'new.txt','pre_sha256':None,'content':'new'}
    replace={'operation':'replace','path':'file.txt','pre_sha256':pre,'content':'after'}
    with pytest.raises(ExecutionError,match='stale_preimage'):
        apply([create,{**replace,'pre_sha256':'0'*64}])
    assert not (tmp_path/'new.txt').exists() and file.read_text()=='before'
    apply([create,replace])
    assert file.read_bytes()==b'after' and (tmp_path/'new.txt').read_bytes()==b'new'
    with pytest.raises(ExecutionError,match='stale_preimage'): apply([replace])
    apply([{'operation':'delete','path':'new.txt','pre_sha256':hashlib.sha256(b'new').hexdigest()}])
    assert not (tmp_path/'new.txt').exists()
    with pytest.raises(ExecutionError,match='stale_snapshot'):
        execute({'operation':'workspace.read','input_digest':base['digest'],'paths':['file.txt']},tmp_path)


def test_read_and_check_are_real_and_scope_is_enforced(tmp_path):
    (tmp_path/'file.txt').write_text('actual')
    sha=snapshot(tmp_path)['digest']
    result=execute({'operation':'workspace.read','input_digest':sha,'paths':['file.txt']},tmp_path)
    assert result['files'][0]['sha256']==hashlib.sha256(b'actual').hexdigest()
    assert execute({'operation':'files.nonempty','input_digest':sha},tmp_path)['assertions']
    (tmp_path/'file.txt').unlink()
    with pytest.raises(ExecutionError,match='check_failed'):
        execute({'operation':'files.nonempty','input_digest':snapshot(tmp_path)['digest']},tmp_path)
    (tmp_path/'tests').mkdir();(tmp_path/'tests'/'test_x.py').write_text('fixed')
    with pytest.raises(ExecutionError,match='edit_scope_denied'):
        execute({'operation':'workspace.edit','input_digest':snapshot(tmp_path)['digest'],
                 'write_paths':['tests'],'edits':[{'path':'tests/test_x.py','operation':'replace',
                 'pre_sha256':hashlib.sha256(b'fixed').hexdigest(),'content':'weakened'}]},tmp_path)


def test_interrupted_edit_resumes_only_exact_outstanding_preimages(tmp_path,monkeypatch):
    from core.tool_jobs import checked_runner as runner
    for name in ('a.txt','b.txt'): (tmp_path/name).write_text('before')
    original=snapshot(tmp_path)
    value={'operation':'workspace.edit','input_digest':original['digest'],'write_paths':['a.txt','b.txt'],
           'operation_key':'server-selected-step-key','edits':[
               {'operation':'replace','path':name,'pre_sha256':hashlib.sha256(b'before').hexdigest(),'content':'after'}
               for name in ('a.txt','b.txt')]}
    replace=runner.os.replace
    def crash(src,dst,**kwargs):
        if str(dst)=='b.txt' or str(dst)==str(tmp_path/'b.txt'):
            raise OSError('simulated interruption after first edit')
        return replace(src,dst,**kwargs)
    monkeypatch.setattr(runner.os,'replace',crash)
    from core.portal_resources import ResourceError
    with pytest.raises((OSError,ResourceError)): execute(value,tmp_path)
    assert (tmp_path/'a.txt').read_text()=='after' and (tmp_path/'b.txt').read_text()=='before'
    monkeypatch.setattr(runner.os,'replace',replace)
    pending=runner.edit_progress(original,snapshot(tmp_path),{'edits':value['edits']})
    assert [i['path'] for i in pending]==['b.txt']
    assert execute(value,tmp_path)['resumed']
    assert (tmp_path/'b.txt').read_text()=='after'
    assert all(i['path']!=runner.JOURNAL for i in snapshot(tmp_path)['manifest'])
    (tmp_path/'a.txt').write_text('conflict')
    with pytest.raises(ExecutionError,match='edit_reconciliation_conflict'): execute(value,tmp_path)


def test_registered_scaffold_uses_existing_contracts_without_nested_jobs(tmp_path,monkeypatch):
    from core.worker import client
    monkeypatch.setattr(client,'run',lambda *a,**k:pytest.fail('nested worker acquisition'))
    units=[{'name':'double','signature':'double(x)','doc':'double input','cases':[{'args':[2],'expect':4}]}]
    result=execute({'operation':'python.scaffold','input_digest':snapshot(tmp_path)['digest'],'build_contracts':units},tmp_path)
    assert result['scaffold']=='minikit' and (tmp_path/'tests/test_functions.py').is_file()
    assert 'assert double(2) == 4' in (tmp_path/'tests/test_functions.py').read_text()


def test_check_registration_is_frozen_and_requires_baseline(tmp_path):
    (tmp_path/'file.txt').write_text('base')
    folder={'checked_edits':{'checks':['files.nonempty'],'write_paths':['file.txt']}}
    context=registration(folder,snapshot(tmp_path),'f'*64)
    before=digest(context)
    folder['checked_edits']['checks'].append('python.pytest')
    assert digest(context)==before
    with pytest.raises(ExecutionError,match='baseline_tests_required'):
        registration(folder,snapshot(tmp_path),'f'*64)


def test_manifest_requires_actual_matching_receipts_and_changes():
    definition={'revision':1}
    receipt={'run_id':'run','check_id':'files.nonempty','definition_digest':digest(definition),'input_digest':'b'*64,
             'passed':True,'source_unchanged':True,'outcome':'succeeded','cleanup':'idle_proved','execution_id':'execution'}
    proposal={'digest':'c'*64,'base_digest':BASE,'output_digest':'b'*64,
              'files':[{'path':'file.txt','operation':'replace','bytes':5,'pre_sha256':BASE,'post_sha256':'b'*64}]}
    args=dict(run_id='run',step={'criteria_ids':['deliverable'],'plan_step':0,'ordinal':5},
              proposal=proposal,receipts=[receipt],contract=ACCEPTANCE,context=CONTEXT)
    manifest=accepted_manifest(**args)
    assert manifest['checks'][0]['execution_id']=='execution' and not manifest['independently_verified']
    for change in ({'passed':False},{'outcome':'unknown'},{'cleanup':'pending'},
                   {'definition_digest':'d'*64},{'input_digest':BASE},{'truncated':True},{'source_unchanged':False}):
        with pytest.raises(ExecutionError,match='checked_output_required'):
            accepted_manifest(**{**args,'receipts':[{**receipt,**change}]})
    with pytest.raises(ExecutionError): accepted_manifest(**{**args,'proposal':{**proposal,'files':[]}})
    with pytest.raises(ExecutionError):
        accepted_manifest(**{**args,'proposal':{**proposal,'files':[{'path':'empty','operation':'create','bytes':0,'post_sha256':'b'*64}]}})


def test_check_scratch_cannot_change_source_or_weaken_tests(tmp_path):
    from core.execution.checked import check_source_unchanged
    (tmp_path/'file.txt').write_text('source')
    frozen=snapshot(tmp_path)
    (tmp_path/'artifact.txt').write_text('scratch output')
    assert check_source_unchanged(frozen,snapshot(tmp_path))
    (tmp_path/'conftest.py').write_text('weakened')
    assert not check_source_unchanged(frozen,snapshot(tmp_path))
    (tmp_path/'conftest.py').unlink();(tmp_path/'file.txt').write_text('self-modified')
    assert not check_source_unchanged(frozen,snapshot(tmp_path))


@pytest.mark.asyncio
async def test_async_worker_cancellation_waits_for_cleanup_receipt(monkeypatch):
    import asyncio
    import concurrent.futures
    from types import SimpleNamespace
    from core.worker import client
    observer=concurrent.futures.Future()
    cleanup=concurrent.futures.Future()
    entered=asyncio.Event()
    def submit(context,request,**kwargs):
        assert kwargs['execution_id']=='execution'
        entered.set()
        return observer
    bridge=SimpleNamespace(submit=submit,cancel_and_wait=lambda *args:cleanup)
    monkeypatch.setattr(client,'request',lambda *a,**k:{'job_id':'execution'})
    monkeypatch.setattr(client,'unavailable_reason',lambda *a:None)
    monkeypatch.setattr(client,'queue_bridge',lambda:bridge)
    task=asyncio.create_task(client.execute_async('python'))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done() and not observer.cancelled()
    cleanup.set_result({'cleanup':'idle_proved'})
    with pytest.raises(asyncio.CancelledError): await task
    assert observer.cancelled()


@pytest.mark.asyncio
async def test_build_gate_dispatches_import_collect_pytest_cli_as_separate_jobs(monkeypatch,tmp_path):
    from core.build import jobs
    calls=[]
    async def execute(profile,**kwargs):
        calls.append((profile,kwargs))
        return {'returncode':0,'stdout':b'1 passed in 0.01s','stderr':b'','outcome':'succeeded'}
    monkeypatch.setattr(jobs,'execute_async',execute)
    report=await jobs.verify(tmp_path)
    assert report['builds'] and report['passed']==1 and report['runs']
    assert len(calls)==4 and all(p=='python' for p,_ in calls)
    assert '--collect-only' in calls[1][1]['args'] and 'minikit.cli' in calls[3][1]['args']


@pytest.mark.skipif(sys.platform!='linux',reason='real Linux Landlock subprocess required')
def test_real_edit_and_check_process_under_landlock(tmp_path):
    from core.worker import landlock
    landlock.abi()  # Unsupported Linux is a gate failure, not a successful skip.
    workspace=tmp_path/'workspace';workspace.mkdir()
    temporary=tmp_path/'temporary';temporary.mkdir()
    (workspace/'file.txt').write_text('base')
    core=Path(__file__).resolve().parents[1]
    bootstrap=('import sys;sys.path.insert(0,sys.argv[1]);from core.worker.landlock import install;'
               'install(sys.argv[2],sys.argv[3],readonly=(sys.argv[1],sys.prefix,sys.base_prefix));'
               'from core.tool_jobs.checked_runner import main;main()')
    def run(value):
        return subprocess.run([sys.executable,'-B','-c',bootstrap,str(core),str(workspace),str(temporary)],
                              cwd=workspace,input=canonical(value),capture_output=True,text=True,timeout=10)
    edit={'operation':'workspace.edit','input_digest':snapshot(workspace)['digest'],'write_paths':['file.txt'],
          'edits':[{'operation':'replace','path':'file.txt','pre_sha256':hashlib.sha256(b'base').hexdigest(),'content':'changed'}]}
    assert run(edit).returncode==0
    assert (workspace/'file.txt').read_text()=='changed'
    assert run({'operation':'files.nonempty','input_digest':snapshot(workspace)['digest']}).returncode==0
    assert run(edit).returncode!=0


def test_reported_models_ignore_unpinned_worker_receipts():
    from core.tool_jobs.service import reported_models
    receipts=[{'model_pin':'b-model'},{'model_pin':None},{'kind':'check'},{'model_pin':'a-model'},{'model_pin':'b-model'}]
    assert reported_models(receipts)==['a-model','b-model']
    assert reported_models([])==[]
