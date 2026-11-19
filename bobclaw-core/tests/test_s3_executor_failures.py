"""Offline regressions for execution diagnostics and malformed repair edits."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.execution.checked import edit_scope, edits
from core.execution.executor import Executor
from core.execution.models import ExecutionError, LOOP_TOPOLOGY, TOPOLOGY


@pytest.mark.parametrize('new_tests', [False, True])
def test_create_without_preimage_is_rejected_before_scope_or_worker(new_tests):
    # A repair job may add a regression test alongside existing app files.
    # Previously edits() accepted this, then Builder scope checking raised KeyError.
    value = {'edits': [{'operation': 'create', 'path': 'app/test_repair.py', 'content': 'pass\n'}]}
    with pytest.raises(ExecutionError, match='invalid_edits'):
        edit_scope(value, ['app'], new_tests)
    explicit = {'edits': [{**value['edits'][0], 'pre_sha256': None}]}
    assert edits(explicit) == explicit
    if new_tests:
        assert edit_scope(explicit, ['app'], new_tests) == explicit
    else:
        with pytest.raises(ExecutionError, match='edit_scope_denied'):
            edit_scope(explicit, ['app'], new_tests)


@pytest.mark.parametrize('loop', [False, True])
async def test_executor_logs_traceback_before_stopping(loop, monkeypatch, caplog):
    from core.execution.failure import FailureRecovery

    step = {'ordinal': 4, 'role': 'worker', 'state': 'ready', 'spec': {'phase': 'task'}}
    store = SimpleNamespace(
        steps=AsyncMock(return_value=[step]),
        loop_next=AsyncMock(return_value={'action': 'steps', 'steps': [step]}),
        loop_view=AsyncMock(return_value={'row': {'deadline': None}, 'outputs': [], 'recent_receipts': []}),
        loop_dependencies=AsyncMock(return_value=[]),
        stop=AsyncMock(),
        checkpoint=AsyncMock(side_effect=KeyError('MODEL_SECRET')),
        checkpoint_loop=AsyncMock(side_effect=KeyError('MODEL_SECRET')),
    )
    monkeypatch.setattr('core.tool_jobs.service.evidence', AsyncMock(return_value=''))
    monkeypatch.setattr('core.execution.executor.prompt_for', lambda *a: 'test')
    monkeypatch.setattr(FailureRecovery, 'prepare', AsyncMock(return_value=('attempt-1', None)))
    monkeypatch.setattr(FailureRecovery, 'call', AsyncMock(return_value=('attempt-1', 'answer', {}, None, {})))
    row = {'id': 'repair-run', 'deadline': None,
           'request': {'task': 'repair app files', 'acceptance': {}},
           'snapshot': {'team': {'topology': LOOP_TOPOLOGY if loop else TOPOLOGY,
                                 'roles': {'worker': 'model'}}, 'models': {'model': {}}}}
    executor = Executor(store, SimpleNamespace(revalidate=AsyncMock()), AsyncMock())
    with caplog.at_level(logging.ERROR, logger='core.execution.executor'):
        await executor.execute(row)
    store.stop.assert_awaited_once()
    assert store.stop.call_args.args[1] == 'KeyError'
    assert 'run=repair-run step=4 attempt=attempt-1' in caplog.text
    assert 'Traceback (most recent call last)' in caplog.text
    assert 'KeyError' in caplog.text
    assert 'MODEL_SECRET' not in caplog.text


async def test_loop_scheduler_failure_is_logged_without_an_attempt(monkeypatch, caplog):
    store = SimpleNamespace(loop_next=AsyncMock(side_effect=KeyError('private-key')), stop=AsyncMock())
    executor = Executor(store, SimpleNamespace(revalidate=AsyncMock()), AsyncMock())
    with caplog.at_level(logging.ERROR, logger='core.execution.executor'):
        await executor.execute_loop({'id': 'scheduler-run', 'request': {}})
    assert 'run=scheduler-run step=None attempt=None' in caplog.text
    assert 'private-key' not in caplog.text
    store.stop.assert_awaited_once()
