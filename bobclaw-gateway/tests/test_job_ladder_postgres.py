"""4.3b storage gate: real temporary Postgres, injected clocks, no providers.

Set S4B_TEST_POSTGRES_DSN. A skipped test does not establish storage behavior.
"""
import asyncio
from dataclasses import replace
import json

import pytest

from tests.test_worker_queue_postgres import jobs, pg, seed_run, request, slot, finish, context
from core.execution.job_policy import JobPolicy, Reconciliation
from core.execution.models import canonical, digest, Authority
from core.execution.budgets import task_digest
from core.execution.store import Store
from core.worker.contract import WorkerUnavailable

PROOF = Reconciliation('a'*64,'b'*64,True,True)


async def install_policy(jobs, monkeypatch, *, actions=()):
    monkeypatch.setenv('BOB_SETTINGS_ID', 'a'*12)
    monkeypatch.setenv('BOB_DEFAULT_OWNER', 'p')
    return await Store(jobs.pool, 'lane').set_policy(
        Authority('lane', 'p', 'p', {}, 'human'),
        dict(request_key='policy', expected_revision=0,
             policy=dict(posture='scoped', scope=dict(auto_actions=list(actions)))))


async def worker_flag_context(jobs, cause):
    # The worker admission contract does not require model-call accounting.
    # Do not repair these fixtures by inventing a call account or allocation.
    assert await jobs.pool.fetchval('SELECT count(*) FROM agent_budget_accounts') == 0
    assert await jobs.pool.fetchval('SELECT count(*) FROM agent_budget_allocations') == 0
    flags = await Store(jobs.pool, 'lane').flags(Authority('lane', 'p', 'p', {}, 'human'), {'run_id': 'run'})
    assert len(flags['items']) == 1 and flags['items'][0]['cause'] == cause
    context = json.loads(await jobs.pool.fetchval('SELECT context FROM agent_flags WHERE id=$1', flags['items'][0]['id']))
    assert context['account'] is None and context['reserved_calls'] is None
    assert context['selected_grant_id'] == 'grant' and context['selected_grant_revision'] == 1
    return context


@pytest.mark.asyncio
async def test_granted_custom_job_still_requires_current_owner_policy(jobs, monkeypatch):
    ctx = selected(await bound_run(jobs, grant=True), 7200)
    await install_policy(jobs, monkeypatch, actions=['model_call'])
    with pytest.raises(WorkerUnavailable, match='current owner policy'):
        await jobs.admit(ctx, 'e', request())
    assert await jobs.pool.fetchval('SELECT count(*) FROM worker_queue') == 0


