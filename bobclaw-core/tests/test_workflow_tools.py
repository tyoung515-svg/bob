"""Real tool functions, signed HTTP seam, service scopes and MCP parity. No models."""
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from core.execution.models import ExecutionError
from core.tools import workflows as tools, workflow_context as context
from core.tools.registry import _skill_face_id
from core.workflow_surface import WorkflowSurface
from core.mcp.server import build_server, MCPConfig
from test_workflow_authority import setup, proposal_args, start, proposed, MemoryStore


@pytest.fixture(autouse=True)
def mock_redis():
    yield


def model_args(key='native-proposal'):
    args = proposal_args(key=key)
    args.pop('origin_conversation_id')
    args.pop('project_id')
    return args


def save_fixture(name, request, result):
    if os.environ.get('WB2B_WRITE_FIXTURES') != '1':
        return
    root = Path(__file__).resolve().parents[2].parent / 'runs/golive/workflow-fixtures/tools'
    root.mkdir(parents=True, exist_ok=True)
    # Only WB2B-owned fixture files are written; WB1/WB2A files stay untouched.
    for suffix, value in [('request', request), ('result', result)]:
        (root / f'wb2b-{name}.{suffix}.json').write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


@pytest.fixture(params=['assistant', 'planner'])
def proposing_face(request):
    return request.param


@pytest.fixture
async def native(setup, monkeypatch, proposing_face):
    s, auth, click = setup
    s.store.pool = object()
    admission = SimpleNamespace(store=s.store)
    import core.workflow_surface as surface
    monkeypatch.setattr(surface, 'WorkflowSurface', lambda admission, **kwargs:
        WorkflowSurface(admission, runtime=s.runtime, clock=s.clock,
            test_only_runner_available=True, **kwargs))
    from core.config import config
    from core.execution import api
    monkeypatch.setattr(config, 'BOBCLAW_SECRET', 'wb2b-test-signature')
    monkeypatch.setattr(api, 'lane_id', lambda: auth.lane)
    monkeypatch.setattr(api, 'policy_epoch', lambda secret: auth.policy_epoch)
    monkeypatch.setattr(api, 'check_resources', AsyncMock())
    monkeypatch.setattr('core.execution.owner.owner_mapping', lambda owner: False)
    app = web.Application()
    app[api.SERVICE_KEY] = admission
    app.add_routes(context.router)
    client = TestClient(TestServer(app))
    await client.start_server()
    monkeypatch.setattr(config, 'PORT', client.server.port)
    now = int(time.time())
    value = {'authority': auth.as_dict(), 'conversation_id': 'assistant-origin',
        'face_id': proposing_face, 'iat': now, 'exp': now+600}
    proof = context.signature(value, config.BOBCLAW_SECRET)
    for bad in ('', 'bad', '0'*64):
        assert not context.valid(value, bad, config.BOBCLAW_SECRET,
            owner=value['authority']['owner'], conversation=value['conversation_id'], face=proposing_face)
    assert not context.valid(value, '', '', owner=value['authority']['owner'],
        conversation=value['conversation_id'], face=proposing_face)
    token = context.bind({'workflow_context': value, 'workflow_context_vouch': proof,
        'face_id': proposing_face, 'user_id': auth.owner, 'conversation_id': 'assistant-origin'})
    face_token = _skill_face_id.set(proposing_face)
    try:
        yield s, auth, click, value, client
    finally:
        _skill_face_id.reset(face_token)
        context.current.reset(token)
        await client.close()


