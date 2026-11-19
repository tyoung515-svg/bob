"""Core-side socket client, synchronous and asynchronous adapters."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import time
import uuid
import threading
import select

from .contract import MAX_OUTPUT, MAX_TIMEOUT, WorkerUnavailable, validate

EXCLUDED = frozenset({'claude_alternate', 'agy_code', 'kimi_cli', 'pi_code',
                      'claude_pc', 'local_strata', 'opencode_serve', 'docker', 'gui'})
EXCLUDED_REASON = 'unavailable: route is not enrolled in the isolated worker release'


def refuse(*args, **kwargs):
    raise WorkerUnavailable(EXCLUDED_REASON)


async def refuse_async(*args, **kwargs):
    refuse()


def configuration() -> dict:
    from core.lane_config import get_lane
    lane = get_lane()
    lane_id = lane.values.get('BOB_WORKER_ID')
    if not lane_id:
        raise WorkerUnavailable('unavailable: lane has no worker boundary; existing-lane migration required')
    from .contract import root_json
    try:
        cfg = root_json(Path('/etc/bob/workers') / (lane_id + '.json'))
    except (OSError, ValueError, WorkerUnavailable) as exc:
        raise WorkerUnavailable('unavailable: worker configuration missing or invalid') from exc
    if not isinstance(cfg, dict) or not {'lane_id', 'worker_uid', 'socket', 'auth_socket', 'workspaces', 'journal_socket', 'journal_path'} <= cfg.keys():
        raise WorkerUnavailable('unavailable: incomplete worker configuration')
    return cfg


def unavailable_reason(profile=None) -> str | None:
    try:
        cfg = configuration()
        path = Path(cfg['auth_socket'] if profile == 'auth-replace' else cfg['socket'])
        if not path.is_socket():
            return 'unavailable: worker socket/service missing'
        # Policy readiness is checked again by the root pre-start helper and daemon.
        marker = Path('/run/bob-workers') / cfg['lane_id'] / 'policy.json'
        from .contract import root_json
        if root_json(marker).get('worker_uid') != cfg['worker_uid']:
            return 'unavailable: worker network policy missing'
    except (OSError, ValueError, WorkerUnavailable):
        return 'unavailable: worker service or network policy missing'
    return None


def request(profile: str, *, cwd=None, args=(), input='', timeout=120, output_limit=MAX_OUTPUT) -> dict:
    cfg = configuration()
    target = Path(cwd or cfg['workspaces']['scratch']).resolve()
    run_root=Path(cfg['run_workspace_root']).resolve() if cfg.get('run_workspace_root') else None
    import re
    choices = sorted(cfg['workspaces'].items(), key=lambda row: len(row[1]), reverse=True)
    if run_root and target.parent==run_root and re.fullmatch('[0-9a-f]{32}',target.name):
        choices=[('run_'+target.name,str(target)),*choices]
    for workspace, path in choices:
        root = Path(path).resolve()
        if target.is_relative_to(root):
            break
    else:
        raise WorkerUnavailable('unavailable: workspace is not registered')
    if timeout is not None and type(timeout) not in (int, float):
        raise ValueError('invalid resource ceiling')
    ceiling = float(120 if timeout is None else timeout)
    return validate(dict(version=1, job_id=uuid.uuid4().hex, profile=profile,
                         workspace=workspace, cwd='' if workspace.startswith('run_') and target==root else target.relative_to(root).as_posix(),
                         args=list(args), input=input or '', timeout=ceiling,
                         output_limit=output_limit, expires=time.time() + ceiling + 20),
                    max_timeout=900 if profile == 'auth-replace' else MAX_TIMEOUT)


def _auth_exchange(payload: dict, *, on_chunk=None) -> dict:
    cfg = configuration()
    reason = unavailable_reason(payload['profile'])
    if reason:
        raise WorkerUnavailable(reason)
    validate(payload)
    out = bytearray()
    err = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(payload['timeout'] + 30)
            sock.connect(cfg['auth_socket'] if payload['profile'] == 'auth-replace' else cfg['socket'])
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != 0:  # SO_PEERCRED identifies the root-created activation listener.
                raise WorkerUnavailable('worker server identity mismatch')
            sock.sendall(json.dumps(payload).encode() + b'\n')
            with sock.makefile('rb') as stream:
                while True:
                    line = stream.readline(65537)
                    if not line or len(line) > 65536:
                        raise WorkerUnavailable('worker closed without a terminal receipt')
                    event = json.loads(line)
                    if event.get('type') == 'chunk':
                        data = base64.b64decode(event['data'], validate=True)
                        (out if event['stream'] == 'stdout' else err).extend(data)
                        if len(out) + len(err) > payload['output_limit']:
                            raise WorkerUnavailable('worker output exceeded admitted limit')
                        if on_chunk:
                            on_chunk(event['stream'], data)
                    elif event.get('type') == 'result':
                        return {**event, 'stdout': bytes(out), 'stderr': bytes(err)}
                    else:
                        raise WorkerUnavailable(event.get('reason', 'worker request refused'))
    except (OSError, ValueError) as exc:
        raise WorkerUnavailable('unavailable: worker transport/protocol failed') from exc


async def _auth_execute_async(profile, **kwargs):
    # Closing the actual async socket on cancellation cancels the job, including descendants.
    cfg = configuration()
    payload = request(profile, **kwargs)
    reason = unavailable_reason(profile)
    if reason:
        raise WorkerUnavailable(reason)
    reader, writer = await asyncio.open_unix_connection(
        cfg['auth_socket'] if profile == 'auth-replace' else cfg['socket'], limit=65536)
    try:
        sock = writer.get_extra_info('socket')
        _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0:
            raise WorkerUnavailable('worker server identity mismatch')
        writer.write(json.dumps(payload).encode() + b'\n')
        await writer.drain()
        out, err = bytearray(), bytearray()
        async with asyncio.timeout(payload['timeout'] + 30):
            while True:
                line = await reader.readline()
                if not line:
                    raise WorkerUnavailable('worker closed without a terminal receipt')
                event = json.loads(line)
                if event.get('type') == 'chunk':
                    (out if event['stream'] == 'stdout' else err).extend(base64.b64decode(event['data'], validate=True))
                    if len(out) + len(err) > payload['output_limit']:
                        raise WorkerUnavailable('worker output exceeded admitted limit')
                elif event.get('type') == 'result':
                    return {**event, 'stdout': bytes(out), 'stderr': bytes(err)}
                else:
                    raise WorkerUnavailable(event.get('reason', 'worker request refused'))
    finally:
        writer.close()
        await writer.wait_closed()


class Transport:
    """Scheduler-only v2 wire transport. Public callers enter exchange instead."""
    def __init__(self, cfg):
        self.cfg = cfg

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect(self.cfg['socket'])
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != 0:
                raise WorkerUnavailable('worker server identity mismatch')
            sock.sendall(b'{"version":2,"op":"hello"}\n')
            return sock
        except BaseException:
            sock.close()
            raise

    def frame(self, sock, buffer, *, stop=None, deadline=None):
        from .journal import WIRE_LIMIT
        while b'\n' not in buffer:
            if stop and stop.is_set():
                raise WorkerUnavailable('execution owner lost controller')
            if deadline is not None and time.monotonic() >= deadline:
                raise WorkerUnavailable('worker terminal receipt deadline exceeded')
            if not select.select([sock], [], [], .2)[0]:
                continue
            part = sock.recv(65536)
            if not part:
                raise WorkerUnavailable('worker closed without terminal receipt')
            buffer.extend(part)
            if len(buffer) > WIRE_LIMIT:
                raise WorkerUnavailable('worker response too large')
        line, _, rest = buffer.partition(b'\n')
        buffer[:] = rest
        value = json.loads(line)
        if value.get('type') == 'error':
            raise WorkerUnavailable(value.get('reason', 'worker refused'))
        return value

    def negotiated(self, sock, buffer):
        hello = self.frame(sock, buffer, deadline=time.monotonic() + 10)
        if hello.get('type') != 'hello' or hello.get('version') != 2 or not {'submit', 'inspect', 'cancel', 'idle_barrier'} <= set(hello.get('features', [])):
            raise WorkerUnavailable('worker v2 negotiation failed')
        return hello

    def hello(self):
        with self.connect() as sock:
            return self.negotiated(sock, bytearray())

    def control(self, value):
        with self.connect() as sock:
            buffer = bytearray()
            self.negotiated(sock, buffer)
            sock.sendall(json.dumps(value).encode() + b'\n')
            return self.frame(sock, buffer, deadline=time.monotonic() + 10)

    def submit(self, value, stop):
        if value['profile'] in {'codex', 'claude', 'mcp'}:
            # submit runs in the queue's transport thread, not the core loop.
            # Keep callbacks alive while the blocking socket drains the job.
            return asyncio.run(self._profile_submit(value, stop))
        return self._submit(value, stop)

    async def _profile_submit(self, value, stop):
        from .profile_network import ProfileNetwork
        async with ProfileNetwork(value, self.cfg) as network:
            receipt = await asyncio.to_thread(self._submit, value, stop, proxy_url=network.url)
        return network.receipt(receipt)

    def _submit(self, value, stop, *, proxy_url=None):
        output = {'stdout': bytearray(), 'stderr': bytearray()}
        with self.connect() as sock:
            buffer = bytearray()
            self.negotiated(sock, buffer)
            # Ephemeral transport metadata, not part of the durable job input or
            # request digest. Only the peer-checked core controller supplies it.
            wire = {**value, 'network_proxy': proxy_url} if proxy_url else value
            sock.sendall(json.dumps(wire).encode() + b'\n')
            deadline = time.monotonic() + value['timeout'] + 30
            while True:
                event = self.frame(sock, buffer, stop=stop, deadline=deadline)
                if event.get('type') == 'chunk':
                    data = base64.b64decode(event['data'], validate=True)
                    output[event['stream']].extend(data)
                    if sum(map(len, output.values())) > value['output_limit']:
                        raise WorkerUnavailable('worker output exceeded admitted limit')
                elif event.get('type') == 'result':
                    return {**event, **{k + '_b64': base64.b64encode(v).decode() for k, v in output.items()}}
                else:
                    raise WorkerUnavailable('worker did not provide a terminal receipt')


_bridges = {}
_bridge_lock = threading.Lock()


def queue_bridge():
    from core.execution.worker_queue import QueueBridge
    from core.config import config
    cfg = configuration()
    with _bridge_lock:
        key = cfg['lane_id']
        if key not in _bridges:
            _bridges[key] = QueueBridge(cfg, config.POSTGRES_URL)
        return _bridges[key]


def close_queues():
    with _bridge_lock:
        bridges = list(_bridges.values())
        _bridges.clear()
    for bridge in bridges:
        bridge.close()


def exchange(payload: dict, *, on_chunk=None, on_wait=None, reconcile=None) -> dict:
    """Explicit v1-shaped Python compatibility adapter; always queues child jobs."""
    validate(payload, max_timeout=900 if payload['profile'] == 'auth-replace' else MAX_TIMEOUT)
    if payload['profile'] == 'auth-replace':
        return _auth_exchange(payload, on_chunk=on_chunk)
    reason = unavailable_reason(payload['profile'])
    if reason:
        raise WorkerUnavailable(reason)
    from core.execution.jobs import operation_context
    bridge = queue_bridge()
    context = operation_context()
    if reconcile is not None:
        future = bridge.submit(context, payload, on_wait=on_wait, reconcile=reconcile)
    else:
        future = bridge.submit(context, payload, on_wait=on_wait) if on_wait else bridge.submit(context, payload)
    try:
        receipt = future.result()
    except BaseException:
        future.cancel()
        raise
    if on_chunk:
        for stream in ('stdout', 'stderr'):
            if receipt[stream]:
                on_chunk(stream, receipt[stream])
    return receipt


async def execute_async(profile, **kwargs):
    on_wait = kwargs.pop('on_wait', None)
    reconcile = kwargs.pop('reconcile', None)
    if profile == 'auth-replace':
        return await _auth_execute_async(profile, **kwargs)
    payload = request(profile, **kwargs)
    reason = unavailable_reason(profile)
    if reason:
        raise WorkerUnavailable(reason)
    from core.execution.jobs import operation_context
    # Bridge initialization can involve DB IO; do not block the core event loop.
    context = operation_context()
    bridge = await asyncio.to_thread(queue_bridge)
    if hasattr(bridge, 'ready'):
        await asyncio.shield(asyncio.wrap_future(bridge.ready))
    if callable(getattr(bridge, 'cancel_and_wait', None)):
        execution_id = payload['job_id']
        future = bridge.submit(context, payload, execution_id=execution_id, on_wait=on_wait, reconcile=reconcile)
        try:
            return await asyncio.shield(asyncio.wrap_future(future))
        except asyncio.CancelledError:
            try:
                rows = await asyncio.shield(asyncio.wrap_future(bridge.cancel_and_wait(execution_id, context, future)))
            except Exception as exc:
                raise WorkerUnavailable('cancellation cleanup not established; execution_id=' + execution_id) from exc
            finally:
                future.cancel()
            stopped = asyncio.CancelledError()
            # Cancellation is only a known stop after the installed journal
            # proves cancelled + idle. A completed or unknown mutation must
            # never be relabeled as an action that did not happen.
            if isinstance(rows,list) and all(row.get('outcome')=='cancelled' and row.get('cleanup')=='idle_proved' for row in rows):
                stopped.worker_cleanup = 'idle_proved'
                stopped.execution_ids = [row['execution_id'] for row in rows]
            raise stopped
    # No await between creating the observer and linking cancellation to it.
    if reconcile is not None:
        future = bridge.submit(context, payload, on_wait=on_wait, reconcile=reconcile)
    else:
        future = bridge.submit(context, payload, on_wait=on_wait) if on_wait else bridge.submit(context, payload)
    return await asyncio.wrap_future(future)


def run(command, *, cwd=None, input=None, timeout=120, text=False, encoding=None,
        errors='replace', capture_output=True, env=None, **kwargs):
    """subprocess.run-shaped adapter; env is intentionally never transmitted."""
    if kwargs or not capture_output:
        raise WorkerUnavailable('unsupported worker execution option')
    command = list(map(str, command))
    binary = Path(command[0]).name
    if binary == 'git':
        profile, args = 'git', command[1:]
        if len(args) >= 2 and args[0] == '-C':
            cwd, args = args[1], args[2:]
    elif binary in {'python', 'python.exe', 'python3', 'python3.13'}:
        profile, args = 'python', command[1:]
    else:
        profile, args = 'shell', ['-c', 'exec ' + shlex.join(command)]
    payload = request(profile, cwd=cwd, args=args,
                      input=input.decode('utf-8') if isinstance(input, bytes) else input, timeout=timeout)
    receipt = exchange(payload)
    out, err = receipt['stdout'], receipt['stderr']
    if text or encoding:
        out, err = out.decode(encoding or 'utf-8', errors), err.decode(encoding or 'utf-8', errors)
    if receipt.get('killed'):
        raise subprocess.TimeoutExpired(command, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(command, receipt['returncode'], out, err)