@pytest.mark.asyncio
async def test_policy_narrowing_cancels_queued_granted_job_before_dispatch(jobs, monkeypatch):
    ctx = selected(await bound_run(jobs, grant=True), 1800)
    await jobs.admit(ctx, 'e', request())
    await install_policy(jobs, monkeypatch, actions=['model_call'])
    async with jobs.pool.acquire() as c:
        assert await dispatch(jobs, c, await slot(jobs, c)) is None
    assert (await jobs.get('e'))['outcome'] == 'cancelled'
    account = await jobs.pool.fetchrow('SELECT * FROM run_job_accounts')
    assert account['dispatched'] == account['held'] == account['seconds_held'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('actions', [[], ['model_call']])
async def test_policy_narrowing_retains_active_job_cleanup_and_accounting(jobs, monkeypatch, actions):
    ctx = selected(await bound_run(jobs, grant=True), 1800)
    await jobs.admit(ctx, 'e', request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs, c)
        row = await dispatch(jobs, c, controller)
        await install_policy(jobs, monkeypatch, actions=actions)
        assert await jobs.pool.fetchval("SELECT cleanup_pending FROM agent_runs WHERE id='run'")
        assert (await jobs.get('e'))['cancel_requested']
        assert not await jobs.enforce_active(c, row)
        account = await jobs.pool.fetchrow('SELECT * FROM run_job_accounts')
        assert account['held'] == 1 and account['seconds_held'] == 1800
        if not actions:
            context = await worker_flag_context(jobs, 'policy_narrowed')
            assert context['job_account']['held'] == 1 and context['job_account']['seconds_held'] == 1800
        await finish(jobs, c, row, controller, execution_seconds=12)
        await finish(jobs, c, row, controller, execution_seconds=12)
    account = await jobs.pool.fetchrow('SELECT * FROM run_job_accounts')
    assert account['held'] == account['seconds_held'] == 0
    assert account['completed'] == 1 and account['seconds_used'] == 12
    assert not await jobs.pool.fetchval("SELECT cleanup_pending FROM agent_runs WHERE id='run'")


@pytest.mark.asyncio
async def test_policy_narrowing_blocks_reconciled_continuation(jobs, monkeypatch):
    ctx = selected(await bound_run(jobs, grant=True), 900)
    await jobs.admit(ctx, 'e', request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs, c)
        row = await dispatch(jobs, c, controller)
        await finish(jobs, c, row, controller, outcome='timed_out', execution_seconds=900)
    await install_policy(jobs, monkeypatch, actions=['model_call'])
    with pytest.raises(WorkerUnavailable, match='current owner policy'):
        await jobs.continue_timeout(ctx, 'e', PROOF, current_workspace_digest='a'*64)
    assert await jobs.pool.fetchval('SELECT count(*) FROM worker_job_attempts') == 1
    assert await jobs.pool.fetchval("SELECT status FROM agent_runs WHERE id='run'") == 'paused'


@pytest.mark.asyncio
async def test_policy_cancellation_drains_jobs_behind_timeout_reconciliation(jobs, monkeypatch):
    ctx = selected(await bound_run(jobs, grant=True), 900)
    await jobs.admit(ctx, 'parent', request())
    await jobs.admit(ctx, 'waiting', request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs, c)
        row = await dispatch(jobs, c, controller)
        await finish(jobs, c, row, controller, outcome='timed_out', execution_seconds=900)
        await install_policy(jobs, monkeypatch, actions=['model_call'])
        assert await dispatch(jobs, c, controller) is None
    assert (await jobs.get('waiting'))['outcome'] == 'cancelled'
    assert await jobs.pool.fetchval('SELECT count(*) FROM worker_queue') == 0
    assert await jobs.pool.fetchval('SELECT dispatched FROM run_job_accounts') == 1


@pytest.mark.asyncio
async def test_policy_and_grant_writers_fence_job_admission_before_queue_lock(jobs, monkeypatch):
    ctx = selected(await bound_run(jobs, grant=True), 900)
    monkeypatch.setenv('BOB_SETTINGS_ID', 'a'*12)
    async with Store(jobs.pool, 'lane').transaction(exclusive_grants=True):
        pending = asyncio.create_task(jobs.admit(ctx, 'e', request()))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), .1)
            # Admission must wait on the common fence without holding the queue.
            async with jobs.pool.acquire() as c, c.transaction():
                assert await c.fetchval('SELECT pg_try_advisory_xact_lock($1::bigint)', jobs.lock)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_pre_ladder_admission_receipt_replays_without_changing_job_policy(jobs):
    ctx = await bound_run(jobs)
    req = request()
    await jobs.admit(ctx, 'legacy', req)
    legacy = digest({k: v for k, v in req.items() if k not in {'job_id', 'expires'}})
    await jobs.pool.execute("UPDATE worker_job_attempts SET admission_digest=$1 WHERE execution_id='legacy'", legacy)
    assert (await jobs.admit(ctx, 'legacy', req))['execution_id'] == 'legacy'
    with pytest.raises(WorkerUnavailable, match='admission conflict'):
        await jobs.admit(selected(ctx, 1800), 'legacy', req)
    assert await jobs.pool.fetchval('SELECT count(*) FROM worker_queue') == 1


