"""GL18: real shipped registries, guide/tools/prompts and owner starter; no models."""
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.faces.registry import FaceRegistry
from core.faces.prompt import interactive_messages
from core.guide import TOPICS, MAX_TOPIC_CHARS, effective_tools, render
from core.tools.registry import bob_guide, _skill_face_id
from test_configured_catalog import lane, configure

FACES = ('assistant', 'builder', 'fixer', 'orchestrator', 'planner', 'researcher', 'reviewer')
# Full assembled always-on messages, same deterministic backend and full effective
# bindings. Guide bodies are never always-on. The starters are written in plain
# sentences (clarity over minimum size); these caps only stop a starter from
# growing back into a pasted manual.
# BU7 gives Assistant explicit discussion rules and Planner the planning card.
# BU9 spells out the distinction between chat-managed work and workflow stages.
# BU11 raises Reviewer from 2900 to 3400 to retain the full read-stage explanation.
BUDGETS = dict(assistant=14500, planner=12000, builder=5096, fixer=4296, orchestrator=3800,
               researcher=3400, reviewer=3400)


@pytest.fixture(autouse=True)
def mock_redis():
    yield  # These tests never invoke execute or Redis.


@pytest.fixture
def configured(lane, monkeypatch):
    cfg = configure(lane, 'c', BOB_LANE_DEFAULT_BACKEND='deepseek_v4_flash')
    registry = FaceRegistry()
    monkeypatch.setattr('core.faces.registry.get_default_registry', lambda: registry)
    return cfg, registry


@pytest.mark.asyncio
@pytest.mark.parametrize('face', FACES)
async def test_topics_match_effective_registry_and_stay_small(configured, face):
    cfg, registry = configured
    index = (await render(registry=registry, face_id=face))['content']
    for p in TOPICS.glob('*.md'):
        assert p.stem + ':' in index
        text = (await render(p.stem, registry=registry, face_id=face))['content']
        assert len(text) <= MAX_TOPIC_CHARS
        assert not re.search(r'[0-9a-f]{8}-[0-9a-f-]{27}|Bearer \S+|sk-[A-Za-z0-9]{12,}|/(?:var|srv|home)/|[A-Z]:\\', text)
        assert 'fake-a' not in text and 'fake-d' not in text
    tools = await effective_tools(registry, face)
    from core.permissions import check_tool_access
    assert {t.name for t in tools} == {name for name in registry.get_allowed_tools(face)
                                      if check_tool_access(face, name)}
    rows = (await render('tools', registry=registry, face_id=face))['content'].split('\n\n')[-1].splitlines()
    assert {row.split(':', 1)[0] for row in rows} == {t.name for t in tools}
    for t, row in zip(tools, rows):
        assert row.startswith(t.name + ': ')
        assert t.description.split()[0] in row


@pytest.mark.asyncio
@pytest.mark.parametrize('face', FACES)
async def test_prompt_glance_pointer_and_measured_budget(configured, face):
    _, registry = configured
    tools = await effective_tools(registry, face)
    messages = interactive_messages([], face, 'deepseek_v4_flash', tools)
    assert 'bob_guide' in messages[0]['content']
    card = json.loads(messages[1]['content'].split('\n', 1)[1])
    assert set(card['tool_glance'].split(', ')) == {t.name for t in tools}
    assert set(card['native_tools']) == {t.name for t in tools}
    assert len(''.join(m['content'] for m in messages)) <= BUDGETS[face]
    assert 'BOB PRODUCT AND UI GUIDE' not in str(messages)
    assert 'bob.product' not in registry.get_face(face).allowed_skills


@pytest.mark.asyncio
async def test_guide_discovery_and_stopping_explanations(configured):
    _, registry = configured
    index = (await render(registry=registry, face_id='assistant'))['content']
    assert set(line.split(':', 1)[0] for line in index.splitlines()) == {p.stem for p in TOPICS.glob('*.md')}
    limits = (await render('limits', registry=registry, face_id='assistant'))['content']
    assert all(name in limits for name in ('fanout width of 20', 'effective_worker_slots of 1', 'workflow_parallelism of 2'))
    assert 'no usable concurrency allowance' in limits and 'tool_round_limit' in limits
    for face in ('assistant', 'builder', 'fixer'):
        prompt = registry.get_system_prompt(face)
        assert 'everyday words' in prompt and 'Never write HTML tags.' in prompt
        assert '"Details:" on its own line' in prompt


