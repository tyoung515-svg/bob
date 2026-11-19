"""Daemon history/state tests. Core storage races use actual temporary Postgres."""
import asyncio
import base64
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.worker.contract import (MAX_OUTPUT, WorkerUnavailable, profile_revision,
                                  request_digest, validate_control, validate_v2)
from core.worker.daemon import ExecutionConnection, ProtocolServer
from core.worker.daemon import verify_cgroup_clean
from core.worker.journal import Journal
from core.execution.jobs import JobContext, job_context, operation_context
from core.execution.worker_queue import WorkerQueue


def wire(**extra):
    value = dict(version=2, op='submit', execution_id='execution1', logical_job_id='job1',
                 profile='python', profile_revision='a' * 64, workspace='scratch', cwd='.',
                 args=['-c', 'print(1)'], input='', timeout=10, output_limit=1024,
                 lane_generation=1, controller_session='controller1',
                 not_after=1010, submit_expires_at=1005)
    value.update(extra)
    value['request_digest'] = request_digest(value)
    return value


@pytest.mark.parametrize('change', [dict(version=True), dict(env={}), dict(timeout=2147483648),
    dict(timeout=float('inf')), dict(output_limit=MAX_OUTPUT + 1), dict(profile='auth-replace'),
    dict(lane_generation=True), dict(controller_session='../x'), dict(profile_revision='x'),
    dict(submit_expires_at=999), dict(submit_expires_at=1040), dict(not_after=2147485000)])
def test_v2_strict_contract(change):
    with pytest.raises((ValueError, TypeError)):
        validate_v2(wire(**change), now=1000)


def test_digest_and_submission_deadlines():
    value = wire()
    assert validate_v2(value, now=1000) is value
    changed = {**value, 'input': 'changed'}
    with pytest.raises(ValueError, match='digest_conflict'):
        validate_v2(changed, now=1000)
    with pytest.raises(ValueError, match='expired'):
        validate_v2(value, now=1006)
    assert validate_v2(value, now=2000, expired_ok=True) is value


def test_python_adapter_rejects_instead_of_clipping_limits(tmp_path,monkeypatch):
    from core.worker import client
    monkeypatch.setattr(client,'configuration',lambda: {'workspaces':{'scratch':str(tmp_path)}})
    for timeout in (0,2147483648,True):
        with pytest.raises(ValueError,match='resource ceiling'):
            client.request('python',timeout=timeout)


@pytest.mark.parametrize('value', [dict(version=1, op='hello'), dict(version=True, op='hello'),
    dict(version=2, op='hello', lane='forged'),
    dict(version=2, op='cancel', lane_generation=1, controller_session='c')])
def test_control_exact_fields(value):
    with pytest.raises(ValueError):
        validate_control(value)


def test_restart_cannot_ack_idle_over_leftover_cgroup_processes(tmp_path):
    membership = tmp_path / 'membership'
    membership.write_text('0::/worker\n')
    group = tmp_path / 'worker'
    group.mkdir()
    processes = group / 'cgroup.procs'
    processes.write_text('42\n')
    verify_cgroup_clean(membership=membership,root=tmp_path,pid=42)
    processes.write_text('42\n99\n')
    with pytest.raises(RuntimeError,match='cleanup not proved'):
        verify_cgroup_clean(membership=membership,root=tmp_path,pid=42)
    processes.unlink()
    with pytest.raises(RuntimeError,match='cleanup not proved'):
        verify_cgroup_clean(membership=membership,root=tmp_path,pid=42)


@pytest.fixture
def journal(tmp_path):
    value = Journal(tmp_path / 'journal.sqlite3', create=True)
    yield value
    value.close()


def admitted(journal, execution_id='e1'):
    state = journal.call('hello')
    journal.call('admit', execution_id=execution_id, request_digest='a' * 64,
                 lane_generation=state['lane_generation'], controller_session=state['controller_session'],
                 daemon_epoch=state['daemon_epoch'])


