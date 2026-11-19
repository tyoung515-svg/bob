"""S9 regressions: supplied receipts and fake models only, no provider calls."""
import json
import logging
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from core.builder.service import Builder, FIELDS
from core.builder.store import BuilderStore
from core.builder.summary import summary
from core.execution.models import Authority, ExecutionError
from core.nodes import execute
from tests.test_tool_loop import _FakeModel, _tool_call_response


@pytest.fixture
def builder_loop(monkeypatch):
    # Runtime Builder is a lane-installed face; the source roster fixture has
    # only the older builder-bob face. Supply just this test's permitted tool.
    registry = SimpleNamespace(get_allowed_tools=lambda face: ['fixture_receipt'],
                               list_skills=lambda face: [],
                               get_face=lambda face: SimpleNamespace(tool_loop_max_rounds=3))
    monkeypatch.setattr('core.faces.registry.get_default_registry', lambda: registry)
    monkeypatch.setattr('core.faces.prompt.interactive_messages', lambda messages, *a: messages)
    monkeypatch.setattr(execute, 'check_tool_access', lambda *a: True)


@pytest.mark.parametrize('receipt', [None, [], 'text', 0, True, {'error': None},
                                     {'error': 'workspace_missing'}, {'error': []},
                                     {'end_turn': True, 'detail': None}])
def test_summary_accepts_null_and_non_object_receipts(receipt):
    assert summary([('fixture', json.dumps(receipt))]).strip()


# Workspace setup and task_finish do not require a workspace. Task finish only
# revokes authority; its no-workspace contract is checked separately below.
@pytest.mark.parametrize('operation', [op for op in FIELDS if op not in {'workspace_create', 'workspace_mount', 'task_finish'}])
@pytest.mark.parametrize('expired_yolo', [False, True])
async def test_every_tool_returns_workspace_missing_before_dispatch(monkeypatch, operation, expired_yolo):
    builder = Builder(SimpleNamespace())
    row = {'project_id': uuid.uuid4(), 'mode': 'yolo' if expired_yolo else 'approve', 'expires_at': 1}
    builder.store.mode = AsyncMock(return_value=row)
    builder.worker = AsyncMock(side_effect=AssertionError('must not dispatch'))
    monkeypatch.setattr(builder, 'workspace', lambda *a: None)
    result = await builder.tool(Authority('lane', 'owner', 'owner', {}, 'portal'),
                                str(uuid.uuid4()), operation, {})
    assert result['error']['code'] == 'workspace_missing'
    assert 'owner' in result['detail']
    assert result['action']['resource']['project_id'] == str(row['project_id'])
    assert summary([(operation, json.dumps(result))]).strip()
    builder.worker.assert_not_awaited()


async def test_task_finish_without_workspace_revokes_authority(monkeypatch):
    builder = Builder(SimpleNamespace())
    conversation = uuid.uuid4()
    builder.store.mode = AsyncMock(return_value={'conversation_id': conversation})
    builder.worker = AsyncMock(side_effect=AssertionError('must not dispatch'))
    monkeypatch.setattr(builder, 'workspace', lambda *a: pytest.fail('finish must not resolve a workspace'))
    end_sessions = AsyncMock()
    monkeypatch.setattr('core.builder.lifecycle.end_sessions', end_sessions)
    auth = Authority('lane', 'owner', 'owner', {}, 'portal')
    result = await builder.tool(auth, str(conversation), 'task_finish', {})
    assert result['status'] == 'completed' and result['end_turn'] is True
    end_sessions.assert_awaited_once_with(builder.pool, auth.lane, auth.owner,
                                         [conversation], reason='task_completed')
    builder.worker.assert_not_awaited()


async def test_missing_conversation_project_cannot_reuse_mode_or_workspace():
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None), execute=AsyncMock())
    store = BuilderStore(pool)
    with pytest.raises(ExecutionError, match='conversation_project_required'):
        await store.mode(Authority('lane', 'owner', 'owner', {}, 'portal'), str(uuid.uuid4()))
    # An old mode/workspace cannot confer authority once the binding disappears.
    pool.execute.assert_not_awaited()


@pytest.mark.parametrize('missing_on_refresh', [False, True])
async def test_mode_disappearing_returns_domain_error(missing_on_refresh):
    row = {'project_id': uuid.uuid4(), 'mode': 'yolo', 'session_until': 1,
           'session_id': 'old-session', 'authority_scope': None, 'credential_epoch': None}
    pool = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(
        side_effect=[row, None] if missing_on_refresh else [None]))
    store = BuilderStore(pool)
    store.binding = AsyncMock(return_value=(uuid.uuid4(), row['project_id']))
    with pytest.raises(ExecutionError, match='builder_mode_unavailable'):
        await store.mode(Authority('lane', 'owner', 'owner', {}, 'portal'), str(uuid.uuid4()))


