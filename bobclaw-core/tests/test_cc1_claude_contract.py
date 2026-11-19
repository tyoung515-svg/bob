"""Offline CC1 contracts. All credentials/codes are obvious nonfunctional fakes."""
import json
import os
from pathlib import Path
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from core.worker import claude


@pytest.mark.parametrize('url', [
    'http://claude.com/cai/oauth/authorize?state=FAKE',
    'https://claude.com.evil.invalid/cai/oauth/authorize',
    'https://claude.com@evil.invalid/', 'https://user@claude.com/',
    'https://claude.com:443/', 'https://claude.com/#fragment',
    'https://claude.com\\@evil.invalid/', 'https://other.invalid/',
    'https://claude.com/' + 'a' * 8192,
])
def test_refuses_unsafe_urls(url):
    assert claude.signin_url('Visit: ' + url) is None


def test_preserves_complete_query_without_decoding():
    url = 'https://claude.com/cai/oauth/authorize?state=FAKE%2BSTATE&scope=user%3Ainference'
    assert claude.signin_url('Visit: ' + url + '\n') == url
    assert claude.signin_url('https://claude.ai/oauth?state=FAKE')


@pytest.mark.parametrize('raw', [b'', b'FAKE', b'\n', b'FAKE CODE\n', b'FAKE\x00CODE\n',
    b'FAKE\rCODE\n', b'FAKE\nSECOND\n', b'x' * 4097 + b'\n', b'\xff\n'])
def test_code_bounds(raw):
    with pytest.raises(ValueError):
        claude.code_line(raw)


def test_code_allows_manual_state_suffix():
    assert claude.code_line(b'FAKE-CODE#FAKE_STATE\r\n') == b'FAKE-CODE#FAKE_STATE\n'
    assert claude.code_line(b'x' * 4096 + b'\n') == b'x' * 4096 + b'\n'


@pytest.mark.parametrize('method,provider,expected', [
    ('claude.ai', 'firstParty', True), ('api_key', 'firstParty', False),
    ('oauth_token', 'firstParty', False), ('claude.ai', 'bedrock', False),
    ('none', 'firstParty', False),
])
def test_status_sanitizes_subscription_only(method, provider, expected):
    raw = json.dumps(dict(loggedIn=True, authMethod=method, apiProvider=provider,
                         configDirectory='/FAKE/PRIVATE', token='FAKE-SECRET'))
    assert claude.status_json(raw) == {'signed_in': expected, 'logged_out': False}
    assert 'FAKE' not in json.dumps(claude.status_json(raw))


@pytest.mark.parametrize('raw', ['[]', '{}', '{', 'x' * 65537, '{"loggedIn":"true"}'])
def test_status_refuses_malformed_output(raw):
    with pytest.raises(ValueError):
        claude.status_json(raw)


