"""S3e app-copy settings tests. Run in Linux/WSL (real fcntl required).

The fake settings service models single-use admission; filesystem/peer/atomic
commit enforcement belongs to the backend's real Linux helper fixtures.
"""
import json
import io
from pathlib import PurePosixPath
import socket
import stat
import struct
import sys
from types import SimpleNamespace

import pytest

from test_owner_session_broker import setup as owner_setup, confirm

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="requires Linux fcntl; run in WSL")


@pytest.fixture
def settings(owner_setup):
    mod, broker, state, session, stream, _ = owner_setup
    session['allowed_operations'] += ['secrets.set', 'policy.get', 'policy.set']
    confirm(broker)
    old_rest = broker._rest
    caps = {op: {'contract_versions': [2], 'implemented': True, 'available': True,
                 'providers': ['fixture-provider'], 'storage_only': True}
            for op in ('secrets.set', 'policy.get', 'policy.set')}

    def catalog(method, path, **kwargs):
        result = old_rest(method, path, **kwargs)
        if path == '/capabilities':
            result['operations'].update(caps)
        return result

    broker._rest = catalog
    used = set()
    state.update(settings_calls=[], mode='success')

    def service(method, path, **kwargs):
        body = kwargs.get('body', {})
        state['settings_calls'].append((method, path))  # never log key bodies
        if path != '/settings/secrets':
            return {'policy': None, 'revision': 0, 'immutable_restrictions': {}}
        if body['request_key'] in used:
            raise mod.BrokerError('gateway_http_409', 'request_key_used')
        used.add(body['request_key'])
        if state['mode'] == 'echo_error':
            raise RuntimeError(body['key'])
        if state['mode'] == 'expiry':
            raise mod.BrokerError('gateway_http_401', 'owner_session_invalid')
        return {'provider': body['provider'], 'stored': state['mode'] != 'not_stored',
                'fingerprint': '0123456789abcdef', 'operation_id': 'receipt-1',
                'accidental_echo': body['key']}

    broker._owner_transport.rest = service
    return mod, broker, state, stream, caps


def args(**changes):
    return dict(contract_version=2, provider='fixture-provider', key='SYNTHETIC-KEY-CANARY',
                request_key='one-use', **changes)


def test_only_safe_receipt_no_key_in_reply_hello_ledger_logs(settings, tmp_path, capsys):
    mod, broker, state, stream, _ = settings
    touched = []
    broker._ledger_remember = lambda *a: touched.append(a)
    reply = broker.handle({'id': 1, 'op': 'secrets.set', 'args': args()})
    result = broker.op_secrets_set({**args(), 'request_key': 'second-use'})
    assert set(result) == {'provider', 'stored', 'fingerprint', 'operation_id'}
    assert result['stored'] is True
    broker.sink.reply(result)
    broker.sink.reply(broker.op_hello({}))
    captured = capsys.readouterr()
    text = stream.getvalue().decode() + captured.out + captured.err
    assert 'SYNTHETIC-KEY-CANARY' not in text
    assert not touched
    assert not list(tmp_path.rglob('*.json'))


@pytest.mark.parametrize('mode,code', [('echo_error', 'settings_unavailable'),
                                     ('not_stored', 'invalid_receipt'),
                                     ('expiry', 'owner_session_invalid')])
def test_failure_scrubbing_and_no_automatic_retry(settings, mode, code, capsys):
    mod, broker, state, stream, _ = settings
    state['mode'] = mode
    with pytest.raises(mod.BrokerError) as error:
        broker.op_secrets_set(args())
    assert error.value.code == code
    assert len(state['settings_calls']) == 1
    assert 'SYNTHETIC-KEY-CANARY' not in json.dumps(mod._error_frame(1, error.value))
    captured = capsys.readouterr()
    assert 'SYNTHETIC-KEY-CANARY' not in captured.out + captured.err + stream.getvalue().decode()


