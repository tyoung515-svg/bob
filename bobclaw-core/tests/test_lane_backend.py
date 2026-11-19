"""Face fallback is opt-in and only applies when both credentials are absent."""
import pytest

from core import teams
from core.config import config
from core.faces.registry import Face
from core.lane_backend import preferred_for_lane


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.delenv('BOB_LANE_DEFAULT_BACKEND', raising=False)
    monkeypatch.setattr(config, 'DEEPSEEK_API_KEY', '')
    monkeypatch.setattr(config, 'MINIMAX_API_KEY', '')
    monkeypatch.setattr(teams, 'get_active_team', lambda: None)


def test_unset_is_identical_and_does_not_probe(monkeypatch):
    import core.lane_backend as lane
    monkeypatch.setattr(lane, 'has_credential', lambda b: pytest.fail('unset must not probe'))
    assert preferred_for_lane('deepseek_v4_flash', 'minimax') == 'deepseek_v4_flash'


@pytest.mark.parametrize('default,expected', [('codex_code', 'codex_code'),
    ('claude', 'claude_api'), ('gemini_flash', 'gemini_flash')])
async def test_face_resolver_uses_lane_default(monkeypatch, default, expected):
    monkeypatch.setenv('BOB_LANE_DEFAULT_BACKEND', default)
    face = Face(id='t', name='Test', system_prompt='p',
                preferred_backend='deepseek_v4_flash', escalation_backend='minimax')
    assert await teams.resolve(None, face=face) == expected


@pytest.mark.parametrize('key', ['DEEPSEEK_API_KEY', 'MINIMAX_API_KEY'])
def test_either_credential_preserves_existing_preference(monkeypatch, key):
    monkeypatch.setenv('BOB_LANE_DEFAULT_BACKEND', 'codex_code')
    monkeypatch.setattr(config, key, 'stub-key')
    assert preferred_for_lane('deepseek_v4_flash', 'minimax') == 'deepseek_v4_flash'


async def test_explicit_team_remains_authoritative(monkeypatch):
    monkeypatch.setenv('BOB_LANE_DEFAULT_BACKEND', 'codex_code')
    face = Face(id='t', name='Test', system_prompt='p', role='worker',
                preferred_backend='deepseek_v4_flash', escalation_backend='minimax')
    monkeypatch.setattr(teams, '_team_role_cfg', lambda *a: {
        'backend': 'local', 'escalation_chain': []})
    assert await teams.resolve('worker', face=face, team='explicit') == 'local'


def test_unconfigured_cli_face_uses_default(monkeypatch, tmp_path):
    monkeypatch.setenv('BOB_LANE_DEFAULT_BACKEND', 'gemini_flash')
    monkeypatch.setattr(config, 'CODEX_HOME', str(tmp_path / 'codex-home'))
    monkeypatch.setattr(config, 'CC_CONFIG_DIR', str(tmp_path / 'claude-home'))
    assert preferred_for_lane('codex_code', 'claude_code') == 'gemini_flash'