def test_profile_environment_and_exclusion():
    from core.worker.contract import child_env, PROFILES
    from core.worker.client import EXCLUDED
    from core.worker.profile_network import PROFILES as NETWORK
    assert 'claude' in PROFILES & NETWORK
    assert {'claude_pc', 'claude_alternate'} <= EXCLUDED
    assert 'claude_code' not in EXCLUDED
    env = child_env({'claude_home': '/lane/cli/claude'}, '/private-job', claude=True)
    assert env['CLAUDE_CONFIG_DIR'] == '/lane/cli/claude'
    assert env['HOME'] == '/private-job'
    assert env['DISABLE_AUTOUPDATER'] == env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] == '1'
    assert env['ENABLE_CLAUDEAI_MCP_SERVERS'] == 'false'
    assert not {'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'USERPROFILE'} & env.keys()


def test_packaged_pin_and_upgrade_inventory():
    import importlib.util
    runtime = Path(__file__).parents[2] / 'deploy/runtime'
    image = (runtime / 'Containerfile').read_text()
    assert 'ARG CLAUDE_VERSION=2.1.327' in image
    assert '343c744baa14bfd38286a7cb90871051f04379956da6c5b803a9e66019a76ac9' in image
    assert 'fetch-checked "$CLAUDE_URL" "$CLAUDE_SHA256"' in image
    spec = importlib.util.spec_from_file_location('cc1_payload', runtime / 'lib/upgrade_payload.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.owned('/usr/local/bin/claude')
    assert not module.owned('/usr/local/bin/arbitrary')


@pytest.mark.asyncio
async def test_claude_setup_policy_exact_hosts_and_no_optional_traffic():
    from core.builder.egress_proxy import Lease
    from core.builder.network import DEFAULTS
    hosts = {'api.anthropic.com', 'claude.ai', 'claude.com', 'platform.claude.com'}
    assert set(DEFAULTS['profiles']['claude']) == hosts
    lease = Lease(None, None, time.time() + 30, profile='claude', setup=True)
    for host in hosts:
        assert await lease.event(dict(type='policy', domain=host, method='CONNECT')) == {'decision': 'allow'}
        assert await lease.event(dict(type='policy', domain=host, method='POST')) == {'decision': 'deny'}
    for host in ('downloads.claude.ai', 'mcp-proxy.anthropic.com', 'example.com', 'sub.claude.ai'):
        assert await lease.event(dict(type='policy', domain=host, method='CONNECT')) == {'decision': 'deny'}
    lease.setup_deadline = time.monotonic() - 1
    with pytest.raises(ValueError):
        await lease.event(dict(type='policy', domain='claude.ai', method='CONNECT'))


# The following run actual subprocess/selectors/flock/dirfd behavior on Linux.
linux = pytest.mark.skipif(sys.platform != 'linux', reason='requires Linux subprocess and dirfd boundary')
from test_runtime_model import model


@pytest.fixture
def claude_runtime(model, monkeypatch):
    root = model.lane_root('luna')
    stub = root / 'fake-claude'
    stub.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
config = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
mode = (config.parent / 'scenario').read_text() if (config.parent / 'scenario').exists() else 'success'
credential = config / '.credentials.json'
args = sys.argv[1:]
if args == ['auth', 'status']:
    signed = credential.exists()
    print(json.dumps(dict(loggedIn=signed, authMethod='claude.ai' if signed else 'none',
                         apiProvider='firstParty', configDirectory='/FAKE/PRIVATE', token='FAKE-SECRET')))
    sys.exit(0 if signed else 1)
if args == ['auth', 'logout']:
    credential.unlink(missing_ok=True)
    sys.exit(0)
if mode == 'early':
    print('FAKE private failure', flush=True)
    sys.exit(1)
url = 'https://claude.com/cai/oauth/authorize?state=FAKE%2BSTATE&scope=user%3Ainference'
# Force multiple writes, including a split query, to exercise parser buffering.
sys.stdout.write('Visit: ' + url[:60]); sys.stdout.flush(); time.sleep(.03)
print(url[60:], flush=True)
print('Paste code here if prompted > ', end='', flush=True)
code = sys.stdin.readline()
if mode == 'timeout':
    time.sleep(10)
if mode == 'wrong':
    print('Authentication failed: Invalid authorization code ' + code, flush=True)
    sys.exit(1)
if not code:
    sys.exit(1)
credential.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'FAKE-ACCESS', 'refreshToken': 'FAKE-REFRESH'}}))
(config / '.claude.json').write_text('{}')
print('Login successful.', flush=True)
''')
    stub.chmod(0o755)
    monkeypatch.setattr(model, 'CLAUDE', str(stub))
    def prepare(root):
        config = model.claude_config(root)
        config.mkdir(parents=True, exist_ok=True)
        return config
    monkeypatch.setattr(model, 'prepare_claude', prepare)
    @contextmanager
    def options(root, config, deadline):
        home = config / 'home'
        home.mkdir(exist_ok=True)
        yield dict(env=claude.environment(config, home), cwd=home, start_new_session=True)
    monkeypatch.setattr(model, 'claude_setup_options', options)
    prepare(root)
    return model, root


@linux
@pytest.mark.parametrize('scenario,expected', [
    ('success', None), ('wrong', 'signin_code_rejected'), ('early', 'signin_failed'),
    ('timeout', 'signin_timeout'), ('cancel', 'signin_cancelled'),
])
def test_scripted_login_and_no_secret_output(claude_runtime, monkeypatch, scenario, expected, capsys):
    import fcntl
    import lane
    model, root = claude_runtime
    (root / 'scenario').write_text(scenario)
    reader, writer = os.pipe()
    source = os.fdopen(reader, 'rb', buffering=0)
    input_file = SimpleNamespace(buffer=source)
    monkeypatch.setattr(sys, 'stdin', input_file)
    events = []
    def receive(event):
        events.append(event)
        if event['event'] == 'signin_link':
            assert event['url'].endswith('state=FAKE%2BSTATE&scope=user%3Ainference')
            for lockpath in (lane.LOCK_PATH, model.MODEL_LOCK.format('luna')):
                with open(lockpath, 'a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert model.signin_in_progress('luna')
            if scenario != 'cancel':
                os.write(writer, b'FAKE-OWNER-CODE#FAKE-STATE\n' +
                         (b'FAKE-SECOND-LINE\n' if scenario == 'success' else b''))
            os.close(writer)
    monkeypatch.setattr(model, 'emit', receive)
    if scenario == 'timeout':
        monkeypatch.setattr(model, 'LOGIN_TIMEOUT', .3)
    try:
        assert model.claude_login('luna') is (expected is None)
        if scenario == 'success':
            assert source.read() == b'FAKE-SECOND-LINE\n'
    finally:
        source.close()
        if scenario == 'early':
            os.close(writer)
    assert not list(root.glob('.claude-signin-*'))
    assert not model.signin_in_progress('luna')
    text = json.dumps(events) + str(capsys.readouterr())
    assert 'FAKE-OWNER-CODE' not in text and 'FAKE-ACCESS' not in text and '/FAKE/PRIVATE' not in text
    if expected:
        assert events[-1]['code'] == expected
        assert not (model.claude_config(root) / '.credentials.json').exists()
    else:
        assert events[-1] == {'event': 'done', 'ok': True}
        assert model.claude_status(root)['signed_in']
        assert model.claude_status(root)['model_selection_required']
        assert model.set_claude_model('luna', 'opus')['ok']
        assert model.claude_status(root)['selected_model'] == 'opus'
        assert not model.set_claude_model('luna', 'not-declared')['ok']
        assert model.claude_logout('luna')['ok']
        assert model.claude_status(root)['selected_model'] is None


@linux
def test_stale_homes_deleted_without_following_links(model, tmp_path):
    root = model.lane_root('luna')
    outside = tmp_path / 'keep'
    outside.mkdir()
    (outside / 'sentinel').write_text('keep')
    for prefix in ('codex', 'claude'):
        home = root / ('.' + prefix + '-signin-abcdefgh')
        home.mkdir()
        (home / 'link').symlink_to(outside, target_is_directory=True)
        (home / 'auth.lock').write_text('FAKE')
    ignored = root / '.claude-signin-other-pattern!'
    ignored.mkdir()
    with model.signin_locked('luna'):
        model.cleanup_signin_homes(root)
    assert (outside / 'sentinel').read_text() == 'keep' and ignored.is_dir()
    assert not (root / '.codex-signin-abcdefgh').exists()
    bad = root / '.claude-signin-abcdefgh'
    bad.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        model.cleanup_signin_homes(root)
    assert (outside / 'sentinel').exists()


@linux
def test_checked_credential_refuses_symlink_and_hardlink(model, tmp_path):
    root = model.lane_root('luna')
    config = tmp_path / 'config'
    config.mkdir()
    source = tmp_path / 'fake-auth'
    source.write_text('{"claudeAiOauth":{"accessToken":"FAKE"}}')
    path = config / '.credentials.json'
    path.symlink_to(source)
    with pytest.raises((OSError, ValueError)):
        model.checked_claude_auth(root, config)
    path.unlink()
    os.link(source, path)
    with pytest.raises(ValueError):
        model.checked_claude_auth(root, config)


@linux
@pytest.mark.parametrize('number', ['SIGTERM', 'SIGHUP', 'SIGINT'])
def test_cancellation_handlers_cover_temp_home_lifetime(model, number):
    import signal
    root = model.lane_root('luna')
    with pytest.raises(KeyboardInterrupt):
        with model.login_cancellation(), model.claude_signin_home(root):
            signal.raise_signal(getattr(signal, number))
    assert not list(root.glob('.claude-signin-*'))


@linux
def test_claude_duplicate_is_immediate_and_not_queued(model, monkeypatch):
    events = []
    monkeypatch.setattr(model, 'emit', events.append)
    monkeypatch.setattr(model, 'model_locked', lambda name: pytest.fail('duplicate took shared lock'))
    with model.signin_locked('luna'):
        assert not model.claude_login('luna')
    assert events == [dict(event='error', code='signin_in_progress',
                           detail='A sign-in is already in progress for this lane.')]
