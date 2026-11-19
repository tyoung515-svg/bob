"""Real disposable Postgres + actual edits/journals; no provider calls.

Set S4B_TEST_POSTGRES_DSN as in test_day_one_postgres.py. Daemon transport and
checked-job model output are fixtures; installed containment remains a WSL gate.
"""
import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import time
import uuid

import pytest

from core.portal_resources import ResourceError

from tests.test_durable_runs_v2 import pg
from tests.test_tool_jobs_postgres import jobs_api
from tests.test_checked_edits_postgres import checked_api
from tests.test_day_one_postgres import work, build, Transport
from core.execution.job_store import JobStore
from core.execution.jobs import current_job, JobContext
from core.execution.job_policy import JobPolicy
from core.execution.models import ExecutionError, canonical
from core.execution.worker_queue import WorkerQueue
from core.execution.workspaces import snapshot
from core.tool_jobs import checked_runner as runner
from core.worker.contract import validate, WorkerUnavailable


async def build_another(h, monkeypatch, label):
    prepare, start, issue = h.work.prepare, h.work.start, h.store.issue_grant
    async def prepare_unique(auth, args): return await prepare(auth, {**args, 'request_key': args['request_key'] + label})
    async def start_unique(auth, args): return await start(auth, {**args, 'request_key': args['request_key'] + label})
    async def issue_unique(auth, args): return await issue(auth, {**args, 'request_key': args['request_key'] + label})
    with monkeypatch.context() as m:
        m.setattr(h.work, 'prepare', prepare_unique)
        m.setattr(h.work, 'start', start_unique)
        m.setattr(h.store, 'issue_grant', issue_unique)
        started, _ = await build(h, 'builder')
    prop = await h.jobs.command(h.auth, 'jobs.proposal', {'job_id': started['id']})
    return {'job_id': started['id'], 'proposal_digest': prop['digest'],
            'expected_revision': prop['revision'], 'request_key': 'publish-' + label}


class PublicationTransport(Transport):
    def __init__(self, h):
        super().__init__(h)
        self.unknown_before = False
        self.unknown_after = False
        self.after_edit = None
        self.wrong_digest = False
        self.failure_before = None

    def submit(self, request, stop):
        self.launches.append(request['execution_id'])
        error = self.failure_before
        self.failure_before = None
        result = None
        unknown = self.unknown_before
        self.unknown_before = False
        if not unknown and not error:
            try:
                result = runner.execute(json.loads(request['input']), self.h.project)
                if self.after_edit: self.after_edit()
                if self.wrong_digest: result['output_digest'] = '0' * 64
            except Exception as exc:
                error = getattr(exc, 'code', type(exc).__name__)
        unknown = unknown or self.unknown_after
        self.unknown_after = False
        out = canonical({'adapter': 'checked-edits/1', 'result': result}) if result else ''
        err = canonical({'adapter': 'checked-edits/1', 'error': error}) if error else ''
        receipt = {k: request[k] for k in ('execution_id', 'request_digest', 'lane_generation', 'controller_session')}
        receipt.update(type='result', daemon_epoch='fixture',
                       outcome='unknown' if unknown else 'failed' if error else 'succeeded',
                       cleanup='idle_proved', returncode=1 if error or unknown else 0,
                       execution_seconds=.01, cleanup_seconds=0,
                       stdout_b64='' if unknown else base64.b64encode(out.encode()).decode(),
                       stderr_b64='' if unknown else base64.b64encode(err.encode()).decode())
        self.receipts[request['execution_id']] = receipt
        return receipt


@pytest.fixture
async def publisher(work, monkeypatch):
    h = work
    h.pub_args = await build_another(h, monkeypatch, 'first')
    worker = JobStore(h.pg, 'lane')
    transport = PublicationTransport(h)
    queue = WorkerQueue(worker, transport, poll=.01)
    h.publication_transport = transport
    h.publication_worker = worker
    h.before_dispatch = None
    h.after_dispatch = None
    queue_started = False
    async def managed_execute(profile, *, cwd, input, timeout, output_limit):
        nonlocal queue_started
        if h.before_dispatch: await h.before_dispatch()
        if not queue_started:
            queue.start()
            queue_started = True
        identifier = uuid.uuid4().hex
        request = validate({'version': 1, 'job_id': identifier, 'profile': profile,
                            'workspace': 'projects', 'cwd': h.pid, 'args': [], 'input': input,
                            'timeout': timeout, 'output_limit': output_limit, 'expires': time.time() + timeout + 20},
                           max_timeout=2147483647)
        result = await asyncio.wait_for(queue.submit(current_job.get(), request, execution_id=identifier), 10)
        if h.after_dispatch: await h.after_dispatch()
        return result
    from core.worker import client
    monkeypatch.setattr(client, 'execute_async', managed_execute)
    try: yield h
    finally: await queue.close()


