"""Offline regressions for the second built-runtime findings."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_runtime_boot import acceptance, unit
from test_worker_runtime import worker

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_directories_repair_both_modes_on_every_start(worker, monkeypatch, tmp_path):
    from core.worker import auth_file
    module, _, _ = worker
    run = tmp_path / 'run/bob-workers'
    monkeypatch.setattr(module, 'RUN', run)
    opened, modes, owners = [], {}, {}
    from contextlib import contextmanager
    @contextmanager
    def directory(path, create=False):
        assert create
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        opened.append(path)
        yield path
    monkeypatch.setattr(auth_file, 'directory', directory)
    monkeypatch.setattr(module.os, 'fchmod', lambda fd, mode: modes.update({fd: mode}), raising=False)
    monkeypatch.setattr(module.os, 'fchown', lambda fd, uid, gid: owners.update({fd: (uid, gid)}), raising=False)
    for _ in range(2):
        assert module.runtime_directory('abc') == run / 'abc'
        assert modes == {run: 0o711, run / 'abc': 0o711}
        assert owners == {run: (0, 0), run / 'abc': (0, 0)}
        modes.clear()
        owners.clear()
    assert opened == [run, run / 'abc'] * 2


@pytest.mark.skipif(os.name == 'nt', reason='real directory modes require POSIX')
def test_runtime_directories_ignore_restrictive_umask(worker, monkeypatch, tmp_path):
    module, _, _ = worker
    monkeypatch.setattr(module, 'RUN', tmp_path / 'bob-workers')
    monkeypatch.setattr(module.os, 'fchown', lambda *args: None)
    old = os.umask(0o077)
    try:
        for _ in range(2):
            path = module.runtime_directory('abc')
            assert path.stat().st_mode & 0o777 == 0o711
            assert path.parent.stat().st_mode & 0o777 == 0o711
            path.chmod(0o700)
            path.parent.chmod(0o700)
    finally:
        os.umask(old)


def test_units_without_identity_dropins_cannot_run_as_generic_account():
    runtime = ROOT / 'deploy/runtime/systemd'
    for name in ('core', 'gateway', 'bridge', 'worker', 'worker-auth'):
        parsed = unit((runtime / f'bob-{name}@.service').read_text())
        assert parsed['Service']['User'] == 'bob-unprovisioned'
        assert parsed['Service']['Group'] == 'bob-unprovisioned'
        assert parsed['Unit']['AssertPathExists'] == f'/etc/systemd/system/bob-{name}@%i.service.d/identity.conf'
    for name in ('worker', 'worker-auth'):
        parsed = unit((runtime / f'bob-{name}@.socket').read_text())
        assert parsed['Socket']['SocketUser'] == 'bob-unprovisioned'
        assert parsed['Socket']['SocketMode'] == '0600'
        assert parsed['Socket']['DirectoryMode'] == '0711'
        assert parsed['Unit']['AssertPathExists'] == f'/etc/systemd/system/bob-{name}@%i.socket.d/identity.conf'
    assert 'bob-unprovisioned' not in (ROOT / 'deploy/runtime/Containerfile').read_text()


class Pool:
    def __init__(self, lock, events, fail=False):
        self.lock, self.events, self.fail = lock, events, fail
        self.terminated = False
    @asynccontextmanager
    async def acquire(self):
        yield self
    @asynccontextmanager
    async def transaction(self):
        self.events.append('begin')
        try:
            yield
        finally:
            if self.lock.locked() and getattr(self, 'locked', False):
                self.lock.release()
            self.events.append('end')
    async def execute(self, sql, *args, **kwargs):
        if 'pg_advisory_xact_lock' in sql:
            assert args == (0x424F42534348454D,)
            await self.lock.acquire()
            self.locked = True
            self.events.append('lock')
        else:
            assert self.locked and self.lock.locked()
            self.events.append('ddl-start')
            await asyncio.sleep(0)
            if self.fail:
                raise RuntimeError('DDL failed')
            self.events.append('ddl-end')
    def terminate(self):
        self.terminated = True


async def test_concurrent_schema_startup_serializes_before_any_ddl(monkeypatch):
    from core import db
    lock, events = asyncio.Lock(), []
    pools = [Pool(lock, events), Pool(lock, events)]
    monkeypatch.setattr(db, '_pg_pool', None)
    monkeypatch.setattr(db.asyncpg, 'create_pool', AsyncMock(side_effect=pools))
    monkeypatch.setattr(db, '_read_init_sql', lambda: 'SCHEMA')
    result = await asyncio.gather(db.init_postgres(), db.init_postgres())
    assert result == pools
    assert [event for event in events if event != 'begin'] == [
        'lock', 'ddl-start', 'ddl-end', 'end', 'lock', 'ddl-start', 'ddl-end', 'end']
    assert not lock.locked() and not any(pool.terminated for pool in pools)


async def test_failed_schema_rolls_back_releases_lock_and_terminates_unpublished_pool(monkeypatch):
    from core import db
    lock = asyncio.Lock()
    pool = Pool(lock, [], fail=True)
    monkeypatch.setattr(db, '_pg_pool', None)
    monkeypatch.setattr(db.asyncpg, 'create_pool', AsyncMock(return_value=pool))
    monkeypatch.setattr(db, '_read_init_sql', lambda: 'SCHEMA')
    with pytest.raises(RuntimeError, match='DDL failed'):
        await db.init_postgres()
    assert not lock.locked() and pool.terminated and db._pg_pool is None


async def test_sqlite_migration_failure_closes_connection(monkeypatch):
    from core import db
    conn = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(db.aiosqlite, 'connect', AsyncMock(return_value=conn))
    monkeypatch.setattr(db, '_init_sqlite_schema', AsyncMock(side_effect=RuntimeError('migration')))
    monkeypatch.setattr(db, '_sqlite_db', None)
    with pytest.raises(RuntimeError, match='migration'):
        await db.init_sqlite(':memory:')
    conn.close.assert_awaited_once()
    assert db._sqlite_db is None


async def test_cancelled_schema_wait_terminates_pool_without_publishing(monkeypatch):
    from core import db
    lock = asyncio.Lock()
    await lock.acquire()
    pool = Pool(lock, [])
    monkeypatch.setattr(db, '_pg_pool', None)
    monkeypatch.setattr(db.asyncpg, 'create_pool', AsyncMock(return_value=pool))
    task = asyncio.create_task(db.init_postgres())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock.locked() and pool.terminated and db._pg_pool is None
    lock.release()


@pytest.mark.parametrize('stage', ['postgres', 'graph'])
def test_fatal_startup_exits_nonzero_with_real_sqlite_thread(stage, tmp_path):
    # A real process is essential: a mocked close cannot detect a stuck thread.
    source = '''import sys
sys.path.insert(0, sys.argv[1])
import start
from aiohttp import web
from core.db import init_sqlite
from unittest.mock import AsyncMock
from types import SimpleNamespace
async def fail():
 raise RuntimeError('S2D_FATAL_STARTUP')
start.init_sqlite=lambda: init_sqlite(':memory:')
start.build_app=web.Application
start.config.validate=lambda: None
if sys.argv[2]=='postgres': start.init_postgres=fail
else:
 start.init_postgres=AsyncMock(return_value=SimpleNamespace(close=AsyncMock(), terminate=lambda: None))
 start.create_graph=fail
start.main(['--host','127.0.0.1','--port','0'])
'''
    result = subprocess.run([sys.executable, '-I', '-c', source, str(ROOT / 'bobclaw-core'), stage],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'S2D_FATAL_STARTUP' in result.stderr, result.stderr


def test_child_failure_keeps_json_diagnostics(acceptance, monkeypatch):
    module, _ = acceptance
    # The preflight fixture blocks construction, so invoke the actual class from
    # its method globals through a fresh import.
    spec = importlib.util.spec_from_file_location('acceptance_errors', ROOT / 'deploy/runtime/tests/acceptance.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    harness = object.__new__(module.Harness)
    harness.names, harness.configs, harness.script = ['a'], {'a': {'service_user': 'service'}}, 'harness.py'
    detail = json.dumps({'probe': 'harness.setup-or-cleanup', 'error': 'WorkerUnavailable', 'detail': 'permission denied'})
    monkeypatch.setattr(module, 'admin', lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=detail, stderr=''))
    with pytest.raises(RuntimeError, match='WorkerUnavailable.*permission denied'):
        harness.call({})


def test_admin_defaults_to_traversable_cwd_and_preserves_explicit_override(monkeypatch):
    spec = importlib.util.spec_from_file_location('acceptance_cwd', ROOT / 'deploy/runtime/tests/acceptance.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    module.admin('runuser', '-u', 'service', '--', '/bin/true')
    module.admin('/bin/true', cwd='/explicit', timeout=5)
    assert calls[0][1]['cwd'] == '/' and calls[0][1]['timeout'] == 120
    assert calls[1][1]['cwd'] == '/explicit' and calls[1][1]['timeout'] == 5


def test_missing_tools_rejected_before_provision(acceptance, monkeypatch):
    module, calls = acceptance
    monkeypatch.setattr(module.shutil, 'which', lambda name: None if name == 'ip' else '/usr/bin/' + name)
    monkeypatch.setattr(sys, 'argv', ['acceptance.py', '--create'])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2 and not calls


@pytest.mark.parametrize('foreign_allowed', [False, True])
def test_socket_probe_checks_both_identities_and_rejects_foreign_access(acceptance, foreign_allowed):
    spec = importlib.util.spec_from_file_location('acceptance_sockets', ROOT / 'deploy/runtime/tests/acceptance.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    harness = object.__new__(module.Harness)
    harness.names = ['a', 'b']
    harness.configs = {name: {'socket': name + '/exec', 'auth_socket': name + '/auth'} for name in harness.names}
    calls, records = [], []
    def call(paths, lane, mode):
        other = 'b' if lane == 'a' else 'a'
        assert mode == 'socket-access'
        assert paths == {'own-exec': lane + '/exec', 'own-auth': lane + '/auth',
                         'foreign-exec': other + '/exec', 'foreign-auth': other + '/auth'}
        calls.append(lane)
        return {'own-exec': True, 'own-auth': True, 'foreign-exec': foreign_allowed,
                'foreign-auth': False, 'policy-readable': True}
    harness.call = call
    harness.record = lambda probe, passed, **detail: records.append((probe, passed))
    harness.socket_access('policy-restart')
    assert calls == ['a', 'b']
    assert records == [('sockets.service-access.policy-restart.' + lane, not foreign_allowed) for lane in calls]