async def bound_run(jobs, *, grant=False, **permissions):
    ctx = await seed_run(jobs)
    jobs.workspace_verifier=lambda row:'a'*64
    scope = dict(profiles={'python':'a'*64},workspaces={'scratch':['.']},attachments=['source'],
                 max_job_attempts=8,job_seconds_budget=30000,allow_cli=False)
    req = dict(task='original',evidence_ids=['source'],project_id='project',job_coverage=scope,
               budget=dict(max_job_attempts=8,job_seconds_budget=30000))
    snap = dict(team={},models={})
    await jobs.pool.execute("UPDATE agent_runs SET request=$1::jsonb,snapshot=$2::jsonb WHERE id='run'",canonical(req),canonical(snap))
    if grant:
        await jobs.pool.execute("INSERT INTO agent_execution_grants(lane,owner,id,run_id,authority_boundary,task_digest,team_digest,models_digest,matcher_version,coverage_kind,job_coverage,allow_long_runs,allow_extended_jobs,allow_custom_job_limit) VALUES('lane','p','grant','run','b',$1,$2,$2,3,'task',$3::jsonb,TRUE,TRUE,TRUE)",task_digest(req),digest({}),canonical(scope))
        await jobs.pool.execute("UPDATE agent_runs SET selected_grant_id='grant',selected_grant_revision=1 WHERE id='run'")
        for field,value in permissions.items():
            assert field in {'allow_long_runs','allow_extended_jobs','allow_custom_job_limit','job_seconds_ceiling','auto_escalate','job_override_seconds','job_auto_escalate'}
            await jobs.pool.execute('UPDATE agent_execution_grants SET '+field+'=$1 WHERE id=$2',value,'grant')
    return ctx


def selected(ctx, seconds, **extra):
    return replace(ctx,policy=JobPolicy(seconds,'deterministic build',**extra))


async def dispatch(jobs,c,controller):
    return await jobs.select(c,controller,{'python':'a'*64},max_timeout=86400)


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_policy', [False, True])
async def test_non_run_timeout_keeps_caller_group_admitting_and_dispatching(jobs, explicit_policy):
    ctx = selected(context(), 900) if explicit_policy else context()
    assert ctx.run_id is None
    await jobs.admit(ctx, 'timeout', request())
    await jobs.admit(ctx, 'already-waiting', request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs, c)
        row = await dispatch(jobs, c, controller)
        assert row['execution_id'] == 'timeout'
        await finish(jobs, c, row, controller, outcome='timed_out',
                     execution_seconds=row['effective_seconds'])
        terminal = await jobs.get('timeout')
        assert terminal['outcome'] == terminal['receipt']['outcome'] == 'timed_out'
        assert terminal['cleanup'] == 'idle_proved'
        assert terminal['reconciliation'] == 'established'
        assert terminal['paused_timeout'] is None and not terminal['paused_unknown']
        visible = await jobs.visible('timeout')
        assert visible['state'] == 'timed_out'
        assert not visible['blocked_by_timeout_reconciliation']
        assert not (await jobs.visible('already-waiting'))['blocked_by_timeout_reconciliation']
        await jobs.admit(ctx, 'new', request())
        for execution_id in ('already-waiting', 'new'):
            next_job = await dispatch(jobs, c, controller)
            assert next_job['execution_id'] == execution_id
            await finish(jobs, c, next_job, controller)
    assert await jobs.pool.fetchval("SELECT count(*) FROM worker_job_events WHERE kind='timeout_reconciliation_required'") == 0