@pytest.mark.asyncio
async def test_role_authority_and_native_invocation_identity(configured):
    _, registry = configured
    readonly = {'bob_guide', 'web_search', 'web_fetch', 'memory_recall',
                'conversations_find', 'conv_search', 'conv_expand', 'get_server_time'}
    # Every face may ask to use this PC (native per-agent/per-task grant decides).
    from core.tools.pc_tools import PC_TOOL_NAMES
    assert set(registry.get_face('researcher').allowed_tools) == readonly
    assert set(registry.get_allowed_tools('researcher')) == readonly | set(PC_TOOL_NAMES)
    assert set(registry.get_allowed_tools('reviewer')) == {'bob_guide', 'fs_list', 'fs_read', 'fs_search'} | set(PC_TOOL_NAMES)
    with pytest.raises(PermissionError):
        await bob_guide.ainvoke({})
    token = _skill_face_id.set('researcher')
    try:
        assert (await bob_guide.ainvoke({'topic': 'tools'}))['face'] == 'researcher'
        with pytest.raises(ValueError):
            await bob_guide.ainvoke({'topic': '../lane.env'})
        with pytest.raises(Exception):
            await bob_guide.ainvoke({'face_id': 'builder'})
    finally:
        _skill_face_id.reset(token)


@pytest.mark.asyncio
async def test_scope_filters_agents_models_limits_and_tools(configured):
    _, registry = configured
    for topic in ('agents', 'models', 'limits', 'tools'):
        text = (await render(topic, registry=registry, visible_faces=['reviewer']))['content']
        assert not re.search(r'^(assistant|builder|fixer|orchestrator|planner|researcher):', text, re.M)
        assert not re.search(r'^\W*(fs_write|shell_run):', text, re.M)
        with pytest.raises(PermissionError):
            await render(topic, registry=registry, face_id='builder', visible_faces=['reviewer'])
    text = (await render('agents', registry=registry, visible_faces=[]))['content']
    assert not re.search(r'^reviewer:', text, re.M)


@pytest.mark.asyncio
async def test_live_configuration_and_redaction(configured, monkeypatch):
    cfg, registry = configured
    from core.config import config
    monkeypatch.setattr(config, 'TOOL_LOOP_MAX_ROUNDS', 17)
    registry.get_face('fixer').tool_loop_max_rounds = 9
    text = (await render('limits', registry=registry))['content']
    assert 'assistant: tool rounds=50' in text and 'fixer: tool rounds=9' in text
    assert 'builder: tool rounds=100' in text
    from dataclasses import replace
    cfg.backends['deepseek_v4_flash'] = replace(cfg.backends['deepseek_v4_flash'], model='changed-model')
    cfg.values['SECRET_CANARY'] = 'never-disclose-this-value'
    text = (await render('models', registry=registry))['content']
    assert 'changed-model' in text and 'never-disclose-this-value' not in text
    registry.get_face('assistant').preferred_backend = '/var/private/11111111-2222-3333-4444-555555555555'
    assert '/var/private' not in (await render('models', registry=registry))['content']


@pytest.mark.asyncio
async def test_adding_topic_is_one_file(configured, tmp_path, monkeypatch):
    import core.guide as guide
    (tmp_path / 'workflows.md').write_text('# Workflows\nActual saved workflow revision and executor contract.\n')
    monkeypatch.setattr(guide, 'TOPICS', tmp_path)
    _, registry = configured
    assert 'workflows: Workflows' in (await render(registry=registry))['content']
    assert 'saved workflow revision' in (await render('workflows', registry=registry))['content']


def test_starter_seeds_only_empty_files(tmp_path):
    from core.starter_memory import STARTER_MEMORY, seed_empty
    file = tmp_path / 'MEMORY.md'
    for existing in (None, b'', b' ', b'- owner note\n', STARTER_MEMORY.encode() + b'Owner detail\n'):
        if existing is not None:
            file.write_bytes(existing)
        elif file.exists():
            file.unlink()
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            assert seed_empty(fd) is (existing in (None, b''))
        finally:
            os.close(fd)
        expected = STARTER_MEMORY.encode() if existing in (None, b'') else existing
        assert file.read_bytes() == expected
        fd = os.open(file, os.O_WRONLY)
        try:
            assert seed_empty(fd) is False
        finally:
            os.close(fd)
        assert file.read_bytes() == expected
    assert len(STARTER_MEMORY) < 500
    assert not any(x in STARTER_MEMORY for x in ('Builder', 'provider', 'model', 'workspace_create'))


def test_starter_status_never_reads_another_owner(configured, monkeypatch, tmp_path):
    from core.starter_memory import starter_empty_for_owner
    # Mismatched owners return before attempting to open any file.
    monkeypatch.setattr(os, 'open', lambda *a, **k: (_ for _ in ()).throw(AssertionError('read')))
    assert starter_empty_for_owner('other-owner') is None


