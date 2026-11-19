"""Subscription/API separation and concrete catalog bindings; no provider calls."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core import lane_config as lc


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.delenv('BOB_SETTINGS_ID', raising=False)
    monkeypatch.delenv('BOB_ROSTER_FILE', raising=False)
    monkeypatch.setenv('BOB_LANE_ROOT', str(tmp_path))
    monkeypatch.setattr(lc, 'execution_mode', lambda: 'worker')
    monkeypatch.setattr(lc.shutil, 'which', lambda name: '/fake/' + name)
    monkeypatch.setattr(lc, 'unavailable_reason_for_lane', lambda lane: None)
    monkeypatch.setattr(lc, 'claude_unavailable_reason', lambda lane: None)
    lc._cached_lane.cache_clear()
    yield tmp_path
    lc._cached_lane.cache_clear()


def configure(root, **values):
    (root / 'lane.env').write_text(''.join(k + '=' + json.dumps(v) + '\n' for k, v in values.items()))
    lc._cached_lane.cache_clear()
    return lc.read_lane(root)


@pytest.mark.asyncio
@pytest.mark.parametrize('signed,selected,present', [
    ('true', 'opus', True), ('false', 'opus', False),
    ('true', '', False), ('true', 'invented', False), ('false', '', False),
])
async def test_claude_enrollment_requires_signin_and_selection(lane, signed, selected, present):
    configure(lane, BOB_LANE_BACKENDS='claude_code', CLAUDE_SIGNED_IN=signed,
              CLAUDE_CODE_MODEL=selected)
    rows = await lc.catalog(SimpleNamespace(discover=AsyncMock(return_value=[])))
    assert bool(rows) is present
    if present:
        row, = rows
        assert row['backend'] == 'claude_code' and row['model'] == 'opus'
        from core.worker.claude import MODELS
        assert row['models'] == list(MODELS) and row['billing'] == 'subscription'
        assert row['available'] is True and row['tool_capable'] is True
        lc.require_backend('claude_code', 'opus')  # admitted: does not raise
        specs = lc.agent_service_definitions(rows)['models']
        spec, = specs.values()
        assert spec['enabled'] is True and spec['model'] == 'opus'
        assert 'reported_model_names' not in spec  # CC2 must observe names, never invent them.


@pytest.mark.asyncio
async def test_subscription_and_api_key_enrollment_are_separate(lane):
    configure(lane, ANTHROPIC_API_KEY='FAKE-KEY', ANTHROPIC_MODEL='fake-api-model',
              BOB_LANE_BACKENDS='claude_code')
    assert set(lc.read_lane(lane).backends) == {'claude_api'}
    configure(lane, BOB_LANE_BACKENDS='claude_code', CLAUDE_SIGNED_IN='true', CLAUDE_CODE_MODEL='default')
    assert set(lc.read_lane(lane).backends) == {'claude_code'}
    with pytest.raises(lc.LaneConfigurationError):
        lc.read_lane(lane).require('claude_api')


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', ['', 'fake-selected-gpt'])
async def test_codex_follows_chatgpt_selection_and_hides_empty(lane, selected):
    home = lane / 'codex-home'
    home.mkdir()
    (home / 'auth.json').write_text('{"tokens":{"access_token":"FAKE-ACCESS"}}')
    configure(lane, OPENAI_CODEX_MODEL=selected)
    rows = await lc.catalog(SimpleNamespace(discover=AsyncMock(return_value=[])))
    coding = [row for row in rows if row['backend'] == 'codex_code']
    assert bool(coding) is bool(selected)
    if selected:
        assert coding[0]['model'] == selected and coding[0]['models'] == [selected]
        assert coding[0]['available']


def test_no_auth_does_not_invent_codex_binding(lane):
    assert 'codex_code' not in configure(lane, OPENAI_CODEX_MODEL='fake-selected-gpt').backends


@pytest.mark.parametrize('args', [['-p', 'fake turn'], ['auth', 'login', '--claudeai'],
                                   ['auth', 'status'], ['auth', 'logout']])
def test_claude_profile_cannot_run_a_turn(tmp_path, args):
    from core.worker.contract import _resolve_profile, WorkerUnavailable
    config = {'profiles': {'claude': {'argv': ['/usr/local/bin/claude'], 'network': True}},
              'workspaces': {'scratch': str(tmp_path)}}
    request = dict(profile='claude', workspace='scratch', cwd='', args=args)
    with pytest.raises(WorkerUnavailable, match='CC2'):
        _resolve_profile(config, request)
