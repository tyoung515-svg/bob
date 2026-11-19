"""Socket-activated per-lane executor. Runs only as the unprivileged worker UID."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import subprocess
import tempfile
import time

from .contract import MAX_REQUEST, WorkerUnavailable, child_env, peer_lane, resolve_profile, root_json, validate
from .restrict import install
from .contract import validate_v2, validate_control, profile_revision, profile_proxy_env, MAX_OUTPUT
from .journal import RemoteJournal


def send(conn, event):
    conn.sendall(json.dumps(event).encode() + b'\n')


def acceptance_fault(config, phase, execution_id):
    """Root-installed, exact-ID crash seam; never supplied by a job request."""
    if 'acceptance' in config.get('profiles', {}) and config.get('acceptance_fault') == {'phase': phase, 'execution_id': execution_id}:
        os._exit(83)


def policy_ready(config):
    marker = Path('/run/bob-workers') / config['lane_id'] / 'policy.json'
    receipt = root_json(marker)
    if receipt.get('worker_uid') != os.getuid() or receipt.get('mode') != 'nft-uid':
        raise RuntimeError('worker network policy missing')
    with socket.socket(socket.AF_UNIX) as control:
        control.settimeout(3)
        control.connect(str(marker.with_name('check.sock')))
        _, uid, _ = struct.unpack('3i', control.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0:
            raise RuntimeError('worker firewall guardian identity mismatch')
        control.sendall(b'?')
        if control.recv(1) != b'1':
            raise RuntimeError('worker network policy unavailable')


def share_auth(config):
    """Codex sometimes replaces mode 0600 files: normalize only the checked inode."""
    from .auth_file import checked, directory
    try:
        with directory(Path(config['codex_home'])) as dfd:
            fd = os.open('auth.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            try:
                checked(fd, config['worker_uid'])
                os.fchmod(fd, 0o660)
            finally:
                os.close(fd)
    except FileNotFoundError:
        pass


def kill_descendants():
    """Include setsid/double forks and children forked by non-leader threads.

    Linux reparents orphans to this daemon (PR_SET_CHILD_SUBREAPER). PPid is a
    kernel relationship; task-controlled names, argv, session IDs and cwd are not
    used as authority. Repeat until waitpid establishes that no children remain.
    """
    # A child can set dumpable=0 and disappear from hidepid's status view. Our
    # own task children lists remain readable and identify it without ptrace.
    for task in Path('/proc/self/task').iterdir():
        try:
            for pid in (task / 'children').read_text().split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except FileNotFoundError:
            pass
    parents = {}
    for path in Path('/proc').glob('[0-9]*/status'):
        try:
            lines = path.read_text().splitlines()
            parents[int(path.parent.name)] = int(next(line.split()[1] for line in lines if line.startswith('PPid:')))
        except (OSError, ValueError, StopIteration):
            continue
    descendants = {os.getpid()}
    while True:
        found = {pid for pid, parent in parents.items() if parent in descendants}
        if found <= descendants:
            break
        descendants |= found
    for pid in descendants - {os.getpid()}:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def verify_cgroup_clean(*, membership=Path('/proc/self/cgroup'), root=Path('/sys/fs/cgroup'), pid=None):
    """Do not mint a new epoch over systemd leftovers, including stuck tasks."""
    pid = os.getpid() if pid is None else pid
    try:
        paths = [line.split(':', 2)[2] for line in membership.read_text().splitlines() if line.startswith('0::')]
        if len(paths) != 1 or '..' in Path(paths[0]).parts:
            raise ValueError('unified worker cgroup required')
        members = {int(value) for value in (root / paths[0].lstrip('/') / 'cgroup.procs').read_text().split()}
        if members != {pid}:
            raise ValueError('previous worker cgroup not clean')
    except (OSError, ValueError) as exc:
        raise RuntimeError('worker restart cleanup not proved') from exc


def job_umask(workspace):
    """Run workspaces are setgid to the lane worker group, which the service user
    is in: group-readable output lets the service snapshot the job's result.
    Every other workspace (scratch, home, codex-home) stays owner-only."""
    return 0o007 if workspace.startswith('run_') else 0o077


def execute(conn, request, config, *, tick=None, cleanup=None, prevalidated=False, proxy_url=None):
    if not prevalidated:
        validate(request)
    policy_ready(config)
    def refuse_resolution(reason):
        # No child/effects exist yet. Persist a known refusal through the same
        # terminal-receipt path, so the queue need not recover an unknown job.
        if cleanup:
            cleanup()
        send(conn, dict(type='result', job_id=request['job_id'], returncode=-1,
                        killed=False, reason=reason, output_bytes=0,
                        execution_seconds=0, cleanup_seconds=0))
    try:
        argv, cwd, network = resolve_profile(config, request)
        proxy_env = profile_proxy_env(config, request['profile'], proxy_url)
    except WorkerUnavailable as exc:
        refuse_resolution(str(exc))
        return
    if request['profile'] == 'auth-replace':
        from .auth_file import replace
        if request['args']:
            raise ValueError('auth profile takes no arguments')
        value = json.loads(request['input'])
        if set(value) != {'expected', 'auth'} or not isinstance(value['auth'], dict):
            raise ValueError('invalid auth update')
        replaced = replace(Path(config['codex_home']) / 'auth.json', config['worker_uid'],
                           value['expected'], value['auth'])
        send(conn, dict(type='result', returncode=0 if replaced else 1, killed=False))
        return
    total, killed, reason = 0, False, None
    started = time.monotonic()
    deadline = started + min(request['timeout'], request['expires'] - time.time())
    if deadline <= time.monotonic():
        raise ValueError('execution deadline expired before launch')
    cleanup_started = False
    cleanup_at = None
    def begin_cleanup():
        nonlocal cleanup_started, cleanup_at
        if not cleanup_started:
            cleanup_at = time.monotonic()
            cleanup_started = True
    # File stdin avoids a pipe write deadlock before output draining starts.
    with tempfile.TemporaryDirectory(prefix='bob-job-') as temporary, tempfile.TemporaryFile() as source:
        source.write(request['input'].encode())
        source.seek(0)
        supervisor_pid, supervisor_pgid = os.getpid(), os.getpgrp()
        def restrict_child():
            os.umask(job_umask(request['workspace']))
            install(network=network,protected_pid=supervisor_pid,protected_pgid=supervisor_pgid)
            from .landlock import install as confine
            selected = (Path(config['run_workspace_root']) / request['workspace'][4:]
                        if request['workspace'].startswith('run_') else Path(config['workspaces'][request['workspace']]))
            if request['profile'] == 'builder':
                from .builder_profile import project_root
                selected = project_root(config, request)
            confine(selected, temporary,
                    readonly=config.get('readonly_inputs', {}).get(request['workspace'], ()),
                    writable=((config['codex_home'],) if request['profile'] == 'codex' else
                              (config['claude_home'],) if request['profile'] == 'claude' else ()))
        try:
            process = subprocess.Popen(argv, cwd=cwd, env={**child_env(config, temporary,
                                       codex=request['profile'] == 'codex',
                                        claude=request['profile'] == 'claude'), **proxy_env}, stdin=source,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       close_fds=True, start_new_session=True, preexec_fn=restrict_child)
        except (FileNotFoundError, NotADirectoryError, PermissionError, subprocess.SubprocessError):
            # cwd/profile may disappear or lose access after resolution. Popen
            # reports a failed chdir/preexec/exec after reaping that unlaunched
            # child. No job code ran; do not turn this race into a daemon exit.
            refuse_resolution('workspace or execution setup unavailable before launch')
            return
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                selector.register(conn, selectors.EVENT_READ, 'control')
                open_streams = 2
                while open_streams or process.poll() is None:
                    control_reason = tick() if tick else None
                    if control_reason:
                        killed, reason = True, control_reason
                        break
                    if process.poll() is not None and not cleanup_started:
                        # A normal parent exit also ends the job. Detached
                        # descendants may still hold stdout/stderr open: kill
                        # them now, then drain the bounded remaining pipe data.
                        begin_cleanup()
                        kill_descendants()
                    share_auth(config)
                    if time.monotonic() >= deadline:
                        killed, reason = True, 'timeout'
                        break
                    for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                        if key.data == 'control':
                            # EOF or any additional control bytes cancel this connection's job.
                            conn.recv(1)
                            killed, reason = True, 'cancelled'
                            break
                        chunk = os.read(key.fd, min(8192, request['output_limit'] - total + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            open_streams -= 1
                            continue
                        allowed = chunk[:max(0, request['output_limit'] - total)]
                        total += len(chunk)
                        if allowed:
                            try:
                                send(conn, dict(type='chunk', stream=key.data,
                                                data=base64.b64encode(allowed).decode()))
                            except OSError:
                                killed, reason = True, 'cancelled'
                                break
                        if total > request['output_limit']:
                            killed, reason = True, 'output_limit'
                            break
                    if killed:
                        break
        finally:
            begin_cleanup()
            # Fast path, followed by subreaper/tree cleanup for detached descendants.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            kill_descendants()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
            deadline = time.monotonic() + 5
            while True:
                kill_descendants()
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                    if not pid:
                        if time.monotonic() >= deadline:
                            # A broken descendant invariant restarts the entire cgroup.
                            os._exit(1)
                        time.sleep(.01)
                except ChildProcessError:
                    break
            share_auth(config)
            # Killing/reaping must not wait on journal availability. A failed
            # cleanup/terminal write then fail-stops, preserving unknown state.
            if cleanup:
                cleanup()
        send(conn, dict(type='result', job_id=request['job_id'], returncode=process.returncode,
                        killed=killed, reason=reason, uid=os.getuid(), output_bytes=min(total, request['output_limit']),
                        execution_seconds=max(0, cleanup_at-started), cleanup_seconds=max(0,time.monotonic()-cleanup_at)))


class ExecutionConnection:
    """Persist the complete bounded receipt before sending its terminal event."""
    def __init__(self, conn, journal, request, epoch, config=None, *, proxy_token=None):
        self.conn, self.journal, self.request, self.epoch = conn, journal, request, epoch
        self.output = {'stdout': bytearray(), 'stderr': bytearray()}
        self.terminal = False
        self.config = config or {}
        self.proxy_token = proxy_token

    def fileno(self):
        return self.conn.fileno()

    def recv(self, size):
        return self.conn.recv(size)

    def sendall(self, data):
        event = json.loads(data)
        if event['type'] == 'chunk':
            self.output[event['stream']].extend(base64.b64decode(event['data']))
        elif event['type'] == 'result':
            r = self.request
            if self.proxy_token:
                # Redact after assembling chunks, including tokens split across
                # pipe reads, before any terminal output reaches the journal.
                for key, value in self.output.items():
                    self.output[key] = value.replace(self.proxy_token.encode(), b'[job-token]')
            event.update(execution_id=r['execution_id'], request_digest=r['request_digest'],
                         lane_generation=r['lane_generation'], controller_session=r['controller_session'],
                         daemon_epoch=self.epoch, cleanup='idle_proved',
                         outcome=('timed_out' if event.get('reason') == 'timeout' else
                                  'cancelled' if event.get('reason') in {'cancelled', 'fenced'} else
                                  'failed' if event.get('killed') or event.get('returncode') else 'succeeded'))
            receipt = {**event, **{key + '_b64': base64.b64encode(value).decode() for key, value in self.output.items()}}
            acceptance_fault(self.config, 'before_terminal', r['execution_id'])
            self.journal.call('terminal', execution_id=r['execution_id'], receipt=receipt)
            self.terminal = True
            acceptance_fault(self.config, 'after_terminal', r['execution_id'])
            data = json.dumps(event).encode() + b'\n'
        self.conn.sendall(data)


class ProtocolServer:
    def __init__(self, config, listener, journal):
        self.config, self.listener, self.journal = config, listener, journal
        self.state = journal.call('boot')
        self.active = None
        self.cancel_reason = None
        self.barrier = None

    def hello(self):
        from .landlock import abi, MIN_ABI
        try:
            filesystem = {'available': True, 'abi': abi(), 'minimum_abi': MIN_ABI}
        except RuntimeError:
            filesystem = {'available': False, 'reason': 'landlock_abi_3_required', 'minimum_abi': MIN_ABI}
        return dict(filesystem=filesystem, type='hello', version=2, features=['submit', 'inspect', 'cancel', 'idle_barrier'],
                    **{**self.state, 'active_execution': self.active},
                    profiles={k: profile_revision(v) for k, v in self.config['profiles'].items()},
                    max_timeout=self.config.get('max_timeout', 3600), max_output=MAX_OUTPUT, compatibility_v1=False,
                    job_time_presets=[900, 1800, 3600])

    def fence(self, value):
        old = self.state['lane_generation']
        self.state = self.journal.call('fence', lane_generation=value['lane_generation'],
                                       controller_session=value['controller_session'])
        if value['lane_generation'] > old:
            self.barrier = None
            if self.active:
                self.cancel_reason = 'fenced'

    def control(self, value):
        validate_control(value)
        if value['op'] == 'hello':
            return self.hello()
        self.fence(value)
        if value['op'] == 'inspect':
            return dict(type='inspection', **self.journal.call('inspect', execution_id=value['execution_id']),
                        daemon_epoch=self.state['daemon_epoch'])
        if value['op'] == 'cancel':
            row = self.journal.call('inspect', execution_id=value['execution_id'])
            if self.active == value['execution_id']:
                # A new controller may fence old work, but cannot cancel another ID.
                self.cancel_reason = 'cancelled'
                return dict(type='cancelling', execution_id=self.active)
            return dict(type='inspection', **row)
        if self.active:
            return dict(type='busy', reason='blocked_by_cleanup', **self.state)
        self.barrier = (value['lane_generation'], self.state['daemon_epoch'])
        return dict(type='idle_barrier', cleanup='idle_proved', **self.state)

    def tick(self):
        import select
        while select.select([self.listener], [], [], 0)[0]:
            self.accept(control_only=True)
        policy_ready(self.config)
        return self.cancel_reason

    def read(self, conn):
        # Peek preserves pipelined commands without unbounded readline buffering.
        data = bytearray()
        while len(data) <= MAX_REQUEST:
            peek = conn.recv(min(65536, MAX_REQUEST + 1 - len(data)), socket.MSG_PEEK)
            size = peek.find(b'\n') + 1
            part = conn.recv(size or len(peek)) if peek else b''
            if not part:
                raise ValueError('incomplete worker frame')
            data.extend(part)
            if len(data) > MAX_REQUEST:
                raise ValueError('worker request too large')
            if part.endswith(b'\n'):
                return json.loads(data)
        raise ValueError('worker request too large')

    def accept(self, *, control_only=False):
        conn, _ = self.listener.accept()
        with conn:
            conn.settimeout(2)
            try:
                _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                peer_lane(uid, self.config)
                value = self.read(conn)
                # Every connection explicitly negotiates v2; raw v1 cannot bypass it.
                validate_control(value)
                if value != {'version': 2, 'op': 'hello'}:
                    raise ValueError('v2 hello required')
                send(conn, self.hello())
                value = self.read(conn)
                if value.get('op') != 'submit':
                    send(conn, self.control(value))
                    return
                if control_only or self.active:
                    raise ValueError('lane_busy')
                # A per-dispatch credential travels outside the frozen request.
                # It is never written to the journal or admitted job input.
                proxy_url = value.pop('network_proxy', None)
                validate_v2(value, expired_ok=True, max_timeout=self.config.get('max_timeout', 3600))
                profile_proxy_env(self.config, value['profile'], proxy_url)
                self.fence(value)
                previous = self.journal.call('inspect', execution_id=value['execution_id'])
                if previous['state'] == 'unknown_history' and previous.get('history_known'):
                    if previous.get('request_digest') != value['request_digest']:
                        raise ValueError('request_digest_conflict')
                    send(conn, dict(type='inspection', **previous))
                    return
                if previous['state'] != 'unknown_history':
                    if previous['request_digest'] != value['request_digest']:
                        raise ValueError('request_digest_conflict')
                    receipt = previous.get('receipt')
                    if receipt:
                        for stream in ('stdout', 'stderr'):
                            data = base64.b64decode(receipt.get(stream + '_b64', ''))
                            for start in range(0, len(data), 8192):
                                send(conn, dict(type='chunk', stream=stream, data=base64.b64encode(data[start:start+8192]).decode()))
                        send(conn, {k: v for k, v in receipt.items() if not k.endswith('_b64')})
                    else:
                        send(conn, dict(type='inspection', **previous))
                    return
                validate_v2(value, max_timeout=self.config.get('max_timeout', 3600))
                if self.barrier != (value['lane_generation'], self.state['daemon_epoch']):
                    raise ValueError('idle_barrier_required')
                if value['profile_revision'] != profile_revision(self.config['profiles'].get(value['profile'], {})):
                    raise ValueError('profile_revision_conflict')
                policy_ready(self.config)
                admission = self.journal.call('admit', execution_id=value['execution_id'], request_digest=value['request_digest'],
                                  lane_generation=value['lane_generation'], controller_session=value['controller_session'],
                                  daemon_epoch=self.state['daemon_epoch'])
                if admission['state'] != 'new':
                    send(conn, dict(type='inspection', **admission))
                    return
                acceptance_fault(self.config, 'after_admission', value['execution_id'])
                self.active = value['execution_id']
                self.cancel_reason = None
                owner = ExecutionConnection(conn, self.journal, value, self.state['daemon_epoch'], self.config,
                    proxy_token=proxy_url.split('bob:', 1)[1].split('@', 1)[0] if proxy_url else None)
                old = {k: value[k] for k in ('profile', 'workspace', 'cwd', 'args', 'input', 'output_limit')}
                old.update(version=1, job_id=self.active, timeout=min(value['timeout'], value['not_after'] - time.time()),
                           expires=value['not_after'])
                try:
                    self.journal.call('running', execution_id=self.active)
                    execute(owner, old, self.config, tick=self.tick,
                            cleanup=lambda: self.journal.call('cleanup', execution_id=self.active), prevalidated=True,
                            proxy_url=proxy_url)
                finally:
                    if not owner.terminal:
                        # An exception/effect without a terminal receipt is never success.
                        # Fail-stop: systemd kills the cgroup before boot can prove idle.
                        os._exit(1)
                    self.active = None
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
                try:
                    send(conn, dict(type='error', reason=str(exc)[:180]))
                except OSError:
                    pass


def serve(config, *, auth_only=False):
    if os.getuid() != config['worker_uid'] or os.getgroups() not in ([], [config['worker_gid']]):
        raise RuntimeError('worker identity/group mismatch')
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise RuntimeError('root-created socket activation required')
    policy_ready(config)
    # Kill children if the daemon is killed; systemd additionally kills the cgroup.
    import ctypes
    libc = ctypes.CDLL(None)
    if libc.prctl(4, 0, 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise RuntimeError('non-dumpable subreaper unavailable')
    listener = socket.socket(fileno=3)
    listener.set_inheritable(False)
    listener.settimeout(1)
    if not auth_only:
        verify_cgroup_clean()
        protocol = ProtocolServer(config, listener, RemoteJournal(config))
        while True:
            try:
                protocol.accept()
            except socket.timeout:
                continue
    while True:
        share_auth(config)
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        with conn:
            conn.settimeout(10)
            try:
                _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                peer_lane(uid, config)
                with conn.makefile('rb') as source:
                    line = source.readline(MAX_REQUEST + 2)
                if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
                    raise ValueError('worker request too large/incomplete')
                conn.settimeout(5)
                request = json.loads(line)
                if auth_only and (not isinstance(request, dict) or request.get('profile') != 'auth-replace'):
                    raise ValueError('auth socket accepts only shared-auth publication')
                execute(conn, request, config)
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                try:
                    send(conn, dict(type='error', reason='worker request refused or execution unavailable'))
                except OSError:
                    pass
        # Reap orphan descendants after killpg, including double forks.
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
            except ChildProcessError:
                break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    parser.add_argument('--auth-only', action='store_true')
    args = parser.parse_args()
    serve(root_json(args.config), auth_only=args.auth_only)


if __name__ == '__main__':
    main()
