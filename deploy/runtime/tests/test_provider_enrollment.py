"""Hermetic journal/enrollment contracts; no root, systemd or provider calls.

The store simulates atomic publication and crash injection. Real no-follow,
permissions and mount enforcement remain in test_settings/test_provisioning.
"""
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import struct
from types import SimpleNamespace
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def enrolled(monkeypatch, tmp_path_factory):
    if os.name == 'nt':
        base = ROOT / 'test-db' / ('enrollment-' + uuid.uuid4().hex)
        base.mkdir(parents=True, mode=0o777)
        monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(LOCK_EX=2, flock=lambda *a: None))
        monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(
            getpwnam=lambda _: SimpleNamespace(pw_uid=1, pw_gid=1)))
    else:
        base = tmp_path_factory.mktemp('enrollment')
    monkeypatch.syspath_prepend(str(ROOT / 'bobclaw-core'))
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/runtime/lib'))
    modules = {}
    for name in ('common', 'lane', 'model', 'settings'):
        spec = importlib.util.spec_from_file_location(name, ROOT / f'deploy/runtime/lib/{name}.py')
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    common, model, settings = (modules[n] for n in ('common', 'model', 'settings'))
    from core import lane_config, settings_client
    monkeypatch.delenv('BOB_SETTINGS_ID', raising=False)
    monkeypatch.setattr(common, 'writer_guard', lambda _: None)
    monkeypatch.setattr(model, 'execution_mode', lambda: 'legacy')
    monkeypatch.setattr(model.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=1, pw_gid=1))
    monkeypatch.setattr(model, 'private_replace', lambda path, content, *a: path.write_text(content))
    monkeypatch.setattr(model, 'FAMILY', base)
    monkeypatch.setattr(modules['lane'], 'LOCK_PATH', str(base / 'allocation.lock'))
    native_open = open
    monkeypatch.setattr(model, 'open', lambda path, mode: native_open(
        base / 'model.lock' if str(path).startswith('/run/bob-model-') else path, mode), raising=False)
    root = base / 'test/bob-memory'
    root.mkdir(parents=True)
    # Real root lane.env has WORKER_ID; generated role envs add SETTINGS_ID.
    (root / 'lane.env').write_text('BOB_WORKER_ID="aaaaaaaaaaaa"\nBOB_WORKER_UID="1"\n')
    cfg = dict(lane='test', lane_id='a'*12, gateway_uid=1, broker_uid=2, service_gid=3)
    store = {'journal': {}, 'vault': {}}
    @contextmanager
    def directory(path):
        yield 'vault' if Path(path).name == 'vault' else 'journal'
    monkeypatch.setattr(settings, 'directory', directory)
    monkeypatch.setattr(settings, 'locked', directory)
    def load(fd, name):
        try:
            return deepcopy(store[fd][name])
        except KeyError:
            raise FileNotFoundError(name) from None
    def publish(fd, name, value, **kwargs):
        if fd == 'vault':
            assert kwargs == {'gid': 3, 'mode': 0o640}
        store[fd][name] = deepcopy(value)
    monkeypatch.setattr(settings, 'load', load)
    monkeypatch.setattr(settings, 'publish', publish)
    monkeypatch.setattr(settings, 'journals', lambda fd: [deepcopy(v) for k, v in store[fd].items()
                                                        if k.startswith('op-')])
    def vault_key(provider, lane_id, fallback):
        assert lane_id == cfg['lane_id']
        return store['vault'].get(provider + '.json', {}).get('key', fallback)
    monkeypatch.setattr(settings_client, 'vault_key', vault_key)
    restarts = []
    monkeypatch.setattr(model, 'systemctl', lambda *a: restarts.append(a))
    monkeypatch.setattr(settings, 'config', lambda _: cfg)
    monkeypatch.setattr(settings.socket, 'SO_PEERCRED', 17, raising=False)
    return SimpleNamespace(settings=settings, model=model, root=root, cfg=cfg,
                           store=store, restarts=restarts, lane_config=lane_config)


def put(h, provider='deepseek', request_key='once', key='fake-key'):
    return h.settings.handle(h.cfg, h.cfg['gateway_uid'], dict(version=1, op='secrets.set',
        provider=provider, key=key, request_key=request_key))


def status(h, request_key='once', readonly=False):
    return h.settings.handle(h.cfg, h.cfg['broker_uid' if readonly else 'gateway_uid'],
        dict(version=1, op='status', request_key=request_key), readonly=readonly)


