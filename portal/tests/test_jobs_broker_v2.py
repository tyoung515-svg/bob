"""S43f app-copy transport contracts. Offline; run with real fcntl in Linux/WSL.

The fake service proves broker routing/replay forwarding, not independent backend
persistence or confinement. Use the backend fixtures against this copy as well.
"""
import base64
import copy
import json
import sys
from types import SimpleNamespace

import pytest
from test_owner_session_broker import setup, confirm  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='requires Linux fcntl; run in WSL')
OPS = ('jobs.attachments', 'jobs.list', 'jobs.start', 'jobs.audit', 'jobs.cancel',
       'jobs.proposal', 'jobs.decide', 'jobs.artifact')
JOB = 'a' * 32
FOREIGN = 'b' * 32
DIGEST = 'd' * 64


def start_args():
    return {'contract_version': 2, 'role': 'proposer', 'task': 'Supplied fixture evidence',
            'attachment': 'attachment-1', 'request_key': 'same-start-key', 'team': 'saved-team',
            'team_revision': '9007199254740993',
            'budget': {'start_calls': 10, 'wall_mode': 'fixed', 'wall_seconds': 1800,
                       'max_job_attempts': '9007199254740993', 'job_seconds_budget': '9223372036854775807'},
            'job_policy': {'proposed_seconds': 900, 'rationale': 'Small report', 'auto_escalate': False},
            'job_coverage': {'profiles': {'proposal': DIGEST}, 'workspaces': {'projects': ['fixture']},
                             'attachments': ['attachment-1'], 'max_job_attempts': '9007199254740993',
                             'job_seconds_budget': '9223372036854775807', 'allow_cli': False}}


def decision_args():
    return {'contract_version': 2, 'job_id': JOB, 'request_key': 'same-decision-key',
            'proposal_digest': DIGEST, 'expected_revision': '9007199254740993', 'decision': 'approve'}


@pytest.fixture
def jobs(setup, monkeypatch):
    mod, broker, state, session, stream, Transport = setup
    session['allowed_operations'].append('jobs.decide')
    operations = {op: {'contract_versions': [2], 'implemented': True, 'available': True} for op in OPS}
    receipts = {}
    touched = []
    broker.ledger = SimpleNamespace(record=lambda *a: touched.append(a), remember=lambda *a: touched.append(a))
    original = Transport.rest

    def rest(self, method, path, **kwargs):
        response = original(self, method, path, **kwargs)  # record the actual bearer/route exactly once
        if path == '/capabilities':
            response['operations'].update(operations)
            return response
        if FOREIGN in path:
            raise mod.BrokerError('not_found', 'not_found')
        if not path.startswith('/api/tool-job'):
            return response
        body = kwargs.get('body') or {}
        if path.endswith('/proposal/decision'):
            assert self._token == state['bearer']
            if body.get('decision') == 'deny':
                return {'decision': {'state': 'denied', 'applied': False}}
            value = {'decision': {'id': 'decision-1', 'key': body['request_key'], 'state': 'approved',
                                  'applied': False, 'apply_reason': 'managed_apply_unavailable_until_4.3e'}}
        elif '/artifacts/' in path:
            params = kwargs.get('params') or {}
            content = b'bounded artifact bytes'
            offset, maximum = int(params.get('offset', 0)), int(params.get('max_bytes', 65536))
            chunk = content[offset:offset + maximum]
            return {'filename': path.rsplit('/', 1)[1], 'content': base64.b64encode(chunk).decode(),
                    'encoding': 'base64', 'sha256': DIGEST, 'offset': offset,
                    'next_offset': offset + len(chunk) if offset + len(chunk) < len(content) else None,
                    'total_bytes': len(content), 'truncated': offset + len(chunk) < len(content)}
        elif path.endswith('/cancel'):
            return {'id': JOB, 'status': 'cancelling', 'terminal': False, 'operation_id': 'cancel-op'}
        elif method == 'POST':
            value = {'id': JOB, 'status': 'queued', 'operation_id': 'start-op'}
        else:
            return response
        key = (path, body.get('request_key'))
        if key in receipts:
            prior, result = receipts[key]
            if prior != body:
                raise mod.BrokerError('idempotency_conflict', 'changed payload')
            return {**result, 'replayed': True}
        receipts[key] = (copy.deepcopy(body), value)
        return value

    monkeypatch.setattr(Transport, 'rest', rest)
    yield mod, broker, state, session, stream, operations, touched