@pytest.mark.asyncio
async def test_run_owned_timeout_requires_reconciliation_before_group_resumes(jobs):
    ctx = selected(await bound_run(jobs), 900)
    await jobs.admit(ctx, 'timeout', request())
    await jobs.admit(ctx, 'already-waiting', request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs, c)
        row = await dispatch(jobs, c, controller)
        assert row['execution_id'] == 'timeout'
        await finish(jobs, c, row, controller, outcome='timed_out', execution_seconds=900)
        terminal = await jobs.get('timeout')
        assert terminal['outcome'] == 'timed_out' and terminal['cleanup'] == 'idle_proved'
        assert terminal['reconciliation'] == 'pending'
        assert terminal['paused_timeout'] == 'timeout'
        assert (await jobs.visible('timeout'))['blocked_by_timeout_reconciliation']
        assert (await jobs.visible('already-waiting'))['blocked_by_timeout_reconciliation']
        with pytest.raises(WorkerUnavailable, match='timeout requires reconciliation'):
            await jobs.admit(ctx, 'new', request())
        assert await dispatch(jobs, c, controller) is None
        child = await jobs.continue_timeout(ctx, 'timeout', PROOF, current_workspace_digest='a'*64)
        continued = await dispatch(jobs, c, controller)
        assert continued['execution_id'] == child['execution_id']
        assert continued['parent_execution_id'] == 'timeout' and continued['selected_seconds'] == 1800
        await finish(jobs, c, continued, controller, execution_seconds=12)
        assert (await jobs.get('timeout'))['paused_timeout'] is None
        assert not (await jobs.visible('already-waiting'))['blocked_by_timeout_reconciliation']
        await jobs.admit(ctx, 'new', request())
        assert (await dispatch(jobs, c, controller))['execution_id'] == 'already-waiting'


@pytest.mark.asyncio
async def test_non_run_unknown_outcome_still_pauses_caller_group(jobs):
    ctx = context()
    await jobs.admit(ctx, 'unknown', request())
    await jobs.admit(ctx, 'already-waiting', request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs, c)
        row = await dispatch(jobs, c, controller)
        await finish(jobs, c, row, controller, outcome='unknown')
        terminal = await jobs.get('unknown')
        assert terminal['cleanup'] == 'idle_proved' and terminal['reconciliation'] == 'unknown'
        assert terminal['paused_unknown'] and terminal['paused_timeout'] is None
        assert (await jobs.visible('already-waiting'))['blocked_by_unknown_outcome']
        with pytest.raises(WorkerUnavailable, match='unknown outcome requires reconciliation'):
            await jobs.admit(ctx, 'new', request())
        assert await dispatch(jobs, c, controller) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('tier',[900,1800,3600])
async def test_each_start_tier_reserves_and_settles_once(jobs,tier):
    ctx=selected(await bound_run(jobs),tier)
    await jobs.admit(ctx,'e',request())
    async with jobs.pool.acquire() as c:
        controller=await slot(jobs,c)
        row=await dispatch(jobs,c,controller)
        account=await c.fetchrow('SELECT * FROM run_job_accounts')
        assert account['dispatched']==account['held']==1 and account['seconds_held']==tier
        await finish(jobs,c,row,controller,execution_seconds=12.5,cleanup_seconds=.25)
        await finish(jobs,c,row,controller,execution_seconds=12.5,cleanup_seconds=.25)
    account=await jobs.pool.fetchrow('SELECT * FROM run_job_accounts')
    assert account['completed']==1 and account['held']==0 and account['seconds_used']==12.5
    assert account['seconds_held']==0 and account['cleanup_seconds']==.25
    assert account['cli_usage_basis']=='estimated_or_unknown'