def test_history_missing_or_damaged_is_never_recreated(tmp_path):
    path = tmp_path / 'missing'
    with pytest.raises(RuntimeError, match='unknown_history'):
        Journal(path)
    assert not path.exists()
    path.write_text('damaged')
    with pytest.raises(Exception):
        Journal(path)
    assert path.read_text() == 'damaged'


def test_journal_survives_closed_connection_and_process_restart(tmp_path):
    path = tmp_path / 'journal'
    old = Journal(path, create=True)
    old.call('boot')
    old.call('fence', lane_generation=7, controller_session='original')
    admitted(old)
    old.call('running', execution_id='e1')
    old.close()
    new = Journal(path)
    try:
        assert new.call('boot')['lane_generation'] == 7
        receipt = new.call('inspect', execution_id='e1')['receipt']
        assert receipt['outcome'] == 'unknown' and receipt['controller_session'] == 'original'
    finally:
        new.close()


def test_durable_fence_and_conflicting_owner(journal):
    journal.call('boot')
    journal.call('fence', lane_generation=4, controller_session='owner')
    for generation, controller in [(3, 'owner'), (4, 'imposter')]:
        with pytest.raises(ValueError, match='stale_controller'):
            journal.call('fence', lane_generation=generation, controller_session=controller)
    assert journal.call('boot')['lane_generation'] == 4


def test_fencing_then_crash_preserves_original_execution_ownership(journal):
    journal.call('boot')
    journal.call('fence', lane_generation=1, controller_session='original')
    admitted(journal)
    journal.call('fence', lane_generation=2, controller_session='takeover')
    journal.call('boot')
    receipt = journal.call('inspect', execution_id='e1')['receipt']
    assert (receipt['lane_generation'], receipt['controller_session']) == (1, 'original')
    assert receipt['outcome'] == 'unknown'


def test_missing_receipt_row_with_retained_tombstone_never_launches(journal):
    journal.call('boot')
    journal.call('fence', lane_generation=1, controller_session='c')
    admitted(journal)
    journal.db.execute("DELETE FROM executions WHERE id='e1'")  # Inject partial history loss.
    state = journal.call('hello')
    args = dict(execution_id='e1', request_digest='a'*64, lane_generation=1,
                controller_session='c', daemon_epoch=state['daemon_epoch'])
    assert journal.call('admit', **args)['state'] == 'unknown_history'
    assert journal.call('inspect', execution_id='e1')['state'] == 'unknown_history'
    with pytest.raises(ValueError, match='conflict'):
        journal.call('admit', **{**args,'request_digest':'b'*64})


@pytest.mark.parametrize('window', ['admitted', 'running', 'cleanup'])
def test_restart_crash_windows_are_unknown_never_replayed(journal, window):
    old = journal.call('boot')
    journal.call('fence', lane_generation=1, controller_session='c')
    admitted(journal)
    if window != 'admitted':
        journal.call(window, execution_id='e1')
    new = journal.call('boot')
    row = journal.call('inspect', execution_id='e1')
    assert new['daemon_epoch'] != old['daemon_epoch']
    assert row['state'] == 'terminal'
    assert row['receipt']['outcome'] == 'unknown'
    assert row['receipt']['cleanup'] == 'idle_proved'
    assert journal.call('inspect', execution_id='lost')['state'] == 'unknown_history'


def test_terminal_journal_precedes_reply_and_survives_restart(journal):
    journal.call('boot')
    journal.call('fence', lane_generation=1, controller_session='controller1')
    admitted(journal, 'execution1')
    class BrokenConnection:
        def sendall(self, data):
            raise BrokenPipeError()
    r = wire(request_digest='a' * 64)
    # Match this fixture's journal digest.
    r['request_digest'] = 'a' * 64
    owner = ExecutionConnection(BrokenConnection(), journal, r, journal.call('hello')['daemon_epoch'])
    with pytest.raises(BrokenPipeError):
        owner.sendall(json.dumps(dict(type='result', returncode=0, killed=False)).encode())
    journal.call('boot')
    result = journal.call('inspect', execution_id='execution1')
    assert result['receipt']['outcome'] == 'succeeded'
    assert owner.terminal
    assert journal.call('admit', execution_id='execution1', request_digest='a' * 64,
                        lane_generation=1, controller_session='controller1',
                        daemon_epoch=journal.call('hello')['daemon_epoch'])['state'] == 'terminal'
    with pytest.raises(ValueError, match='conflict'):
        journal.call('admit', execution_id='execution1', request_digest='b' * 64,
                     lane_generation=1, controller_session='controller1', daemon_epoch=journal.call('hello')['daemon_epoch'])