async def test_expired_yolo_rotates_before_returning_mode():
    row = {'project_id': uuid.uuid4(), 'mode': 'yolo', 'session_until': 1, 'expires_at': 1,
           'session_id': 'old-session', 'authority_scope': None, 'credential_epoch': None}
    refreshed = {**row, 'mode': 'auto', 'session_until': time.time() + 7200, 'expires_at': None}
    pool = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(side_effect=[row, refreshed]))
    store = BuilderStore(pool)
    store.binding = AsyncMock(return_value=(uuid.uuid4(), row['project_id']))
    assert (await store.mode(Authority('lane', 'owner', 'owner', {}, 'portal'), str(uuid.uuid4())))['mode'] == 'auto'
    assert any("mode=CASE WHEN mode='yolo' THEN 'auto'" in call.args[0] for call in pool.execute.call_args_list)


async def test_missing_worker_receipt_is_a_domain_error(monkeypatch):
    monkeypatch.setattr('core.worker.client.execute_async', AsyncMock(return_value=None))
    with pytest.raises(ExecutionError, match='builder_cleanup_unproved'):
        await Builder(None).worker(Authority('lane', 'owner', 'owner', {}, 'portal'), '.', 'fs_search', {})


@pytest.mark.parametrize('payload', [None, [], {'adapter': 'builder-command/1', 'result': None}])
async def test_invalid_worker_payload_is_structured(monkeypatch, payload):
    receipt = {'cleanup': 'idle_proved', 'stdout': json.dumps(payload), 'returncode': 0}
    monkeypatch.setattr('core.worker.client.execute_async', AsyncMock(return_value=receipt))
    result = await Builder(None).worker(Authority('lane', 'owner', 'owner', {}, 'portal'), '.', 'fs_search', {})
    assert result['error']['code'] == 'invalid_builder_receipt'


@pytest.mark.parametrize('receipt', [{'error': None}, {'status': 'needs_workspace', 'error': {'code': 'workspace_missing'}}])
async def test_model_tool_call_then_empty_final_is_visible(monkeypatch, receipt, builder_loop):
    @tool
    async def fixture_receipt() -> str:
        """Return a supplied tool receipt."""
        return json.dumps(receipt)
    fake = _FakeModel([_tool_call_response('fixture_receipt', 'one'), AIMessage(content='')])
    monkeypatch.setattr(execute, '_build_tool_model', lambda _: fake)
    monkeypatch.setattr(execute, 'get_all_tools', AsyncMock(return_value=[fixture_receipt]))
    events = []
    reply = await execute._run_tool_loop([], 'deepseek_v4_flash', 'builder', events.append)
    assert reply.startswith('The Builder finished without writing a report.')
    assert [e['content'] for e in events if e['type'] == 'token'] == [reply]