def connection(h, request, *, readonly=False, on_send=lambda _: None):
    """Socket transport fake; runs the real serve/handle/journal sequence."""
    class Connection:
        def settimeout(self, timeout):
            assert timeout == 10

        def getsockopt(self, *args):
            return struct.pack('3i', 100, h.cfg['broker_uid' if readonly else 'gateway_uid'], 100)

        def makefile(self, mode):
            assert mode == 'rb'
            return io.BytesIO(json.dumps(request).encode() + b'\n')

        def sendall(self, payload):
            on_send(json.loads(payload))
            self.reply = json.loads(payload)

    return Connection()


def secret_request(provider='deepseek', request_key='once'):
    return dict(version=1, op='secrets.set', provider=provider, key='fake-key', request_key=request_key)


@pytest.mark.parametrize('provider', ['anthropic', 'deepseek', 'google', 'glm', 'kimi', 'qwen'])
def test_store_enrolls_preset_with_no_duplicate_env_key(enrolled, provider):
    h = enrolled
    result = put(h, provider)
    spec = h.model.SETUP[provider]
    lane = h.lane_config.read_lane(h.root)
    assert set(lane.backends) == {spec['backend']}
    assert lane.backends[spec['backend']].model == spec['model']
    assert h.model.status('test')['ready']
    assert h.model.status('test')['providers'][provider + '_key']
    assert lane.default_backend == spec['backend']
    assert spec['backend'] in h.model.read_env(h.root / 'lane.env')['BOB_LANE_BACKENDS']
    assert not (h.root / 'providers.env').exists()
    assert h.restarts == [('restart', 'bob-core@test.service')]
    assert status(h, readonly=True) == dict(operation_id=result['operation_id'], phase='ready', result=result)
    assert 'fake-key' not in json.dumps(h.store['journal'])
    with pytest.raises(h.settings.SettingsError, match='request_key_used'):
        put(h, provider)


def test_second_key_preserves_default_and_explicit_model(enrolled):
    h = enrolled
    put(h, 'anthropic')
    put(h, 'deepseek', 'second')
    values = h.model.read_env(h.root / 'lane.env')
    values['DEEPSEEK_MODEL'] = 'owner-chosen'
    h.model.save_env(h.root / 'lane.env', values)
    before = list(h.restarts)
    put(h, 'deepseek', 'rotation', 'fake-rotation')
    lane = h.lane_config.read_lane(h.root)
    assert lane.default_backend == 'claude_api'
    assert lane.backends['deepseek_v4_flash'].model == 'owner-chosen'
    assert lane.values['DEEPSEEK_API_KEY'] == 'fake-rotation'
    assert set(lane.backends) == {'claude_api', 'deepseek_v4_flash'}
    assert h.restarts == before
    assert status(h, 'rotation', readonly=True)['phase'] == 'ready'


def test_vault_entry_cannot_activate_additional_paid_tiers(enrolled):
    h = enrolled
    values = h.model.read_env(h.root / 'lane.env')
    values.update(BOB_LANE_BACKENDS='deepseek_v4,gemini_pro,glm_5_3_flash_or,kimi_platform',
                  DEEPSEEK_PRO_MODEL='pro-choice', GEMINI_PRO_MODEL='pro-choice',
                  OPENROUTER_GLM_FLASH_MODEL='aggregator-choice', KIMI_PLATFORM_MODEL='payg-choice')
    h.model.save_env(h.root / 'lane.env', values)
    for provider in ('deepseek', 'google', 'glm', 'kimi', 'qwen'):
        put(h, provider, provider)
    assert set(h.lane_config.read_lane(h.root).backends) == {
        h.model.SETUP[p]['backend'] for p in ('deepseek', 'google', 'glm', 'kimi', 'qwen')}


@pytest.mark.parametrize('boundary', ['pointer', 'preset', 'default', 'restart', 'ready-receipt'])
@pytest.mark.parametrize('resume', ['status', 'reconcile', 'next-key'])
def test_committed_failure_resumes_without_replaying_credential(enrolled, monkeypatch, boundary, resume):
    h = enrolled
    hit = [False]
    def fail_once():
        if not hit[0]:
            hit[0] = True
            raise OSError('fake-key')
    if boundary in {'pointer', 'ready-receipt'}:
        original = h.settings.publish
        def publish(fd, name, value, **kwargs):
            original(fd, name, value, **kwargs)
            if (name == 'deepseek.json' if boundary == 'pointer' else value.get('phase') == 'ready'):
                fail_once()
        monkeypatch.setattr(h.settings, 'publish', publish)
    elif boundary in {'preset', 'default'}:
        target = 'save_preset' if boundary == 'preset' else 'set_default'
        original = getattr(h.model, target)
        def step(*a, **kw):
            result = original(*a, **kw)
            fail_once()
            return result
        monkeypatch.setattr(h.model, target, step)
    else:
        original = h.model.systemctl
        def restart(*a):
            fail_once()
            original(*a)
        monkeypatch.setattr(h.model, 'systemctl', restart)
    with pytest.raises(h.settings.SettingsError, match='settings_failed'):
        put(h)
    committed = deepcopy(h.store['vault'])
    pending = status(h, readonly=True)
    assert pending['phase'] in {'committed', 'restart-pending', 'ready'}
    if resume == 'reconcile':
        h.settings.recover_all(h.cfg)
    elif resume == 'next-key':
        put(h, 'google', 'next')
    else:
        status(h)
    assert status(h, readonly=True)['phase'] == 'ready'
    assert h.model.status('test')['ready']
    assert all(h.store['vault'][k] == v for k, v in committed.items())
    assert h.restarts and all(a == ('restart', 'bob-core@test.service') for a in h.restarts)


