"""S3i app-side fixtures. Run with Linux fcntl; no network or real factors.
These projections do not establish root-helper or physical USB/print acceptance.
"""
import io
import json
import time
import sys

import pytest
from test_password_change_broker import password, receipt_socket
from test_owner_session_broker import setup as owner_setup

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux fcntl; run in WSL")
OPS = ('binding', 'prepare', 'commit', 'cancel')
CANARIES = ('PASSWORD-CANARY', 'NEW-PASSWORD-CANARY', 'S' * 32, '654321', 'A' * 52)
BINDING = dict(installation_id='install-1', lane_id='012345abcdef', owner='owner')


def prepare(pair='password_totp', key='prepare-once'):
    fields = dict(password=CANARIES[0], totp=CANARIES[3])
    if pair == 'setup':
        fields = dict(setup_capability='SETUP-CANARY', new_seed=CANARIES[2])
    if pair in {'password_stick', 'totp_stick'}:
        fields = dict(old_stick=dict(BINDING, recovery_id='old-stick', secret=CANARIES[4]))
        fields.update(dict(password=CANARIES[0], new_seed=CANARIES[2]) if pair == 'password_stick'
                      else dict(totp=CANARIES[3], new_password=CANARIES[1]))
    return dict(contract_version=2, request_key=key, pair=pair,
                replacement=dict(recovery_id='fresh-stick', verifier='b' * 64), **fields)


def commit(key='commit-once', new_totp=''):
    return dict(contract_version=2, request_key=key, challenge_id='challenge-1', material_saved=True, new_totp=new_totp)


@pytest.fixture
def recovery(password):
    mod, broker, state, stream, _ = password
    binding = dict(BINDING, owner=broker.sub)
    caps = {op: dict(contract_versions=[2], implemented=True, available=True) for op in OPS}
    original = broker.transport.rest
    state.update(factor_calls=[], recovery_mode='ready')

    def service(method, path, **kw):
        if path == '/capabilities':
            value = original(method, path, **kw)
            for op, cap in caps.items():
                if cap:
                    value['operations']['auth.factors.' + op] = cap
            return value
        if not path.startswith('/settings/factors/'):
            return original(method, path, **kw)
        op = path.rsplit('/', 1)[1]
        assert kw['owner_headers'] == {'X-Bob-Broker-Link': broker._owner_link_nonce}
        state['factor_calls'].append((method, op))  # no credential-bearing body
        if op == 'binding':
            return dict(binding, seed=CANARIES[2], bearer=state['bearer'])
        body = kw['body']
        assert body['contract_version'] == 2
        if state['recovery_mode'] in {'wrong', 'mixed', 'expired'}:
            raise mod.BrokerError('gateway_http_422', ' '.join(CANARIES))
        if op == 'prepare':
            value = dict(challenge_id='challenge-1', expires_at=int(time.time()) + 300, state='proof_verified')
        else:
            value = dict(changed=op == 'commit', restarted=op == 'commit')
        state['receipt'] = value
        if state['recovery_mode'] == 'dropped':
            raise mod.BrokerError('transport_unavailable', ' '.join(CANARIES))
        return dict(value, password=CANARIES[0], seed=CANARIES[2], secret=CANARIES[4], bearer=state['bearer'])

    broker.transport.rest = service
    return mod, broker, state, stream, caps


@pytest.mark.parametrize('pair', ['setup', 'password_stick', 'password_totp', 'totp_stick'])
def test_all_pairs_exact_safe_projection_and_single_use(recovery, pair):
    mod, broker, state, _, _ = recovery
    safe = broker.op_factors_prepare(prepare(pair))
    assert set(safe) == {'challenge_id', 'expires_at', 'state'}
    assert safe['state'] == 'proof_verified'
    assert broker._owner_transport is None
    assert broker.op_factors_commit(commit(new_totp='123456' if pair in {'setup', 'password_stick'} else '')) == dict(changed=True, restarted=True)
    with pytest.raises(mod.BrokerError, match='request_key_used'):
        broker.op_factors_prepare(prepare(pair))
    assert state['factor_calls'] == [('POST', 'prepare'), ('POST', 'commit')]
    assert not [hit for hit in state['hits'] if hit[2] == '/auth/owner/confirm']


@pytest.mark.parametrize('mode', ['wrong', 'mixed', 'expired'])
def test_wrong_mixed_binding_and_expiry_do_not_resend(recovery, mode):
    mod, broker, state, _, _ = recovery
    state['recovery_mode'] = mode
    with pytest.raises(mod.BrokerError, match='invalid_factors'):
        broker.op_factors_prepare(prepare())
    assert state['factor_calls'] == [('POST', 'prepare')]