@pytest.mark.parametrize('operation,args', [
    ('fs_write', {'path': 'index.html', 'content': '<h1>Hello</h1>'}),
    ('shell_run', {'command': 'ls'}), ('preview_file', {'path': 'index.html'}),
    ('checkpoint_list', {}), ('shell_run', {'command': 'curl https://pypi.org', 'network': True}),
])
@pytest.mark.parametrize('binding', ['no_workspace', 'no_project', 'expired_yolo'])
async def test_model_tool_call_with_missing_binding_is_structured(monkeypatch, operation, args, binding, builder_loop):
    from pathlib import Path
    from core.builder import service
    from core.execution import chat_context, chat_records, owner
    project, conversation = uuid.uuid4(), uuid.uuid4()
    auth = Authority('lane', 'owner', 'owner', {}, 'portal')
    row = {'project_id': project, 'conversation_id': conversation, 'mode': 'yolo',
           'session_id': 'old-session', 'authority_scope': None, 'credential_epoch': None,
           'session_until': 1 if binding == 'expired_yolo' else time.time() + 3600, 'expires_at': 1}
    async def fetchrow(sql, *args):
        if 'SELECT c.project_id' in sql:
            return None if binding == 'no_project' else {'project_id': project}
        return row
    async def update(sql, *args):
        if "UPDATE builder_modes SET mode='auto'" in sql or 'mode=CASE' in sql:
            row.update(mode='auto', expires_at=None, session_until=time.time() + 7200)
    pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=fetchrow), execute=AsyncMock(side_effect=update))
    monkeypatch.setattr(chat_records, 'POOL', pool)
    monkeypatch.setattr(owner, 'check_epoch', AsyncMock())
    monkeypatch.setattr('core.tool_jobs.service.lane_root', lambda: Path('.'))
    # The registered workspace cannot be used if the conversation lacks a project.
    monkeypatch.setattr(service, 'load_folders', lambda root: [
        {'owner': 'owner', 'project_id': str(project), 'path': 'projects/' + str(project)}
    ] if binding == 'no_project' else [])
    worker = AsyncMock(side_effect=AssertionError('must not dispatch'))
    monkeypatch.setattr(Builder, 'worker', worker)
    @tool
    async def fixture_receipt() -> str:
        """Exercise the real scoped Builder entrypoint."""
        return await service.invoke(operation, args)
    fake = _FakeModel([_tool_call_response('fixture_receipt', 'one'), AIMessage(content='')])
    monkeypatch.setattr(execute, '_build_tool_model', lambda _: fake)
    monkeypatch.setattr(execute, 'get_all_tools', AsyncMock(return_value=[fixture_receipt]))
    token = chat_context.current.set({'face_id': 'builder', 'exp': time.time() + 60,
                                     'conversation_id': str(conversation), 'authority': auth.as_dict()})
    try:
        reply = await execute._run_tool_loop([], 'deepseek_v4_flash', 'builder', None)
    finally:
        chat_context.current.reset(token)
    assert ('conversation_project_required' if binding == 'no_project' else 'workspace_missing') in reply
    worker.assert_not_awaited()


@pytest.mark.parametrize('path', ['tool', 'generic', 'single'])
async def test_failed_turn_has_safe_traceback_text_and_ids(monkeypatch, caplog, path):
    monkeypatch.setattr(execute, '_is_tool_enabled', lambda *a: path == 'tool')
    monkeypatch.setattr(execute, '_run_tool_loop', AsyncMock(side_effect=AttributeError('private-message')))
    monkeypatch.setattr(execute, '_stream_and_collect', AsyncMock(side_effect=AttributeError('private-message')))
    save = AsyncMock()
    monkeypatch.setattr(execute, '_append_agent_turn_event', save)
    state = {'task': 'private-message', 'messages': [], 'conversation_id': 'conv-9',
             'face_id': 'builder', 'backend': 'deepseek_v4_flash',
             'agent_service_single_turn': path == 'single'}
    with caplog.at_level(logging.ERROR):
        result = await execute.execute_node(state)
    assert result['error_type'] == 'AttributeError'
    assert result['turn_id'] in result['messages'][0]['content']
    assert save.call_args.kwargs['assistant_response'] == result['messages'][0]['content']
    assert save.call_args.args[0]['turn_id'] == result['turn_id']
    assert 'Traceback (most recent call last)' in caplog.text
    assert 'conv-9' in caplog.text and 'face=builder backend=deepseek_v4_flash' in caplog.text
    assert 'private-message' not in caplog.text


@pytest.mark.parametrize('partial', [False, True])
async def test_api_streams_unstreamed_failure_and_logs_ids(caplog, partial):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from api.server import _stream_graph_turn

    class Graph:
        async def astream(self, *a, **kw):
            if partial:
                yield 'custom', {'type': 'token', 'content': 'partial'}
            yield 'updates', {'execute': {'messages': [{'role': 'assistant', 'content': 'Builder failed turn turn-9'}],
                                          'error': 'AttributeError', 'error_type': 'AttributeError', 'turn_id': 'turn-9'}}

    async def handler(request):
        return await _stream_graph_turn(request=request, graph=Graph(), graph_input={}, thread_id='conv-9', approvals={},
                                   face_id='builder', user_content='private-message', model_override=None,
                                   backend_override='fixture')
    app = web.Application()
    app.router.add_get('/', handler)
    with caplog.at_level(logging.WARNING):
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/')
            body = await response.text()
    events = [json.loads(line[5:]) for line in body.splitlines() if line.startswith('data:')]
    assert ''.join(e['content'] for e in events if e['type'] == 'chunk') == ('partial' if partial else '') + 'Builder failed turn turn-9'
    error = next(e for e in events if e['type'] == 'error')
    assert error['error_type'] == 'AttributeError' and error['turn_id'] == 'turn-9'
    assert 'conversation_id=conv-9 turn_id=turn-9' in caplog.text
    assert 'private-message' not in caplog.text
