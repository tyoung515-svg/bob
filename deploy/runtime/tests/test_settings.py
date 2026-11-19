"""Real Linux file/socket boundary; Windows runs only the pure schema suites."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(os.name == 'nt' or getattr(os, 'geteuid', lambda: -1)() != 0,
                                reason='root Linux no-follow, fsync and peer-credential tests')
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def helper(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'bobclaw-core'))
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/runtime/lib'))
    spec = importlib.util.spec_from_file_location('s3d_root_settings', ROOT / 'deploy/runtime/lib/settings.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'STATE', tmp_path)
    home = tmp_path / ('a'*12)
    (home / 'vault').mkdir(parents=True)
    home.chmod(0o711)
    (home / 'vault').chmod(0o750)
    cfg = dict(lane='s3d-test', lane_id='a'*12, gateway_uid=1001, service_uid=1002, worker_uid=1003,
               bridge_uid=1004, broker_uid=1005, service_gid=0)
    import model
    # Boundary suite isolates enrollment; the real journal -> model sequence
    # and crash points are covered in test_provider_enrollment.
    monkeypatch.setattr(model, 'enroll_provider', lambda *a: False)
    return module, cfg, home


def test_atomic_vault_receipts_single_use_and_no_secret_journals(helper):
    m, cfg, home = helper
    key = 'CANARY-SECRET-123'
    request = dict(version=1, op='secrets.set', provider='deepseek', key=key, request_key='once')
    result = m.handle(cfg, cfg['gateway_uid'], request)
    assert result == dict(provider='deepseek', stored=True, fingerprint=m.fingerprint(key), operation_id=result['operation_id'])
    with pytest.raises(m.SettingsError, match='request_key_used'):
        m.handle(cfg, cfg['gateway_uid'], {**request, 'key': 'different'})
    for name in ('op-'+result['operation_id']+'.json', 'key-'+m.hashlib.sha256(b'once').hexdigest()+'.json'):
        text = (home / name).read_text()
        assert key not in text and m.hashlib.sha256(key.encode()).hexdigest() not in text
    status = m.handle(cfg, cfg['gateway_uid'], dict(version=1, op='status', request_key='once'))
    assert status['phase'] == 'ready' and status['result'] == result
    assert json.loads((home / 'vault/deepseek.json').read_text())['key'] == key
    assert not list(home.glob('.stage-*'))


@pytest.mark.parametrize('role', ['service', 'worker', 'bridge', 'broker'])
def test_wrong_control_peers_cannot_store(helper, role):
    m, cfg, _ = helper
    with pytest.raises(m.SettingsError, match='peer_denied'):
        m.handle(cfg, cfg[role+'_uid'], dict(version=1, op='secrets.set', provider='deepseek', key='x', request_key='once'))


def test_receipt_socket_schema_never_admits_a_write(helper):
    m, cfg, _ = helper
    with pytest.raises(m.SettingsError, match='unsupported_operation'):
        m.handle(cfg, cfg['broker_uid'], dict(version=1, op='secrets.set', provider='deepseek', key='x', request_key='once'), readonly=True)


@pytest.mark.parametrize('boundary', ['generation', 'pointer', 'receipt'])
def test_interrupted_publication_reconciles_without_replaying_secret(helper, monkeypatch, boundary):
    m, cfg, home = helper
    request = dict(version=1, op='secrets.set', provider='deepseek', key='CANARY-SECRET-123', request_key='once')
    native = m.publish
    failed = [False]
    def publish(fd, name, value, **kwargs):
        native(fd, name, value, **kwargs)
        hit = ((boundary == 'generation' and 'key' in value and name != 'deepseek.json')
               or (boundary == 'pointer' and name == 'deepseek.json')
               or (boundary == 'receipt' and value.get('phase') == 'ready'))
        if hit and not failed[0]:
            failed[0] = True
            raise OSError('CANARY-SECRET-123')
    monkeypatch.setattr(m, 'publish', publish)
    with pytest.raises(m.SettingsError) as caught:
        m.handle(cfg, cfg['gateway_uid'], request)
    assert 'CANARY' not in str(caught.value)
    status = m.handle(cfg, cfg['gateway_uid'], dict(version=1, op='status', request_key='once'))
    assert status['phase'] == ('failed' if boundary == 'generation' else 'ready')
    with pytest.raises(m.SettingsError, match='request_key_used'):
        m.handle(cfg, cfg['gateway_uid'], request)


def test_no_follow_and_hardlink_refusals(helper, tmp_path):
    m, cfg, home = helper
    outside = tmp_path / 'outside'
    outside.write_text('CANARY-SECRET-123')
    name = 'key-'+m.hashlib.sha256(b'once').hexdigest()+'.json'
    (home / name).symlink_to(outside)
    with pytest.raises(OSError):
        m.handle(cfg, cfg['broker_uid'], dict(version=1, op='status', request_key='once'), readonly=True)
    (home / name).unlink()
    os.link(outside, home / name)
    with pytest.raises(m.SettingsError, match='unsafe_storage'):
        m.handle(cfg, cfg['broker_uid'], dict(version=1, op='status', request_key='once'), readonly=True)
    assert outside.read_text() == 'CANARY-SECRET-123'


def test_real_peer_rejection_never_reflects_body(helper):
    m, cfg, _ = helper
    server, client = socket.socketpair()
    thread = threading.Thread(target=m.serve, args=(cfg, server))
    thread.start()
    with client:
        client.sendall(b'{"key":"CANARY-SECRET-123"}\n')
        result = client.recv(32768)
    thread.join(timeout=5)
    server.close()
    assert result == b'{"error": "peer_denied"}\n' and not thread.is_alive()


def test_real_socket_reply_precedes_restart_and_receipt_stays_pending(helper, monkeypatch):
    m, cfg, _ = helper
    cfg = {**cfg, 'gateway_uid': os.getuid()}
    import model
    monkeypatch.setattr(model, 'enroll_provider', lambda *a: True)
    grace_started, release = threading.Event(), threading.Event()
    restarts, errors = [], []
    def grace(seconds):
        assert seconds == m.RESTART_GRACE_SECONDS
        grace_started.set()
        assert release.wait(timeout=5)
    monkeypatch.setattr(m.time, 'sleep', grace)
    monkeypatch.setattr(model, 'restart_provider', lambda name: restarts.append(name))
    server, client = socket.socketpair()
    def serve():
        try:
            with server:
                m.serve(cfg, server)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        with client:
            client.settimeout(5)
            client.sendall(json.dumps(dict(version=1, op='secrets.set', provider='deepseek',
                key='CANARY-SECRET-123', request_key='once')).encode() + b'\n')
            result = json.loads(client.recv(32768))
        assert set(result) == {'provider', 'stored', 'fingerprint', 'operation_id'}
        assert grace_started.wait(timeout=5)
        assert not restarts
        request = dict(version=1, op='status', operation_id=result['operation_id'])
        assert m.handle(cfg, cfg['broker_uid'], request, readonly=True)['phase'] == 'restart-pending'
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    assert restarts == [cfg['lane']]
    assert m.handle(cfg, cfg['broker_uid'], request, readonly=True)['phase'] == 'ready'