@pytest.mark.parametrize('workspace,cwd,launch_error', [
    ('run_' + 'a' * 32, '', None), ('scratch', 'missing', None), ('foreign', '', None),
    ('scratch', '', FileNotFoundError), ('scratch', '', NotADirectoryError), ('scratch', '', PermissionError),
    ('scratch', '', subprocess.SubprocessError)])
def test_resolution_refusal_is_durable_without_launch_or_unknown_outcome(journal, tmp_path, monkeypatch, workspace, cwd, launch_error):
    from core.worker import daemon
    monkeypatch.setattr(daemon, 'policy_ready', lambda cfg: None)
    def forbidden_launch(*args, **kwargs):
        if launch_error:
            raise launch_error('cwd changed after resolution')
        pytest.fail('refused resolution launched a child')
    monkeypatch.setattr(daemon.subprocess, 'Popen', forbidden_launch)
    if os.name == 'nt' and launch_error:
        monkeypatch.setattr(daemon.os, 'getpgrp', lambda: 1, raising=False)
    journal.call('boot')
    journal.call('fence', lane_generation=1, controller_session='controller1')
    admitted(journal, 'execution1')
    events = []
    conn = SimpleNamespace(sendall=lambda data: events.append(json.loads(data)))
    owned = wire(); owned['request_digest'] = 'a' * 64
    owner = ExecutionConnection(conn, journal, owned, journal.call('hello')['daemon_epoch'])
    cfg = {'service_uid': tmp_path.stat().st_uid, 'run_workspace_root': str(tmp_path),
           'workspaces': {'scratch': str(tmp_path)},
           'profiles': {'python': {'argv': ['/usr/bin/python3']}}}
    request = dict(version=1, job_id='execution1', profile='python', workspace=workspace,
                   cwd=cwd, args=[], input='', timeout=10, expires=time.time()+10, output_limit=1024)
    journal.call('running', execution_id='execution1')
    daemon.execute(owner, request, cfg,
                   cleanup=lambda: journal.call('cleanup', execution_id='execution1'))
    receipt = journal.call('inspect', execution_id='execution1')['receipt']
    assert owner.terminal and events[0]['outcome'] == 'failed'
    assert receipt['reason'] == ('workspace or execution setup unavailable before launch' if launch_error
                                 else 'workspace resolution refused')
    assert receipt['cleanup'] == 'idle_proved' and receipt['execution_seconds'] == 0
    assert receipt['stdout_b64'] == receipt['stderr_b64'] == ''
    journal.call('boot')
    assert journal.call('inspect', execution_id='execution1')['receipt'] == receipt


def test_barrier_required_after_fencing_and_stale_cancel_cannot_hit_new_id(journal):
    server = ProtocolServer({'profiles': {}}, None, journal)
    def control(op, generation=1, controller='c', **extra):
        return server.control(dict(version=2, op=op, lane_generation=generation, controller_session=controller, **extra))
    assert server.hello()['version'] == 2
    assert control('idle_barrier')['cleanup'] == 'idle_proved'
    server.active = 'new-job'
    assert control('cancel', execution_id='old-job')['state'] == 'unknown_history'
    assert server.cancel_reason is None
    with pytest.raises(ValueError, match='stale_controller'):
        control('cancel', controller='old-owner', execution_id='new-job')
    assert control('idle_barrier', generation=2)['type'] == 'busy'
    assert server.cancel_reason == 'fenced' and server.barrier is None
    server.active = None
    assert control('idle_barrier', generation=2)['lane_generation'] == 2


