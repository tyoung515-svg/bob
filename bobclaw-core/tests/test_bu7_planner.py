"""BU7 conversation/planning split: real templates and resolver; no providers."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import yaml

from core.faces.registry import Face, FaceRegistry
from core.faces.prompt import interactive_messages
from core.permissions import check_tool_access
from core.tools.registry import get_all_tools
from core.execution.models import Authority, ExecutionError, digest
from core.execution.workflows import WorkflowRuntime, PREMADES
from core.execution.workflow_controller import WorkflowController
from test_configured_catalog import lane, configure


@pytest.fixture(autouse=True)
def mock_redis():
    yield


@pytest.fixture
def registry(lane, monkeypatch):
    configure(lane, 'c', BOB_LANE_DEFAULT_BACKEND='deepseek_v4_flash')
    registry = FaceRegistry()
    monkeypatch.setattr('core.faces.registry.get_default_registry', lambda: registry)
    monkeypatch.setattr('core.faces.registry.get_registry', lambda: registry)
    return registry


@pytest.mark.parametrize('face', ['assistant', 'planner'])
async def test_template_validation_catalog_and_bound_tool_golden(registry, face):
    golden = json.loads((Path(__file__).parent / 'roster_golden/conversation_faces.json').read_text(encoding='utf-8'))[face]
    raw = yaml.safe_load((Path(__file__).parents[1] / 'core/faces/templates' / (face + '.yaml')).read_text(encoding='utf-8'))
    validated = Face.model_validate(raw)
    registry._validate_registered_tools(validated)
    assert validated.id == face and validated.name == golden['name']
    assert sorted(validated.allowed_tools) == golden['declared_tools']
    assert sorted(registry.get_allowed_tools(face)) == golden['effective_tools']
    bound = await get_all_tools(registry.get_allowed_tools(face))
    assert sorted(t.name for t in bound) == golden['effective_tools']
    assert all(check_tool_access(face, t.name) for t in bound)
    assert face in {f.id for f in registry.list_faces()}
    assert registry.load_skill(face, 'bob.setup', 1)['ok']
    assert not ({'work_start', 'work_prepare', 'work_team_start', 'work_recipe_save',
                 'work_team_save', 'shell_run', 'fs_write', 'fs_edit', 'fs_delete',
                 'workflows_start', 'workflow_run_resume', 'workflow_gate', 'grant_start'} & {t.name for t in bound})
    assert check_tool_access(face, 'memory_retire') == (face == 'assistant')
    assert check_tool_access(face, 'memory_correct') == (face == 'assistant')


def test_distinct_cards_and_conversation_rules(registry):
    def card(face):
        return json.loads(interactive_messages([], face, 'deepseek_v4_flash')[1]['content'].split('\n', 1)[1])
    assistant = card('assistant')['action_suggestions']
    planner = card('planner')['action_suggestions']
    assert set(assistant) == set(planner) == {'format', 'handoff', 'workflow', 'calendar'}
    for name in ('calendar', 'handoff', 'workflow'):
        assert assistant[name]['fields'] == planner[name]['fields']
        assert assistant[name]['authority'] == planner[name]['authority']
    assert 'ask at most three short questions' in planner['handoff']['when']
    assert 'Write the plan and acceptance criteria' in planner['handoff']['when']
    assert 'call/time budget' in planner['workflow']['when']
    assert 'only when its answer changes what happens next' in assistant['handoff']['when']
    assert 'Do not end every reply with questions.' in assistant['handoff']['when']
    assert 'no full plan, file list, acceptance criteria, cost estimate, bob-action block or ask_operator card' in assistant['handoff']['when']
    assert 'one concrete choice blocks it' in assistant['handoff']['when']
    assert 'Only after the user asks for a plan or work' in assistant['workflow']['when']
    assert 'During discussion' in assistant['calendar']['when']
    assert 'planner' not in assistant['handoff']['fields']['agent']
    prompt = registry.get_system_prompt('assistant')
    assert 'If the user wants to talk it over first' in prompt
    assert 'never open with it' in prompt.lower()
    assert "only the user's click" in prompt.lower()
    assert registry.get_face('planner').simple_slot is None


@pytest.mark.parametrize('face', ['assistant', 'planner', None])
async def test_real_workflow_resolver_freezes_origin_face_and_model(registry, monkeypatch, face):
    monkeypatch.setattr('core.execution.api.check_resources', AsyncMock())
    runtime = WorkflowRuntime(SimpleNamespace(store=SimpleNamespace(pool=object())))
    binding = dict(binding_id='selected', backend='deepseek_v4_flash', model='model', model_identity='canonical',
                   face_ids=['assistant', 'planner'], runnable=True)
    runtime.bindings = AsyncMock(return_value=[binding])
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value={
        'face_id': face, 'backend_preference': binding['backend'], 'model_preference': binding['model']}))
    auth = Authority('lane', 'owner', 'owner', {}, 'portal')
    definition = dict(inputs={}, workspace={'required': False}, authority={'checks': []}, stages=[])
    args = dict(inputs={}, origin_conversation_id=str(uuid.uuid4()))
    resolved = await runtime.resolve(connection, auth, args, definition, 'unused')
    expected = face or 'assistant'
    assert resolved['assistant_binding'] == {**binding, 'face_id': expected}
    assert set(resolved['faces']) == {expected}
    assert resolved['faces'][expected]['definition']['id'] == expected
    assert resolved['faces'][expected]['digest'] == digest(resolved['faces'][expected]['definition'])
    # A binding enrolled for the other face is not a silent substitute.
    runtime.bindings = AsyncMock(return_value=[{**binding, 'face_ids': ['planner' if expected == 'assistant' else 'assistant']}])
    with pytest.raises(ExecutionError, match='assistant_binding_unavailable'):
        await runtime.resolve(connection, auth, args, definition, 'unused')
    connection.fetchrow.return_value['face_id'] = 'builder'
    with pytest.raises(ExecutionError, match='conversation_not_found'):
        await runtime.resolve(connection, auth, args, definition, 'unused')


@pytest.mark.parametrize('face', ['assistant', 'planner', None])
async def test_summary_uses_frozen_origin_face_and_legacy_default(registry, face):
    expected = face or 'assistant'
    binding = {'binding_id': 'selected', **({'face_id': face} if face else {})}
    record = {'initiator': Authority('lane', 'owner', 'owner', {}, 'portal').as_dict(),
        'coverage': {'assistant_binding': binding, 'limits': {'workflow_tool_rounds': 3}},
        'resolved': {'faces': {expected: {'definition': {'system_prompt': registry.get_system_prompt(expected)}}}},
        'run': {'origin_conversation_id': 'origin', 'stages': [], 'report': {'digest': 'report-digest'}}}
    controller = WorkflowController.__new__(WorkflowController)
    controller.parked, controller.writer_locks = set(), {}
    controller.store = SimpleNamespace(_release_holds=AsyncMock())
    controller.service = SimpleNamespace(make_report=AsyncMock())
    controller.load = AsyncMock(return_value=record)
    controller.stage_event = AsyncMock()
    async def change(row, action):
        return await action(None, record)
    async def stage(row, key, spec):
        s = {'key': key, 'kind': spec['kind'], 'agent': spec['agent']}
        record['run']['stages'].append(s)
        return s
    async def summary(ctx, payload):
        assert ctx.face == ctx.spec['agent'] == expected
        assert ctx.summary and ctx.round_limit == 1
        assert ctx.messages([], [])[0]['content'] == registry.get_system_prompt(expected)
        assert payload['report_digest'] == 'report-digest'
        return 'Attributed report; browser behavior remains unverified.'
    controller.change, controller.ensure_stage = change, stage
    controller.turn = SimpleNamespace(start=AsyncMock(side_effect=summary))
    row = {'id': 'run', 'lane': 'lane', 'owner': 'owner'}
    await controller.assistant_summary(row)
    assert record['assistant_summary'] == 'delivered'
    assert record['run']['stages'][0]['agent'] == expected
    controller.turn.start.assert_awaited_once()
    controller.store._release_holds.assert_awaited_once()


def test_workflow_planning_stage_still_uses_orchestrator():
    for definition in PREMADES.values():
        if 'planner' in definition['agents']:
            assert definition['agents']['planner']['face_id'] == 'orchestrator'