async def publish(h, args=None):
    return await h.work.command(h.owner, 'work.publish', args or h.pub_args)


async def publication(h, args=None):
    return await h.pg.fetchrow('SELECT * FROM work_publications WHERE job_id=$1', (args or h.pub_args)['job_id'])


@pytest.mark.asyncio
async def test_predispatch_stale_source_conflict_commits_and_replays_receipt(publisher):
    h = publisher
    args = dict(h.pub_args)
    (h.project/'file.txt').write_text('owner changed source')
    code = 'proposal_stale'
    for _ in range(2):
        with pytest.raises(ExecutionError, match=code) as caught: await publish(h, args)
        row = await publication(h, args)
        assert row['state'] == 'conflict'
        assert caught.value.status == 409 and caught.value.detail == json.loads(row['receipt'])
    assert not h.publication_transport.launches
    assert not (h.project/'app').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['incomplete', 'digest_same_key', 'digest_new_key', 'revision'])
async def test_caller_refusal_leaves_no_row_then_corrected_request_publishes(publisher, failure):
    h = publisher
    args = dict(h.pub_args)
    run_id = await h.pg.fetchval('SELECT run_id FROM tool_jobs WHERE id=$1', args['job_id'])
    code = 'proposal_digest_conflict'
    if failure == 'incomplete':
        await h.pg.execute("UPDATE agent_runs SET status='running' WHERE id=$1", run_id)
        code = 'work_not_completed'
    elif failure == 'revision':
        args['expected_revision'] += 1
        code = 'revision_conflict'
    else: args['proposal_digest'] = '0' * 64
    with pytest.raises(ExecutionError, match=code) as caught: await publish(h, args)
    assert caught.value.status == 409
    assert await publication(h) is None and not h.publication_transport.launches
    await h.pg.execute("UPDATE agent_runs SET status='completed' WHERE id=$1", run_id)
    if failure == 'digest_new_key': h.pub_args['request_key'] = 'corrected'
    assert (await publish(h))['state'] == 'published'
    assert (await publication(h))['state'] == 'completed'
    assert len(h.publication_transport.launches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['snapshot_timeout', 'snapshot_unavailable', 'unlisted_worker_failure'])
async def test_worker_transient_error_is_unknown_then_same_key_retry_publishes(publisher, code):
    h = publisher
    h.publication_transport.failure_before = code
    with pytest.raises(ExecutionError, match=code): await publish(h)
    row = await publication(h)
    assert row['state'] == 'unknown'
    assert json.loads(row['receipt'])['code'] == code
    assert not (h.project/'app').exists()
    assert (await publish(h))['state'] == 'published'
    assert (await publication(h))['operation_key'] == row['operation_key']
    assert len(h.publication_transport.launches) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['owner_session_invalid', 'credential_epoch_changed'])
async def test_owner_expires_at_queued_dispatch_then_renewal_allows_retry(publisher, monkeypatch, code):
    from core.execution import owner
    h = publisher
    original_authority = h.publication_worker.authority
    original_owner = owner.require_owner
    expired, fail_dispatch = False, True
    async def owner_session(auth, operation):
        if expired: raise ExecutionError(code, 403)
        await original_owner(auth, operation)
    async def authority(c, *, context=None, row=None):
        nonlocal expired
        if row is not None and fail_dispatch: expired = True
        return await original_authority(c, context=context, row=row)
    monkeypatch.setattr(owner, 'require_owner', owner_session)
    monkeypatch.setattr(h.publication_worker, 'authority', authority)
    with pytest.raises(ExecutionError, match='publication_not_established'): await publish(h)
    assert (await publication(h))['state'] == 'unknown'
    assert not h.publication_transport.launches
    expired, fail_dispatch = False, False
    h.owner = replace(h.owner, credential_id='renewed', credential_epoch=2)
    assert (await publish(h))['state'] == 'published'
    assert json.loads((await publication(h))['authority'])['credential_id'] == 'renewed'
    assert len(h.publication_transport.launches) == 1


