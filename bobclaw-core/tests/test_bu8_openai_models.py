"""BU8 account model selection and catalog contracts; no account/provider access."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import lane_config as lc
from core.backends.openai_codex_models import visible_model_slugs, enrolled_models
from core.backends.openai_codex_auth import CodexProviderError
from test_runtime_model_configured import runtime, mock_redis, tmp_path


def signed_in(runtime):
    model, _, root = runtime
    home = model.prepare(root)
    (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'fake-private-access',
        'refresh_token': 'fake-private-refresh', 'account_id': 'fake-private-account'},
        'last_refresh': '2026-11-17T12:34:56Z'}))
    return model, root


def test_ranking_default_priority_hidden_duplicates_and_bound():
    slugs = visible_model_slugs({'models': [
        {'slug': 'hidden', 'visibility': 'hidden', 'is_default': True},
        {'slug': 'second', 'priority': 1}, {'slug': 'first', 'is_default': True, 'priority': 50},
        {'slug': 'second', 'priority': 0}, {'slug': 'invalid\nname'}, {'slug': 'third'}]})
    assert slugs == ['first', 'second', 'third']
    many = ['default'] + ['model-' + str(i) for i in range(20)]
    assert enrolled_models(many, 'model-19') == tuple(many[:11] + ['model-19'])
    for data in ({}, {'models': []}, {'models': [{'slug': 'hidden', 'visibility': 'hide'}]}):
        with pytest.raises(CodexProviderError): visible_model_slugs(data)


def test_list_is_sanitized_and_read_only(runtime):
    model, root = signed_in(runtime)
    before = {p: p.read_bytes() for p in (root / 'lane.env', root / 'codex-home/auth.json')}
    listing = model.openai_models('new-lane')
    assert listing == {'ok': True, 'lane': 'new-lane', 'backend': 'openai_codex',
        'signed_in': True, 'selected_model': None, 'last_refresh': '2026-11-17T12:34:56Z',
        'model_selection_required': True, 'account_default': 'account-default',
        'models': [{'slug': s, 'account_default': i == 0, 'selected': False}
                   for i, s in enumerate(['account-default', 'gpt-6.1-sol', 'account-cheaper'])]}
    assert {p: p.read_bytes() for p in before} == before
    assert model.restarts == []
    serialized = json.dumps(listing)
    for word in ('fake-private', 'access_token', 'refresh_token', 'account_id', 'Authorization'):
        assert word not in serialized


def test_choose_validates_live_list_and_preserves_lane_default(runtime):
    model, root = signed_in(runtime)
    model.set_key('new-lane', 'deepseek', 'fake-key')
    before = (root / 'lane.env').read_bytes()
    restarts = list(model.restarts)
    refused = model.set_openai_model('new-lane', 'not-in-account')
    assert not refused['ok'] and refused['error']['code'] == 'chatgpt_model_not_visible'
    assert (root / 'lane.env').read_bytes() == before and model.restarts == restarts
    selected = model.set_openai_model('new-lane', 'gpt-6.1-sol')
    assert selected['ok'] and selected['selected_model'] == 'gpt-6.1-sol'
    assert [r['slug'] for r in selected['models'] if r['selected']] == ['gpt-6.1-sol']
    assert model.status('new-lane')['default_backend'] == 'deepseek_v4_flash'
    assert model.restarts[-1] == ('restart', 'bob-core@new-lane.service')
    lane = lc.read_lane(root)
    assert lane.require('openai_codex', 'account-cheaper').model == 'gpt-6.1-sol'
    with pytest.raises(ValueError): lane.require('openai_codex', 'not-in-account')
    assert lane.backends['codex_code'].model is None


def test_completion_chooses_account_default_and_preserves_explicit_model(runtime):
    model, root = signed_in(runtime)
    state = model.complete_openai_setup(root)
    assert state['selected_model'] == 'account-default' and not state['model_selection_required']
    model.set_openai_model('new-lane', 'gpt-6.1-sol')
    assert model.complete_openai_setup(root)['selected_model'] == 'gpt-6.1-sol'
    assert model.status('new-lane')['chatgpt']['last_refresh'] == '2026-11-17T12:34:56Z'


def test_unreadable_catalog_keeps_signin_and_requires_model(runtime, monkeypatch):
    model, root = signed_in(runtime)
    def unavailable(root): raise CodexProviderError('fake-private-provider-body')
    monkeypatch.setattr(model, 'visible_openai_models', unavailable)
    state = model.complete_openai_setup(root)
    assert state['signed_in'] and state['model_selection_required'] and state['selected_model'] is None
    assert 'choose' in state['model_notice']
    row = lc.read_lane(root).backends['openai_codex'].row()
    assert 'choose a model' in row['label'] and row['models'] == []
    before = (root / 'lane.env').read_bytes()
    result = model.set_openai_model('new-lane', 'gpt-6.1-sol')
    assert result['error']['code'] == 'chatgpt_models_unavailable'
    assert 'fake-private' not in json.dumps(result)
    assert (root / 'lane.env').read_bytes() == before and model.restarts == []


def test_signed_out_status_and_missing_refresh_timestamp(runtime, monkeypatch):
    model, _, root = runtime
    assert model.status('new-lane')['chatgpt'] == {'signed_in': False, 'selected_model': None,
        'last_refresh': None, 'model_selection_required': False}
    monkeypatch.setattr(model, 'visible_openai_models', lambda root: pytest.fail('no request without sign-in'))
    assert model.openai_models('new-lane')['error']['code'] == 'chatgpt_signin_required'
    home = model.prepare(root)
    (home / 'auth.json').write_text(json.dumps({'tokens': {'refresh_token': 'fake-private'},
                                              'last_refresh': 'fake-private'}))
    assert model.status('new-lane')['chatgpt']['last_refresh'] is None


async def test_catalog_definitions_and_workflow_bindings_have_identity_and_family(runtime, monkeypatch):
    from core.roster import AgentServices
    from core.execution.dispatcher import exact_models
    from core.execution.workflows import WorkflowRuntime, review_facts
    from core.backends._lc_openai import TOOL_CAPABLE_BACKENDS
    from core.nodes.execute import _is_tool_enabled
    model, root = signed_in(runtime)
    model.complete_openai_setup(root)
    values = model.read_env(root / 'lane.env')
    values['CODEX_MODEL'] = 'concrete-cli-coding-model'
    model.save_env(root / 'lane.env', values)
    model.set_key('new-lane', 'deepseek', 'fake-key')
    lane = lc.read_lane(root)
    monkeypatch.delenv('BOB_ROSTER_FILE', raising=False)
    monkeypatch.setenv('BOB_LANE_ROOT', str(root))
    monkeypatch.setattr(lc, 'get_lane', lambda: lane)
    rows = [b.row() for b in lane.backends.values()]
    gpt = next(r for r in rows if r['backend'] == 'openai_codex')
    assert gpt['model'] == 'account-default' and gpt['tool_capable']
    assert gpt['family'] == 'openai' and gpt['model_identity'] == 'openai/account-default'
    definitions = AgentServices.model_validate(lc.agent_service_definitions(rows))
    specs = {key: spec.as_supplied() for key, spec in definitions.models.items()}
    assert any(s['backend'] == 'codex_code' and s['model'] == 'concrete-cli-coding-model' for s in specs.values())
    assert 'openai_codex' in TOOL_CAPABLE_BACKENDS and 'codex_code' not in TOOL_CAPABLE_BACKENDS
    assert not _is_tool_enabled('assistant', 'codex_code')
    runtime = WorkflowRuntime(SimpleNamespace(models=AsyncMock(return_value=exact_models(specs))))
    bindings = await runtime.bindings(None)
    assert not any(b['backend'] == 'codex_code' for b in bindings)
    gpts = [b for b in bindings if b['backend'] == 'openai_codex']
    assert {b['model'] for b in gpts} == {'account-default', 'gpt-6.1-sol', 'account-cheaper'}
    assert all(b['tool_capable'] and b['runnable'] and not b['reasons'] for b in gpts), gpts
    assert all(b['family'] == 'openai' and b['model_identity'] == 'openai/' + b['model'] for b in gpts)
    assert all('reported_model_names' not in b for b in gpts)
    deepseek = next(b for b in bindings if b['backend'] == 'deepseek_v4_flash')
    definition = {'review_policy': 'different_family', 'stages': [
        {'id': 'build', 'kind': 'agent', 'agent': 'builder', 'depends_on': []},
        {'id': 'review', 'kind': 'agent', 'agent': 'reviewer', 'review_of': 'build', 'depends_on': ['build']}]}
    assert review_facts(definition, {'builder': deepseek, 'reviewer': gpts[0]})['policy_met']
    assert not review_facts(definition, {'builder': gpts[1], 'reviewer': gpts[0]})['policy_met']


def test_readme_command_examples_and_provider_list():
    text = (Path(__file__).resolve().parents[2] / 'deploy/runtime/README.md').read_text(encoding='utf-8')
    for command in ('bob-model openai-models <lane>', 'bob-model openai-set-model <lane> <slug>'):
        assert command in text
    assert 'gpt-mini' not in text
    assert 'real sign-in completion not yet run' in text


@pytest.mark.parametrize('pin', [None, 'gpt-6.1-sol'])
async def test_workflow_origin_uses_conversation_pin_or_selected_default(runtime, monkeypatch, pin):
    import uuid
    from core.execution.models import Authority
    from core.execution.workflows import WorkflowRuntime
    model, root = signed_in(runtime)
    model.complete_openai_setup(root)
    model.set_default(root, 'openai_codex')
    lane = lc.read_lane(root)
    monkeypatch.delenv('BOB_ROSTER_FILE', raising=False)
    monkeypatch.setenv('BOB_LANE_ROOT', str(root))
    monkeypatch.setattr(lc, 'get_lane', lambda: lane)
    monkeypatch.setattr('core.execution.api.check_resources', AsyncMock())
    specs = lc.agent_service_definitions([b.row() for b in lane.backends.values()])['models']
    runtime = WorkflowRuntime(SimpleNamespace(store=SimpleNamespace(pool=object()),
        models=AsyncMock(return_value=specs)))
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value={
        'face_id': 'assistant', 'backend_preference': 'openai_codex', 'model_preference': pin}))
    result = await runtime.resolve(connection, Authority('lane', 'owner', 'owner', {}, 'portal'),
        {'inputs': {}, 'origin_conversation_id': str(uuid.uuid4())},
        {'inputs': {}, 'workspace': {'required': False}, 'authority': {'checks': []}, 'stages': []}, 'unused')
    assert result['assistant_binding']['model'] == (pin or 'account-default')
    assert result['assistant_binding']['face_id'] == 'assistant'


@pytest.mark.parametrize('command,slug,exit_code', [
    ('openai-models', None, 0), ('openai-set-model', 'gpt-6.1-sol', 0),
    ('openai-set-model', 'not-visible', 1)])
def test_cli_argv_json_and_exit_contract(runtime, monkeypatch, capsys, command, slug, exit_code):
    import builtins
    import sys
    model, root = signed_in(runtime)
    monkeypatch.setattr(model, 'require_root', lambda: None)
    monkeypatch.setattr(sys.modules['common'], 'writer_guard', lambda name: None)
    monkeypatch.setattr(sys.modules['lane'], 'LOCK_PATH', root / 'allocation.lock')
    monkeypatch.setattr(model.fcntl, 'flock', lambda *args: None, raising=False)
    monkeypatch.setattr(model.fcntl, 'LOCK_EX', 2, raising=False)
    monkeypatch.setattr(model, 'open', lambda name, *args, **kwargs:
        builtins.open(root / 'model.lock' if str(name).startswith('/run/') else name, *args, **kwargs),
        raising=False)
    monkeypatch.setattr(sys, 'argv', ['bob-model', command, 'new-lane'] + ([slug] if slug else []))
    assert model.main() == exit_code
    output = json.loads(capsys.readouterr().out)
    assert output['ok'] == (exit_code == 0)
    assert output['backend'] == 'openai_codex' and output['lane'] == 'new-lane'
    if command == 'openai-models':
        assert output['selected_model'] is None and model.restarts == []
    elif exit_code == 0:
        assert output['selected_model'] == slug and len(model.restarts) == 1
    else:
        assert output['error']['code'] == 'chatgpt_model_not_visible' and model.restarts == []