@pytest.mark.asyncio
async def test_timeout_progression_current_state_and_concurrent_continuation(jobs):
    ctx=selected(await bound_run(jobs),900)
    row=await jobs.admit(ctx,'initial',request())
    async with jobs.pool.acquire() as c:
        controller=await slot(jobs,c)
        for selected_seconds in (900,1800,3600):
            row=await dispatch(jobs,c,controller)
            assert row['selected_seconds']==selected_seconds and row['logical_job_id']=='initial'
            await finish(jobs,c,row,controller,outcome='timed_out',execution_seconds=selected_seconds)
            with pytest.raises(WorkerUnavailable,match='timeout requires reconciliation'):
                await jobs.admit(ctx,'reset-'+str(selected_seconds),request())
            if selected_seconds<3600:
                results=await asyncio.gather(*(jobs.continue_timeout(ctx,row['execution_id'],PROOF,current_workspace_digest='a'*64) for _ in range(4)))
                assert len({r['execution_id'] for r in results})==1
                child=results[0]
                assert child['execution_id']!=row['execution_id'] and child['parent_execution_id']==row['execution_id']
                assert child['timeout_episode']==row['timeout_episode']+1
                assert child['reconciliation_evidence']['workspace_digest']=='a'*64
                assert child['request']['workspace']==row['request']['workspace']
        with pytest.raises(WorkerUnavailable,match='job_grant_required'):
            await jobs.continue_timeout(ctx,row['execution_id'],PROOF,current_workspace_digest='a'*64)
    assert await jobs.pool.fetchval("SELECT status FROM agent_runs WHERE id='run'")=='paused'
    assert await jobs.pool.fetchval('SELECT count(*) FROM worker_job_attempts')==3


@pytest.mark.asyncio
@pytest.mark.parametrize('selected_seconds,granted',[(600,False),(7200,False),(600,True),(7200,True)])
async def test_custom_and_above_sixty_admission_and_dispatch(jobs,selected_seconds,granted):
    ctx=selected(await bound_run(jobs,grant=granted),selected_seconds)
    if not granted:
        with pytest.raises(WorkerUnavailable,match='grant_required'): await jobs.admit(ctx,'e',request())
        assert await jobs.pool.fetchval('SELECT count(*) FROM worker_queue')==0
    else:
        await jobs.admit(ctx,'e',request())
        async with jobs.pool.acquire() as c:
            row=await dispatch(jobs,c,await slot(jobs,c))
            assert row['selected_seconds']==row['effective_seconds']==selected_seconds


@pytest.mark.asyncio
@pytest.mark.parametrize('permission', ['allow_long_runs','allow_extended_jobs'])
async def test_call_only_or_partial_grants_cannot_authorize_extended(jobs,permission):
    ctx=selected(await bound_run(jobs,grant=True,**{permission:False}),7200)
    with pytest.raises(WorkerUnavailable): await jobs.admit(ctx,'e',request())


@pytest.mark.asyncio
async def test_reviewed_owner_override_wins_and_strict_policy_survives_restart(jobs):
    ctx=selected(await bound_run(jobs,grant=True,job_override_seconds=1800,job_auto_escalate=False),3600)
    await jobs.admit(ctx,'e',request())
    async with jobs.pool.acquire() as c:
        controller=await slot(jobs,c)
        row=await dispatch(jobs,c,controller)
        assert row['proposed_seconds']==3600 and row['selected_seconds']==row['owner_override']==1800
        await finish(jobs,c,row,controller,outcome='timed_out',execution_seconds=1800)
    with pytest.raises(WorkerUnavailable,match='escalation_disabled'):
        await jobs.continue_timeout(ctx,'e',PROOF,current_workspace_digest='a'*64)


@pytest.mark.asyncio
@pytest.mark.parametrize('window',['admission','dispatch','continuation','recovery'])
async def test_revocation_every_boundary_and_no_silent_grant_switch(jobs,window):
    ctx=selected(await bound_run(jobs,grant=True),900)
    if window!='admission': await jobs.admit(ctx,'e',request())
    async with jobs.pool.acquire() as c:
        controller=await slot(jobs,c)
        row=await dispatch(jobs,c,controller) if window in {'continuation','recovery'} else None
        if window=='continuation': await finish(jobs,c,row,controller,outcome='timed_out',execution_seconds=900)
        await jobs.pool.execute("UPDATE agent_execution_grants SET revoked=TRUE,revision=2 WHERE id='grant'")
        if window=='admission':
            with pytest.raises(WorkerUnavailable,match='grant_revoked'): await jobs.admit(ctx,'e',request())
        elif window=='dispatch':
            assert await dispatch(jobs,c,controller) is None
        elif window=='continuation':
            with pytest.raises(WorkerUnavailable,match='grant_revoked'): await jobs.continue_timeout(ctx,'e',PROOF,current_workspace_digest='a'*64)
        else:
            assert not await jobs.enforce_active(c,row)
            assert (await jobs.get('e'))['cancel_requested']
            await finish(jobs,c,row,controller)
            assert (await jobs.get('e'))['outcome']=='cancelled'