async def test_real_native_functions_and_fixtures(native):
    s, auth, click, value, _ = native
    calls = [(tools.workflows_catalog, {}), (tools.workflows_list, {}),
        (tools.workflows_get, {'id': 'premade-plan-build-review-fix', 'revision': 1})]
    for tool, args in calls:
        result = await tool.ainvoke(args)
        assert 'error' not in result
        save_fixture('native-'+tool.name, args, result)
    args = model_args()
    result = await tools.workflows_propose.ainvoke(args)
    assert 'error' not in result
    p = result['proposal']
    assert p['coverage']['assistant_summary_calls'] == 1
    record = s.store.data[auth.lane, auth.owner, 'proposal', p['id']]
    assert record['args']['origin_conversation_id'] == value['conversation_id']
    assert {k: p['proposed_by'][k] for k in ('actor', 'token_type')} == {'actor': auth.actor, 'token_type': auth.token_type}
    assert p['proposed_by']['via'] in {'assistant', 'agent', 'owner'}
    assert result['action']['proposal_digest'] == p['digest']
    assert s.store.starts == 0
    save_fixture('native-workflows_propose', args, result)
    run = (await start(s, click, p))['run']
    calls = [(tools.workflow_run_status, {'run_id': run['id']}),
        (tools.workflow_run_report, {'run_id': run['id']}),
        (tools.workflow_run_message, {'run_id': run['id'], 'stage_key': 'plan',
            'text': 'Keep the original acceptance criteria.', 'request_key': 'native-message'}),
        (tools.workflow_run_stop, {'run_id': run['id'], 'request_key': 'native-stop'}),
        (tools.workflow_run_report, {'run_id': run['id']})]
    for tool, args in calls:
        result = await tool.ainvoke(args)
        assert 'error' not in result
        save_fixture('native-'+tool.name, args, result)
    assert result['report']['outcome'] == 'stopped'


@pytest.mark.parametrize('tool', tools.WORKFLOW_TOOLS)
async def test_native_extra_identity_fields_refused(native, tool):
    for key in ('origin_conversation_id', 'assistant_origin', 'authority', 'owner'):
        with pytest.raises(Exception):
            await tool.ainvoke({key: 'forged'})


async def test_catalog_explains_existing_review_enforcement(native):
    result = await tools.workflows_catalog.ainvoke({})
    text = result['review_policy']['explanation']
    assert 'platform checks the review policy when a proposal is made' in text
    assert "owner's explicit same-model review choice" in text and 'recorded' in text


async def test_cross_owner_and_origin_scope_including_replay(native):
    s, auth, click, value, _ = native
    p = (await tools.workflows_propose.ainvoke(model_args()))['proposal']
    run = (await start(s, click, p))['run']
    args = {'run_id': run['id'], 'stage_key': 'plan', 'text': 'Scoped message', 'request_key': 'same-key'}
    assert (await tools.workflow_run_message.ainvoke(args))['state'] == 'queued'
    # Replaying that exact receipt from a different conversation must be hidden.
    other = {**value, 'conversation_id': 'different-conversation'}
    from core.config import config
    token = context.current.set((other, context.signature(other, config.BOBCLAW_SECRET)))
    try:
        assert (await tools.workflow_run_message.ainvoke(args))['error']['code'] == 'not_found'
        assert (await tools.workflow_run_stop.ainvoke({'run_id': run['id'], 'request_key': 'foreign-stop'}))['error']['code'] == 'not_found'
        # Reads remain available within the caller's authorized owner scope.
        assert (await tools.workflow_run_status.ainvoke({'run_id': run['id']}))['run']['id'] == run['id']
    finally:
        context.current.reset(token)
    foreign = {**value, 'authority': replace(auth, owner='foreign', actor='foreign').as_dict()}
    token = context.current.set((foreign, context.signature(foreign, config.BOBCLAW_SECRET)))
    try:
        for tool in (tools.workflow_run_status, tools.workflow_run_report):
            assert (await tool.ainvoke({'run_id': run['id']}))['error']['code'] == 'not_found'
    finally:
        context.current.reset(token)
    await tools.workflow_run_stop.ainvoke({'run_id': run['id'], 'request_key': 'stop'})
    assert (await tools.workflow_run_message.ainvoke({**args, 'request_key': 'terminal-message'}))['error']['code'] == 'run_terminal'