def test_unsupported_provider_refused_before_storage(settings):
    mod, broker, state, _, _ = settings
    with pytest.raises(mod.BrokerError) as error:
        broker.op_secrets_set({**args(), 'provider': 'not-advertised'})
    assert error.value.code == 'unsupported_provider'
    assert not state['settings_calls']


def test_request_key_single_use_even_changed_body(settings):
    mod, broker, state, _, _ = settings
    broker.op_secrets_set(args())
    for key in ['SYNTHETIC-KEY-CANARY', 'OTHER-SYNTHETIC-KEY']:
        with pytest.raises(mod.BrokerError) as error:
            broker.op_secrets_set({**args(), 'key': key})
        assert error.value.code == 'request_key_used'
    assert len(state['settings_calls']) == 3


@pytest.mark.parametrize('op', ['secrets.set', 'policy.get', 'policy.set'])
def test_independent_missing_operation(settings, op):
    mod, broker, state, _, caps = settings
    del caps[op]
    payload = args() if op == 'secrets.set' else {} if op == 'policy.get' else {
        'contract_version': 2, 'policy': {}, 'expected_revision': 0, 'request_key': 'policy-key'}
    with pytest.raises(mod.BrokerError) as error:
        broker.handle({'id': 1, 'op': op, 'args': payload})
    assert error.value.code == 'client_upgrade_required'
    assert not state['settings_calls']


def test_invalid_secret_fields_cannot_echo_key(settings):
    mod, broker, _, _, _ = settings
    for payload in [{**args(), 'SYNTHETIC-KEY-CANARY': True}, {**args(), 'contract_version': True}]:
        with pytest.raises(mod.BrokerError) as error:
            broker.op_secrets_set(payload)
        assert 'SYNTHETIC-KEY-CANARY' not in json.dumps(mod._error_frame(1, error.value))


def test_invalid_secret_envelope_cannot_echo_field_names(settings):
    mod, broker, _, _, _ = settings
    with pytest.raises(mod.BrokerError) as error:
        broker.handle({'id': 1, 'op': 'secrets.set', 'args': args(),
                       'SYNTHETIC-KEY-CANARY': True})
    assert 'SYNTHETIC-KEY-CANARY' not in json.dumps(mod._error_frame(1, error.value))


@pytest.mark.parametrize('changes', [
    {'key': 'space key'}, {'key': 'line\nbreak'}, {'key': '\x85hidden'},
    {'key': 'k' * 16385}, {'provider': '../provider'}, {'provider': 'UPPER'},
    {'request_key': '../receipt'}, {'request_key': 'two words'},
])
def test_secret_schema_refuses_before_storage_without_echo(settings, changes):
    mod, broker, state, _, _ = settings
    with pytest.raises(mod.BrokerError) as error:
        broker.op_secrets_set({**args(), **changes})
    assert not state['settings_calls']
    assert not any(value in json.dumps(mod._error_frame(1, error.value)) for value in changes.values())


def test_jsonl_serve_projects_receipts_and_safe_failures(settings, capsys, tmp_path):
    mod, broker, state, stream, _ = settings
    frames = [
        {'id': 1, 'op': 'secrets.set', 'args': args()},
        {'id': 2, 'op': 'secrets.set', 'args': args()},
        {'id': 3, 'op': 'hello', 'args': {}},
        {'id': 4, 'op': 'secrets.set', 'args': {**args(), 'SYNTHETIC-KEY-CANARY': True}},
    ]
    mod.serve(broker, broker.sink, io.BytesIO(('\n'.join(json.dumps(f) for f in frames) + '\n').encode()))
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]['value']['stored'] is True
    assert lines[1]['error']['code'] == 'request_key_used'
    assert lines[3]['ok'] is False
    captured = capsys.readouterr()
    output = stream.getvalue().decode() + captured.out + captured.err
    assert 'SYNTHETIC-KEY-CANARY' not in output
    assert 'SYNTHETIC-KEY-CANARY' not in mod._OWNER_SECRETS
    # Check every durable file, rather than assuming a particular ledger name.
    for file in tmp_path.rglob('*'):
        if file.is_file():
            assert b'SYNTHETIC-KEY-CANARY' not in file.read_bytes()