@pytest.mark.asyncio
async def test_revoke_api_reports_pending_cleanup_and_cancels_active_job(jobs):
    ctx=selected(await bound_run(jobs,grant=True),900)
    await jobs.admit(ctx,'e',request())
    async with jobs.pool.acquire() as c:
        controller=await slot(jobs,c)
        row=await dispatch(jobs,c,controller)
        result=await Store(jobs.pool,'lane').revoke_grant(Authority('lane','p','p',{},'human'),dict(grant_id='grant',expected_revision=1,request_key='revoke'))
        assert result['affected_runs']==[dict(run_id='run',cleanup_pending=True)]
        assert (await jobs.get('e'))['cancel_requested']
        context = await worker_flag_context(jobs, 'grant_revoked')
        assert context['job_account']['held'] == 1 and context['job_account']['seconds_held'] == 900
        replay = await Store(jobs.pool,'lane').revoke_grant(Authority('lane','p','p',{},'human'),dict(grant_id='grant',expected_revision=1,request_key='revoke'))
        assert replay == result
        assert await jobs.pool.fetchval('SELECT count(*) FROM agent_flags') == 1
        await finish(jobs,c,row,controller,execution_seconds=2)
    assert not await jobs.pool.fetchval("SELECT cleanup_pending FROM agent_runs WHERE id='run'")
    assert await jobs.pool.fetchval("SELECT reason FROM agent_runs WHERE id='run'")=='grant_revoked'
    account = await jobs.pool.fetchrow('SELECT * FROM run_job_accounts')
    assert account['held'] == account['seconds_held'] == 0
    assert account['completed'] == 1 and account['seconds_used'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('ceiling,expected', [('wall',100),('seconds',125)])
async def test_remaining_wall_and_job_time_do_not_reset_on_ladder(jobs,ceiling,expected):
    ctx=selected(await bound_run(jobs),1800)
    if ceiling=='wall': await jobs.pool.execute("UPDATE agent_runs SET deadline=to_timestamp(1100) WHERE id='run'")
    else: await jobs.pool.execute("UPDATE agent_runs SET request=jsonb_set(request,'{budget,job_seconds_budget}','125') WHERE id='run'")
    await jobs.admit(ctx,'e',request())
    async with jobs.pool.acquire() as c:
        controller=await slot(jobs,c)
        row=await dispatch(jobs,c,controller)
        assert row['effective_seconds']==expected and row['selected_seconds']==1800
        await finish(jobs,c,row,controller,outcome='timed_out',execution_seconds=expected)
    with pytest.raises(WorkerUnavailable,match='timeout exhausted'):
        await jobs.continue_timeout(ctx,'e',PROOF,current_workspace_digest='a'*64)


@pytest.mark.asyncio
async def test_unknown_time_holds_and_job_count_exhaustion(jobs):
    ctx=selected(await bound_run(jobs),900)
    await jobs.admit(ctx,'e',request())
    async with jobs.pool.acquire() as c:
        controller=await slot(jobs,c)
        row=await dispatch(jobs,c,controller)
        await finish(jobs,c,row,controller,outcome='unknown')
        await finish(jobs,c,row,controller,outcome='unknown')
    account=await jobs.pool.fetchrow('SELECT * FROM run_job_accounts')
    assert account['held']==account['unknown']==account['dispatched']==1 and account['seconds_held']==900
    assert account['completed']==account['seconds_used']==0


@pytest.mark.asyncio
async def test_unsent_cancel_does_not_charge_and_finite_attempts_block_queue_growth(jobs):
    ctx=selected(await bound_run(jobs),900)
    await jobs.pool.execute("UPDATE agent_runs SET request=jsonb_set(request,'{budget,max_job_attempts}','1') WHERE id='run'")
    await jobs.admit(ctx,'e',request())
    with pytest.raises(WorkerUnavailable,match='attempt_budget'): await jobs.admit(ctx,'second',request())
    await jobs.cancel('e',ctx)
    account=await jobs.pool.fetchrow('SELECT * FROM run_job_accounts')
    assert account['dispatched']==account['held']==account['seconds_used']==0
    await jobs.admit(ctx,'replacement',request())


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['before_send','after_admission','after_effect','after_journal','after_reply'])
async def test_crash_windows_keep_time_hold_or_settle_exactly_once(jobs,phase):
    from core.execution.worker_queue import WorkerQueue
    from types import SimpleNamespace
    ctx=selected(await bound_run(jobs),900)
    await jobs.admit(ctx,'e',request())
    async with jobs.pool.acquire() as c:
        old=await slot(jobs,c,'old')
        row=await dispatch(jobs,c,old)
        receipt={k:row[k] for k in ('execution_id','request_digest','lane_generation','controller_session','daemon_epoch')}
        receipt.update(outcome='succeeded',cleanup='idle_proved',execution_seconds=23,cleanup_seconds=.1)
        queue=WorkerQueue(jobs,SimpleNamespace(hello=lambda:dict(active_execution=None)),poll=.001)
        async def control(op,controller,execution_id=None):
            if op=='inspect': return dict(receipt=receipt if phase in {'after_journal','after_reply'} else None)
            return dict(type='idle_barrier',cleanup='idle_proved',daemon_epoch='new',lane_generation=controller['generation'],controller_session=controller['controller_session'])
        queue.control=control
        await queue.recover(c,await jobs.takeover(c,'new'))
        account=await jobs.pool.fetchrow('SELECT * FROM run_job_accounts')
        assert account['dispatched']==1
        assert account['seconds_used']==(23 if phase in {'after_journal','after_reply'} else 0)
        assert account['seconds_held']==(0 if phase in {'after_journal','after_reply'} else 900)


