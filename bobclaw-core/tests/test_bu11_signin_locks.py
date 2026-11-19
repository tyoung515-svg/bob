"""Real Linux flock/process contracts with a scripted, offline Codex helper."""
import json
import multiprocessing
from pathlib import Path
import sys

import pytest

from test_runtime_model import model

if sys.platform == 'linux':
    import fcntl

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='requires Linux flock and fork')



def invoke(model, argv, result, key=None):
    if key is not None:
        import io
        sys.stdin = type('Input', (), {'buffer': io.BytesIO(key)})()
    sys.argv = ['bob-model', *argv]
    try:
        result.put(('exit', model.main()))
    except BaseException as exc:
        result.put(('exception', type(exc).__name__))


def test_wait_leaves_status_keys_models_defaults_and_other_lanes_available(model, monkeypatch):
    ctx = multiprocessing.get_context('fork')
    root = model.lane_root('luna')
    release = root / 'finish-signin'
    ready = ctx.Event()
    events = ctx.Queue()
    monkeypatch.setattr(model, 'require_root', lambda: None)
    monkeypatch.setattr(model, 'emit', events.put)
    monkeypatch.setattr(model, 'visible_openai_models', lambda root, **kwargs: ['stub-account-default', 'owner-model'])
    # Existing auth lets model selection be exercised while the new login waits.
    home = model.prepare(root)
    (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'old-token'}}))
    codex = Path(model.CODEX)
    codex.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ['CODEX_HOME'])
if sys.argv[1:] == ['login', '--with-api-key']:
    auth = {'OPENAI_API_KEY': sys.stdin.read()}
else:
    print('https://auth.openai.com/codex/device ABCD-EFGHI', flush=True)
    deadline = time.monotonic() + 12
    while not (home.parent / 'finish-signin').exists():
        if time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(.02)
    auth = {'tokens': {'access_token': 'new-token'}}
