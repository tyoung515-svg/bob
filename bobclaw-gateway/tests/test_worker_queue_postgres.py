"""4.3a real temporary Postgres: scheduling, races and crash-window recovery.

S4B_TEST_POSTGRES_DSN is required; a skip is not a storage-gate pass. No providers.
"""
import asyncio
import copy
import json
from types import SimpleNamespace
import time

import pytest

from tests.test_durable_runs_v2 import pg
from core.execution.store import Store
from core.execution.job_store import JobStore
from core.execution.jobs import JobContext
from core.execution.worker_queue import WorkerQueue
from core.worker.contract import WorkerUnavailable, validate_v2


def request(label='a'):
    return dict(version=1, job_id=label, profile='python', workspace='scratch', cwd='.',
                args=['-c', 'print(1)'], input=label, timeout=10, output_limit=1024, expires=time.time()+20)


def context(principal='p', group='g'):
    return JobContext(principal, principal, group, 'fixture')


@pytest.fixture
async def jobs(pg):
    await Store(pg, 'lane').initialize()
    store = JobStore(pg, 'lane', clock=lambda: 1000)
    await store.initialize()
    return store


async def slot(jobs, c, owner='controller'):
    value = await jobs.takeover(c, owner)
    await jobs.barrier(c, value, dict(type='idle_barrier', cleanup='idle_proved',
        lane_generation=value['generation'], controller_session=owner, daemon_epoch='epoch'))
    value['daemon_epoch'] = 'epoch'
    return value


async def finish(jobs, c, row, controller, **changes):
    receipt = {k: row[k] for k in ('execution_id', 'request_digest', 'lane_generation', 'controller_session', 'daemon_epoch')}
    receipt.update(type='result', outcome='succeeded', cleanup='idle_proved', returncode=0, killed=False)
    receipt.update(changes)
    await jobs.settle(c, row, receipt, controller)
    return receipt


@pytest.mark.asyncio
async def test_concurrent_idempotency_and_conflict(jobs):
    results = await asyncio.gather(*(jobs.admit(context(), 'one', request()) for _ in range(8)))
    assert {r['execution_id'] for r in results} == {'one'}
    with pytest.raises(WorkerUnavailable, match='conflict'):
        await jobs.admit(context(), 'one', request('changed'))
    assert await jobs.pool.fetchval('SELECT count(*) FROM worker_queue') == 1
    assert await jobs.pool.fetchval('SELECT count(*) FROM worker_job_events') == 1


@pytest.mark.asyncio
async def test_fifo_round_robin_and_one_physical_controller(jobs):
    for execution, principal, group in [('a1','p','a'), ('a2','p','a'), ('b1','p','b'), ('c1','q','c'), ('b2','p','b')]:
        await jobs.admit(context(principal, group), execution, request(execution))
    selected = []
    async with jobs.pool.acquire() as c, jobs.pool.acquire() as other:
        assert await c.fetchval('SELECT pg_try_advisory_lock($1)', jobs.controller_lock)
        assert not await other.fetchval('SELECT pg_try_advisory_lock($1)', jobs.controller_lock)
        controller = await slot(jobs, c)
        try:
            for _ in range(5):
                row = await jobs.select(c, controller, {'python': 'a'*64})
                selected.append(row['execution_id'])
                validate_v2(row['request'], now=1000)
                assert row['request_digest']
                await finish(jobs, c, row, controller)
        finally:
            await c.execute('SELECT pg_advisory_unlock($1)', jobs.controller_lock)
    assert selected == ['a1', 'b1', 'c1', 'a2', 'b2']
    assert await jobs.pool.fetchval('SELECT count(*) FROM worker_job_dedup') == 5


@pytest.mark.asyncio
async def test_principal_bound_cannot_be_bypassed_by_groups_and_lane_bound(jobs):
    jobs.principal_limit, jobs.lane_limit = 2, 3
    await asyncio.gather(jobs.admit(context('p','c1'), '1', request()), jobs.admit(context('p','c2'), '2', request()))
    with pytest.raises(WorkerUnavailable, match='bound'):
        await jobs.admit(context('p','c3'), '3', request())
    await jobs.admit(context('q','c1'), '4', request())
    with pytest.raises(WorkerUnavailable, match='bound'):
        await jobs.admit(context('r','c1'), '5', request())


@pytest.mark.asyncio
async def test_cancel_ownership_and_late_result_cannot_resume_cancelled_job(jobs):
    await jobs.admit(context(), 'e', request())
    with pytest.raises(WorkerUnavailable, match='cancellation owner'):
        await jobs.cancel('e', context('other'))
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs, c)
        row = await jobs.select(c, controller, {'python':'a'*64})
        await jobs.cancel('e', context())
        await finish(jobs, c, row, controller)
        await finish(jobs, c, row, controller)
    assert (await jobs.get('e'))['outcome'] == 'cancelled'
    assert await jobs.pool.fetchval("SELECT count(*) FROM worker_job_events WHERE kind='terminal'") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('window', ['before_send','after_admission','after_effect','after_journal','after_reply'])
