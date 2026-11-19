"""4.3b pure policy/clock/wire matrices. Storage tests require real Postgres."""
from copy import deepcopy
import pytest

from core.execution.job_policy import (JobPolicy, Reconciliation, authorize, coverage,
    effective_limit, next_tier, within_coverage, permits_job)
from core.execution.models import ExecutionError, budget
from core.execution.grants import grant_matches
from tests.test_owner_grants import fixtures
from tests.test_worker_protocol_v2 import wire
from core.worker.contract import validate_v2


def scope(**changes):
    return dict(profiles={'python':'a'*64},workspaces={'scratch':['.']},attachments=['source'],
                max_job_attempts=8,job_seconds_budget=15000,allow_cli=False,**changes)


@pytest.mark.parametrize('current,expected',[(900,1800),(1800,3600),(3600,7200),(7200,14400),(600,900)])
@pytest.mark.parametrize('representation',[int,float],ids=['policy-integer','postgres-double'])
def test_next_tier(current,expected,representation):
    result=next_tier(representation(current))
    assert result==expected
    assert type(result) is int


@pytest.mark.parametrize('selected',[3600.25, 3600.5, float('nan'), float('inf'), 1073741824, 1073741824.0])
def test_extended_tier_rejects_fractional_nonfinite_and_overflow(selected):
    with pytest.raises(ExecutionError,match='invalid_integer'):
        next_tier(selected)


def test_policy_and_override_still_require_integers():
    with pytest.raises(ExecutionError,match='invalid_integer'):
        JobPolicy(900.0,'preset')
    with pytest.raises(ExecutionError,match='invalid_integer'):
        JobPolicy(900,'preset',owner_override=1800.0)


@pytest.mark.parametrize('selected',[900,1800,3600])
def test_presets_need_no_long_run_grant(selected):
    authorize(selected)
    assert effective_limit(selected,installed=3600)==(selected,'selected_limit')
    assert validate_v2(wire(timeout=selected,not_after=1000+selected),now=1000)


@pytest.mark.parametrize('selected',[600,901,2700,3601,7200])
def test_custom_and_extended_require_grants_before_reduction(selected):
    with pytest.raises(ExecutionError,match='job_grant_required'):
        authorize(selected)


@pytest.mark.parametrize('selected,extended,custom,long,allowed',[
    (600,False,True,False,True),(600,True,False,True,False),
    (7200,True,True,True,True),(7200,False,True,True,False),
    (7200,True,False,True,True),(7200,True,True,False,False),
    (3600,False,False,False,True),(2700,False,True,False,True)])
def test_grant_matrix(selected,extended,custom,long,allowed):
    grant=dict(allow_extended_jobs=extended,allow_custom_job_limit=custom,allow_long_runs=long,auto_escalate=True)
    if allowed:
        authorize(selected,grant)
    else:
        with pytest.raises(ExecutionError): authorize(selected,grant)


def test_owner_proposal_override_and_strict_limit_are_distinct():
    policy=JobPolicy(3600,'large build',owner_override=1800,auto_escalate=False)
    assert policy.proposed_seconds==3600 and policy.selected_seconds==1800 and not policy.auto_escalate
    with pytest.raises(ExecutionError): JobPolicy(0,'bad')
    with pytest.raises(ExecutionError): JobPolicy(900,'')


@pytest.mark.parametrize('changes,result',[
    ({'run_wall':200},(200,'run_wall')),
    ({'time_budget':20},(20,'job_time_budget')),
    ({'owner_ceiling':100},(100,'owner_job_ceiling')),
    ({'run_wall':200,'time_budget':20},(20,'job_time_budget'))])
def test_injected_remaining_time(changes,result):
    assert effective_limit(1800,installed=3600,**changes)==result


@pytest.mark.parametrize('changes', [{'run_wall':0},{'time_budget':-.01},{'installed':900}])
def test_exhaustion_and_installed_ceiling_never_dispatch(changes):
    with pytest.raises(ExecutionError): effective_limit(1800,**changes)


