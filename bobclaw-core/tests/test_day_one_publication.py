"""Publication receipts and actual filesystem/journal recovery, without models."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.portal_resources import ResourceError

from core.execution.models import Authority, ExecutionError, canonical
from core.execution.publication import TERMINAL_CONFLICTS, authorize_worker, failure_receipt, publish, worker_output
from core.execution.workspaces import snapshot
from core.tool_jobs import checked_runner as runner


@pytest.fixture(autouse=True)
def mock_redis():
    # This offline adapter suite never imports the chat graph or uses Redis.
    yield


def edit(name, content, before=None):
    return {'operation': 'create' if before is None else 'replace', 'path': name,
            'pre_sha256': hashlib.sha256(before.encode()).hexdigest() if before is not None else None,
            'content': content}


def apply(root, items, base, key='publish'):
    return runner.apply_edits(root, {'edits': items}, ['app'], operation_key=key, input_digest=base)


def test_output_digest_covers_frozen_scope_and_exclusions(tmp_path):
    (tmp_path/'unrelated.txt').write_text('preserve')
    base = snapshot(tmp_path)['digest']
    result = apply(tmp_path, [edit('app/a.py', 'x=1\n')], base)
    assert result['output_digest'] == snapshot(tmp_path)['digest']
    assert runner.read_journal(tmp_path)['output_digest'] == result['output_digest']
    assert (tmp_path/'unrelated.txt').read_text() == 'preserve'
    (tmp_path/'app/__pycache__').mkdir()
    (tmp_path/'app/__pycache__/a.pyc').write_bytes(b'cache')
    assert snapshot(tmp_path)['digest'] == result['output_digest']
    assert apply(tmp_path, [edit('app/a.py', 'x=1\n')], base)['resumed']


@pytest.mark.skipif(os.name == 'nt', reason='directory flock is the installed Linux runtime contract')
def test_output_snapshot_is_taken_while_directory_lock_is_held(tmp_path, monkeypatch):
    import fcntl
    original = runner.snapshot
    calls = []
    def checked_snapshot(root):
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError): fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(fd)
        calls.append(1)
        return original(root)
    base = snapshot(tmp_path)['digest']
    monkeypatch.setattr(runner, 'snapshot', checked_snapshot)
    apply(tmp_path, [edit('app/a.py', 'one')], base)
    assert len(calls) == 2


def test_prewrite_failure_retry_rejects_changed_workspace(tmp_path):
    base = snapshot(tmp_path)['digest']
    with pytest.raises(ExecutionError, match='edit_scope_denied'):
        apply(tmp_path, [edit('other/a.py', 'one')], base)
    assert not (tmp_path/runner.JOURNAL).exists()
    (tmp_path/'unrelated.txt').write_text('concurrent')
    with pytest.raises(ExecutionError, match='stale_snapshot'):
        apply(tmp_path, [edit('app/a.py', 'one')], base)
    assert not (tmp_path/'app').exists()


def test_stale_preimage_validates_whole_batch_before_writes(tmp_path):
    (tmp_path/'app').mkdir()
    (tmp_path/'app/b.py').write_text('current')
    base = snapshot(tmp_path)['digest']
    with pytest.raises(ExecutionError, match='stale_preimage'):
        apply(tmp_path, [edit('app/a.py', 'first'), edit('app/b.py', 'second', 'stale')], base)
    assert not (tmp_path/'app/a.py').exists()
    assert (tmp_path/'app/b.py').read_text() == 'current'
    assert not (tmp_path/runner.JOURNAL).exists()


def test_half_written_journal_reconciles_only_original_intent(tmp_path, monkeypatch):
    items = [edit('app/a.py', 'one'), edit('app/b.py', 'two')]
    base = snapshot(tmp_path)['digest']
    replace = runner.os.replace
    def crash(source, target, **kwargs):
        if str(target).replace('\\', '/').endswith('b.py'):
            raise OSError('simulated crash after first file')
        return replace(source, target, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(runner.os, 'replace', crash)
        with pytest.raises((OSError, ResourceError)): apply(tmp_path, items, base)
    assert (tmp_path/'app/a.py').read_text() == 'one'
    assert not runner.read_journal(tmp_path)['completed']
    blocker = runner.journal_blocker(tmp_path, 'other')
    assert blocker['operation_key'] == 'publish' and 'original request key' in blocker['recovery']
    with pytest.raises(ExecutionError, match='edit_reconciliation_required') as caught:
        apply(tmp_path, items, base, 'other')
    assert caught.value.detail == blocker
    with pytest.raises(ExecutionError, match='edit_journal_conflict'):
        apply(tmp_path, [edit('app/a.py', 'changed')], base)
    result = apply(tmp_path, items, base)
    assert result['resumed'] and runner.read_journal(tmp_path)['completed']
    assert (tmp_path/'app/b.py').read_text() == 'two'
    assert result['output_digest'] == snapshot(tmp_path)['digest']


def test_journal_reconciliation_refuses_unrelated_changes_and_scope_loss(tmp_path):
    base = snapshot(tmp_path)['digest']
    items = [edit('app/a.py', 'one')]
    apply(tmp_path, items, base)
    with pytest.raises(ExecutionError, match='edit_scope_denied'):
        runner.apply_edits(tmp_path, {'edits': items}, [], operation_key='publish', input_digest=base)
    (tmp_path/'unrelated.txt').write_text('external change')
    with pytest.raises(ExecutionError, match='edit_reconciliation_conflict'):
        apply(tmp_path, items, base)


@pytest.mark.parametrize('content', ['broken json', '{}', '{"completed":false}'])
def test_invalid_journal_is_not_deleted(tmp_path, content):
    (tmp_path/runner.JOURNAL).write_text(content)
    with pytest.raises(ExecutionError, match='edit_journal_invalid'): runner.read_journal(tmp_path)
    assert (tmp_path/runner.JOURNAL).read_text() == content


@pytest.mark.skipif(os.name == 'nt', reason='directory flock is the installed Linux runtime contract')
def test_two_publishers_same_base_exactly_one_wins_without_partial_writes(tmp_path):
    base = snapshot(tmp_path)['digest']
    def publish_one(key):
        try:
            return apply(tmp_path, [edit('app/a.py', key), edit('app/b.py', key)], base, key)
        except ExecutionError as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish_one, ['first', 'second']))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert 'stale_snapshot' in results
    assert (tmp_path/'app/a.py').read_text() == (tmp_path/'app/b.py').read_text()


@pytest.mark.parametrize('code', sorted(TERMINAL_CONFLICTS))
def test_deterministic_refusals_have_terminal_conflict_receipt(code):
    state, receipt = failure_receipt(ExecutionError(code, 409, {'cause': 'known'}), 'op')
    assert state == 'conflict'
    assert receipt == {'code': code, 'operation_key': 'op', 'detail': {'cause': 'known'}}


@pytest.mark.parametrize('error', [TimeoutError(), ConnectionError(), asyncio.CancelledError(), ExecutionError('publication_not_established', 409)])
def test_ambiguous_outcomes_remain_unknown(error):
    assert failure_receipt(error, 'op')[0] == 'unknown'


def attempt(auth, *, outcome='succeeded', result=None, code=None):
    stdout = canonical({'adapter': 'checked-edits/1', 'result': result or {'output_digest': 'sha'}})
    stderr = canonical({'adapter': 'checked-edits/1', 'error': code}) if code else ''
    return {'owner': auth.owner, 'principal': auth.actor, 'caller_group': 'publish:job',
            'boundary': auth.boundary, 'outcome': outcome, 'cleanup': 'idle_proved',
            'receipt': {'returncode': 0 if outcome == 'succeeded' else 1,
                        'stdout_b64': base64.b64encode(stdout.encode()).decode(),
                        'stderr_b64': base64.b64encode(stderr.encode()).decode()}}


@pytest.mark.parametrize('code', sorted(TERMINAL_CONFLICTS))
def test_worker_deterministic_error_survives_terminal_receipt(code):
    auth = Authority('lane', 'owner', 'owner', {}, 'human')
    with pytest.raises(ExecutionError, match=code):
        worker_output(attempt(auth, outcome='failed', code=code), auth, 'job')


def test_worker_success_needs_bound_durable_output_digest():
    auth = Authority('lane', 'owner', 'owner', {}, 'human')
    assert worker_output(attempt(auth), auth, 'job') == 'sha'
    for mutation in [{'owner': 'other'}, {'caller_group': 'publish:other'}, {'cleanup': 'unknown'}, {'receipt': {}}, {'outcome': 'timed_out'}]:
        with pytest.raises(ExecutionError, match='publication_not_established'):
            worker_output({**attempt(auth), **mutation}, auth, 'job')


@pytest.mark.asyncio
async def test_conflict_replay_returns_stored_receipt_without_dispatch(monkeypatch):
    from core.execution import owner
    from core.tool_jobs import service
    from core.worker import client
    auth = Authority('lane', 'owner', 'owner', {}, 'human')
    receipt = {'code': 'stale_snapshot', 'operation_key': 'original-op', 'detail': {'cause': 'changed'}}
    connection = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(return_value={
        'request_key': 'key', 'proposal_digest': 'sha', 'state': 'conflict', 'receipt': canonical(receipt)}))
    @asynccontextmanager
    async def transaction(): yield connection
    admission = SimpleNamespace(store=SimpleNamespace(transaction=transaction))
    monkeypatch.setattr(owner, 'require_owner', AsyncMock())
    monkeypatch.setattr(service.Jobs, 'row', AsyncMock(return_value={'id': 'job', 'attachment_id': 'folder'}))
    monkeypatch.setattr(service.Jobs, 'attachment', AsyncMock(return_value={'path': 'projects/p'}))
    dispatch = AsyncMock()
    monkeypatch.setattr(client, 'execute_async', dispatch)
    with pytest.raises(ExecutionError) as caught:
        await publish(admission, auth, {'job_id': 'job', 'proposal_digest': 'sha', 'expected_revision': 1, 'request_key': 'key'})
    assert caught.value.code == 'stale_snapshot' and caught.value.status == 409
    assert caught.value.detail == receipt
    dispatch.assert_not_called()


RETRYABLE_CODES = [
    'work_not_completed', 'revision_conflict', 'proposal_digest_conflict',
    'snapshot_timeout', 'snapshot_unavailable', 'snapshot_changed',
    'edit_reconciliation_required', 'edit_journal_invalid',
    'owner_session_invalid', 'credential_epoch_changed', 'owner_state_unavailable',
    'worker_unavailable', 'worker_paused', 'scope_denied', 'new_execution_error',
    'snapshot_link_or_mount', 'snapshot_mount_drift', 'snapshot_shared_file',
    'snapshot_special_or_shared_file', 'snapshot_manifest_too_large', 'snapshot_too_large',
    'invalid_edits', 'invalid_edit_path', 'duplicate_edit_path',
    'expected_preimage_required', 'edits_too_large', 'overlapping_edit_paths',
    'unsupported_fields', 'artifact_digest_conflict',
]


@pytest.mark.parametrize('code', RETRYABLE_CODES)
def test_unlisted_typed_failures_and_worker_codes_remain_unknown(code):
    auth = Authority('lane', 'owner', 'owner', {}, 'human')
    with pytest.raises(ExecutionError) as caught:
        worker_output(attempt(auth, outcome='failed', code=code), auth, 'job')
    assert caught.value.code == code
    assert failure_receipt(caught.value, 'op')[0] == 'unknown'


@pytest.fixture
def publication_harness(monkeypatch):
    """Exercise real preflight/dispatch authorization with an in-memory SQL stub.

    Postgres atomicity and actual filesystem edits are covered in the PG suite.
    """
    from core.execution import owner, publication, workspaces
    from core.execution.job_store import JobStore
    from core.tool_jobs import service
    from core.worker import client
    h = SimpleNamespace(record=None, run='completed', launches=0, worker_error=None,
                        dispatch_error=None, frozen_error=None, owner_valid=True,
                        expire_at_dispatch=False, credential='first')
    h.auth = Authority('lane', 'owner', 'owner', {}, 'human', credential_id='first')
    h.args = {'job_id': 'job', 'proposal_digest': 'sha', 'expected_revision': 2, 'request_key': 'key'}
    folder = {'path': 'projects/p', 'attachment_id': 'folder'}
    row = {'id': 'job', 'attachment_id': 'folder', 'registration': folder,
           'revision': 2, 'run_id': 'run', 'role': 'coder'}
    old, new = hashlib.sha256(b'old').hexdigest(), hashlib.sha256(b'new').hexdigest()
    base = {'digest': 'base', 'manifest': [{'path': 'app/a.py', 'kind': 'file', 'sha256': old}]}
    output = {'digest': 'output', 'manifest': [{'path': 'app/a.py', 'kind': 'file', 'sha256': new}],
              'blobs': {new: b'new'}}
    proposal = {'digest': 'sha', 'checks_passed': True, 'required_checks': ['python.compile'],
                'output_digest': 'output', 'files': [{'operation': 'replace', 'path': 'app/a.py',
                                                     'pre_sha256': old, 'post_sha256': new}],
                'check_runs': [{'check_id': 'python.compile', 'passed': True,
                                'source_unchanged': True, 'snapshot_digest': 'output'}]}
    async def fetchrow(sql, *args):
        if 'FROM work_publications' in sql: return dict(h.record) if h.record else None
        if 'FROM job_proposals' in sql: return {'digest': 'sha', 'payload': canonical(proposal)}
        raise AssertionError(sql)
    async def fetchval(sql, *args):
        if 'FROM agent_runs' in sql: return h.run
        if 'FROM job_decisions' in sql: return None
        raise AssertionError(sql)
    async def execute(sql, *args):
        if sql.startswith('INSERT INTO work_publications'):
            assert h.record is None
            h.record = dict(request_key=args[3], proposal_digest=args[4], operation_key=args[5],
                            state='intent', authority=args[6], receipt=None)
        elif sql.startswith('UPDATE work_publications SET authority'):
            h.record['authority'] = args[3]
        elif sql.startswith('UPDATE work_publications SET state'):
            if h.record['state'] != 'completed':
                if "state='completed'" in sql: h.record.update(state='completed', receipt=args[3])
                else: h.record.update(state=args[3], receipt=args[4])
        else: assert 'pg_advisory_xact_lock' in sql
    connection = SimpleNamespace(execute=execute, fetchrow=fetchrow, fetchval=fetchval)
    @asynccontextmanager
    async def transaction(): yield connection
    h.event = AsyncMock()
    h.admission = SimpleNamespace(store=SimpleNamespace(transaction=transaction, pool=None, _event=h.event))
    async def require_owner(auth, operation):
        if not h.owner_valid or auth.credential_id != h.credential:
            raise ExecutionError('owner_session_invalid', 403)
    async def frozen(self, c, row, kind):
        if h.frozen_error: raise h.frozen_error
        return base if kind == 'base' else output
    async def dispatch(*args, **kwargs):
        h.launches += 1
        assert h.record['state'] in {'intent', 'unknown'}
        if h.expire_at_dispatch: h.owner_valid = False
        await authorize_worker(connection, 'lane', context=SimpleNamespace(
            owner=h.auth.owner, principal=h.auth.actor, group='publish:job', boundary=h.auth.boundary))
        if h.dispatch_error: raise h.dispatch_error
        return {'execution_id': 'attempt'}
    async def get_attempt(*args):
        return attempt(h.auth, outcome='failed' if h.worker_error else 'succeeded',
                       code=h.worker_error, result={'output_digest': 'output'})
    monkeypatch.setattr(owner, 'require_owner', require_owner)
    monkeypatch.setattr(service.Jobs, 'row', AsyncMock(return_value=row))
    monkeypatch.setattr(service.Jobs, 'attachment', AsyncMock(return_value=folder))
    monkeypatch.setattr(service.Jobs, 'frozen', frozen)
    monkeypatch.setattr(JobStore, 'get', get_attempt)
    monkeypatch.setattr(client, 'execute_async', dispatch)
    h.snapshot = lambda *args, **kwargs: base
    monkeypatch.setattr(workspaces, 'snapshot', lambda *args, **kwargs: h.snapshot(*args, **kwargs))
    h.blocker = AsyncMock(return_value=None)
    monkeypatch.setattr(publication, 'blocking_publication', h.blocker)
    return h


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['incomplete', 'digest_same_key', 'digest_new_key', 'revision'])
async def test_new_caller_refusal_does_not_reserve_publication(publication_harness, bad):
    h = publication_harness
    args = dict(h.args)
    code = 'proposal_digest_conflict'
    if bad == 'incomplete': h.run, code = 'running', 'work_not_completed'
    elif bad == 'revision': args['expected_revision'], code = 1, 'revision_conflict'
    else: args['proposal_digest'] = 'wrong'
    with pytest.raises(ExecutionError, match=code) as caught:
        await publish(h.admission, h.auth, args)
    assert caught.value.status == 409
    assert h.record is None and h.launches == 0
    h.run = 'completed'
    if bad == 'digest_new_key': h.args['request_key'] = 'corrected'
    assert (await publish(h.admission, h.auth, h.args))['state'] == 'published'
    assert h.record['state'] == 'completed' and h.launches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['snapshot_timeout', 'worker_paused', 'unrecognized_worker_failure'])
async def test_worker_transient_failure_unknown_then_retry_succeeds(publication_harness, code):
    h = publication_harness
    h.worker_error = code
    with pytest.raises(ExecutionError, match=code): await publish(h.admission, h.auth, h.args)
    assert h.record['state'] == 'unknown'
    operation_key = h.record['operation_key']
    h.worker_error = None
    assert (await publish(h.admission, h.auth, h.args))['state'] == 'published'
    assert h.record['operation_key'] == operation_key and h.launches == 2


@pytest.mark.asyncio
async def test_expired_dispatch_owner_unknown_then_renewed_owner_succeeds(publication_harness):
    h = publication_harness
    h.expire_at_dispatch = True
    with pytest.raises(ExecutionError, match='owner_session_invalid'):
        await publish(h.admission, h.auth, h.args)
    assert h.record['state'] == 'unknown'
    h.expire_at_dispatch, h.owner_valid, h.credential = False, True, 'renewed'
    h.auth = replace(h.auth, credential_id='renewed')
    assert (await publish(h.admission, h.auth, h.args))['state'] == 'published'
    assert 'renewed' in h.record['authority'] and h.launches == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ExecutionError('snapshot_unavailable', 409), TimeoutError('snapshot')])
async def test_existing_intent_preflight_failure_stays_unknown(publication_harness, error):
    h = publication_harness
    h.dispatch_error = ConnectionError('lost before write')
    with pytest.raises(ConnectionError): await publish(h.admission, h.auth, h.args)
    h.dispatch_error, h.frozen_error = None, error
    with pytest.raises(type(error)): await publish(h.admission, h.auth, h.args)
    assert h.record['state'] == 'unknown' and h.launches == 1
    h.frozen_error = None
    assert (await publish(h.admission, h.auth, h.args))['state'] == 'published'


@pytest.mark.asyncio
async def test_other_job_blocker_unknown_then_reconciliation_allows_retry(publication_harness):
    h = publication_harness
    h.dispatch_error = ConnectionError('lost before write')
    with pytest.raises(ConnectionError): await publish(h.admission, h.auth, h.args)
    h.dispatch_error = None
    blocker = {'operation_key': 'other-op', 'job_id': 'other-job', 'request_key': 'other-key',
               'recovery': 'Retry that job with its original request key'}
    h.blocker.return_value = blocker
    with pytest.raises(ExecutionError, match='edit_reconciliation_required') as caught:
        await publish(h.admission, h.auth, h.args)
    assert h.record['state'] == 'unknown' and h.launches == 1
    assert caught.value.detail['detail'] == blocker
    h.blocker.return_value = None
    assert (await publish(h.admission, h.auth, h.args))['state'] == 'published'


@pytest.mark.asyncio
async def test_worker_reported_blocker_keeps_detail_and_is_retryable(publication_harness):
    h = publication_harness
    blocker = {'operation_key': 'other-op', 'job_id': 'other-job', 'request_key': 'other-key'}
    h.worker_error = 'edit_reconciliation_required'
    # Preflight is clear; the other job journals between preflight and dispatch.
    h.blocker.side_effect = [None, blocker, None]
    with pytest.raises(ExecutionError, match='edit_reconciliation_required') as caught:
        await publish(h.admission, h.auth, h.args)
    assert h.record['state'] == 'unknown'
    assert caught.value.detail['detail'] == blocker
    h.worker_error = None
    assert (await publish(h.admission, h.auth, h.args))['state'] == 'published'


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['snapshot_timeout', 'snapshot_unavailable', 'edit_reconciliation_required'])
async def test_new_transient_preflight_leaves_no_row_then_retry_succeeds(publication_harness, code):
    h = publication_harness
    h.frozen_error = ExecutionError(code, 409)
    with pytest.raises(ExecutionError, match=code): await publish(h.admission, h.auth, h.args)
    assert h.record is None and h.launches == 0
    h.frozen_error = None
    assert (await publish(h.admission, h.auth, h.args))['state'] == 'published'


@pytest.mark.asyncio
async def test_true_stale_source_conflict_never_dispatches_on_replay(publication_harness):
    h = publication_harness
    h.snapshot = lambda *args, **kwargs: {'digest': 'changed'}
    for _ in range(2):
        with pytest.raises(ExecutionError, match='proposal_stale'):
            await publish(h.admission, h.auth, h.args)
        assert h.record['state'] == 'conflict'
    assert h.launches == 0
