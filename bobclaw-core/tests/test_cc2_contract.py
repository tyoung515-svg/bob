"""CC2 admission, CLI options, subscription-only environment and status evidence."""
import json
from types import SimpleNamespace
import pytest
from core.worker.claude_turn_contract import invocation, turn_environment, validate_start, input_frame, MCP_TIMEOUT_MS
from core.worker.contract import peer_lane


def start():
    return {'version': 1, 'model': 'sonnet', 'system': 'FACE SYSTEM',
        'history': [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'answer'},
                    {'role': 'user', 'content': 'new'}],
        'tools': [{'name': 'fs_read', 'description': 'Read', 'inputSchema': {'type': 'object'}}],
        'timeout': 86400, 'network_proxy': None}


def test_fixed_cli_uses_only_documented_options_and_no_default_usage_caps():
    value = start()
    validate_start(value)
    argv = invocation(value, '/private/requests', '/private/responses')
    assert argv[0] == '/usr/local/bin/claude'
    assert argv[argv.index('--tools')+1] == ''
    assert argv[argv.index('--system-prompt')+1] == 'FACE SYSTEM'
    assert argv[argv.index('--setting-sources')+1] == ''
    assert argv[argv.index('--allowedTools')+1] == 'mcp__bob__fs_read'
    assert argv[argv.index('--permission-mode')+1] == 'dontAsk'
    assert not {'--bare', '--resume', '--continue', '--max-budget-usd', '--max-turns', '--fallback-model'} & set(argv)
    settings = json.loads(argv[argv.index('--settings')+1])
    assert settings['disableAllHooks'] is True and settings['enabledPlugins'] == {}
    servers = json.loads(argv[argv.index('--mcp-config')+1])['mcpServers']
    assert set(servers) == {'bob'}
    history = json.loads(input_frame(value['history']))
    assert json.loads(history['message']['content'].split('\n', 1)[1]) == value['history']


def test_environment_is_fresh_and_never_inherits_redirects(monkeypatch):
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
                 'CLAUDE_CODE_USE_FOUNDRY', 'AWS_ACCESS_KEY_ID', 'OPENAI_API_KEY', 'NODE_OPTIONS', 'PYTHONPATH'):
        monkeypatch.setenv(name, 'MUST-NOT-INHERIT')
    env = turn_environment({'claude_home': '/lane/claude'}, '/private', {'HTTPS_PROXY': 'lease'})
    assert 'MUST-NOT-INHERIT' not in json.dumps(env)
    assert env['MCP_TOOL_TIMEOUT'] == str(MCP_TIMEOUT_MS)
    assert env['CLAUDE_CONFIG_DIR'] == '/lane/claude' and env['HOME'] == '/private'


@pytest.mark.parametrize('field,value', [('model', 'other'), ('timeout', float('nan')), ('tools', {}),
    ('history', [{'role': 'system', 'content': 'authority'}])])
def test_wire_rejects_caller_selected_authority_and_bad_input(field, value):
    wire = start(); wire[field] = value
    with pytest.raises(ValueError): validate_start(wire)
    wire = start(); wire['owner'] = 'forged'
    with pytest.raises(ValueError): validate_start(wire)


def test_another_lane_worker_cannot_use_core_turn_socket():
    with pytest.raises(PermissionError): peer_lane(42, {'service_uid': 41, 'lane_id': 'lane'})


def test_tool_adapter_is_worker_only(monkeypatch):
    from core.backends._lc_openai import tool_backend, TOOL_CAPABLE_BACKENDS
    import core.worker
    monkeypatch.setattr(core.worker, 'mode', lambda: 'legacy')
    assert not tool_backend('claude_code')
    monkeypatch.setattr(core.worker, 'mode', lambda: 'worker')
    assert tool_backend('claude_code') and 'claude_code' not in TOOL_CAPABLE_BACKENDS
    assert not tool_backend('claude_pc') and not tool_backend('claude_alternate')


@pytest.mark.asyncio
async def test_no_api_fallback_on_claude_subscription_failure(monkeypatch):
    from core.nodes import execute
    from core.backends.claude_turn import ClaudeTurnError, failure
    from unittest.mock import AsyncMock
    monkeypatch.setattr(execute, 'require_state', lambda s: None)
    monkeypatch.setattr(execute, 'require_backend', lambda *a: None)
    monkeypatch.setattr(execute, 'configured_mode', lambda: True)
    monkeypatch.setattr(execute, 'tool_backend', lambda b: b == 'claude_code')
    monkeypatch.setattr(execute, '_append_agent_turn_event', AsyncMock())
    loop = AsyncMock(side_effect=ClaudeTurnError(failure({'error': {'code': 'usage_limit_reached'}})))
    monkeypatch.setattr(execute, '_run_tool_loop', loop)
    send = AsyncMock()
    monkeypatch.setattr(execute, '_send_to_backend', send)
    result = await execute.execute_node({'backend': 'claude_code', 'model_override': 'sonnet',
        'face_id': 'assistant', 'messages': [], 'escalation_backend': 'claude_api'})
    assert result['error'] == 'model_usage_limit'
    send.assert_not_awaited()


@pytest.fixture(autouse=True)
def _lane_configured_by_bob(monkeypatch):
    # The confined Claude path exists only for a lane configured through Bob's
    # own sign-in; the suite default (conftest) is the legacy roster.
    monkeypatch.delenv('BOB_ROSTER_FILE', raising=False)