async def test_crash_windows_import_terminal_once_or_unknown_never_replay(jobs, window):
    await jobs.admit(context(), 'e', request())
    async with jobs.pool.acquire() as c:
        old = await slot(jobs, c, 'old')
        row = await jobs.select(c, old, {'python':'a'*64})
        controller = await jobs.takeover(c, 'new')
        receipt = {k: row[k] for k in ('execution_id', 'request_digest', 'lane_generation', 'controller_session', 'daemon_epoch')}
        receipt.update(type='result', outcome='succeeded', cleanup='idle_proved', returncode=0)
        calls = []
        transport = SimpleNamespace(hello=lambda: {'active_execution': 'e' if window in {'after_admission','after_effect'} else None, 'profiles': {'python':'a'*64}},
                                    submit=lambda *a: pytest.fail('uncertain attempt replayed'))
        queue = WorkerQueue(jobs, transport, poll=.001)
        async def control(op, slot, execution_id=None):
            calls.append(op)
            if op == 'inspect':
                return {'receipt': receipt if window in {'after_journal','after_reply'} else None}
            if op == 'cancel':
                return {'type': 'cancelling'}
            return dict(type='idle_barrier', cleanup='idle_proved', daemon_epoch='new-epoch',
                        lane_generation=slot['generation'], controller_session=slot['controller_session'])
        queue.control = control
        await queue.recover(c, controller)
        result = await jobs.get('e')
        assert result['outcome'] == ('succeeded' if window in {'after_journal','after_reply'} else 'unknown')
        assert result['cleanup'] == 'idle_proved'
        assert await jobs.select(c, controller, {'python':'a'*64}) is None
        assert (await jobs.slot(c))['active_execution'] is None
        assert await jobs.pool.fetchval('SELECT count(*) FROM worker_job_dedup') == 1


@pytest.mark.asyncio
async def test_fencing_late_receipt_and_cleanup_required(jobs):
    await jobs.admit(context(), 'e', request())
    async with jobs.pool.acquire() as c:
        old = await slot(jobs, c, 'old')
        row = await jobs.select(c, old, {'python':'a'*64})
        with pytest.raises(WorkerUnavailable, match='cleanup'):
            await finish(jobs, c, row, old, cleanup='unknown')
        assert (await jobs.slot(c))['active_execution'] == 'e'
        await jobs.takeover(c, 'new')
        with pytest.raises(WorkerUnavailable, match='stale settlement'):
            await finish(jobs, c, row, old)


@pytest.mark.asyncio
async def test_dispatch_intent_and_terminal_dedup_survive_client_reconnect(jobs):
    original = request()
    await jobs.admit(context(), 'e', original)
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs, c)
        row = await jobs.select(c, controller, {'python':'a'*64})
        assert (await jobs.admit(context(), 'e', original))['outcome'] == 'dispatch_intent'
        await finish(jobs, c, row, controller)
        reconnected = JobStore(jobs.pool, 'lane')
        await reconnected.initialize()
        assert (await reconnected.admit(context(), 'e', original))['outcome'] == 'succeeded'
        assert await reconnected.select(c, controller, {'python':'a'*64}) is None


@pytest.mark.asyncio
async def test_visible_wait_does_not_acquire_worker_slot(jobs):
    await jobs.admit(context(), 'e', request())
    visible = await jobs.visible('e')
    assert visible['state'] == 'waiting_for_lane_worker'
    assert visible['selected_tier'] == 10 and visible['queued_at']
    async with jobs.pool.acquire() as c:
        assert (await jobs.slot(c))['active_execution'] is None


async def seed_run(jobs):
    await jobs.pool.execute("INSERT INTO agent_team_heads VALUES('lane','p','fixture',1)")
    await jobs.pool.execute("INSERT INTO agent_team_versions(lane,owner,id,revision,actor,boundary,definition) VALUES('lane','p','fixture',1,'p','b','{}')")
    await jobs.pool.execute("INSERT INTO agent_runs(lane,owner,id,actor,boundary,run_authority,team,team_revision,snapshot,request,status,generation) VALUES('lane','p','run','p','b','{}','fixture',1,'{}','{}','running',1)")
    return JobContext('p','p','run:run','b','run',1)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['generation=2','authorization_revoked=TRUE'])