def test_ceiling_and_escalation_off():
    with pytest.raises(ExecutionError,match='owner_job_ceiling'):
        authorize(1800,dict(job_seconds_ceiling=900))
    with pytest.raises(ExecutionError,match='escalation_disabled'):
        authorize(1800,dict(auto_escalate=False),continuation=True)


def test_legacy_short_requests_remain_compatible_but_explicit_custom_needs_grant():
    authorize(120,explicit=False)
    with pytest.raises(ExecutionError): authorize(120,explicit=True)


@pytest.mark.parametrize('changes', [dict(effects_established=False),dict(resumable=False),dict(workspace_digest='x'),dict(effects_digest='x')])
def test_unknown_effects_or_workspace_never_authorize_continuation(changes):
    value=dict(workspace_digest='a'*64,effects_digest='b'*64,effects_established=True,resumable=True)
    value.update(changes)
    assert not Reconciliation(**value).established('a'*64)


def test_reconciliation_binds_current_hash():
    proof=Reconciliation('a'*64,'b'*64,True,True)
    assert proof.established('a'*64) and not proof.established('c'*64)


@pytest.mark.parametrize('change',['profile','path','workspace','attachment','attempts','seconds','cli'])
def test_project_job_coverage_cannot_widen(change):
    approved=scope(); requested=deepcopy(approved)
    if change=='profile': requested['profiles']['python']='b'*64
    if change=='path': approved['workspaces']['scratch']=['src']; requested['workspaces']['scratch']=['src-other']
    if change=='workspace': requested['workspaces']['foreign']=['.']
    if change=='attachment': requested['attachments'].append('foreign')
    if change=='attempts': requested['max_job_attempts']+=1
    if change=='seconds': requested['job_seconds_budget']=None
    if change=='cli': requested['allow_cli']=True
    assert not within_coverage(approved,requested)


def test_profile_revision_and_cli_gate():
    approved=scope()
    assert permits_job(approved,{'profile':'python','workspace':'scratch','cwd':'.'},'a'*64)
    assert not permits_job(approved,{'profile':'python','workspace':'scratch','cwd':'.'},'b'*64)
    approved['profiles']['codex']='c'*64
    approved['allow_cli']=True
    assert not permits_job(approved,{'profile':'codex','workspace':'scratch','cwd':'.'},'c'*64)


def test_v3_project_grants_keep_future_task_scope_and_v2_does_not_gain_jobs():
    row,grant=fixtures()
    grant.update(matcher_version=3,coverage_kind='project',run_id=None,job_coverage=scope())
    row['request']['job_coverage']=scope()
    row['request']['task']='future task'
    assert grant_matches(grant,row)
    row['request']['job_coverage']['profiles']['python']='b'*64
    assert not grant_matches(grant,row)
    grant['matcher_version']=99
    assert not grant_matches(grant,row)


@pytest.mark.parametrize('value',[dict(max_job_attempts=0),dict(job_seconds_budget=0),dict(max_job_attempts=True)])
def test_job_budgets_are_finite(value):
    with pytest.raises(ExecutionError): budget(dict(start_calls=10,wall_mode='none',wall_seconds=None,**value))


def test_custom_wire_bound_is_installed_not_claimed_authority():
    value=wire(timeout=7200,not_after=8200)
    assert validate_v2(value,now=1000,max_timeout=86400)
    with pytest.raises(ValueError): validate_v2(value,now=1000,max_timeout=3600)


def test_extended_doubling_does_not_imply_custom_permission():
    grant=dict(allow_long_runs=True,allow_extended_jobs=True,allow_custom_job_limit=False,auto_escalate=True)
    authorize(7200,grant,continuation=True)
    authorize(14400,grant,continuation=True)
    with pytest.raises(ExecutionError,match='custom_job_grant_required'):
        authorize(5000,grant)
    with pytest.raises(ExecutionError,match='custom_job_grant_required'):
        authorize(900,grant,force_custom=True)