from tests.test_agent_loop_postgres import loop_h
from tests.test_grants_postgres import request as run_request, approve


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['task','project'])
async def test_reviewed_v3_prepare_issue_start_contract(loop_h,kind):
    h=loop_h
    scope=dict(profiles={'python':'a'*64},workspaces={'scratch':['src']},attachments=['S1'],
               max_job_attempts=32,job_seconds_budget=10000,allow_cli=False)
    body=run_request(calls=10,project_id='project',job_coverage=scope,
        budget=dict(start_calls=10,wall_mode='none',wall_seconds=None,max_job_attempts=32,job_seconds_budget=10000))
    proposal=await h.service.prepare(h.owner,dict(request=body,request_key='prepare'))
    assert proposal['coverage']['matcher_version']==3
    issued=await approve(h,proposal,kind,allow_extended_jobs=True,allow_custom_job_limit=True,
                         job_seconds_ceiling=7200,job_override_seconds=1800,job_auto_escalate=False)
    grant=issued['grant']
    assert grant['matcher_version']==3 and grant['job_coverage']==scope
    assert grant['allow_extended_jobs'] and grant['job_seconds_ceiling']==7200
    assert grant['job_override_seconds']==1800 and not grant['job_auto_escalate']
    started=await h.service.start(h.owner,{**body,'intent_id':proposal['intent_id']})
    assert started['run']['selected_grant_id']==grant['id']
    assert started['run']['job_account']['max_job_attempts']==32
    if kind=='project':
        future={**body,'request_key':'future','task':'future task S1'}
        assert (await h.service.start(h.owner,future))['run']['selected_grant_id']==grant['id']
        wider={**future,'request_key':'wider','job_coverage':{**scope,'workspaces':{'scratch':['.']}}}
        # Above 200 requires the pinned project grant; a wider scope cannot match.
        wider['budget']={**wider['budget'],'start_calls':400}
        with pytest.raises(Exception,match='confirmation_required'):
            await h.service.start(h.owner,wider)