async def test_run_generation_checked_at_admission_dispatch_and_cancel(jobs,mutation):
    current = await seed_run(jobs)
    stale = JobContext('p','p','run:run','b','run',0)
    with pytest.raises(WorkerUnavailable, match='stale owning run'):
        await jobs.admit(stale, 'stale', request())
    await jobs.admit(current, 'e', request())
    with pytest.raises(WorkerUnavailable, match='cancellation owner'):
        await jobs.cancel('e', stale)
    await jobs.pool.execute("UPDATE agent_runs SET "+mutation+" WHERE id='run'")
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs,c)
        assert await jobs.select(c,controller,{'python':'a'*64}) is None
    assert (await jobs.get('e'))['outcome'] == 'cancelled'


@pytest.mark.asyncio
async def test_unknown_pauses_owning_run_but_releases_physical_slot(jobs):
    current = await seed_run(jobs)
    await jobs.admit(current,'e',request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs,c)
        row = await jobs.select(c,controller,{'python':'a'*64})
        await finish(jobs,c,row,controller,outcome='unknown')
        assert (await jobs.slot(c))['active_execution'] is None
    assert await jobs.pool.fetchval("SELECT status FROM agent_runs WHERE id='run'") == 'paused'
    assert (await jobs.get('e'))['reconciliation'] == 'unknown'
    with pytest.raises(WorkerUnavailable, match='group paused'):
        await jobs.admit(current,'automatic-retry',request())
    await jobs.admit(context('other','other'),'unrelated',request())


@pytest.mark.asyncio
async def test_queued_cancel_is_prompt_and_second_dispatch_needs_cleanup(jobs):
    await jobs.admit(context(),'e',request())
    await jobs.admit(context(),'waiting',request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs,c)
        first = await jobs.select(c,controller,{'python':'a'*64})
        assert first['execution_id'] == 'e'
        assert await jobs.select(c,controller,{'python':'a'*64}) is None
        await jobs.cancel('waiting',context())
        assert (await jobs.get('waiting'))['outcome'] == 'cancelled'
        assert (await jobs.slot(c))['active_execution'] == 'e'


@pytest.mark.asyncio
@pytest.mark.parametrize('deadline,expected',[(1002,2),(999,None)])
async def test_existing_run_wall_caps_dispatch_with_injected_clock(jobs,deadline,expected):
    current = await seed_run(jobs)
    await jobs.pool.execute("UPDATE agent_runs SET deadline=to_timestamp($1) WHERE id='run'",deadline)
    await jobs.admit(current,'e',request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs,c)
        row = await jobs.select(c,controller,{'python':'a'*64})
    if expected is None:
        assert row is None and (await jobs.get('e'))['outcome']=='cancelled'
    else:
        assert row['selected_seconds']==10 and row['effective_seconds']==expected
        assert row['request']['timeout']==expected and row['request']['not_after']==deadline


@pytest.mark.asyncio
async def test_removed_installed_profile_fails_without_holding_lane(jobs):
    await jobs.admit(context(),'e',request())
    async with jobs.pool.acquire() as c:
        controller = await slot(jobs,c)
        assert await jobs.select(c,controller,{}) is None
        assert (await jobs.slot(c))['active_execution'] is None
    row = await jobs.get('e')
    assert row['outcome']=='failed' and row['request_digest'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_reply',[False,True])
async def test_real_db_pump_executes_once_and_imports_lost_reply(jobs,lost_reply):
    class Transport:
        generation = 0
        receipts = {}
        launches = []
        def hello(self):
            return dict(active_execution=None,lane_generation=self.generation,profiles={'python':'a'*64})
        def control(self,value):
            self.generation = max(self.generation,value['lane_generation'])
            if value['op']=='inspect':
                return {'receipt':self.receipts.get(value['execution_id'])}
            return dict(type='idle_barrier',cleanup='idle_proved',daemon_epoch='epoch',
                        lane_generation=value['lane_generation'],controller_session=value['controller_session'])
        def submit(self,value,stop):
            self.launches.append(value['execution_id'])
            time.sleep(.01)
            receipt = {k:value[k] for k in ('execution_id','request_digest','lane_generation','controller_session')}
            receipt.update(type='result',outcome='succeeded',cleanup='idle_proved',daemon_epoch='epoch',returncode=0,killed=False)
            self.receipts[value['execution_id']] = receipt
            if lost_reply and len(self.launches)==1:
                raise OSError('injected loss after durable worker terminal')
            return receipt
    transport = Transport()
    queue = WorkerQueue(jobs,transport,poll=.005)
    queue.start()
    try:
        results = await asyncio.wait_for(asyncio.gather(queue.submit(context('p','a'),request(),execution_id='a'),
            queue.submit(context('q','b'),request(),execution_id='b')),timeout=10)
        assert all(result['outcome']=='succeeded' for result in results)
        assert sorted(transport.launches)==['a','b']
        assert await jobs.pool.fetchval('SELECT count(*) FROM worker_job_dedup')==2
        assert await jobs.pool.fetchval("SELECT count(*) FROM worker_job_events WHERE kind IN ('terminal','reconciled')")==2
    finally:
        await queue.close()