@pytest.mark.parametrize('op', OPS)
@pytest.mark.parametrize('mode', ['missing', 'old', 'blocked'])
def test_independent_operation_capabilities(recovery, op, mode):
    mod, broker, state, _, caps = recovery
    caps[op].clear()
    if mode == 'old':
        caps[op].update(contract_versions=[1], available=True)
    if mode == 'blocked':
        caps[op].update(contract_versions=[2], available=False, reason='provisioning_required')
    args = {'binding': dict(contract_version=2), 'prepare': prepare(), 'commit': commit(),
            'cancel': dict(contract_version=2, request_key='cancel-once', challenge_id='challenge-1')}[op]
    with pytest.raises(mod.BrokerError) as error:
        getattr(broker, 'op_factors_' + op)(args)
    assert error.value.code in {'operation_unavailable', 'client_upgrade_required'}
    assert not state['factor_calls']
    assert broker._require_owner_operation('auth.change_password')


@pytest.mark.parametrize('identity', ['agent', 'human'])
def test_nonportal_denied_before_factors(recovery, identity):
    mod, broker, state, _, _ = recovery
    broker.identity['token_type'] = identity
    with pytest.raises(mod.BrokerError, match='owner_portal_required'):
        broker.op_factors_prepare(prepare())
    assert not state['factor_calls']


@pytest.mark.parametrize('change', [dict(contract_version=True), dict(pair='password'), dict(owner='forged'),
                                   dict(request_key='../key'), dict(replacement={'recovery_id':'id','verifier':'F'*64}),
                                   dict(totp=False), dict(totp='12345'), dict(new_seed=CANARIES[2])])
def test_closed_schema_no_dispatch_or_sensitive_error(recovery, change):
    mod, broker, state, _, _ = recovery
    with pytest.raises(mod.BrokerError) as error:
        broker.op_factors_prepare(prepare() | change)
    assert not state['factor_calls']
    for value in CANARIES:
        assert value not in json.dumps(mod._error_frame(1, error.value))


@pytest.mark.parametrize('operation', ['prepare', 'commit'])
def test_lost_reply_resolved_via_independent_receipt_no_resend(recovery, receipt_socket, operation):
    mod, broker, state, _, _ = recovery
    state['recovery_mode'] = 'dropped'
    with pytest.raises(mod.BrokerError, match='recovery_outcome_unknown'):
        getattr(broker, 'op_factors_' + operation)(prepare() if operation == 'prepare' else commit())
    safe = broker.op_settings_status({'request_key': operation + '-once'})
    assert safe['result'] == state['receipt']
    assert state['factor_calls'] == [('POST', operation)]
    assert receipt_socket == [dict(version=1, op='status', request_key=operation + '-once')]
    serialized = json.dumps(safe)
    for value in (*CANARIES, state['bearer']):
        assert value not in serialized


def test_no_recovery_bytes_seed_or_bearer_in_reply_hello_ledger_log(recovery, tmp_path, capsys):
    mod, broker, state, stream, _ = recovery
    ledger = []
    broker._ledger_remember = lambda *args: ledger.append(args)
    frames = [dict(id=1, op='auth.factors.prepare', args=prepare('setup')),
              dict(id=2, op='auth.factors.commit', args=commit()), dict(id=3, op='hello', args={})]
    mod.serve(broker, broker.sink, io.BytesIO(('\n'.join(json.dumps(f) for f in frames)+'\n').encode()))
    cap = capsys.readouterr()
    output = stream.getvalue().decode() + cap.out + cap.err
    assert json.loads(stream.getvalue().splitlines()[0])['ok']
    assert not ledger
    for value in (*CANARIES, state['bearer']):
        assert value not in output
        assert value not in mod._OWNER_SECRETS
        for file in tmp_path.rglob('*'):
            if file.is_file():
                assert value.encode() not in file.read_bytes()


def test_cancel_and_failed_save_do_not_commit(recovery):
    _, broker, state, _, _ = recovery
    broker.op_factors_prepare(prepare())
    assert broker.op_factors_cancel(dict(contract_version=2, request_key='cancel-once', challenge_id='challenge-1')) == dict(changed=False, restarted=False)
    assert state['factor_calls'] == [('POST','prepare'), ('POST','cancel')]