def test_context_in_run_fails_closed_and_principal_group_is_stable():
    from core.execution.call_guard import required
    token = required.set(True)
    try:
        with pytest.raises(WorkerUnavailable, match='context required'):
            operation_context()
        context = JobContext('owner', 'principal', 'run:r', 'boundary', 'r', 1)
        with job_context(context):
            assert operation_context() is context
    finally:
        required.reset(token)
    assert operation_context().group == operation_context().group


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', [None, {'outcome': 'succeeded', 'cleanup': 'idle_proved'}])
async def test_recovery_sequence_never_resubmits(terminal):
    calls = []
    row = dict(execution_id='e', request_digest='digest', lane_generation=1, controller_session='old')
    store = SimpleNamespace(barrier=AsyncMock(), get=AsyncMock(return_value=row), settle=AsyncMock(), enforce_active=AsyncMock(return_value=True))
    queue = WorkerQueue(store, SimpleNamespace(hello=lambda: calls.append('hello') or {'active_execution': 'e'}), poll=.001)
    async def control(op, slot, execution_id=None):
        calls.append(op)
        if op == 'idle_barrier':
            return dict(type=op, daemon_epoch='new', cleanup='idle_proved')
        if op == 'inspect':
            return {'receipt': terminal}
        return {'type': 'cancelling'}
    queue.control = control
    await queue.recover(None, dict(generation=2, controller_session='new', active_execution='e'))
    assert calls == ['hello', 'inspect', 'cancel', 'idle_barrier', 'inspect']
    receipt = store.settle.await_args.args[2]
    assert receipt['outcome'] == ('unknown' if terminal is None else 'succeeded')
    store.barrier.assert_awaited_once()
    store.settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_intent_is_persisted_before_observer_exits():
    store = SimpleNamespace(admit=AsyncMock(), get=AsyncMock(return_value={'outcome': 'running'}), cancel=AsyncMock())
    queue = WorkerQueue(store, None, poll=.001)
    context = JobContext('o', 'p', 'g', 'b')
    task = asyncio.create_task(queue.submit(context, {}, execution_id='e'))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.cancel.assert_awaited_once_with('e', context)


@pytest.mark.asyncio
async def test_orderly_shutdown_persists_intent_before_closing_execution_owner():
    events = []
    async def cancel(*args):
        events.append('intent')
    queue = WorkerQueue(SimpleNamespace(cancel=AsyncMock(side_effect=cancel)), None)
    queue.active = dict(execution_id='e', owner='o', principal='p', caller_group='g', boundary='b', run_id=None, run_generation=None)
    async def owner():
        try:
            await asyncio.Event().wait()
        finally:
            events.append('owner_closed')
    queue.task = asyncio.create_task(owner())
    await asyncio.sleep(0)
    await queue.close()
    assert events == ['intent','owner_closed']


@pytest.mark.skipif(sys.platform != 'linux', reason='real Linux subprocess/subreaper/seccomp')
@pytest.mark.parametrize('network',[False,True])
def test_seccomp_protects_supervisor_signals_and_keeps_job_child_management(network):
    source = '''import ctypes,errno,os,socket,subprocess,sys
sys.path.insert(0,sys.argv[1])
from core.worker.restrict import install
parent=os.getppid(); group=os.getpgid(parent)
install(network=sys.argv[2]=='True',protected_pid=parent,protected_pgid=group)
for kind in (socket.SOCK_STREAM,socket.SOCK_DGRAM,socket.SOCK_SEQPACKET):
 a,b=socket.socketpair(socket.AF_UNIX,kind)
 with a,b:a.send(b'ok'); assert b.recv(8)==b'ok'
# Signal 0 never delivers a signal, even if a regression removes the filter.
for target in (parent,-group,-1):
 try: os.kill(target,0)
 except OSError as exc: assert exc.errno==errno.EPERM,exc
 else: raise AssertionError('supervisor signal target permitted')
libc=ctypes.CDLL(None,use_errno=True)
for number,args in [(129,(parent,0,0)),(200,(parent,0)),(234,(parent,parent,0)),
                    (297,(parent,parent,0,0)),(424,(-1,0,0,0))]:
 ctypes.set_errno(0)
 assert libc.syscall(number,*args)==-1 and ctypes.get_errno()==errno.EPERM,(number,ctypes.get_errno())
os.kill(os.getpid(),0)
p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
os.kill(p.pid,0); p.terminate(); p.wait(timeout=5)
'''
    result = subprocess.run([sys.executable,'-I','-c',source,str(Path(__file__).resolve().parents[1]),str(network)],
                            capture_output=True,text=True,timeout=15,start_new_session=True)
    assert result.returncode==0,result.stderr


