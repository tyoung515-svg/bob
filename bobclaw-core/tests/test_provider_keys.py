"""Offline adapter/config/catalog contracts; every HTTP/model call is a fake."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import lane_config, settings_client
from core.config import config, MAX_WORKER_USD_BY_BACKEND, MAX_FANOUT_WIDTH_BY_BACKEND
from core.backends.glm import GLMClient
from core.backends.kimi import KimiClient
from core.backends.qwen_cloud import QwenCloudClient


@pytest.fixture(autouse=True)
def mock_redis():
    yield  # No execute-module import or optional model SDKs for pure contracts.


def test_live_rotations_and_aggregator_credentials_stay_separate(monkeypatch):
    current = ['first']
    monkeypatch.setattr(settings_client, 'provider_key', lambda provider, fallback: provider + '-' + current[0])
    clients = [GLMClient(api_key='old'), KimiClient(api_key='old'), QwenCloudClient(api_key='old')]
    assert [c.api_key for c in clients] == ['glm-first', 'kimi-first', 'qwen-first']
    current[0] = 'rotated'
    assert [c.api_key for c in clients] == ['glm-rotated', 'kimi-rotated', 'qwen-rotated']
    assert GLMClient(api_key='aggregator-only', vault_provider=None).api_key == 'aggregator-only'


def test_qwen_payload_defaults_overrides_and_transport_config(monkeypatch):
    monkeypatch.setattr(config, 'QWEN_MODEL', 'configured-model')
    monkeypatch.setattr(config, 'QWEN_BASE_URL', 'https://configured.invalid/v1/')
    client = QwenCloudClient(api_key='inert')
    assert client.base_url == 'https://configured.invalid/v1'
    assert client._prepare_payload(model=None, messages=[], stream=False, temperature=0.2) == {
        'model': 'configured-model', 'messages': [], 'stream': False, 'temperature': 0.2}
    assert client._prepare_payload(model='owner-model', messages=[], stream=True)['model'] == 'owner-model'
    assert 'qwen_cloud' in MAX_WORKER_USD_BY_BACKEND
    assert 'qwen_cloud' in MAX_FANOUT_WIDTH_BY_BACKEND
    assert lane_config.BACKEND_FIELDS['qwen_cloud'] == ('DASHSCOPE_API_KEY', 'QWEN_MODEL')
    setup = lane_config.provider_setup()
    assert setup['glm']['model'] == config.ZAI_MODEL
    assert setup['kimi']['model'] == config.KIMI_MODEL
    assert setup['kimi']['backend'] == 'kimi_code'
    assert 'qwen_research' not in {setup[p]['backend'] for p in ('glm', 'kimi', 'qwen')}


@pytest.mark.asyncio
async def test_qwen_actual_sync_and_stream_dispatch_preserve_model_override(monkeypatch):
    # Compile the real dispatch functions without importing unrelated optional
    # LangChain SDKs. Adapter methods below are local fakes, never HTTP calls.
    source = Path(__file__).parents[1] / 'core/nodes/execute.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    names = {'_default_send_to_backend', '_default_stream_to_backend'}
    nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    namespace = {'require_backend': lambda *a: None}
    unit = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), str(source), 'exec'), namespace)
    chat = AsyncMock(return_value={'choices': [{'message': {'content': 'answer'}}]})
    seen = []
    async def stream(self, messages, model=None):
        seen.append((messages, model))
        yield 'part'
    monkeypatch.setattr(QwenCloudClient, 'chat', chat)
    monkeypatch.setattr(QwenCloudClient, 'stream_chat', stream)
    messages = [{'role': 'user', 'content': 'offline'}]
    assert await namespace['_default_send_to_backend'](messages, 'qwen_cloud', 'owner-model') == 'answer'
    assert chat.await_args.kwargs == {'messages': messages, 'model': 'owner-model'}
    assert [d async for d in namespace['_default_stream_to_backend'](messages, 'qwen_cloud', 'owner-model')] == ['part']
    assert seen == [(messages, 'owner-model')]


def test_public_catalog_refreshes_without_restart_and_never_reads_vault(monkeypatch):
    monkeypatch.setenv('BOB_SETTINGS_ID', 'a'*12)
    monkeypatch.setenv('BOB_SERVICE_ROLE', 'gateway')
    rows = [[]]
    def read_env(path):
        assert path.name == 'public.env'
        return {'BOB_PUBLIC_BACKENDS': json.dumps(rows[0])}
    def forbidden(*a):
        raise AssertionError('public catalog must not read credentials')
    monkeypatch.setattr(lane_config, 'read_env', read_env)
    monkeypatch.setattr(settings_client, 'vault_key', forbidden)
    assert lane_config.get_lane().backends == {}
    rows[0] = [dict(backend='qwen_cloud', kind='cloud', model='configured', models=['configured'], available=True)]
    assert lane_config.get_lane().backends['qwen_cloud'].available
    assert lane_config.get_lane().values.get('DASHSCOPE_API_KEY') is None


def test_lc_configs_use_latest_keys_without_substituting_aggregator(monkeypatch):
    pytest.importorskip('langchain_openai')
    from core.backends import _lc_openai as lc
    monkeypatch.setattr(settings_client, 'provider_key', lambda provider, fallback: provider + '-rotated')
    monkeypatch.setattr(lc, 'ChatOpenAI', lambda **kwargs: SimpleNamespace(**kwargs))
    assert lc.build_chat_openai('glm_5_3').api_key == 'glm-rotated'
    assert lc.build_chat_openai('qwen_cloud').api_key == 'qwen-rotated'
    assert lc.build_chat_openai('glm_5_3_flash_or').api_key == config.OPENROUTER_API_KEY