async def test_signed_context_cannot_be_forged_and_no_mutation_operations(native):
    _, _, _, value, client = native
    from core.config import config
    proof = context.signature(value, config.BOBCLAW_SECRET)
    for operation in ('start', 'grant_start', 'resume', 'gate', 'save', 'copy', 'delete'):
        response = await client.post('/api/chat-workflows/'+operation,
            json={'context': value, 'vouch': proof, 'args': {}})
        assert response.status == 404
    for changed in ({**value, 'face_id': 'builder'},
                    {**value, 'face_id': 'planner' if value['face_id'] == 'assistant' else 'assistant'},
                    {**value, 'conversation_id': 'forged'},
                    {**value, 'authority': {**value['authority'], 'owner': 'forged'}}):
        response = await client.post('/api/chat-workflows/catalog',
            json={'context': changed, 'vouch': proof, 'args': {}})
        assert response.status == 403
    for key in ('origin_conversation_id', 'assistant_origin'):
        response = await client.post('/api/chat-workflows/propose',
            json={'context': value, 'vouch': proof, 'args': {**model_args(), key: 'forged'}})
        assert response.status == 422
    token = context.current.set(None)
    try:
        assert (await tools.workflows_catalog.ainvoke({}))['error']['code'] == 'workflow_context_unavailable'
    finally:
        context.current.reset(token)
    orchestrator = {**value, 'face_id': 'orchestrator'}
    token = context.current.set((orchestrator, context.signature(orchestrator, config.BOBCLAW_SECRET)))
    try:
        assert 'templates' in await tools.workflows_catalog.ainvoke({})
        assert (await tools.workflows_propose.ainvoke(model_args()))['error']['code'] == 'not_found'
    finally:
        context.current.reset(token)


def test_face_allowlists_and_no_nested_worker_workflows(monkeypatch):
    from core.faces.registry import FaceRegistry
    registry = FaceRegistry()
    names = {t.name for t in tools.WORKFLOW_TOOLS}
    for face in registry.all_faces():
        allowed = set(registry.get_allowed_tools(face.id)) & names
        assert allowed == (names if face.id in {'assistant', 'planner'} else set(tools.WORKFLOW_READ_NAMES)
            if face.id == 'orchestrator' else set())


async def test_owner_needs_you_projects_scoped_persisted_proposals(setup):
    s, auth, _ = setup
    surface = WorkflowSurface(SimpleNamespace(store=s.store), runtime=s.runtime, clock=s.clock)
    agent = replace(auth, token_type='agent', actor='outside-token', scope={'may_touch': ['fixture/**']})
    own = (await surface.command(agent, 'workflows.propose', {'contract_version': 1, **proposal_args()}))['proposal']
    foreign = replace(auth, owner='other-owner', actor='other-owner')
    await surface.command(foreign, 'workflows.propose', {'contract_version': 1, **proposal_args(key='other')})
    transaction = s.store.transaction
    class Query:
        async def fetch(self, sql, lane, owner, cursor):
            return [{'id': id} for l, o, kind, id in sorted(s.store.data)
                    if (l, o, kind) == (lane, owner, 'proposal') and id > cursor]
    @asynccontextmanager
    async def tx(**kwargs):
        async with transaction(**kwargs):
            yield Query()
    s.store.transaction = tx
    result = await surface.command(auth, 'workflows.proposals', {'contract_version': 1})
    assert len(result['items']) == 1
    assert result['items'][0]['id'] == own['id']
    assert result['items'][0]['proposed_by']['actor'] == 'outside-token'
    stranger = replace(agent, actor='another-token')
    assert not (await surface.command(stranger, 'workflows.proposals', {'contract_version': 1}))['items']
    assert (await surface.command(agent, 'workflows.proposals', {'contract_version': 1}))['items'][0]['id'] == own['id']
    save_fixture('owner-needs-you', {'contract_version': 1}, result)