def calls(broker):
    return {'jobs.attachments': lambda: broker.op_jobs_attachments({'contract_version': 2}),
            'jobs.list': lambda: broker.op_jobs_list({'contract_version': 2, 'limit': 50}),
            'jobs.start': lambda: broker.op_jobs_start(start_args()),
            'jobs.audit': lambda: broker.op_jobs_audit({'contract_version': 2, 'job_id': JOB, 'after_event_seq': '9007199254740993'}),
            'jobs.cancel': lambda: broker.op_jobs_cancel({'contract_version': 2, 'job_id': JOB, 'request_key': 'same-cancel-key'}),
            'jobs.proposal': lambda: broker.op_jobs_proposal({'contract_version': 2, 'job_id': JOB}),
            'jobs.decide': lambda: broker.op_jobs_decide(decision_args()),
            'jobs.artifact': lambda: broker.op_jobs_artifact({'contract_version': 2, 'job_id': JOB, 'filename': 'report-' + JOB + '.json', 'max_bytes': 4})}


def test_all_eight_fixed_routes_and_separate_owner_decision(jobs):
    mod, broker, state, session, stream, operations, touched = jobs
    confirm(broker)
    expected = [('GET', '/api/tool-job-attachments'), ('GET', '/api/tool-jobs'), ('POST', '/api/tool-jobs'),
                ('GET', '/api/tool-jobs/' + JOB + '/audit'), ('POST', '/api/tool-jobs/' + JOB + '/cancel'),
                ('GET', '/api/tool-jobs/' + JOB + '/proposal'), ('POST', '/api/tool-jobs/' + JOB + '/proposal/decision'),
                ('GET', '/api/tool-jobs/' + JOB + '/artifacts/report-' + JOB + '.json')]
    for (op, call), route in zip(calls(broker).items(), expected):
        value = call()
        assert state['hits'][-1][1:3] == route
        assert (state['hits'][-1][0] == state['bearer']) is (op == 'jobs.decide')
        if op == 'jobs.start':
            assert state['hits'][-1][3]['body'] == start_args()
        elif op == 'jobs.audit':
            assert state['hits'][-1][3]['params']['after_event_seq'] == '9007199254740993'
        elif op == 'jobs.cancel':
            assert value['status'] == 'cancelling' and value['terminal'] is False
        elif op == 'jobs.decide':
            assert value['decision']['applied'] is False
            assert 'job_id' not in state['hits'][-1][3]['body']
        elif op == 'jobs.artifact':
            assert len(base64.b64decode(value['content'])) == 4
    assert not touched


@pytest.mark.parametrize('missing', OPS)
def test_each_operation_negotiates_without_disabling_the_others(jobs, missing):
    mod, broker, state, session, stream, operations, touched = jobs
    confirm(broker)
    operations[missing] = {}
    with pytest.raises(mod.BrokerError, match='client_upgrade_required'):
        calls(broker)[missing]()
    for operation, call in calls(broker).items():
        if operation != missing:
            call()


def test_unavailable_reason_and_foreign_resource_denial_passthrough(jobs):
    mod, broker, state, session, stream, operations, touched = jobs
    operations['jobs.start']['available'] = False
    operations['jobs.start']['reason'] = 'landlock_abi_3_required'
    with pytest.raises(mod.BrokerError) as exc:
        broker.op_jobs_start(start_args())
    assert exc.value.code == 'operation_unavailable'
    assert exc.value.detail == 'landlock_abi_3_required'
    for op in ('audit', 'cancel', 'proposal', 'artifact', 'decide'):
        args = {'contract_version': 2, 'job_id': FOREIGN}
        if op == 'artifact': args['filename'] = 'report-' + JOB + '.json'
        if op == 'decide':
            confirm(broker)
            args = {**decision_args(), 'job_id': FOREIGN}
        with pytest.raises(mod.BrokerError) as exc:
            getattr(broker, 'op_jobs_' + op)(args)
        assert exc.value.code == 'not_found'


def test_request_key_single_use_recovery_and_conflict(jobs):
    mod, broker, state, session, stream, operations, touched = jobs
    first = broker.op_jobs_start(start_args())
    again = broker.op_jobs_start(start_args())
    assert first['id'] == again['id'] and again['replayed'] is True
    with pytest.raises(mod.BrokerError, match='idempotency_conflict'):
        broker.op_jobs_start({**start_args(), 'task': 'Changed task'})
    confirm(broker)
    decision = broker.op_jobs_decide(decision_args())
    assert broker.op_jobs_decide(decision_args())['replayed'] is True
    assert decision['decision']['applied'] is False
    with pytest.raises(mod.BrokerError, match='idempotency_conflict'):
        broker.op_jobs_decide({**decision_args(), 'expected_revision': 2})
    assert len([h for h in state['hits'] if h[1] == 'POST' and h[2] == '/api/tool-jobs']) == 3