def test_automatic_memory_context_is_small_and_explicitly_shortened(monkeypatch):
    from core.config import config
    from core.prompt_memory import prior_context
    monkeypatch.setattr(config, 'MEMORY_CONTEXT_MAX_CHARS', 500)
    assert prior_context(['first', 'second']) == 'Prior context:\n- first\n- second'
    result = prior_context(['x' * 10000, 'last'])
    assert len(result) == 500 and 'Recall shortened' in result
    assert 'sixth' not in prior_context(['one'] * 5 + ['sixth'])


@pytest.mark.asyncio
async def test_live_catalog_rows_are_sanitized_and_not_reinvented(configured):
    _, registry = configured
    rows = [dict(backend='lane-only', model='model-2', kind='local', available=False,
                 models=['model-1', 'model-2'], api_key='canary-secret-value',
                 unavailable_reason='/var/private/foreign-owner')]
    registry.get_face('assistant').codex_posture = {'model': 'face-pin-model', 'api_key': 'posture-secret-canary'}
    text = (await render('models', registry=registry, backend_rows=rows))['content']
    assert 'lane-only: model=model-2' in text and 'enrolled=model-1, model-2' in text
    assert 'available=false' in text
    assert 'canary-secret' not in text and 'foreign-owner' not in text
    assert 'codex_posture.model=face-pin-model' in text and 'posture-secret-canary' not in text


@pytest.mark.skipif(not hasattr(os, 'O_NOFOLLOW'), reason='requires Linux no-follow owner-file reads')
def test_starter_status_is_owner_scoped_and_never_follows_links(tmp_path, monkeypatch):
    from core import lane_config
    from core.starter_memory import STARTER_MEMORY, starter_empty_for_owner
    lane = tmp_path / 'owner' / 'bob-memory'
    lane.mkdir(parents=True)
    file = lane.parent / 'MEMORY.md'
    file.write_bytes(STARTER_MEMORY.encode())
    monkeypatch.setattr(lane_config, 'configured_mode', lambda: True)
    monkeypatch.setattr(lane_config, 'get_lane', lambda: SimpleNamespace(root=lane))
    assert starter_empty_for_owner('owner') is True
    assert starter_empty_for_owner('foreign') is None
    file.write_bytes(STARTER_MEMORY.encode() + b'Owner detail')
    assert starter_empty_for_owner('owner') is False
    file.unlink()
    target = tmp_path / 'outside'
    target.write_bytes(STARTER_MEMORY.encode())
    file.symlink_to(target)
    assert starter_empty_for_owner('owner') is None


async def test_reviewer_chat_and_workflow_tool_inventories(configured):
    from core.execution.agent_turn import StageContext
    from core.execution.workflow_context import current
    from core.permissions import check_tool_access
    from core.tools.registry import get_all_tools
    _, registry = configured
    golden = json.loads((Path(__file__).parent / 'roster_golden/conversation_faces.json').read_text())['reviewer']
    assert sorted(registry.get_face('reviewer').allowed_tools) == golden['declared_tools']
    assert sorted(registry.get_allowed_tools('reviewer')) == golden['allowed_tools']
    assert sorted(t.name for t in await effective_tools(registry, 'reviewer')) == golden['effective_tools']
    assert not any(check_tool_access('reviewer', name) for name in ('fs_list', 'fs_read', 'fs_search'))
    ctx = StageContext(None, {}, {'coverage': {'limits': {'workflow_tool_rounds': 10},
        'authority': {'workspace_ops': ['read']}}}, {'key': 'review'}, {'access': 'read'}, {}, 'reviewer')
    token = current.set(ctx)
    try:
        bound = ctx.filter_tools(await get_all_tools([name for name in registry.get_allowed_tools('reviewer')
                                                     if check_tool_access('reviewer', name)]))
        assert sorted(t.name for t in bound) == golden['workflow_read_tools']
        assert sorted(t.name for t in await effective_tools(registry, 'reviewer')) == golden['workflow_read_tools']
        tools = (await render('tools', registry=registry, face_id='reviewer'))['content']
        assert all(name + ':' in tools for name in golden['workflow_read_tools'])
        assert not any(name + ':' in tools for name in ('fs_write', 'fs_edit', 'fs_delete', 'shell_run'))
    finally:
        current.reset(token)
    assert sorted(t.name for t in await effective_tools(registry, 'reviewer')) == golden['effective_tools']
