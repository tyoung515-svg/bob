"""The same fixtures run against packaged and app brokers on the Linux gate.

Set BOB_APP_BROKER for a checkout in another location. Missing app sources are
an explicit skip (e.g. image-only gates); an existing older broker must fail.
"""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

pytest.importorskip('fcntl', reason='Both real brokers require POSIX file locking')

BACKEND = Path(__file__).resolve().parents[2]
FIXTURES = json.loads((Path(__file__).with_name('owner_read_broker_fixtures.json')).read_text())
BROKERS = [BACKEND / 'portal-broker/portal-broker.py',
           Path(os.environ.get('BOB_APP_BROKER', str(BACKEND.parent / 'app/portal/portal-broker.py')))]


@pytest.fixture(autouse=True)
def _isolated_gateway_db():
    yield


@pytest.fixture(params=BROKERS, ids=['packaged', 'app'])
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