def test_no_owner_fallback_agent_denial_and_output_privacy(jobs, capsys):
    mod, broker, state, session, stream, operations, touched = jobs
    with pytest.raises(mod.BrokerError, match='owner_confirmation_required'):
        broker.op_jobs_decide(decision_args())
    assert not any(h[2].endswith('/decision') for h in state['hits'])
    broker.identity['token_type'] = 'agent'
    with pytest.raises(mod.BrokerError, match='owner_portal_required'):
        broker.op_jobs_decide(decision_args())
    broker.identity['token_type'] = 'portal'
    confirm(broker)
    broker.sink.reply({'value': broker.op_jobs_decide(decision_args())})
    broker.sink.event('job', {'text': state['bearer']})
    out = stream.getvalue().decode() + ''.join(capsys.readouterr())
    for secret in (state['bearer'], 'synthetic-password', '123456', 'bounded artifact bytes'):
        assert secret not in out
    assert not touched


@pytest.mark.parametrize('value', [True, 0, 1.5, '01', '9223372036854775808', 9007199254740992])
def test_integer_validation_preserves_decimal_strings_and_rejects_rounding(jobs, value):
    mod, broker, state, session, stream, operations, touched = jobs
    raw = start_args()
    raw['budget']['max_job_attempts'] = value
    before = len(state['hits'])
    with pytest.raises(mod.BrokerError): broker.op_jobs_start(raw)
    assert len(state['hits']) == before


def test_legacy_unknown_fields_and_unbounded_artifacts_never_dispatch(jobs):
    mod, broker, state, session, stream, operations, touched = jobs
    old = {'role': 'researcher', 'task': 'Old request', 'request_key': 'old-key-1'}
    with pytest.raises(mod.BrokerError, match='client_upgrade_required'): broker.op_jobs_start(old)
    for extra in ({'owner_bearer': 'forbidden'}, {'wall_seconds': 120}, {'argv': ['anything']}):
        with pytest.raises(mod.BrokerError): broker.op_jobs_start({**start_args(), **extra})
    for maximum in (0, 65537, True):
        with pytest.raises(mod.BrokerError):
            broker.op_jobs_artifact({'job_id': JOB, 'filename': 'report-' + JOB + '.json', 'max_bytes': maximum})
    for name in ('../secret', 'report-' + JOB + '.json/other', 'report-' + JOB + '.json\n'):
        with pytest.raises(mod.BrokerError): broker.op_jobs_artifact({'job_id': JOB, 'filename': name})
    assert not any(h[2].startswith('/api/') for h in state['hits'])


@pytest.mark.parametrize('operation,mode', [('jobs.list','readable'), ('jobs.cancel','writable'), ('jobs.start','runnable')])
def test_operation_mode_denials_remain_local_to_that_feature(jobs, operation, mode):
    mod, broker, state, session, stream, operations, touched = jobs
    operations[operation][mode] = False
    operations[operation]['reason'] = 'fixture_mode_unavailable'
    with pytest.raises(mod.BrokerError) as exc:
        calls(broker)[operation]()
    assert exc.value.code == 'operation_unavailable'
    assert exc.value.detail == 'fixture_mode_unavailable'
    broker.op_jobs_proposal({'job_id': JOB})


def test_bounded_read_decimal_strings_are_not_rounded_or_clamped(jobs):
    mod, broker, state, session, stream, operations, touched = jobs
    broker.op_jobs_list({'contract_version': 2, 'limit': '50'})
    assert state['hits'][-1][3]['params']['limit'] == '50'
    value = broker.op_jobs_artifact({'job_id': JOB, 'filename': 'report-' + JOB + '.json', 'offset': '0', 'max_bytes': '4'})
    assert state['hits'][-1][3]['params']['offset'] == '0'
    assert len(base64.b64decode(value['content'])) == 4


def test_artifact_server_cannot_exceed_readback_bound_or_echo_credentials(jobs, monkeypatch):
    mod, broker, state, session, stream, operations, touched = jobs
    original = broker._rest
    payload = {'filename': 'report-' + JOB + '.json', 'content': base64.b64encode(b'12345').decode(),
               'encoding': 'base64', 'sha256': DIGEST, 'offset': 0, 'next_offset': None,
               'total_bytes': 5, 'truncated': False, 'owner_bearer': 'must-not-be-relayed'}
    def rest(method, path, **kwargs):
        return dict(payload) if '/artifacts/' in path else original(method, path, **kwargs)
    monkeypatch.setattr(broker, '_rest', rest)
    args = {'job_id': JOB, 'filename': payload['filename'], 'max_bytes': 4}
    with pytest.raises(mod.BrokerError, match='invalid_response'):
        broker.op_jobs_artifact(args)
    payload['content'] = base64.b64encode(b'1234').decode()
    payload['total_bytes'] = 4
    value = broker.op_jobs_artifact(args)
    assert 'owner_bearer' not in value
    broker.sink.reply({'value': value})
    assert 'must-not-be-relayed' not in stream.getvalue().decode()
    assert not touched


