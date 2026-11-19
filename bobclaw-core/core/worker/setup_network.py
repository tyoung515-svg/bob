"""Root setup only: fixed CLI policies, socket lifetime, no job authority."""
from contextlib import contextmanager
import json
import os
import secrets
import socket
import struct
import time

from .contract import profile_proxy_env


@contextmanager
def setup_cli_env(config, deadline, profile):
    """Hold a non-inheritable control FD for the helper's remaining time."""
    if profile not in {'codex', 'claude'}:
        raise ValueError('unsupported setup profile')
    if os.geteuid() != 0:
        raise PermissionError('root setup required')
    remaining = min(900, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError('setup deadline reached')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(min(5, remaining))
        control.connect('/run/bob-egress-' + config['lane_id'] + '/control.sock')
        _, uid, _ = struct.unpack('3i', control.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != config['egress_uid']:
            raise PermissionError('proxy identity mismatch')
        token = secrets.token_hex(32)
        # Enrollment and connection time both consume the original deadline.
        control.sendall((json.dumps(dict(type='setup-' + profile, token=token, profile=profile,
            expires_at=time.time() + min(remaining, deadline-time.monotonic())))+'\n').encode())
        with control.makefile('rb') as stream:
            if json.loads(stream.readline(32768)).get('ready') is not True:
                raise ValueError('setup proxy enrollment refused')
            yield profile_proxy_env(config, profile,
                f'http://bob:{token}@127.0.0.1:{config["egress_port"]}')


@contextmanager
def setup_codex_env(config, deadline):
    with setup_cli_env(config, deadline, 'codex') as env:
        yield env