@pytest.mark.skipif(sys.platform != 'linux', reason='real Linux subprocess/subreaper/seccomp')
@pytest.mark.enable_socket
def test_real_daemon_dedup_and_owner_disconnect(tmp_path):
    """No systemd/DAC claim: the installed namespace is acceptance-only."""
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('outside workspace')
    readonly = tmp_path / 'readonly'
    readonly.mkdir()
    (readonly / 'input').write_text('declared input')
    # The test interpreter may be a uv/venv install outside /usr and /opt/bob.
    # Declare only its installation and this input, never the enclosing home or
    # test-state directory. Keep the real child Landlock/seccomp setup intact.
    runtime = sorted({str(Path(p).resolve()) for p in
                      (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix, sys.executable)})
    config = {'service_uid': os.getuid(), 'worker_uid': os.getuid(), 'lane_id': 'unit',
              'worker_home': str(tmp_path), 'codex_home': str(tmp_path),
              'workspaces': {'scratch': str(workspace)},
              'readonly_inputs': {'scratch': [*runtime, str(readonly)]},
              'profiles': {'python': {'argv': [sys.executable], 'network': False}}}
    source = '''import ctypes,json,socket,sys
from core.worker import daemon as d
from core.worker.journal import Journal
cfg=json.loads(sys.argv[1]); path=sys.argv[2]
libc=ctypes.CDLL(None)
assert libc.prctl(4,0,0,0,0)==0 and libc.prctl(36,1,0,0,0)==0
d.policy_ready=lambda cfg:None; d.share_auth=lambda cfg:None
s=socket.socket(socket.AF_UNIX); s.bind(path); s.listen(20)
j=Journal(path+'.db',create=True); p=d.ProtocolServer(cfg,s,j)
print('ready',flush=True)
while True:p.accept()
'''
    path = str(tmp_path / 'exec.sock')
    process = subprocess.Popen([sys.executable, '-c', source, json.dumps(config), path],
                               cwd=str(Path(__file__).resolve().parents[1]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def connection():
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(15)
        s.connect(path)
        stream = s.makefile('rb')
        s.sendall(b'{"version":2,"op":"hello"}\n')
        hello = json.loads(stream.readline())
        assert hello['version'] == 2
        assert hello['filesystem']['available'], hello['filesystem']
        assert hello['filesystem']['abi'] >= 3
        return s, stream
    def control(op, **extra):
        s, stream = connection()
        with s, stream:
            s.sendall(json.dumps(dict(version=2, op=op, lane_generation=1, controller_session='controller1', **extra)).encode()+b'\n')
            return json.loads(stream.readline())
    try:
        assert process.stdout.readline().strip() == b'ready'
        assert control('idle_barrier')['cleanup'] == 'idle_proved'
        effect = workspace / 'effect'
        program = '''import errno,os
from pathlib import Path
p=Path('effect'); p.write_text(p.read_text()+'x' if p.exists() else 'x')
assert Path(READONLY,'input').read_text()=='declared input'
for path,flags in [(OUTSIDE,os.O_RDONLY),(OUTSIDE,os.O_WRONLY),
                   (str(Path(READONLY,'input')),os.O_WRONLY)]:
 try: fd=os.open(path,flags)
 except OSError as exc: assert exc.errno in (errno.EACCES,errno.EPERM),exc
 else:
  os.close(fd)
  raise AssertionError('Landlock permitted forbidden access: '+path)
print('ok')
'''.replace('READONLY', repr(str(readonly))).replace('OUTSIDE', repr(str(outside)))
        r = wire(profile_revision=profile_revision(config['profiles']['python']),
                 args=['-c', program],
                 not_after=time.time()+10, submit_expires_at=time.time()+5)
        for _ in range(2):
            s, stream = connection()
            with s, stream:
                s.sendall(json.dumps(r).encode()+b'\n')
                while True:
                    event = json.loads(stream.readline())
                    if event['type'] == 'result':
                        assert event['outcome'] == 'succeeded'
                        break
        assert effect.read_text() == 'x'
        assert outside.read_text() == 'outside workspace'
        assert (readonly / 'input').read_text() == 'declared input'
        import sqlite3
        with sqlite3.connect(path+'.db') as database:
            database.execute('DELETE FROM executions WHERE id=?',(r['execution_id'],))
        s, stream = connection()
        with s, stream:
            s.sendall(json.dumps(r).encode()+b'\n')
            event = json.loads(stream.readline())
            assert event['type'] == 'inspection' and event['state'] == 'unknown_history'
        assert effect.read_text() == 'x'
        for exit_path in ('parent_exit','timeout','output_limit'):
            pid_file = workspace / ('pid-' + exit_path)
            program = '''import os,time,pathlib,ctypes
p=pathlib.Path(PIDFILE)
if os.fork()==0:
 os.setsid(); ctypes.CDLL(None).prctl(4,0,0,0,0)
 p.write_text(str(os.getpid()))
 while True: time.sleep(1)
while not p.exists(): time.sleep(.01)
ACTION
'''.replace('PIDFILE',repr(str(pid_file))).replace('ACTION',
    "print('x'*50000,flush=True)" if exit_path == 'output_limit' else 'pass' if exit_path == 'parent_exit' else 'time.sleep(10)')
            r = wire(execution_id=exit_path, profile_revision=profile_revision(config['profiles']['python']),
                     args=['-c',program], timeout=.5, not_after=time.time()+2, submit_expires_at=time.time()+1)
            started = time.monotonic()
            s, stream = connection()
            with s, stream:
                s.sendall(json.dumps(r).encode()+b'\n')
                while True:
                    event = json.loads(stream.readline())
                    if event['type'] == 'result':
                        break
            expected = 'succeeded' if exit_path == 'parent_exit' else 'timed_out' if exit_path == 'timeout' else 'failed'
            assert event['outcome'] == expected
            assert event['cleanup'] == 'idle_proved' and time.monotonic()-started < 5
            assert not Path('/proc',pid_file.read_text()).exists()
        r = wire(execution_id='disconnect', profile_revision=profile_revision(config['profiles']['python']),
                 args=['-c', 'import os,time; print(os.getpid(),flush=True); time.sleep(30)'],
                 not_after=time.time()+10, submit_expires_at=time.time()+5)
        s, stream = connection()
        s.sendall(json.dumps(r).encode()+b'\n')
        event = json.loads(stream.readline())
        pid = int(base64.b64decode(event['data']))
        stream.close(); s.close()
        for _ in range(100):
            if control('inspect', execution_id='disconnect').get('receipt'):
                break
            time.sleep(.02)
        assert control('inspect', execution_id='disconnect')['receipt']['outcome'] == 'cancelled'
        assert not Path('/proc', str(pid)).exists()
    finally:
        process.terminate()
        process.communicate(timeout=10)


@pytest.mark.skipif(sys.platform != 'linux', reason='real Linux DAC/daemon/Landlock subprocess')
@pytest.mark.enable_socket
def test_real_daemon_run_root_search_only_and_resolution_refusals():
    # Outside pytest's possibly mode-0700 ancestors. Root drops to nobody so
    # DAC failures remain real even in root-run Linux gates/containers.
    with tempfile.TemporaryDirectory(prefix='bob-run-resolution-') as temporary:
        base = Path(temporary); base.chmod(0o755)
        service_uid = os.geteuid()
        worker_uid, worker_gid = (65534, 65534) if service_uid == 0 else (service_uid, os.getegid())
        state = base / 'state'; state.mkdir(mode=0o700)
        runs = base / 'run-workspaces'; runs.mkdir()
        selected = runs / ('a' * 32); selected.mkdir()
        sibling = runs / ('b' * 32); sibling.mkdir()
        (sibling / 'private').write_text('unchanged')
        (selected / 'link').symlink_to(sibling / 'private')
        linked = runs / ('c' * 32); linked.symlink_to(selected, target_is_directory=True)
        unprotected = runs / ('d' * 32); unprotected.mkdir()
        missing_input = runs / ('f' * 32); missing_input.mkdir()
        scratch = base / 'scratch'; scratch.mkdir()
        for path in (state, scratch, selected, sibling, unprotected, missing_input, sibling / 'private', runs):
            if service_uid == 0:
                os.chown(path, worker_uid if path in (state, scratch) else service_uid, worker_gid)
        selected.chmod(0o2770); sibling.chmod(0o2770); unprotected.chmod(0o777); missing_input.chmod(0o2770)
        race = selected / 'race'; race.mkdir(mode=0o2770); race.chmod(0o2770)
        (sibling / 'private').chmod(0o660)
        # Non-root owner also gets only search, making this test unprivileged.
        runs.chmod(0o2710 if service_uid == 0 else 0o2110)
        config = {'service_uid': service_uid, 'worker_uid': worker_uid, 'worker_gid': worker_gid,
                  'lane_id': 'unit', 'codex_home': str(state), 'run_workspace_root': str(runs),
                  'workspaces': {'scratch': str(scratch)},
                  'readonly_inputs': {'run_'+missing_input.name: [str(base / 'absent-input')]},
                  'profiles': {'shell': {'argv': ['/bin/sh'], 'network': False}}}
        source = '''import ctypes,json,os,socket,sys
from core.worker import daemon as d
from core.worker.journal import Journal
from core.worker import landlock
from core.portal_resources import safe_open
cfg=json.loads(sys.argv[1]); path=sys.argv[2]
if os.geteuid()==0:
 os.setgroups([]); os.setgid(cfg['worker_gid']); os.setuid(cfg['worker_uid'])
try: os.listdir(cfg['run_workspace_root'])
except PermissionError: pass
else: raise AssertionError('worker can list run root')
try: os.mkdir(cfg['run_workspace_root']+'/forbidden')
except PermissionError: pass
else: raise AssertionError('worker can write run root')
# DAC permits the sibling; the successful child below must deny it by Landlock.
with open(cfg['run_workspace_root']+'/'+'b'*32+'/private','r+') as f:
 assert f.read()=='unchanged'; f.seek(0); f.write('unchanged')
resolve=d.resolve_profile
def resolve_then_remove(cfg, request):
 result=resolve(cfg, request)
 if request['cwd']=='race': os.rmdir(result[1])
 return result
d.resolve_profile=resolve_then_remove
libc=ctypes.CDLL(None)
assert libc.prctl(4,0,0,0,0)==0 and libc.prctl(36,1,0,0,0)==0
d.policy_ready=lambda cfg:None; d.share_auth=lambda cfg:None
s=socket.socket(socket.AF_UNIX); s.bind(path); s.listen(20)
j=Journal(path+'.db',create=True); p=d.ProtocolServer(cfg,s,j)
print('ready',flush=True)
while True:p.accept()
'''
        path = str(state / 'exec.sock')
        process = subprocess.Popen([sys.executable, '-c', source, json.dumps(config), path],
                                   cwd=str(Path(__file__).resolve().parents[1]),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def exchange(value):
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(10); conn.connect(path)
                with conn.makefile('rb') as stream:
                    conn.sendall(b'{"version":2,"op":"hello"}\n')
                    hello = json.loads(stream.readline())
                    assert hello['filesystem']['available'], hello
                    conn.sendall(json.dumps(value).encode()+b'\n')
                    events = []
                    while True:
                        event = json.loads(stream.readline()); events.append(event)
                        if event['type'] != 'chunk': return hello, events
        control = dict(version=2, lane_generation=1, controller_session='controller1')
        def request(execution_id, workspace, cwd='', program='echo should-not-launch > effect'):
            return wire(execution_id=execution_id, profile='shell', workspace=workspace, cwd=cwd,
                        profile_revision=profile_revision(config['profiles']['shell']), args=['-c', program],
                        not_after=time.time()+10, submit_expires_at=time.time()+5)
        try:
            assert process.stdout.readline().strip() == b'ready'
            hello, _ = exchange(dict(control, op='idle_barrier'))
            epoch = hello['daemon_epoch']
            refused = [('missing_run', 'run_'+'e'*32, ''), ('invalid_run', 'run_invalid', ''),
                       ('linked_run', 'run_'+linked.name, ''), ('unprotected_run', 'run_'+unprotected.name, ''),
                       ('missing_cwd', 'run_'+selected.name, 'missing'),
                       ('removed_after_resolve', 'run_'+selected.name, 'race'),
                       ('missing_declared_input', 'run_'+missing_input.name, ''),
                       ('missing_scratch_cwd', 'scratch', 'missing'), ('foreign_workspace', 'foreign', '')]
            for execution_id, workspace, cwd in refused:
                r = request(execution_id, workspace, cwd)
                for _ in range(2):  # Same ID replays the durable refusal.
                    hello, events = exchange(r)
                    receipt = events[-1]
                    assert receipt['type'] == 'result' and receipt['outcome'] == 'failed', events
                    assert receipt['cleanup'] == 'idle_proved' and receipt['execution_seconds'] == 0
                    assert receipt['reason'] and hello['daemon_epoch'] == epoch
                    assert len(events) == 1 and receipt['request_digest'] == r['request_digest']
                _, events = exchange(dict(control, op='inspect', execution_id=execution_id))
                assert events[-1]['receipt']['outcome'] == 'failed'
                assert not (selected / 'effect').exists() and not (scratch / 'effect').exists()
            # A path-escaping cwd is malformed: refused at validation, before admission.
            hello, events = exchange(request('escaped_cwd', 'run_'+selected.name, '../'+sibling.name))
            assert events == [{'type': 'error', 'reason': 'invalid workspace relative cwd'}], events
            assert hello['daemon_epoch'] == epoch and not (selected / 'effect').exists()
            hello, _ = exchange(dict(control, op='idle_barrier'))
            assert hello['daemon_epoch'] == epoch
            # Sibling is DAC-readable/writeable, but Landlock still excludes it.
            # Use shell builtins so a dropped worker needs no test-venv grants.
            program = ('echo ok > allowed; '
                       f'if (echo escaped > {sibling}/private) 2>/dev/null; then exit 20; fi; '
                       'if (echo escaped > link) 2>/dev/null; then exit 21; fi; echo confined')
            for workspace in ('run_'+selected.name, 'scratch'):
                r = request('valid_'+workspace, workspace, program=program if workspace.startswith('run_') else 'echo scratch')
                hello, events = exchange(r)
                assert events[-1]['outcome'] == 'succeeded', events
                assert hello['daemon_epoch'] == epoch and process.poll() is None
            assert (selected / 'allowed').read_text().strip() == 'ok'
            assert (sibling / 'private').read_text() == 'unchanged'
        finally:
            process.terminate()
            process.communicate(timeout=10)
            runs.chmod(0o700)  # permit TemporaryDirectory cleanup as non-root