@pytest.mark.parametrize('field,value', [('offset', True), ('total_bytes', '01'),
                                        ('content', 'not-base64!'), ('next_offset', 1)])
def test_invalid_artifact_chunks_are_not_relayed(jobs, monkeypatch, field, value):
    mod, broker, state, session, stream, operations, touched = jobs
    original = broker._rest
    payload = {'filename': 'report-' + JOB + '.json', 'content': 'eA==', 'encoding': 'base64',
               'sha256': DIGEST, 'offset': 0, 'next_offset': None, 'total_bytes': 1, 'truncated': False}
    payload[field] = value
    monkeypatch.setattr(broker, '_rest', lambda method, path, **kw: dict(payload) if '/artifacts/' in path else original(method, path, **kw))
    with pytest.raises(mod.BrokerError, match='invalid_response'):
        broker.op_jobs_artifact({'job_id': JOB, 'filename': payload['filename']})
    assert not touched


def checked_args():
    raw = start_args()
    raw.update(role='coder', request_key='checked-start-key', worker_contract='checked-edits/1',
               acceptance={'criteria': [{'id': 'checked-proposal', 'required': True, 'output_kind': 'proposal',
                                         'checks': {'required_check_ids': ['files.nonempty']}}]})
    raw['budget'].update(wall_mode='none', wall_seconds=None)
    raw['job_policy']['auto_escalate'] = True
    raw['job_coverage']['profiles'] = {'checked': DIGEST}
    return raw


def checked_capability(broker, monkeypatch, *, available=True):
    original = broker._rest
    def rest(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if path == '/capabilities':
            result['tool_jobs'] = {'coder': {'available': available, 'worker_contract': 'checked-edits/1',
                                            'reason': 'installed_checked_profile_required' if not available else None},
                                   'acceptance_revision': 'checked-edits/1'}
        return result
    monkeypatch.setattr(broker, '_rest', rest)


def test_checked_start_negotiation_is_feature_local(jobs, monkeypatch):
    mod, broker, state, session, stream, operations, touched = jobs
    with pytest.raises(mod.BrokerError, match='client_upgrade_required'):
        broker.op_jobs_start(checked_args())
    assert not any(h[1] == 'POST' for h in state['hits'])
    broker.op_jobs_start(start_args())  # execution/2.5 reports still work
    checked_capability(broker, monkeypatch)
    raw = checked_args()
    broker.op_jobs_start(raw)
    assert state['hits'][-1][3]['body'] == raw
    assert state['hits'][-1][3]['body']['budget']['max_job_attempts'] == '9007199254740993'
    assert not touched


def test_checked_profile_refusal_and_record_only_authority(jobs, monkeypatch):
    mod, broker, state, session, stream, operations, touched = jobs
    checked_capability(broker, monkeypatch, available=False)
    with pytest.raises(mod.BrokerError) as exc:
        broker.op_jobs_start(checked_args())
    assert exc.value.code == 'operation_unavailable'
    assert exc.value.detail == 'installed_checked_profile_required'
    assert not any(h[1] == 'POST' for h in state['hits'])
    assert not hasattr(broker, 'op_jobs_apply')


@pytest.mark.parametrize('change', ['contract', 'empty_checks', 'duplicate_checks', 'argv', 'optional_only', 'wall', 'attempts', 'report'])
def test_checked_invalid_requests_never_dispatch(jobs, change):
    mod, broker, state, session, stream, operations, touched = jobs
    raw = checked_args()
    criterion = raw['acceptance']['criteria'][0]
    if change == 'contract': raw['worker_contract'] = 'checked-edits/2'
    elif change == 'empty_checks': criterion['checks']['required_check_ids'] = []
    elif change == 'duplicate_checks': criterion['checks']['required_check_ids'] *= 2
    elif change == 'argv': criterion['checks']['argv'] = ['not-authorized']
    elif change == 'optional_only': criterion['required'] = False
    elif change == 'wall': raw['budget']['wall_seconds'] = 900
    elif change == 'attempts': raw['budget']['max_job_attempts'] = '01'
    elif change == 'report': raw['role'] = 'proposer'
    before = len(state['hits'])
    with pytest.raises(mod.BrokerError): broker.op_jobs_start(raw)
    assert len(state['hits']) == before


def test_worker_artifact_uses_exact_filename_and_bounded_readback(jobs):
    mod, broker, state, session, stream, operations, touched = jobs
    args = {'contract_version': 2, 'job_id': JOB, 'filename': 'worker-' + JOB + '.json', 'max_bytes': '4'}
    result = broker.op_jobs_artifact(args)
    assert result['filename'] == args['filename']
    assert len(base64.b64decode(result['content'])) == 4
    with pytest.raises(mod.BrokerError): broker.op_jobs_artifact({**args, 'filename': args['filename'] + '\n'})