def test_native_key_entry_uses_same_vault_and_rotates_app_key(enrolled):
    h = enrolled
    put(h)
    result = h.model.set_key('test', 'deepseek', 'native-rotation')
    assert result == {'ok': True, 'default_backend': 'deepseek_v4_flash'}
    assert h.store['vault']['deepseek.json']['key'] == 'native-rotation'
    assert h.lane_config.read_lane(h.root).values['DEEPSEEK_API_KEY'] == 'native-rotation'
    assert not (h.root / 'providers.env').exists()


def test_socket_replies_with_enrollment_written_before_grace_and_restart(enrolled, monkeypatch):
    h = enrolled
    events = []
    def sent(result):
        assert set(result) == {'provider', 'stored', 'fingerprint', 'operation_id'}
        assert status(h, readonly=True) == dict(operation_id=result['operation_id'], phase='restart-pending')
        assert h.lane_config.read_lane(h.root).default_backend == 'deepseek_v4_flash'
        assert not h.restarts
        events.append('reply')
    def grace(seconds):
        assert seconds == h.settings.RESTART_GRACE_SECONDS == 1.5
        assert status(h, readonly=True)['phase'] == 'restart-pending'
        events.append('grace')
    def restart(*args):
        assert events == ['reply', 'grace']
        assert status(h, readonly=True)['phase'] == 'restart-pending'
        events.append('restart')
        h.restarts.append(args)
    monkeypatch.setattr(h.settings.time, 'sleep', grace)
    monkeypatch.setattr(h.model, 'systemctl', restart)
    conn = connection(h, secret_request(), on_send=sent)
    h.settings.serve(h.cfg, conn)
    assert events == ['reply', 'grace', 'restart']
    assert h.restarts == [('restart', 'bob-core@test.service')]
    assert status(h, readonly=True)['phase'] == 'ready'


@pytest.mark.parametrize('resume', ['status', 'startup', 'next-key'])
def test_crash_after_reply_resumes_restart_debt(enrolled, monkeypatch, resume):
    h = enrolled
    class Crash(BaseException):
        pass
    def crash(_):
        raise Crash()
    monkeypatch.setattr(h.settings.time, 'sleep', crash)
    conn = connection(h, secret_request())
    with pytest.raises(Crash):
        h.settings.serve(h.cfg, conn)
    assert conn.reply['stored']
    committed = deepcopy(h.store['vault'])
    assert status(h, readonly=True)['phase'] == 'restart-pending'
    assert not h.restarts
    monkeypatch.setattr(h.settings.time, 'sleep', lambda _: None)
    if resume == 'startup':
        h.settings.recover_all(h.cfg)
    else:
        request = (dict(version=1, op='status', request_key='once') if resume == 'status'
                   else secret_request('google', 'next'))
        def sent(result):
            assert not h.restarts
            assert status(h, readonly=True)['phase'] == 'restart-pending'
            if resume == 'status':
                assert result == dict(operation_id=conn.reply['operation_id'], phase='restart-pending')
        h.settings.serve(h.cfg, connection(h, request, on_send=sent))
    assert status(h, readonly=True)['phase'] == 'ready'
    assert h.restarts
    assert all(h.store['vault'][k] == v for k, v in committed.items())


def test_post_reply_restart_failure_stays_pending_until_control_resume(enrolled, monkeypatch):
    h = enrolled
    monkeypatch.setattr(h.settings.time, 'sleep', lambda _: None)
    original = h.model.systemctl
    def failed(*args):
        raise OSError('fake-key')
    monkeypatch.setattr(h.model, 'systemctl', failed)
    conn = connection(h, secret_request())
    h.settings.serve(h.cfg, conn)
    assert conn.reply['stored'] and 'error' not in conn.reply
    assert status(h, readonly=True)['phase'] == 'restart-pending'
    assert not h.restarts
    monkeypatch.setattr(h.model, 'systemctl', original)
    assert status(h)['phase'] == 'ready'
    assert h.restarts == [('restart', 'bob-core@test.service')]