async def test_mcp_real_functions_service_parity_scope_and_fixtures(setup):
    s, auth, click = setup
    surface = WorkflowSurface(SimpleNamespace(store=s.store), runtime=s.runtime, clock=s.clock)
    agent = replace(auth, token_type='agent', actor='mcp-token', scope={'may_touch': ['fixture/**']})
    hits = []
    async def gateway(method, path, params=None, json_body=None):
        from core.execution.workflows import FIELDS
        hits.append((method, path, params, json_body))
        args = dict(params or json_body or {})
        if path == '/workflows/catalog': op = 'catalog'
        elif path == '/workflows': op = 'list'
        elif path == '/workflow-proposals': op = 'propose'
        elif path.startswith('/workflows/'):
            op = 'get'; args['id'] = path.split('/')[-1]
        else:
            bits = path.split('/'); args['run_id'] = bits[2]
            op = {'events': 'events', 'messages': 'message', 'stop': 'stop'}.get(bits[-1], 'run')
            args.pop('wait', None)  # Gateway-only long-poll transport parameter.
        try:
            return json.dumps(await surface.command(agent, 'workflows.'+op, args))
        except ExecutionError as exc:
            return '[error:http_'+str(exc.status)+'] '+exc.code
    from mcp.server.fastmcp import FastMCP
    from core.mcp.workflows import register
    mcp = FastMCP('workflow-test')
    register(mcp, gateway)
    registered = {t.name: t for t in mcp._tool_manager.list_tools()}
    for description in (registered['workflows_catalog'].description, tools.workflows_catalog.description):
        assert 'platform checks the review policy when a proposal is made' in description
        assert "owner's explicit same-model review choice" in ' '.join(description.split())
        assert 'recorded' in description
    assert set(registered) == {t.name for t in tools.WORKFLOW_TOOLS} | {'workflow_run_events'}
    for name, args in [('workflows_catalog', {}), ('workflows_list', {}),
            ('workflows_get', {'id': 'premade-plan-build-review-fix', 'revision': 1}),
            ('workflows_propose', model_args('mcp-proposal'))]:
        result = json.loads(await registered[name].fn(**args))
        save_fixture('mcp-'+name, args, result)
    p = result['proposal']
    assert p['coverage']['assistant_summary_calls'] == 0
    assert p['proposed_by']['actor'] == 'mcp-token'
    assert s.store.starts == 0
    with pytest.raises(ExecutionError):
        await start(s, agent, p)
    with pytest.raises(Exception):
        await mcp._tool_manager.call_tool('workflows_propose', {**model_args(), 'origin_conversation_id': 'forged'})
    run = (await start(s, click, p))['run']
    for name, args in [('workflow_run_status', {'run_id': run['id']}),
            ('workflow_run_events', {'run_id': run['id'], 'after_seq': 0, 'wait': 0}),
            ('workflow_run_message', {'run_id': run['id'], 'stage_key': 'plan', 'text': 'Retain evidence.', 'request_key': 'mcp-message'}),
            ('workflow_run_stop', {'run_id': run['id'], 'request_key': 'mcp-stop'}),
            ('workflow_run_report', {'run_id': run['id']})]:
        result = json.loads(await registered[name].fn(**args))
        save_fixture('mcp-'+name, args, result)
    agent = replace(agent, actor='another-token')
    assert 'http_404' in await registered['workflow_run_status'].fn(run['id'])
    assert 'http_404' in await registered['workflow_run_message'].fn(run['id'], 'plan', 'No', 'foreign')
    assert 'http_404' in await registered['workflow_run_stop'].fn(run['id'], 'foreign-stop')