def test_policy_forwarding_preserves_attachment_rules_and_exact_revision(settings):
    mod, broker, state, _, _ = settings
    received = []
    broker._owner_transport.rest = lambda method, path, **kw: received.append((method, path, kw)) or {}
    broker.op_policy_get({})
    policy = {'posture': 'scoped', 'scope': {'auto_actions': ['read'], 'escalate_actions': ['write'],
              'may_touch': [{'attachment_id': 'registered', 'path': 'src/**'}], 'may_not_touch': []}}
    payload = {'contract_version': 2, 'policy': policy, 'expected_revision': '9007199254740993',
               'request_key': 'policy-once'}
    broker.op_policy_set(payload)
    assert received[0][0:2] == ('GET', '/settings/policy?contract_version=2')
    assert received[1][0:2] == ('POST', '/settings/policy')
    assert received[1][2]['body'] == payload
    for version in [True, 1, '2', None]:
        with pytest.raises(mod.BrokerError):
            broker.op_policy_set({**payload, 'contract_version': version})


@pytest.mark.parametrize('phase', ['prepared', 'committed', 'ready', 'failed', 'invalid'])
def test_receipt_lookup_without_gateway_or_owner_never_replays_credentials(settings, monkeypatch, phase):
    mod, broker, _, _, _ = settings
    broker._forget_owner()
    broker._rest = lambda *a, **kw: pytest.fail('receipt lookup must not call gateway')
    monkeypatch.setenv('BOB_SETTINGS_ID', 'a' * 12)
    requests = []
    receipt = {'provider': 'fixture-provider', 'stored': True, 'fingerprint': '0123456789abcdef',
               'operation_id': 'receipt-1', 'key': 'SYNTHETIC-KEY-CANARY'}
    reply = {'operation_id': 'receipt-1', 'phase': phase, 'result': receipt,
             'error': 'SYNTHETIC-KEY-CANARY', 'key': 'SYNTHETIC-KEY-CANARY'}

    class InstalledPath:
        def __init__(self, value):
            self.path = PurePosixPath(value)

        def __truediv__(self, value):
            return InstalledPath(self.path / value)

        @property
        def parents(self):
            return tuple(InstalledPath(p) for p in self.path.parents)

        def lstat(self):
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)

        def __str__(self):
            return str(self.path)

    class ReceiptSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def settimeout(self, value):
            assert value == 10

        def connect(self, path):
            assert path == '/run/bob-settings/' + 'a' * 12 + '/receipt.sock'

        def getsockopt(self, *args):
            return struct.pack('3i', 1, 0, 0)

        def sendall(self, value):
            requests.append(json.loads(value))

        def makefile(self, mode):
            return io.BytesIO(json.dumps(reply).encode() + b'\n')

    monkeypatch.setattr(mod, 'Path', InstalledPath)
    monkeypatch.setattr(socket, 'socket', lambda *a: ReceiptSocket())
    if phase == 'invalid':
        with pytest.raises(mod.BrokerError, match='settings_receipt_unavailable'):
            broker.op_settings_status({'request_key': 'one-use'})
    else:
        result = broker.op_settings_status({'request_key': 'one-use'})
        assert 'SYNTHETIC-KEY-CANARY' not in json.dumps(result)
        assert result['phase'] == phase
        assert ('result' in result) == (phase == 'ready')
        if phase == 'ready':
            assert set(result['result']) == {'provider', 'stored', 'fingerprint', 'operation_id'}
        if phase == 'failed':
            assert result['error'] == 'settings_failed'
    assert requests == [{'version': 1, 'op': 'status', 'request_key': 'one-use'}]
