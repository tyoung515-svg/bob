"""BU16 chat choices, exact pins and isolation; fake provider/CLI evidence only."""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.messages import HumanMessage
from core import lane_config as lc
from core.model_choices import with_choices, validate_pin
from core.backends.openai_codex import OpenAICodexClient, ChatOpenAICodex
from core.worker.claude import MODELS
from core.worker.claude_turn_contract import invocation, validate_start
from test_openai_codex_auth import auth_lane, NOW
from test_openai_codex_transport import sse, text_item
from test_cc1_catalog import lane, configure
from test_cc2_claude_turn import FakeLink


@pytest.mark.parametrize('backend,kind,selected,models,billing', [
    ('openai_codex', 'cloud', 'gpt-6.2-sol', ('gpt-6.2-sol', 'gpt-6.1-sol'), 'subscription'),
    ('claude_code', 'cli', 'opus', MODELS, 'subscription'),
    ('deepseek_v4_flash', 'cloud', 'deepseek-v4-flash', ('deepseek-v4-flash',), 'api_key'),
    ('codex_code', 'cli', 'gpt-6.2-sol', ('gpt-6.2-sol',), 'subscription'),
])
def test_provider_shapes_preserve_old_client_fields(backend, kind, selected, models, billing):
    row = lc.LaneBackend(backend, kind, selected, models=models).row()
    assert row['model'] == selected and row['models'] == list(models)
    assert [c['model'] for c in row['choices']] == list(models)
    assert [c['model'] for c in row['choices'] if c['default']] == [selected]
    assert row['billing'] == billing and type(row['tool_capable']) is bool
    assert selected not in row['provider_label']
    assert with_choices(row) == row
    assert lc.LaneBackend(backend, kind, models=(), available=False).row()['choices'] == []


@pytest.mark.asyncio
async def test_claude_aliases_labels_and_stage_default(lane, monkeypatch):
    configure(lane, CLAUDE_SIGNED_IN='true', CLAUDE_CODE_MODEL='opus')
    monkeypatch.setattr(lc, 'claude_observations', lambda _: {
        'models': {'opus': 'claude-opus-5-5', 'sonnet': 'claude-sonnet-full'}})
    row, = await lc.catalog(SimpleNamespace(discover=AsyncMock(side_effect=AssertionError('no probe'))))
    assert row['models'] == list(MODELS)
    assert [c['model'] for c in row['choices'] if c['default']] == ['opus']
    labels = {c['model']: c['label'] for c in row['choices']}
    assert labels['opus'] == 'opus · claude-opus-5-5'
    assert labels['sonnet'] == 'sonnet · claude-sonnet-full'
    for alias in MODELS:
        assert lc.get_lane().require('claude_code', alias).model == 'opus'
    assert [s['model'] for s in lc.agent_service_definitions([row])['models'].values()] == ['opus']


@pytest.mark.asyncio
async def test_cached_catalog_never_calls_account_or_changes_default(auth_lane, monkeypatch):
    lane, _ = auth_lane
    row = lc.LaneBackend('openai_codex', 'cloud', 'chosen', models=('other', 'chosen'))
    lane.backends = {'openai_codex': row}
    monkeypatch.setattr(lc, 'get_lane', lambda: lane)
    monkeypatch.setattr(OpenAICodexClient, 'visible_models', AsyncMock(side_effect=AssertionError('catalog must not call provider')))
    router = SimpleNamespace(discover=AsyncMock(side_effect=AssertionError('no local provider')))
    assert await lc.catalog(router) == await lc.catalog(router) == [row.row()]
    assert lane.backends['openai_codex'] is row


