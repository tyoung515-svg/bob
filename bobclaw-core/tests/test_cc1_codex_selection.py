"""Chosen ChatGPT model reaches fresh/resumed Codex calls without provider I/O."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from core import lane_config
from core.backends.codex_code import CodexCodeClient


@pytest.mark.asyncio
@pytest.mark.parametrize('resume', [None, 'fake-thread'])
@pytest.mark.parametrize('selected_model', ['gpt-fake-selected', 'o3-fake-selected'])
async def test_selected_model_is_passed_to_cli(tmp_path, monkeypatch, resume, selected_model):
    monkeypatch.setattr(lane_config, 'configured_mode', lambda: True)
    monkeypatch.setattr(lane_config, 'get_lane', lambda: SimpleNamespace(values={}, backends={
        'codex_code': SimpleNamespace(model=selected_model), 'openai_codex': object()}))
    from core.backends import codex_code
    monkeypatch.setattr(codex_code, 'execution_mode', lambda: 'worker')
    client = CodexCodeClient(cli_path='/usr/local/bin/codex')
    monkeypatch.setattr(client, '_work_dir', lambda: str(tmp_path))
    spawn = AsyncMock(return_value=(b'{"type":"item.completed","item":{"type":"agent_message","text":"fake reply"}}', b'', 0))
    monkeypatch.setattr(client, '_spawn', spawn)
    result = await client.chat(prompt='fake prompt', resume_session_id=resume)
    argv = spawn.await_args.args[0]
    assert argv[argv.index('-m') + 1] == selected_model
    assert 'model_provider=litellm' not in argv
    assert result['text'] == 'fake reply'
