"""Dispatch fidelity regressions. Provider calls replaced at transport seam."""
from unittest.mock import AsyncMock
import pytest
from core.nodes import decompose, dispatch, worker, execute

@pytest.mark.asyncio
async def test_decomposition_preserves_exact_http_model(monkeypatch):
    send = AsyncMock(return_value='1. inspect source\n2. report evidence')
    monkeypatch.setattr('core.nodes.execute._send_to_backend', send)
    await decompose.decompose_node({'task': 'Review source ' + 'x' * 160,
        'backend': 'glm_5_3', 'model_override': 'glm-5.3-flash'})
    assert send.await_args.args[1:] == ('glm_5_3', 'glm-5.3-flash')

@pytest.mark.asyncio
async def test_fanout_preserves_original_evidence_and_model(monkeypatch):
    calls = []
    async def send(messages, backend, model=None):
        calls.append((messages, backend, model))
        return 'source-backed result'
    monkeypatch.setattr(worker, '_send_to_backend', send)
    monkeypatch.setattr(worker, '_emit_worker_state', AsyncMock())
    monkeypatch.setattr(dispatch, 'emit_event_sync', lambda *a, **kw: None)
    original = 'Read-only review. SOURCE_MARKER_Z8: exact evidence 94 mm; no edits.'
    scope = {'may_touch': ['authorized/**'], 'auto_actions': []}
    sends = dispatch._route_after_dispatch({
        'task': original, 'face_id': 'researcher', 'backend': 'glm_5_3',
        'model_override': 'glm-5.3-flash', 'scope': scope,
        'fanout_subtasks': [{'idx': 0, 'text': 'inspect evidence'},
                            {'idx': 1, 'text': 'report findings'}]})
    for item in sends:
        assert item.arg['scope'] == scope
        result = await worker.worker_node(item.arg)
        assert result['worker_results'][0]['status'] == 'ok'
    assert len(calls) == 2
    for messages, backend, model in calls:
        assert original in '\n'.join(m['content'] for m in messages)
        assert (backend, model) == ('glm_5_3', 'glm-5.3-flash')

@pytest.mark.asyncio
@pytest.mark.parametrize('response', ['', '  \n'])
async def test_worker_empty_response_is_failure(monkeypatch, response):
    monkeypatch.setattr(worker, '_send_to_backend', AsyncMock(return_value=response))
    monkeypatch.setattr(worker, '_emit_worker_state', AsyncMock())
    result = await worker.worker_node({'task': 'inspect', 'backend': 'glm_5_3'})
    entry = result['worker_results'][0]
    assert entry['status'] == 'failed'
    assert 'empty_response' in entry['error']

@pytest.mark.asyncio
async def test_worker_without_pin_preserves_two_arg_transport(monkeypatch):
    send = AsyncMock(return_value='ok')
    monkeypatch.setattr(worker, '_send_to_backend', send)
    monkeypatch.setattr(worker, '_emit_worker_state', AsyncMock())
    await worker.worker_node({'task': 'inspect', 'backend': 'deepseek_v4_flash'})
    assert len(send.await_args.args) == 2

@pytest.mark.asyncio
async def test_invocation_pin_wins_over_cli_posture(monkeypatch):
    send = AsyncMock(return_value='ok')
    monkeypatch.setattr(worker, '_send_to_backend', send)
    monkeypatch.setattr(worker, '_emit_worker_state', AsyncMock())
    await worker.worker_node({'task': 'inspect', 'backend': 'pi_code',
        'model_override': 'zai/glm-5.3-flash', 'pi_posture': {'model': 'stale-model'}})
    assert send.await_args.args[2] == 'zai/glm-5.3-flash'

@pytest.mark.asyncio
async def test_empty_stream_is_explicit_failure(monkeypatch):
    async def empty(*args):
        if False:
            yield ''
    monkeypatch.setattr(execute, '_stream_to_backend', empty)
    with pytest.raises(ValueError, match='empty_response'):
        await execute._stream_and_collect([{'role': 'user', 'content': 'probe'}],
                                          'glm_5_3', 'glm-5.3-flash', None)