@pytest.mark.asyncio
async def test_legacy_roster_rows_are_additive(monkeypatch):
    from api import server
    monkeypatch.setenv('BOB_ROSTER_FILE', 'legacy-roster-selector.yaml')
    monkeypatch.setattr(server, 'get_lane', lambda: SimpleNamespace(backends={}))
    request = SimpleNamespace(app={server.ROUTER_KEY: SimpleNamespace(discover=AsyncMock(return_value=[]))})
    rows = json.loads((await server.list_available_models(request)).text)
    for row in rows:
        assert {'provider_label', 'choices', 'tool_capable', 'billing'} <= row.keys()
        if row.get('model'):
            assert [c['model'] for c in row['choices'] if c['default']] == [row['model']]
    cli = next(r for r in rows if r['backend'] == 'claude_code')
    assert cli['model'] is None and cli['choices'] == []  # no invented legacy CLI identity


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', ['gpt-6.2-sol', 'gpt-6.1-sol', 'account-extra'])
async def test_each_choice_reaches_provider_and_bu5_first_name_is_per_binding(auth_lane, monkeypatch, selected):
    from core.execution.workflow_controller import WorkflowController
    lane, auth_path = auth_lane
    row = lc.LaneBackend('openai_codex', 'cloud', 'gpt-6.2-sol',
        models=('gpt-6.2-sol', 'gpt-6.1-sol', 'account-extra'))
    lane.backends = {'openai_codex': row}
    before = {p: p.read_bytes() for p in [auth_path, lane.root / 'lane.env']}
    bodies = []
    def endpoint(request):
        assert request.method == 'POST' and request.url.path.endswith('/responses')
        bodies.append(json.loads(request.content))
        return sse([text_item()], model=selected + '-reported')
    client = OpenAICodexClient(lane=lane, clock=lambda: NOW, transport=httpx.MockTransport(endpoint))
    reply = await ChatOpenAICodex(client=client, model_override=selected).ainvoke('fixture')
    assert bodies[0]['model'] == selected
    assert reply.response_metadata['model_name'] == selected + '-reported'
    assert reply.response_metadata['requested_model'] == selected
    assert reply.usage_metadata['input_tokens'] == 3
    # Exercise the real BU5 rule, without claiming a database durability test.
    record = {'run': {}, 'stage': {'actual_models': []}}
    controller = object.__new__(WorkflowController)
    controller.store = SimpleNamespace(wf_model_result=AsyncMock())
    controller.stage = lambda r, key: r['stage']
    async def change(row, action):
        return await action(None, record)
    controller.change = change
    definitions = lc.agent_service_definitions([row.row()])['models']
    for key, spec in definitions.items():
        context = SimpleNamespace(binding={'binding_id': key, **spec}, stage={'key': 'fixture'}, row={})
        actual = spec['model'] + '-reported'
        assert await controller.model_result(context, 'attempt', actual) is None
        assert await controller.model_result(context, 'attempt', actual) is None
        assert 'Earlier in this run' in await controller.model_result(context, 'attempt', actual + '-changed')
    assert len(record['run']['model_coverage']) == 3
    # A subsequent unpinned conversation still executes the provider default.
    await client.complete([])
    assert bodies[-1]['model'] == 'gpt-6.2-sol'
    assert lane.backends['openai_codex'] is row and row.model == 'gpt-6.2-sol'
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.asyncio
async def test_invalid_pin_is_refused_before_auth_or_provider(auth_lane, monkeypatch):
    from api import server
    lane, _ = auth_lane
    lane.backends['openai_codex'] = lc.LaneBackend('openai_codex', 'cloud', 'chosen', models=('chosen', 'other'))
    client = OpenAICodexClient(lane=lane, transport=httpx.MockTransport(lambda _: pytest.fail('no provider call')))
    credentials = AsyncMock(side_effect=AssertionError('no auth refresh'))
    monkeypatch.setattr(client.auth, 'credentials', credentials)
    sentence = 'The model "invented" is not offered by GPT (ChatGPT sign-in).'
    with pytest.raises(lc.LaneConfigurationError) as caught:
        await client.complete([], model='invented')
    assert str(caught.value) == sentence
    credentials.assert_not_awaited()
    with pytest.raises(ValueError) as caught:
        validate_pin([lane.backends['openai_codex'].row()], 'openai_codex', 'invented')
    assert str(caught.value) == sentence
    monkeypatch.delenv('BOB_ROSTER_FILE', raising=False)
    monkeypatch.setattr(server, 'get_lane', lambda: lane)
    # An initialized placeholder passes service readiness; dispatch must not reach it.
    request = SimpleNamespace(app={server.GRAPH_KEY: object(), server.FACES_KEY: object()},
        json=AsyncMock(return_value={'conversation_id': 'fixture',
        'content': 'test', 'backend': 'openai_codex', 'model': 'invented'}))
    response = await server.chat(request)
    assert response.status == 400 and json.loads(response.text)['message'] == sentence


