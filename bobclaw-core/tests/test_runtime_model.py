"""Runtime model setup with a local Codex stub; no credentials or network."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parents[2] / 'deploy/runtime/lib'


@pytest.fixture
def model(tmp_path, monkeypatch):
    from core import worker as boundary
    monkeypatch.setattr(boundary, 'EXECUTION_BOUNDARY', tmp_path / 'absent-execution-boundary')
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'legacy')
    saved = {key: sys.modules.get(key) for key in ('common', 'lane', 'model')}
    try:
        for name in saved:
            spec = importlib.util.spec_from_file_location(name, RUNTIME / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        module = sys.modules['model']
        monkeypatch.setattr(module, 'FAMILY', tmp_path / 'family')
        monkeypatch.setattr(module.pwd, 'getpwnam', lambda name: SimpleNamespace(
            pw_uid=os.getuid(), pw_gid=os.getgid()))
        root = tmp_path / 'family/luna/bob-memory'
        root.mkdir(parents=True)
        (root / 'lane.env').write_text('BOB_DEFAULT_OWNER="luna"\n')
        monkeypatch.setattr(module, 'visible_openai_models', lambda root, **kwargs: ['stub-account-default'])
        monkeypatch.setattr(sys.modules['lane'], 'LOCK_PATH', str(tmp_path / 'allocation.lock'))
        monkeypatch.setattr(module, 'MODEL_LOCK', str(tmp_path / 'model-{}.lock'))
        monkeypatch.setattr(module, 'SIGNIN_LOCK', str(tmp_path / 'signin-{}.lock'))
        module.restarts = []
        monkeypatch.setattr(module, 'systemctl', lambda *args: module.restarts.append(args))
        stub = tmp_path / 'codex'
        stub.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ['CODEX_HOME'])
if sys.argv[1:] == ['login', '--with-api-key']:
    key = sys.stdin.read()
    if key == 'reject-key':
        print(key, file=sys.stderr)
        sys.exit(1)
    auth = {'OPENAI_API_KEY': key}
elif sys.argv[1:] == ['login', '--device-auth']:
    print('\\x1b[1mhttps://auth.openai.com/codex/device\\x1b[0m', file=sys.stderr, flush=True)
    print('ABCD-EFGHI', file=sys.stderr, flush=True)
    time.sleep(0.1)
    auth = {'tokens': {'access_token': 'stub-token'}}
else:
    sys.exit(2)
(home / 'auth.json').write_text(json.dumps(auth))
''')
        stub.chmod(0o755)
        monkeypatch.setattr(module, 'CODEX', str(stub))
        from core import lane_config
        monkeypatch.setattr(lane_config.shutil, 'which', lambda binary: str(stub) if binary == 'codex' else None)
        module.real_codex_options = module.codex_options
        # The test runner is not root and cannot call setgroups. Test the
        # privilege-drop arguments separately; run the stub as the current user.
        monkeypatch.setattr(module, 'codex_options', lambda root: {
            k: v for k, v in module.real_codex_options(root).items()
            if k not in {'user', 'group', 'extra_groups'}})
        yield module
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def test_status_exact_fresh_and_default_without_credential(model):
    assert model.status('luna') == {'lane': 'luna', 'providers': {
        'openai_signin': False, 'openai_key': False, 'anthropic_key': False,
        'deepseek_key': False, 'google_key': False, 'glm_key': False,
        'kimi_key': False, 'qwen_key': False}, 'default_backend': None, 'ready': False,
        'signin_in_progress': False,
        'claude': {'signed_in': False, 'selected_model': None, 'last_checked': None,
                   'model_selection_required': False},
        'chatgpt': {'signed_in': False, 'selected_model': None, 'last_refresh': None,
                    'model_selection_required': False}}
    with pytest.raises(ValueError, match='needs provider'):
        model.set_default(model.lane_root('luna'), 'gemini_flash')
    assert model.status('luna')['ready'] is False


@pytest.mark.parametrize('provider,backend', [('anthropic', 'claude_api'),
    ('deepseek', 'deepseek_v4_flash'), ('google', 'gemini_flash'), ('openai', 'codex_code'),
    ('glm', 'glm_5_3'), ('kimi', 'kimi_code'), ('qwen', 'qwen_cloud')])
def test_set_key_is_private_lane_scoped_and_ready(model, provider, backend, capsys):
    key = 'stub-key-$HOME-"quoted"-\\-end'
    result = model.set_key('luna', provider, key)
    assert result == {'ok': True, 'default_backend': backend}
    state = model.status('luna')
    assert state['ready'] and state['providers'][provider + '_key']
    root = model.lane_root('luna')
    path = root / ('codex-home/auth.json' if provider == 'openai' else 'providers.env')
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_uid == os.getuid()
    assert model.read_env(root / 'lane.env')['CODEX_HOME'] == str(root / 'codex-home')
    assert model.restarts == [('restart', 'bob-core@luna.service')]
    assert not (root / 'providers.env').exists() if provider == 'openai' else True
    captured = capsys.readouterr()
    assert key not in captured.out + captured.err


def test_second_key_keeps_default_and_existing_keys(model):
    model.set_key('luna', 'deepseek', 'stub-one')
    model.set_key('luna', 'google', 'stub-two')
    state = model.status('luna')
    assert state['default_backend'] == 'deepseek_v4_flash'
    assert state['providers']['google_key'] and state['providers']['deepseek_key']
    with pytest.raises(ValueError, match='needs provider'):
        model.set_default(model.lane_root('luna'), 'claude')
    assert model.status('luna')['ready']


@pytest.mark.parametrize('raw,expected', [('key\n', 'key'), ('key\r\n', 'key'),
                                        ('key', 'key'), (' key ', ' key ')])
def test_key_line_endings(model, raw, expected):
    assert model.read_key(io.StringIO(raw)) == expected


@pytest.mark.parametrize('raw', ['', '\n', '\r\n', 'key\n\n', 'key\r', 'a\x00b',
                                  'a\tb', 'a\x7fb', 'a\x85b', 'a\u2028b', 'a\u202eb'])
def test_key_refusals(model, raw):
    with pytest.raises(ValueError):
        model.read_key(io.StringIO(raw))


@pytest.mark.parametrize('raw,expected', [(b'key\r\n', 'key'), (b'key\n', 'key'), (b'key', 'key')])
def test_key_binary_input_preserves_exact_line_ending_rules(model, raw, expected):
    assert model.read_key(io.BytesIO(raw)) == expected


def test_lone_carriage_return_on_real_stdin_is_refused(model):
    with pytest.raises(ValueError):
        model.read_key(io.BytesIO(b'key\r'))


def test_device_login_streams_code_before_auth_saved(model, monkeypatch):
    events = []
    def receive(event):
        events.append(event)
        if event['event'] == 'device_code':
            assert not (model.lane_root('luna') / 'codex-home/auth.json').exists()
    monkeypatch.setattr(model, 'emit', receive)
    assert model.openai_login('luna')
    assert events == [{'event': 'device_code', 'url': 'https://auth.openai.com/codex/device',
                       'code': 'ABCD-EFGHI'}, {'event': 'done', 'ok': True,
                       'chatgpt': {'signed_in': True, 'selected_model': 'stub-account-default',
                                   'last_refresh': None, 'model_selection_required': False}}]
    assert model.status('luna')['providers']['openai_signin']
    assert model.status('luna')['default_backend'] == 'openai_codex'
    assert model.status('luna')['ready']


def test_device_login_failure_and_timeout(model, monkeypatch):
    events = []
    monkeypatch.setattr(model, 'emit', events.append)
    monkeypatch.setattr(model, 'CODEX', '/missing-codex-stub')
    assert not model.openai_login('luna')
    assert events[-1]['event'] == 'error'
    assert model.status('luna')['default_backend'] is None


def test_login_deadline_kills_child(model, monkeypatch):
    events = []
    monkeypatch.setattr(model, 'emit', events.append)
    monkeypatch.setattr(model, 'LOGIN_TIMEOUT', 0.01)
    assert not model.openai_login('luna')
    assert events[-1]['event'] == 'error'
    assert 'timed out' in events[-1]['detail']
    assert not model.status('luna')['ready']


def test_openai_reject_does_not_print_key_or_change_default(model, capsys):
    with pytest.raises(ValueError, match='output withheld'):
        model.set_key('luna', 'openai', 'reject-key')
    assert model.status('luna')['default_backend'] is None
    assert model.restarts == []
    output = capsys.readouterr()
    assert 'reject-key' not in output.out + output.err


def test_child_runs_as_bob_without_inheriting_provider_secrets(model):
    options = model.real_codex_options(model.lane_root('luna'))
    assert options['user'] == os.getuid() and options['group'] == os.getgid()
    assert options['extra_groups'] == [] and options['umask'] == 0o077
    assert set(options['env']) == {'PATH', 'HOME', 'CODEX_HOME'}


@pytest.mark.parametrize('name', ['../dad', 'missing', 'Dad'])
def test_invalid_or_unknown_lane(model, name):
    with pytest.raises((ValueError, FileNotFoundError)):
        model.status(name)


def test_status_ignores_other_lane_and_host_credentials(model, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'host-must-not-count')
    monkeypatch.setenv('CODEX_HOME', '/some/other/lane')
    assert not model.status('luna')['providers']['anthropic_key']
    assert not model.status('luna')['providers']['openai_signin']


def test_set_default_accepts_core_names_but_does_not_invent_credentials(model):
    from core.config import KNOWN_BACKENDS
    assert KNOWN_BACKENDS <= model.SUPPORTED_BACKENDS
    root = model.lane_root('luna')
    with pytest.raises(ValueError, match='needs provider'):
        model.set_default(root, 'minimax')
    assert not model.status('luna')['ready']
    model.save_env(root / 'providers.env', {'MINIMAX_API_KEY': 'stub-key'})
    model.save_env(root / 'lane.env', {'BOB_LANE_BACKENDS': 'minimax', 'MINIMAX_MODEL': 'test-model'})
    model.set_default(root, 'minimax')
    assert model.status('luna')['ready']
    with pytest.raises(ValueError, match='unsupported'):
        model.set_default(root, 'not-a-backend')


def test_device_parser_prefers_device_url_over_help_link(model):
    assert model.device_code('https://openai.com/help\nhttps://auth.openai.com/codex/device\nABCD-EFGHI') == {
        'event': 'device_code', 'url': 'https://auth.openai.com/codex/device', 'code': 'ABCD-EFGHI'}
