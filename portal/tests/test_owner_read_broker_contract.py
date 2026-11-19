"""App broker contract using the backend's unchanged shared S2B fixtures.

Linux/WSL only: imports the real broker and its real POSIX locking dependency.
The backend companion test runs the same fixtures against both broker copies.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

pytest.importorskip('fcntl', reason='Both real brokers require POSIX file locking')

PORTAL = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((Path(__file__).with_name('owner_read_broker_fixtures.json')).read_text())
BROKERS = [PORTAL / 'portal-broker.py']


@pytest.fixture(params=BROKERS, ids=['app'])
def module(request, tmp_path, monkeypatch):
    path = request.param
    if not path.is_file():
        pytest.skip(f'Broker source absent: {path}')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    name = '_s2b_broker_' + path.parent.parent.name
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize('fixture', FIXTURES, ids=lambda f: f['name'])
def test_shared_owner_read_contract(module, fixture):
    calls = []
    def rest(method, path, **kwargs):
        calls.append([method, path, kwargs.get('body')])
        return fixture.get('response', {})
    broker = module.Broker(SimpleNamespace(rest=rest), {'sub': 'orchid-7'}, SimpleNamespace())
    frame = {'id': 1, 'op': fixture['op'], 'args': fixture['args']}
    if 'error' in fixture:
        with pytest.raises(module.BrokerError) as raised:
            broker.handle(frame)
        assert raised.value.code == fixture['error']
        assert calls == []
        assert module._error_frame(1, raised.value)['error']['code'] == fixture['error']
    else:
        result = broker.handle(frame)
        assert result.value == fixture['response']
        assert calls == [fixture['call']]


def test_discovery_and_typed_expiry(module):
    operations = {'memory.list': {'version': 1, 'implemented': True, 'readable': True,
                                  'writable': False, 'runnable': False}}
    def rest(method, path, **kwargs):
        if path == '/capabilities':
            return {'operations': operations}
        raise module.BrokerError('gateway_http_410', 'snapshot_expired')
    broker = module.Broker(SimpleNamespace(rest=rest), {'sub': 'orchid-7'}, SimpleNamespace())
    assert broker.op_hello({})['operations'] == operations
    with pytest.raises(module.BrokerError) as raised:
        broker.handle({'id': 1, 'op': 'memory.list', 'args': {'cursor': 'opaque'}})
    assert raised.value.code == 'snapshot_expired'


def test_older_server_unavailable(module):
    def rest(*args, **kwargs):
        raise module.BrokerError('gateway_http_404', '404: Not Found')
    broker = module.Broker(SimpleNamespace(rest=rest), {'sub': 'orchid-7'}, SimpleNamespace())
    assert broker.op_hello({})['operations'] == {}
    with pytest.raises(module.BrokerError) as raised:
        broker.handle({'id': 1, 'op': 'files.roots', 'args': {}})
    assert raised.value.code == 'unavailable'


@pytest.mark.parametrize('code', [
    'snapshot_expired', 'invalid_cursor', 'not_found', 'scope_denied',
    'file_too_large', 'directory_too_large', 'revision_conflict',
    'snapshot_capacity', 'snapshot_too_large', 'lane_unavailable',
    'projects_unavailable', 'memory_unavailable', 'invalid_path', 'invalid_args',
])
def test_typed_owner_read_errors_survive_the_envelope(module, code):
    def rest(*args, **kwargs):
        raise module.BrokerError('gateway_http_400', code)
    broker = module.Broker(SimpleNamespace(rest=rest), {'sub': 'orchid-7'}, SimpleNamespace())
    with pytest.raises(module.BrokerError) as raised:
        broker.handle({'id': 7, 'op': 'files.read',
                       'args': {'attachment_id': 'opaque', 'path': 'file'}})
    frame = module._error_frame(7, raised.value)
    assert frame == {'id': 7, 'ok': False, 'error': {'code': code, 'detail': code}}


def test_portal_identity_still_requires_gateway_authentication_and_same_subject(module):
    claims = {'sub': 'orchid-7', 'exp': module.time.time() + 300, 'token_type': 'portal'}
    transport = SimpleNamespace(rest=lambda *a, **kw: {'authenticated': True, 'user': 'orchid-7'})
    assert module.bind_identity(transport, claims, 'http://127.0.0.1:7836')['sub'] == 'orchid-7'
    for status, code in [({'authenticated': False}, 'token_rejected'),
                         ({'authenticated': True, 'user': 'foreign'}, 'identity_mismatch')]:
        transport.rest = lambda *a, **kw: status
        with pytest.raises(module.BrokerError) as raised:
            module.bind_identity(transport, claims, 'http://127.0.0.1:7836')
        assert raised.value.code == code
    with pytest.raises(module.BrokerError) as raised:
        module.bind_identity(transport, dict(claims, token_type='admin'), 'http://127.0.0.1:7836')
    assert raised.value.code == 'token_type_unsupported'