async def test_mcp_workflow_proxies_forward_token_and_preserve_refusals(monkeypatch):
    hits = []
    status = [200]
    class Response:
        @property
        def status(self): return status[0]
        async def text(self):
            return json.dumps({'run': {'report': {'outcome': 'stopped'}}}) if self.status == 200 else '{"error":{"code":"not_found"}}'
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, method, path, **kwargs):
            hits.append((method, path, kwargs))
            return Response()
    monkeypatch.setattr('core.mcp.server.aiohttp.ClientSession', Session)
    mcp = build_server(MCPConfig('scoped-canary', 'http://gateway', ['reviewer']))
    registered = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert json.loads(await registered['workflow_run_report'].fn('run-1')) == {'report': {'outcome': 'stopped'}}
    await registered['workflow_run_events'].fn('run-1', 9007199254740993, 100, 25)
    assert hits[-1][2]['params'] == {'contract_version': 1, 'after_seq': 9007199254740993, 'limit': 100, 'wait': 25}
    await registered['workflows_propose'].fn(**model_args())
    assert 'origin_conversation_id' not in hits[-1][2]['json']
    assert all(h[2]['headers'] == {'Authorization': 'Bearer scoped-canary'} for h in hits)
    status[0] = 404
    for name in ('workflow_run_status', 'workflow_run_report'):
        assert 'http_404' in await registered[name].fn('hidden-run')


async def test_workflows_guide_real_mcp_tool_and_resource_redaction(monkeypatch):
    from core.guide import render
    from core.faces.registry import FaceRegistry
    registry = FaceRegistry()
    # A secret-looking backend is not relevant to this topic and never leaks.
    registry.get_face('assistant').preferred_backend = '/srv/private/sk-abcdefghijklmnop'
    class Response:
        status = 200
        def __init__(self, params): self.params = params
        async def text(self):
            return json.dumps(await render(self.params['topic'], registry=registry,
                face_id=self.params['face_id'] or None, visible_faces=['reviewer']))
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, method, path, **kwargs): return Response(kwargs['params'])
    monkeypatch.setattr('core.mcp.server.aiohttp.ClientSession', Session)
    mcp = build_server(MCPConfig('guide-canary', 'http://gateway', ['reviewer']))
    guide = next(t for t in mcp._tool_manager.list_tools() if t.name == 'bob_guide')
    text = await guide.fn('workflows', 'reviewer')
    assert 'Start click' in text and '/srv/private' not in text and 'sk-abcdefghijklmnop' not in text
    assert 'workflows_catalog' in str(await mcp.read_resource('bob://guide/workflows'))


def test_generated_fixture_pairs_are_complete():
    if os.environ.get('WB2B_WRITE_FIXTURES') != '1':
        return
    root = Path(__file__).resolve().parents[2].parent / 'runs/golive/workflow-fixtures/tools'
    names = ['native-'+t.name for t in tools.WORKFLOW_TOOLS]
    names += ['mcp-'+t.name for t in tools.WORKFLOW_TOOLS] + ['mcp-workflow_run_events', 'owner-needs-you']
    for name in names:
        for suffix in ('request', 'result'):
            assert isinstance(json.loads((root / f'wb2b-{name}.{suffix}.json').read_text(encoding='utf-8')), dict)
    (root / 'WB2B-README.md').write_text(
        '# WB2B workflow tool fixtures\n\n'
        'These 18 request/result pairs are generated by python scripts/wb2b_gate.py --write-fixtures. '
        'Native fixtures invoke the real LangChain tools through the signed loopback endpoint and WorkflowSurface/WorkflowService. '
        'MCP fixtures invoke the registered real proxy functions through the same service operations. '
        'Persistence, enrollment, clock and owner-click authorization are test adapters; no provider or production runner was called. '
        'IDs vary between regenerations. The native report example is the final stopped report. '
        'The owner-needs-you pair exercises the pending proposal projection and authenticated token attribution.\n\n'
        'Only wb2b-* files and this README in this tools subfolder are regenerated. Parent WB1/WB2A fixtures are untouched.\n',
        encoding='utf-8')