(home / 'auth.json').write_text(json.dumps(auth))
''')
    def emit(event):
        events.put(event)
        if event.get('event') == 'device_code':
            ready.set()
    monkeypatch.setattr(model, 'emit', emit)
    children = []
    def command(*argv, key=None):
        result = ctx.Queue()
        child = ctx.Process(target=invoke, args=(model, list(argv), result, key))
        children.append(child)
        child.start()
        child.join(2)
        assert not child.is_alive(), f'{argv[0]} blocked during sign-in'
        assert result.get(timeout=1) == ('exit', 0)
    result = ctx.Queue()
    login = ctx.Process(target=invoke, args=(model, ['openai-login', 'luna'], result))
    children.append(login)
    login.start()
    try:
        assert ready.wait(3), 'scripted sign-in did not reach its wait'
        assert events.get(timeout=1)['event'] == 'device_code'
        command('status', 'luna')
        state = events.get(timeout=1)
        assert state['signin_in_progress'] is True
        assert login.is_alive()
        # A duplicate must fail before it can queue for either shared lock.
        import lane
        with open(lane.LOCK_PATH, 'a') as allocation:
            fcntl.flock(allocation, fcntl.LOCK_EX)
            duplicate_result = ctx.Queue()
            duplicate = ctx.Process(target=invoke, args=(model, ['openai-login', 'luna'], duplicate_result))
            children.append(duplicate)
            duplicate.start()
            duplicate.join(2)
            assert not duplicate.is_alive(), 'duplicate waited for the allocation lock'
            assert duplicate_result.get(timeout=1) == ('exit', 1)
        error = events.get(timeout=1)
        assert error == {'event': 'error', 'code': 'signin_in_progress',
                         'detail': model.LOGIN_ERRORS['signin_in_progress']}
        command('openai-models', 'luna')
        assert events.get(timeout=1)['ok']
        command('openai-set-model', 'luna', 'owner-model')
        assert events.get(timeout=1)['selected_model'] == 'owner-model'
        # Exercise a different lane through the same global allocation lock.
        other = root.parents[1] / 'other/bob-memory'
        other.mkdir(parents=True)
        (other / 'lane.env').write_text('BOB_DEFAULT_OWNER="other"\n')
        command('status', 'other')
        assert events.get(timeout=1)['lane'] == 'other'
        # Native pipe reading and the actual key writer must both complete.
        command('set-key', 'luna', 'deepseek', key=b'provider-key')
        assert events.get(timeout=1)['ok']
        command('set-key', 'luna', 'openai', key=b'openai-key')
        assert events.get(timeout=1)['ok']
        command('set-default', 'luna', 'deepseek_v4_flash')
        assert events.get(timeout=1)['ok']
        release.touch()
        login.join(4)
        assert not login.is_alive()
        assert result.get(timeout=1) == ('exit', 0)
        assert events.get(timeout=1)['event'] == 'done'
        state = model.status('luna')
        assert state['signin_in_progress'] is False
        assert state['providers']['deepseek_key'] and state['providers']['openai_key']
        assert state['providers']['openai_signin']
        assert state['default_backend'] == 'deepseek_v4_flash'
        assert state['chatgpt']['selected_model'] == 'owner-model'
        assert model.read_env(root / 'providers.env')['DEEPSEEK_API_KEY'] == 'provider-key'
        assert json.loads((home / 'auth.json').read_text())['OPENAI_API_KEY'] == 'openai-key'
        assert not list(root.glob('.codex-signin-*'))
    finally:
        release.touch(exist_ok=True)
        for child in children:
            child.join(1)
            if child.is_alive():
                child.terminate()
                child.join(2)


def test_shared_lock_order_is_allocation_then_model(model, monkeypatch):
    calls = []
    native = fcntl.flock
    def flock(stream, operation):
        calls.append((Path(stream.name).name, operation))
        native(stream, operation)
    monkeypatch.setattr(model.fcntl, 'flock', flock)
    with model.model_locked('luna'):
        pass
    assert calls == [('allocation.lock', fcntl.LOCK_EX), ('model-luna.lock', fcntl.LOCK_EX)]


def test_catalog_wait_has_no_shared_locks_and_rechecks_credentials(model, monkeypatch):
    root = model.lane_root('luna')
    home = model.prepare(root)
    (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'old'}}))
    import lane
    def catalog(root):
        for lockpath in (lane.LOCK_PATH, model.MODEL_LOCK.format('luna')):
            with open(lockpath, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Simulate a credential replacement while the network call was out.
        (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'new'}}))
        return ['selected']
    monkeypatch.setattr(model, 'visible_openai_models', catalog)
    result = model.set_openai_model('luna', 'selected')
    assert result['error']['code'] == 'chatgpt_models_unavailable'
    assert 'OPENAI_CODEX_MODEL' not in model.read_env(root / 'lane.env')


def test_removed_lane_cannot_be_restarted_by_pending_signin(model, monkeypatch):
    root = model.lane_root('luna')
    def device(root, home):
        (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'new'}}))
        (root / '.disabled').touch()
        return True
    monkeypatch.setattr(model, 'device_login', device)
    monkeypatch.setattr(model, 'visible_openai_models', lambda root, **kwargs: ['selected'])
    assert not model.openai_login('luna')
    assert not (root / 'codex-home/auth.json').exists()
    assert not model.restarts
    assert not model.signin_in_progress('luna')


def test_signin_catalog_is_unlocked_and_final_store_rereads_lane(model, monkeypatch):
    import lane
    root = model.lane_root('luna')
    def device(root, home):
        (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'new-token'}}))
        return True
    def catalog(root, **kwargs):
        for lockpath in (lane.LOCK_PATH, model.MODEL_LOCK.format('luna')):
            with open(lockpath, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with model.model_locked('luna'):
            model.set_key('luna', 'deepseek', 'saved-during-catalog')
            values = model.read_env(root / 'lane.env')
            values['OPENAI_CODEX_MODEL'] = 'owner-model'
            model.save_env(root / 'lane.env', values)
        return ['account-default', 'owner-model']
    monkeypatch.setattr(model, 'device_login', device)
    monkeypatch.setattr(model, 'visible_openai_models', catalog)
    assert model.openai_login('luna')
    state = model.status('luna')
    assert state['providers']['deepseek_key']
    assert state['default_backend'] == 'deepseek_v4_flash'
    assert state['chatgpt']['selected_model'] == 'owner-model'


def test_key_input_wait_holds_neither_shared_lock(model, monkeypatch):
    import lane
    class Input:
        def read(self):
            for lockpath in (lane.LOCK_PATH, model.MODEL_LOCK.format('luna')):
                with open(lockpath, 'a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return b'key-from-pipe'
    monkeypatch.setattr(sys, 'stdin', type('Stdin', (), {'buffer': Input()})())
    monkeypatch.setattr(sys, 'argv', ['bob-model', 'set-key', 'luna', 'deepseek'])
    monkeypatch.setattr(model, 'require_root', lambda: None)
    assert model.main() == 0
    assert model.status('luna')['providers']['deepseek_key']


@pytest.mark.parametrize('use_pipe', [False, True])
def test_lane_password_wait_is_outside_allocation_lock(model, monkeypatch, use_pipe):
    import lane
    def password(*args):
        with open(lane.LOCK_PATH, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return 'owner-password\r\n' if use_pipe else 'owner-password'
    def add(name, value):
        assert name == 'new-lane' and value == 'owner-password'
        with open(lane.LOCK_PATH, 'a') as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(lane, 'require_root', lambda: None)
    monkeypatch.setattr(lane.getpass, 'getpass', password)
    monkeypatch.setattr(sys, 'stdin', type('Input', (), {'read': password})())
    monkeypatch.setattr(lane, 'add', add)
    monkeypatch.setattr(sys, 'argv', ['bob-lane', 'add', 'new-lane'] + (['--password-stdin'] if use_pipe else []))
    assert lane.main() == 0