@pytest.mark.asyncio
async def test_job_capabilities_require_live_installed_handshake(monkeypatch):
    from core.execution import owner
    from core.worker import client
    monkeypatch.setattr(owner,'unavailable_reason',lambda:None)
    monkeypatch.setattr(client,'configuration',lambda:{})
    monkeypatch.setattr(client.Transport,'hello',lambda self:dict(job_time_presets=[900,1800,3600],max_timeout=86400,profiles={'python':'a'*64}))
    caps=await owner.job_time_capabilities()
    assert caps['available'] and caps['installed_max_seconds']==86400 and not caps['codex_cli']
    assert owner.operation_capabilities(caps)['grants.issue']['job_grants_available']
    monkeypatch.setattr(client.Transport,'hello',lambda self:dict(max_timeout=900))
    caps=await owner.job_time_capabilities()
    assert not caps['available'] and caps['reason']=='installed_job_ladder_unavailable'


def test_actual_workspace_hash_retains_partial_state_and_detects_changes(tmp_path):
    from core.execution.job_reconciliation import workspace_snapshot
    (tmp_path/'partial').write_text('completed step one',encoding='utf8')
    first=workspace_snapshot(tmp_path)
    assert first['files']==1 and first['manifest'][0][0]=='partial'
    assert first['digest']==workspace_snapshot(tmp_path)['digest']
    (tmp_path/'partial').write_text('step two continued',encoding='utf8')
    assert first['digest']!=workspace_snapshot(tmp_path)['digest']


def test_workspace_manifest_is_bounded(tmp_path):
    from core.execution.job_reconciliation import workspace_snapshot
    from core.worker.contract import WorkerUnavailable
    (tmp_path/'large').write_bytes(b'x'*100)
    with pytest.raises(WorkerUnavailable,match='bound exceeded'):
        workspace_snapshot(tmp_path,max_bytes=50)


def test_reconciliation_manifest_and_effects_are_digest_bound():
    from core.execution.models import digest
    manifest=[['partial','file',384,'a'*64]]
    effects={'established':True,'evidence':['adapter acknowledgement']}
    proof=Reconciliation(digest(manifest),digest(effects),True,True,manifest,effects)
    assert proof.established(digest(manifest))
    effects['evidence'].append('tampered')
    assert not proof.established(digest(manifest))


@pytest.mark.asyncio
async def test_queue_continues_only_after_adapter_and_new_durable_identity():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core.execution.jobs import JobContext
    from core.execution.worker_queue import WorkerQueue
    parent=dict(outcome='timed_out',cleanup='idle_proved',receipt={})
    store=SimpleNamespace(admit=AsyncMock(),get=AsyncMock(side_effect=[parent,dict(outcome='succeeded',receipt={'returncode':0})]),
        continue_timeout=AsyncMock(return_value={'execution_id':'continued'}),block_timeout=AsyncMock())
    proof=Reconciliation('a'*64,'b'*64,True,True)
    queue=WorkerQueue(store,None)
    ctx=JobContext('owner','principal','group','boundary')
    result=await queue.submit(ctx,{},execution_id='original',reconcile=lambda row:(proof,'a'*64))
    store.continue_timeout.assert_awaited_once_with(ctx,'original',proof,current_workspace_digest='a'*64)
    assert result['execution_id']=='continued' and result['outcome']=='succeeded'
    store.block_timeout.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_timeout_without_a_verified_adapter_pauses_and_flags():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core.execution.jobs import JobContext
    from core.execution.worker_queue import WorkerQueue
    store=SimpleNamespace(admit=AsyncMock(),get=AsyncMock(return_value=dict(outcome='timed_out',receipt={})),block_timeout=AsyncMock())
    ctx=JobContext('owner','principal','group','boundary')
    result=await WorkerQueue(store,None).submit(ctx,{},execution_id='original')
    assert result['continuation_requires_reconciliation']
    store.block_timeout.assert_awaited_once_with(ctx,'original','job_reconciliation_required')