@pytest.mark.asyncio
async def test_other_checked_operation_blocker_clears_then_same_proposal_publishes(publisher, monkeypatch):
    h = publisher
    h.publication_transport.unknown_before = True
    with pytest.raises(WorkerUnavailable): await publish(h)
    # Another checked job journals a no-op replacement. Reconciliation leaves
    # the original source digest intact, so this proposal can still publish.
    base = snapshot(h.project)['digest']
    content = (h.project/'file.txt').read_text()
    edits = {'edits': [{'operation': 'replace', 'path': 'file.txt', 'content': content,
                        'pre_sha256': hashlib.sha256(content.encode()).hexdigest()}]}
    original = runner.write_journal
    def crash_after_intent(root, state):
        original(root, state)
        if not state['completed']: raise OSError('other job lost after journal intent')
    with monkeypatch.context() as m:
        m.setattr(runner, 'write_journal', crash_after_intent)
        # safe_open reports any OSError inside the locked edit as ResourceError.
        with pytest.raises((OSError, ResourceError)):
            runner.apply_edits(h.project, edits, ['file.txt'], operation_key='other-job', input_digest=base)
    with pytest.raises(ExecutionError, match='edit_reconciliation_required') as caught: await publish(h)
    assert (await publication(h))['state'] == 'unknown'
    assert caught.value.detail['detail']['operation_key'] == 'other-job'
    assert len(h.publication_transport.launches) == 1
    runner.apply_edits(h.project, edits, ['file.txt'], operation_key='other-job', input_digest=base)
    assert snapshot(h.project)['digest'] == base
    assert (await publish(h))['state'] == 'published'
    assert len(h.publication_transport.launches) == 2


@pytest.mark.asyncio
async def test_unknown_prewrite_retry_conflicts_if_workspace_changed(publisher):
    h = publisher
    h.publication_transport.unknown_before = True
    with pytest.raises(WorkerUnavailable): await publish(h)
    assert (await publication(h))['state'] == 'unknown'
    assert not (h.project/runner.JOURNAL).exists()
    (h.project/'file.txt').write_text('owner changed source')
    with pytest.raises(ExecutionError, match='stale_snapshot'): await publish(h)
    assert (await publication(h))['state'] == 'conflict'
    assert not (h.project/'app').exists()
    launches = len(h.publication_transport.launches)
    with pytest.raises(ExecutionError, match='stale_snapshot'): await publish(h)
    assert len(h.publication_transport.launches) == launches == 2


@pytest.mark.asyncio
async def test_lost_reply_reconciles_completed_journal_with_original_key(publisher):
    h = publisher
    h.publication_transport.unknown_after = True
    with pytest.raises(WorkerUnavailable): await publish(h)
    assert (await publication(h))['state'] == 'unknown'
    assert runner.read_journal(h.project)['completed']
    with pytest.raises(ExecutionError, match='publication_already_requested'):
        await publish(h, {**h.pub_args, 'request_key': 'different'})
    with pytest.raises(ExecutionError, match='publication_already_requested'):
        await publish(h, {**h.pub_args, 'proposal_digest': '0' * 64})
    result = await publish(h)
    assert result['state'] == 'published' and (await publication(h))['state'] == 'completed'
    assert result['output_digest'] == snapshot(h.project)['digest']
    assert len(h.publication_transport.launches) == 2
    assert (await publish(h))['replayed']
    assert len(h.publication_transport.launches) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['input', 'cleanup', 'authority'])
async def test_unknown_retry_never_bypasses_binding_cleanup_or_owner(publisher, monkeypatch, changed):
    h = publisher
    h.publication_transport.unknown_before = True
    with pytest.raises(WorkerUnavailable): await publish(h)
    old = await h.pg.fetchrow("SELECT * FROM worker_job_attempts WHERE caller_group=$1 ORDER BY queued_at DESC LIMIT 1",
                              'publish:' + h.pub_args['job_id'])
    wire = json.loads(old['request'])
    identifier = uuid.uuid4().hex
    request = {k: wire[k] for k in ('profile', 'workspace', 'cwd', 'args', 'input', 'output_limit')}
    request.update(version=1, job_id=identifier, timeout=900, expires=time.time() + 920)
    context = JobContext(h.owner.owner, h.owner.actor, 'publish:' + h.pub_args['job_id'], h.owner.boundary,
                         policy=JobPolicy(900, 'Owner-reviewed checked source publication', auto_escalate=False))
    if changed == 'input':
        value = json.loads(request['input']); value['input_digest'] = '0' * 64
        request['input'] = canonical(value)
    elif changed == 'cleanup':
        await h.pg.execute("UPDATE worker_job_attempts SET cleanup='unknown' WHERE execution_id=$1", old['execution_id'])
    else:
        from core.execution import owner
        async def denied(auth, operation): raise ExecutionError('owner_session_invalid', 403)
        monkeypatch.setattr(owner, 'require_owner', denied)
    with pytest.raises((WorkerUnavailable, ExecutionError)):
        await h.publication_worker.admit(context, identifier, request)
    assert not await h.pg.fetchval('SELECT count(*) FROM worker_job_attempts WHERE execution_id=$1', identifier)
    assert await h.pg.fetchval('SELECT paused_unknown FROM worker_callers WHERE caller_group=$1', context.group)
    assert not (h.project/'app').exists()