def test_failed_send_leaves_restart_for_resume(enrolled, monkeypatch):
    h = enrolled
    def failed(_):
        raise OSError('closed')
    def unexpected_sleep(_):
        pytest.fail('grace must follow successful sendall')
    monkeypatch.setattr(h.settings.time, 'sleep', unexpected_sleep)
    h.settings.serve(h.cfg, connection(h, secret_request(), on_send=failed))
    assert not h.restarts
    assert status(h, readonly=True)['phase'] == 'restart-pending'
    h.settings.recover_all(h.cfg)
    assert status(h, readonly=True)['phase'] == 'ready'


def test_socket_rotation_needs_neither_grace_nor_restart(enrolled, monkeypatch):
    h = enrolled
    put(h)
    before = list(h.restarts)
    def unexpected_sleep(_):
        pytest.fail('rotation must not wait for a restart')
    monkeypatch.setattr(h.settings.time, 'sleep', unexpected_sleep)
    conn = connection(h, secret_request(request_key='rotation'))
    h.settings.serve(h.cfg, conn)
    assert conn.reply['stored']
    assert h.restarts == before
    assert status(h, 'rotation', readonly=True)['phase'] == 'ready'


def test_receipt_socket_observes_pending_without_restart(enrolled, monkeypatch):
    h = enrolled
    deferred = []
    put_request = secret_request()
    h.settings.handle(h.cfg, h.cfg['gateway_uid'], put_request, after_reply=deferred)
    def unexpected_sleep(_):
        pytest.fail('receipt socket must never schedule a restart')
    monkeypatch.setattr(h.settings.time, 'sleep', unexpected_sleep)
    conn = connection(h, dict(version=1, op='status', request_key='once'), readonly=True)
    h.settings.serve(h.cfg, conn, readonly=True)
    assert conn.reply['phase'] == 'restart-pending'
    assert not h.restarts


def test_native_first_key_restarts_before_return_and_rotation_does_not(enrolled, monkeypatch):
    h = enrolled
    original = h.model.systemctl
    def restart(*args):
        journal = next(v for k, v in h.store['journal'].items() if k.startswith('op-'))
        assert journal['phase'] == 'restart-pending'
        original(*args)
    monkeypatch.setattr(h.model, 'systemctl', restart)
    result = h.model.set_key('test', 'deepseek', 'native-key')
    assert result == {'ok': True, 'default_backend': 'deepseek_v4_flash'}
    assert h.restarts == [('restart', 'bob-core@test.service')]
    assert all(v['phase'] == 'ready' for v in h.store['journal'].values())
    h.model.set_key('test', 'deepseek', 'native-rotation')
    assert h.restarts == [('restart', 'bob-core@test.service')]
    assert all(v['phase'] == 'ready' for v in h.store['journal'].values())


def test_worker_reload_decision_uses_core_env_content(enrolled, monkeypatch):
    h = enrolled
    monkeypatch.setattr(h.model, 'execution_mode', lambda: 'worker')
    def save_env(path, values):
        # Simulate worker's role environment generation; root metadata is not
        # itself a reason to restart if the core's loaded bytes stay identical.
        path.write_text(h.model.render_env(values))
        core = {k: v for k, v in values.items() if k != 'ROOT_METADATA'}
        (h.root / 'core.env').write_text(h.model.render_env(core))
    monkeypatch.setattr(h.model, 'save_env', save_env)
    put(h)
    before = (h.root / 'core.env').read_bytes()
    values = h.model.read_env(h.root / 'lane.env')
    values['ROOT_METADATA'] = 'changed'
    (h.root / 'lane.env').write_text(h.model.render_env(values))
    put(h, request_key='rotation', key='rotated')
    assert (h.root / 'core.env').read_bytes() == before
    assert h.restarts == [('restart', 'bob-core@test.service')]
    assert status(h, 'rotation', readonly=True)['phase'] == 'ready'


def test_old_pending_journal_retains_restart_debt_when_env_already_written(enrolled):
    h = enrolled
    result = put(h)
    h.restarts.clear()
    operation = 'op-' + result['operation_id'] + '.json'
    value = h.store['journal'][operation]
    value['phase'] = 'restart-pending'
    value.pop('restart_required')
    value.pop('enrollment_complete')
    h.settings.recover_all(h.cfg)
    assert h.restarts == [('restart', 'bob-core@test.service')]
    assert status(h, readonly=True)['phase'] == 'ready'
