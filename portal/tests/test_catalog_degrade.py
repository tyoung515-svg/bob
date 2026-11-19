"""op_catalog: a gateway that answers with HTTP errors is reachable, not unreachable.

Observed 2026-11-01 on the candidate: /faces, /models/available and
/backends/catalog returned 500 (core pointed at a closed port) and /capabilities
returned 403, and the broker reported `transport_unavailable`, which the app shows
as "server unreachable" and uses to discard a successful hello.
"""
import importlib.util
from pathlib import Path

import pytest

BROKER = Path(__file__).resolve().parents[1] / 'portal-broker.py'


@pytest.fixture
def broker_module(tmp_path, monkeypatch):
    monkeypatch.setenv('BOB_PORTAL_STATE_DIR', str(tmp_path))
    spec = importlib.util.spec_from_file_location('portal_broker_catalog_under_test', BROKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Transport:
    def __init__(self, module, outcomes):
        self.module = module
        self.outcomes = {'/agent-services/catalog?contract_version=2': {}, **outcomes}

    def rest(self, method, path, params=None, body=None, timeout=None, headers=None):
        outcome = self.outcomes[path]
        if isinstance(outcome, str):
            raise self.module.BrokerError(outcome, 'detail')
        return outcome


def make(module, outcomes):
    identity = {'sub': 'dad', 'gateway': 'http://127.0.0.1:1', 'faces': []}
    return module.Broker(Transport(module, outcomes), identity, sink=None)


PATHS = ('/faces', '/models/available', '/backends/catalog', '/capabilities')


def test_all_http_errors_return_an_empty_catalogue_with_the_codes(broker_module):
    broker = make(broker_module, {
        '/faces': 'gateway_http_500', '/models/available': 'gateway_http_500',
        '/backends/catalog': 'gateway_http_500', '/capabilities': 'gateway_http_403'})
    reply = broker.op_catalog({})
    assert reply['faces'] == [] and reply['models'] == [] and reply['backends'] == []
    warnings = reply['capabilities']['warnings']
    assert 'faces: gateway_http_500' in warnings
    assert 'capabilities: gateway_http_403' in warnings
    assert any('no catalogue loaded' in item for item in warnings)


def test_all_transport_failures_still_raise(broker_module):
    broker = make(broker_module, {path: 'transport_unavailable' for path in PATHS})
    with pytest.raises(broker_module.BrokerError) as caught:
        broker.op_catalog({})
    assert caught.value.code == 'transport_unavailable'


def test_one_transport_failure_among_http_errors_still_raises(broker_module):
    outcomes = {path: 'gateway_http_500' for path in PATHS}
    outcomes['/capabilities'] = 'transport_unavailable'
    broker = make(broker_module, outcomes)
    with pytest.raises(broker_module.BrokerError) as caught:
        broker.op_catalog({})
    assert caught.value.code == 'transport_unavailable'


def test_a_partial_catalogue_is_unchanged(broker_module):
    outcomes = {path: 'gateway_http_500' for path in PATHS}
    outcomes['/faces'] = [{'id': 'assistant', 'name': 'Assistant'}]
    broker = make(broker_module, outcomes)
    reply = broker.op_catalog({})
    assert [row['id'] for row in reply['faces']] == ['assistant']
    assert not any('no catalogue loaded' in item for item in reply['capabilities']['warnings'])