@pytest.mark.asyncio
async def test_every_claude_alias_reaches_fake_cli_argument_and_observation(monkeypatch):
    from core.backends.claude_turn import ClaudeTurn
    monkeypatch.setattr(lc, 'require_backend', lambda *a: None)
    for alias in MODELS:
        records = []
        link = FakeLink([{'text': 'fixture'}])
        turn = ClaudeTurn(model=alias, connector=link.connect,
            recorder=lambda tid, kind, payload: records.append((kind, payload)))
        try:
            reply = await turn.ainvoke([HumanMessage(content='test')])
            validate_start(link.value)
            argv = invocation(link.value, '/private/requests', '/private/responses')
            assert argv[argv.index('--model') + 1] == alias
            assert ('model', {'selection': alias, 'reported': reply.response_metadata['model_name']}) in records
        finally:
            await turn.close()


@pytest.fixture
def broker(monkeypatch):
    if sys.platform == 'win32' and 'fcntl' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'fcntl', ModuleType('fcntl'))
    spec = importlib.util.spec_from_file_location('bu16_broker', Path(__file__).resolve().parents[2] / 'portal-broker/portal-broker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_broker_passthrough_and_legacy_shape(broker):
    entries = [lc.LaneBackend('openai_codex', 'cloud', 'chosen', models=('chosen', 'other')).row(),
               lc.LaneBackend('claude_code', 'cli', 'opus', models=MODELS).row(),
               lc.LaneBackend('deepseek_v4_flash', 'cloud', 'deepseek-v4-flash', models=('deepseek-v4-flash',)).row(),
               lc.LaneBackend('unavailable', 'cloud', available=False).row()]
    b = object.__new__(broker.Broker)
    b.identity, b.catalog_seconds = {}, 25
    b._component = lambda method, route, component, warnings, deadline: entries if component == 'models' else {}
    for source, row in zip(entries, b.op_catalog({})['models']):
        for key in ('backend', 'model', 'label', 'available', 'provider_label', 'choices', 'tool_capable', 'billing'):
            assert row.get(key) == source.get(key)
    legacy = {'backend': 'legacy', 'model': 'old', 'available': True, 'label': 'Legacy / old'}
    assert b._model_row(legacy)['choices'] == [{'model': 'old', 'label': 'old', 'default': True}]
    assert b._model_row(legacy)['provider_label'] == 'Legacy'


def test_broker_send_checks_choices_before_start(broker):
    row = lc.LaneBackend('openai_codex', 'cloud', 'chosen', models=('chosen', 'other')).row()
    b = object.__new__(broker.Broker)
    b.identity = {}
    b._rest = Mock(return_value=[row])
    b._start = Mock(return_value=None)
    args = {'conversation_id': 'fixture', 'content': 'test', 'backend': 'openai_codex'}
    with pytest.raises(broker.BrokerError) as caught:
        b.op_chat_send({**args, 'model': 'invented'})
    assert caught.value.code == 'invalid_pin'
    assert caught.value.detail == 'The model "invented" is not offered by GPT (ChatGPT sign-in).'
    b._start.assert_not_called()
    result = b.op_chat_send({**args, 'model': 'other'})
    assert b._start.call_args.args[0].requested['model'] == 'other'
    assert row['model'] == 'chosen'


@pytest.mark.asyncio
@pytest.mark.parametrize('backend,expected_input', [('openai_codex', 8), ('claude_code', 4)])
async def test_core_stream_keeps_actual_model_and_correct_usage_semantics(backend, expected_input):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from api import server
    class Graph:
        async def astream(self, *args, **kwargs):
            yield 'custom', {'type': 'model_observation', 'backend': backend,
                'model': 'selection', 'provider_reported_model': 'actual-full'}
            for _ in range(2):
                yield 'custom', {'type': 'model_usage', 'backend': backend, 'model': 'actual-full',
                    'available': True, 'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}
            yield 'custom', {'type': 'token', 'backend': backend, 'content': 'fixture'}
    async def handler(request):
        return await server._stream_graph_turn(request=request, graph=Graph(), graph_input={},
            thread_id='fixture', approvals={}, face_id='assistant', user_content='test',
            model_override='selection', backend_override=backend)
    app = web.Application()
    app.router.add_get('/fixture', handler)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/fixture')
        events = [json.loads(line[6:]) for line in (await response.text()).splitlines() if line.startswith('data: ')]
    complete = events[-1]
    assert complete['type'] == 'message_complete' and complete['model'] == 'actual-full'
    assert complete['requested_model'] == 'selection' and complete['backend'] == backend
    assert complete['tokens_in'] == expected_input
    assert complete['provider_reported_model'] == 'actual-full'
