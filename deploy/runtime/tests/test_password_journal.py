"""Real root/Linux dirfd journal and boot recovery. Skips are not evidence."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
import sys

import pytest

from test_settings import helper

pytestmark = pytest.mark.skipif(os.name == 'nt' or getattr(os, 'geteuid', lambda: -1)() != 0,
                               reason='requires real root Linux fsync/no-follow/flock')


@pytest.fixture
def credentials(helper, monkeypatch):
    m, cfg, home = helper
    monkeypatch.setitem(sys.modules, 'settings', m)
    spec = importlib.util.spec_from_file_location('credentials', __import__('pathlib').Path(m.__file__).with_name('credentials.py'))
    c = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'credentials', c)
    spec.loader.exec_module(c)
    path = home / 'credentials'
    path.mkdir(mode=0o711)
    with m.directory(path) as fd:
        m.publish(fd, 'initial.json', dict(epoch=1, password='old-password-canary', recovery_id='keep',
                                         recovery_hash='keep-hash', totp_secret='JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP',
                                         totp_enrolled=True, reset_at=None))
        m.publish(fd, 'current.env', c.environment(dict(epoch=1, password='old-password-canary')))
        m.publish(fd, 'active.json', dict(epoch=1, generation='initial', operation_id=None), mode=0o644)
    loaded = [1]
    restarts = []
    monkeypatch.setattr(c, 'ready', lambda cfg, epoch: loaded[0] == epoch)
    def restart(cfg, epoch):
        restarts.append((cfg['lane'], epoch))
        loaded[0] = epoch
        return True
    monkeypatch.setattr(c, 'restart', restart)
    return m, c, cfg, home, loaded, restarts


def request(key='once', **changes):
    return dict(version=1, op='password.change', new='new-password-canary', expected_epoch=1, request_key=key, **changes)


class PowerLoss(BaseException):
    pass


@pytest.mark.parametrize('boundary', ['prepared', 'generation', 'env', 'pointer', 'committed', 'restart-pending', 'restarted', 'terminal'])
def test_restart_at_every_journal_boundary_and_boot_repair(credentials, monkeypatch, boundary):
    m, c, cfg, home, loaded, restarts = credentials
    def crash(name):
        if name == boundary:
            raise PowerLoss()
    monkeypatch.setattr(c, 'checkpoint', crash)
    with pytest.raises(PowerLoss):
        m.handle(cfg, cfg['gateway_uid'], request())
    monkeypatch.setattr(c, 'checkpoint', lambda name: None)
    # Boot repair runs before systemd loads the gateway EnvironmentFile. It may
    # reconcile journals, but must never initiate a restart or replay a password.
    before = list(restarts)
    m.repair(cfg)
    assert restarts == before
    m.recover_all(cfg)  # Fresh control process startup after the crash.
    result = m.handle(cfg, cfg['broker_uid'], dict(version=1, op='status', request_key='once'), readonly=True)
    committed = boundary in {'pointer', 'committed', 'restart-pending', 'restarted', 'terminal'}
    assert result['phase'] == ('ready' if committed else 'failed')
    assert result['result'] == dict(changed=committed, restarted=committed)
    active = json.loads((home / 'credentials/active.json').read_text())
    assert active['epoch'] == (2 if committed else 1)
    generation = json.loads((home / 'credentials' / (active['generation']+'.json')).read_text())
    assert generation['recovery_id'] == 'keep' and generation['recovery_hash'] == 'keep-hash'
    assert generation['totp_secret'] == 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'
    assert (home / 'credentials/current.env').read_text() == c.environment(generation)
    with pytest.raises(m.SettingsError, match='request_key_used'):
        m.handle(cfg, cfg['gateway_uid'], request())
    assert len(restarts) == int(committed)
    assert all('password-canary' not in p.read_text() for p in home.glob('*.json'))


def test_restart_failure_is_committed_and_receipt_cannot_restart(credentials, monkeypatch):
    m, c, cfg, home, loaded, restarts = credentials
    def failed(*args):
        raise OSError('new-password-canary')
    monkeypatch.setattr(c, 'restart', failed)
    assert m.handle(cfg, cfg['gateway_uid'], request()) == dict(changed=True, restarted=False)
    status = m.handle(cfg, cfg['broker_uid'], dict(version=1, op='status', request_key='once'), readonly=True)
    assert status == dict(operation_id=status['operation_id'], phase='failed', result=dict(changed=True, restarted=False))
    assert json.loads((home/'credentials/active.json').read_text())['epoch'] == 2
    assert 'canary' not in json.dumps(status)


def test_concurrent_mutations_epoch_compare_and_single_use(credentials):
    m, c, cfg, home, loaded, restarts = credentials
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda key: m.handle(cfg, cfg['gateway_uid'], request(key)), ['one','two']))
    assert results.count(dict(changed=True, restarted=True)) == 1
    assert results.count(dict(changed=False, restarted=False)) == 1
    assert len(restarts) == 1


@pytest.mark.parametrize('which', ['op', 'key'])
def test_crash_between_receipt_indexes_uses_canonical_journal(credentials, monkeypatch, which):
    m, c, cfg, home, loaded, restarts = credentials
    native = m.publish
    def publish(fd, name, value, **kwargs):
        native(fd, name, value, **kwargs)
        if name.startswith(which+'-') and isinstance(value, dict) and value.get('phase') == 'ready':
            raise PowerLoss()
    monkeypatch.setattr(m, 'publish', publish)
    with pytest.raises(PowerLoss):
        m.handle(cfg, cfg['gateway_uid'], request())
    monkeypatch.setattr(m, 'publish', native)
    m.recover_all(cfg)
    receipt = m.handle(cfg, cfg['broker_uid'], dict(version=1, op='status', request_key='once'), readonly=True)
    assert receipt['phase'] == 'ready' and receipt['result'] == dict(changed=True, restarted=True)
    assert len(restarts) == 1


def test_receipt_only_observes_pending_and_never_writes_credentials(credentials, monkeypatch):
    m, c, cfg, home, loaded, restarts = credentials
    def crash(name):
        if name == 'pointer':
            raise PowerLoss()
    monkeypatch.setattr(c, 'checkpoint', crash)
    with pytest.raises(PowerLoss):
        m.handle(cfg, cfg['gateway_uid'], request())
    monkeypatch.setattr(c, 'checkpoint', lambda name: None)
    before = {p.name: p.stat().st_mtime_ns for p in (home/'credentials').iterdir()}
    status = m.handle(cfg, cfg['broker_uid'], dict(version=1, op='status', request_key='once'), readonly=True)
    assert status['phase'] == 'restart-pending' and 'result' not in status
    assert not restarts and before == {p.name: p.stat().st_mtime_ns for p in (home/'credentials').iterdir()}


def test_readiness_requires_loaded_epoch_and_mapped_mainpid_listener(credentials,monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from pathlib import Path
    import threading
    from types import SimpleNamespace
    m,c,cfg,home,loaded,restarts=credentials
    spec=importlib.util.spec_from_file_location('readiness_credentials',Path(c.__file__))
    native=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    epoch=[1]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path=='/auth/generation'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(dict(ready=True,credential_epoch=epoch[0])).encode())
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    pid=[os.getpid()]
    def mainpid(argv,**kwargs):
        assert argv==['/usr/bin/systemctl','show','-p','MainPID','--value',f'bob-gateway@{cfg["lane"]}.service']
        return SimpleNamespace(stdout=str(pid[0]))
    monkeypatch.setattr(native.subprocess,'run',mainpid)
    probe={**cfg,'gateway_uid':os.geteuid(),'gateway_port':server.server_port}
    try:
        assert native.ready(probe,2) is False  # Healthy HTTP, stale loaded epoch.
        epoch[0]=2
        assert native.ready(probe,2) is True
        assert native.ready({**probe,'gateway_uid':os.geteuid()+1},2) is False
        pid[0]=0
        assert native.ready(probe,2) is False  # Correct JSON cannot replace unit ownership.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
