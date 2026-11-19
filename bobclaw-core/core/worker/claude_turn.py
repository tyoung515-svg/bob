"""Socket-activated Claude-only supervisors, independent of the tool-job queue.

Only the lane core UID can submit. A fork owns one connection, private pipes,
process group and descendant tree. EOF/Stop/deadline tears down that tree before
an idle receipt. No model-selected executable, path, env or tool authority.
"""
import base64
import ctypes
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

from .contract import MAX_REQUEST, MAX_OUTPUT, peer_lane, profile_proxy_env, root_json
from .claude_turn_contract import validate_start, invocation, input_frame, turn_environment
from .daemon import kill_descendants, policy_ready, send


def frame(conn, limit=MAX_REQUEST):
    data = bytearray()
    while len(data) <= limit:
        peek = conn.recv(min(65536, limit + 1 - len(data)), socket.MSG_PEEK)
        if not peek:
            raise ValueError('Claude controller closed')
        size = peek.find(b'\n') + 1
        data.extend(conn.recv(size or len(peek)))
        if data.endswith(b'\n'):
            return json.loads(data)
    raise ValueError('Claude frame too large')


def supervise(conn, value, config):
    validate_start(value)
    if config.get('profiles', {}).get('claude', {}).get('argv') != ['/usr/local/bin/claude']:
        raise ValueError('Claude fixed profile missing')
    proxy = profile_proxy_env(config, 'claude', value['network_proxy'])
    policy_ready(config)
    libc = ctypes.CDLL(None)
    if libc.prctl(4, 0, 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise RuntimeError('Claude supervisor isolation unavailable')
    reason = None
    with tempfile.TemporaryDirectory(prefix='bob-claude-turn-') as name, tempfile.TemporaryFile() as source:
        temporary = Path(name)
        requests, responses = temporary / 'requests', temporary / 'responses'
        for pipe in (requests, responses):
            os.mkfifo(pipe, 0o600)
        request_fd = os.open(requests, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        response_fd = os.open(responses, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        source.write(input_frame(value['history']).encode())
        source.seek(0)
        supervisor_pid, supervisor_pgid = os.getpid(), os.getpgrp()
        def restrict():
            os.umask(0o077)
            child_libc = ctypes.CDLL(None)
            if child_libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != supervisor_pid:
                os._exit(126)
            from .restrict import install
            from .landlock import install as confine
            install(network=True, protected_pid=supervisor_pid, protected_pgid=supervisor_pgid)
            # No project or scratch grant. Bob tools run in the normal worker.
            confine(temporary, temporary, writable=(config['claude_home'],))
        process = None
        try:
            process = subprocess.Popen(invocation(value, requests, responses), cwd=temporary,
                env=turn_environment(config, temporary, proxy), stdin=source,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
                start_new_session=True, preexec_fn=restrict)
            deadline = time.monotonic() + value['timeout']
            buffers = {'cli': bytearray(), 'mcp': bytearray(), 'control': bytearray()}
            pending = bytearray()
            total = 0
            with selectors.DefaultSelector() as selector:
                selector.register(conn, selectors.EVENT_READ, 'control')
                selector.register(process.stdout, selectors.EVENT_READ, 'cli')
                selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
                selector.register(request_fd, selectors.EVENT_READ, 'mcp')
                while True:
                    policy_ready(config)
                    if time.monotonic() >= deadline:
                        reason = 'timeout'
                        break
                    if pending:
                        try:
                            count = os.write(response_fd, pending)
                            del pending[:count]
                        except BlockingIOError:
                            pass
                    # Drain stdout before considering normal exit.
                    if process.poll() is not None and not any(k.data == 'cli' for k in selector.get_map().values()):
                        break
                    for key, _ in selector.select(.1):
                        chunk = conn.recv(65536) if key.data == 'control' else os.read(key.fd, 65536)
                        if not chunk:
                            if key.data == 'control':
                                reason = 'cancelled'
                                break
                            selector.unregister(key.fileobj)
                            if key.data in buffers and buffers[key.data]:
                                raise ValueError('incomplete Claude stream')
                            continue
                        if key.data == 'stderr':
                            total += len(chunk)  # Never forward arbitrary credential-bearing stderr.
                        else:
                            buffer = buffers[key.data]
                            buffer.extend(chunk)
                            if len(buffer) > MAX_OUTPUT:
                                raise ValueError('Claude stream frame too large')
                            while b'\n' in buffer:
                                line, _, rest = buffer.partition(b'\n')
                                buffer[:] = rest
                                if not line.strip():
                                    continue
                                event = json.loads(line)
                                if not isinstance(event, dict):
                                    raise ValueError('invalid Claude stream object')
                                if key.data == 'control':
                                    if event == {'type': 'stop'}:
                                        reason = 'cancelled'
                                        break
                                    if set(event) != {'type', 'response'} or event['type'] != 'mcp' or not isinstance(event['response'], dict):
                                        raise ValueError('invalid Claude control frame')
                                    pending.extend(json.dumps(event['response']).encode() + b'\n')
                                    if len(pending) > MAX_OUTPUT:
                                        raise ValueError('Claude pipe response too large')
                                else:
                                    total += len(line)
                                    send(conn, {'type': key.data, 'event': event})
                        if total > MAX_OUTPUT:
                            raise ValueError('Claude output limit')
                        if reason:
                            break
                    if reason:
                        break
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            reason = 'protocol_error'
        finally:
            if process is not None:
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
                    except ChildProcessError:
                        break
                    if not pid:
                        if time.monotonic() >= deadline:
                            os._exit(1)  # Fail-stop cgroup cleanup; never assert idle.
                        time.sleep(.01)
            os.close(request_fd)
            os.close(response_fd)
    send(conn, {'type': 'terminal', 'returncode': process.returncode if process else -1, 'reason': reason, 'cleanup': 'idle_proved'})


def serve(config):
    if os.getuid() != config['worker_uid'] or os.getgroups() not in ([], [config['worker_gid']]):
        raise RuntimeError('Claude worker identity mismatch')
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise RuntimeError('root-created Claude socket required')
    policy_ready(config)
    libc = ctypes.CDLL(None)
    if libc.prctl(4, 0, 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise RuntimeError('non-dumpable Claude listener/subreaper required')
    from .daemon import verify_cgroup_clean
    verify_cgroup_clean()
    listener = socket.socket(fileno=3)
    listener.set_inheritable(False)
    listener.settimeout(.2)
    children = set()
    while True:
        for pid in list(children):
            reaped, status = os.waitpid(pid, os.WNOHANG)
            if reaped:
                children.remove(pid)
                if status:
                    # A lost supervisor cannot prove which descendants remain.
                    # Exit the service: systemd kills the WHOLE cgroup before
                    # restart; never keep a potentially orphaned CLI alive.
                    raise RuntimeError('Claude supervisor lost; cgroup cleanup required')
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        try:
            _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            peer_lane(uid, config)
            if len(children) >= 4:
                send(conn, {'type': 'error', 'reason': 'Claude turn capacity unavailable'})
                continue
            pid = os.fork()
            if pid:
                children.add(pid)
                continue
            listener.close()
            status = 1
            try:
                conn.settimeout(10)
                value = frame(conn)
                conn.settimeout(None)
                supervise(conn, value, config)
                status = 0
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                try:
                    send(conn, {'type': 'error', 'reason': 'Claude turn unavailable'})
                except OSError:
                    pass
            finally:
                conn.close()
                os._exit(status)
        except PermissionError:
            pass
        finally:
            conn.close()


if __name__ == '__main__':
    import sys
    serve(root_json(Path(sys.argv[1])))