@pytest.mark.asyncio
async def test_lock_held_output_is_used_after_another_writer_changes_source(publisher):
    h = publisher
    h.publication_transport.after_edit = lambda: (h.project/'later.txt').write_text('later independent edit')
    result = await publish(h)
    assert (await publication(h))['state'] == 'completed'
    assert result['output_digest'] != snapshot(h.project)['digest']
    assert (h.project/'app/calc.py').read_text() == h.content


@pytest.mark.asyncio
async def test_output_digest_conflict_is_terminal(publisher):
    h = publisher
    h.publication_transport.wrong_digest = True
    for _ in range(2):
        with pytest.raises(ExecutionError, match='publication_output_conflict'): await publish(h)
    assert (await publication(h))['state'] == 'conflict'
    assert len(h.publication_transport.launches) == 1


@pytest.mark.asyncio
async def test_stuck_journal_names_blocking_job_and_original_key_then_reconciles(publisher, monkeypatch):
    h = publisher
    # A second checked job must freeze the same original source before writes.
    second = await build_another(h, monkeypatch, 'second')
    original = runner.os.replace
    def crash(source, target, **kwargs):
        if str(target).replace('\\', '/').endswith('test_calc.py'):
            raise OSError('crash after first edit')
        return original(source, target, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(runner.os, 'replace', crash)
        # An OSError without errno is io_error under C6's filesystem taxonomy.
        # It does not establish publication; the durable state stays unknown.
        with pytest.raises(ExecutionError, match='^io_error$'): await publish(h)
    assert (await publication(h))['state'] == 'unknown'
    assert not runner.read_journal(h.project)['completed']
    status = await h.work.command(h.auth, 'work.status', {'job_id': second['job_id']})
    blocker = status['blocking_publication']
    assert blocker['job_id'] == h.pub_args['job_id']
    assert blocker['request_key'] == h.pub_args['request_key']
    assert blocker['operation_key'] == (await publication(h))['operation_key']
    assert 'original request key' in blocker['recovery']
    with pytest.raises(ExecutionError, match='edit_reconciliation_required') as caught: await publish(h, second)
    # New preflight failures do not reserve the second job's publication slot.
    assert caught.value.detail == blocker
    assert await publication(h, second) is None
    await publish(h)
    assert (h.project/'app/test_calc.py').exists()
    assert runner.read_journal(h.project)['completed']


@pytest.mark.asyncio
async def test_completed_receipt_survives_late_failure_from_same_key(publisher):
    h = publisher
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def lose_first_reply():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise ConnectionError('late reply loss')
    h.after_dispatch = lose_first_reply
    first = asyncio.create_task(publish(h))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        completed = await publish(h)
        stored = (await publication(h))['receipt']
        release.set()
        with pytest.raises(ConnectionError): await first
        assert (await publication(h))['state'] == 'completed'
        assert (await publication(h))['receipt'] == stored
        assert (await publish(h))['published_at'] == completed['published_at']
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_competing_jobs_same_base_one_completed_one_conflict(publisher, monkeypatch):
    h = publisher
    second = await build_another(h, monkeypatch, 'second')
    arrived, ready = 0, asyncio.Event()
    async def both_ready():
        nonlocal arrived
        arrived += 1
        if arrived == 2: ready.set()
        await asyncio.wait_for(ready.wait(), 5)
    h.before_dispatch = both_ready
    results = await asyncio.gather(publish(h), publish(h, second), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    loser = next(r for r in results if isinstance(r, Exception))
    assert isinstance(loser, ExecutionError) and loser.code == 'stale_snapshot'
    states = sorted([ (await publication(h))['state'], (await publication(h, second))['state'] ])
    assert states == ['completed', 'conflict']
    assert (h.project/'app/calc.py').read_text() == h.content
    assert (h.project/'app/test_calc.py').exists()
    assert (h.project/'file.txt').read_text() == 'registered base'