@pytest.mark.asyncio
async def test_call_only_proposal_cannot_acquire_job_permissions(loop_h):
    h=loop_h
    proposal=await h.service.prepare(h.owner,dict(request=run_request(calls=10),request_key='prepare'))
    from core.execution.models import ExecutionError
    with pytest.raises(ExecutionError,match='job_coverage_required'):
        await approve(h,proposal,allow_extended_jobs=True)
    assert await h.pg.fetchval('SELECT count(*) FROM agent_execution_grants')==0


@pytest.mark.asyncio
async def test_queued_jobs_cannot_bypass_timeout_and_changed_workspace_blocks_dispatch(jobs):
    ctx=selected(await bound_run(jobs),900)
    await jobs.admit(ctx,'e',request())
    await jobs.admit(ctx,'already-waiting',request())
    async with jobs.pool.acquire() as c:
        controller=await slot(jobs,c)
        row=await dispatch(jobs,c,controller)
        await finish(jobs,c,row,controller,outcome='timed_out',execution_seconds=900)
        assert await dispatch(jobs,c,controller) is None
        child=await jobs.continue_timeout(ctx,'e',PROOF,current_workspace_digest='a'*64)
        jobs.workspace_verifier=lambda row:'changed-after-reconciliation'
        assert await dispatch(jobs,c,controller) is None
        assert (await jobs.get(child['execution_id']))['outcome']=='cancelled'
    assert await jobs.pool.fetchval('SELECT dispatched FROM run_job_accounts')==1


@pytest.mark.asyncio
@pytest.mark.parametrize('tier',[3600,7200])
async def test_extended_default_continuation_needs_extended_permission_without_custom(jobs,tier):
    ctx=selected(await bound_run(jobs,grant=True,allow_custom_job_limit=False),tier)
    await jobs.admit(ctx,'e',request())
    async with jobs.pool.acquire() as c:
        controller=await slot(jobs,c)
        row=await dispatch(jobs,c,controller)
        await finish(jobs,c,row,controller,outcome='timed_out',execution_seconds=tier)
        child=await jobs.continue_timeout(ctx,'e',PROOF,current_workspace_digest='a'*64)
        assert child['request']['timeout']==tier*2
        continued=await dispatch(jobs,c,controller)
        assert continued['execution_id']==child['execution_id'] and continued['selected_seconds']==tier*2


@pytest.mark.asyncio
async def test_revoke_serializes_with_worker_admission(jobs):
    ctx=selected(await bound_run(jobs,grant=True),7200)
    result=await asyncio.gather(jobs.admit(ctx,'raced',request()),
        Store(jobs.pool,'lane').revoke_grant(Authority('lane','p','p',{},'human'),dict(grant_id='grant',expected_revision=1,request_key='revoke-race')),
        return_exceptions=True)
    assert not isinstance(result[1],Exception)
    context = await worker_flag_context(jobs, 'grant_revoked')
    if not isinstance(result[0],Exception):
        assert (await jobs.get('raced'))['cancel_requested']
        assert context['job_account']['held'] == context['job_account']['dispatched'] == 0
    else:
        assert isinstance(result[0], WorkerUnavailable)
        assert context['job_account'] is None
    assert await jobs.pool.fetchval("SELECT revoked FROM agent_execution_grants WHERE id='grant'")
    assert await jobs.pool.fetchval("SELECT status FROM agent_runs WHERE id='run'") == 'paused'
    assert await jobs.pool.fetchval('SELECT coalesce(sum(dispatched),0) FROM run_job_accounts')==0
