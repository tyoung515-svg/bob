"""Trusted gateway client and provider-only vault reader. No HTTP key reads."""
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct

from .settings_contract import MAX_REQUEST, PROVIDERS, SettingsError, validate

VAULT_STATE = Path('/var/lib/bob-settings')


def installed_id():
    from core.worker import mode
    lane_id = os.environ.get('BOB_SETTINGS_ID', '')
    if mode() != 'worker' or not re.fullmatch('[0-9a-f]{12}', lane_id):
        raise SettingsError('settings_identity_split_required')
    return lane_id


def _root_path(path, *, socket_file=False):
    """Verify every listener ancestor; symlinks and writable parents fail closed."""
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if (info.st_uid != 0 or stat.S_ISLNK(info.st_mode) or info.st_mode & (0o002 if parent == path and socket_file else 0o022)
                or (parent == path and socket_file and not stat.S_ISSOCK(info.st_mode))):
            raise SettingsError('unsafe_settings_listener')


def call(value, *, receipt=False):
    validate(value, receipt=receipt)
    lane_id = installed_id()
    path = Path('/run/bob-settings') / lane_id / ('receipt.sock' if receipt else 'control.sock')
    try:
        _root_path(path, socket_file=True)
        if not receipt and os.getuid() != int(os.environ['BOB_GATEWAY_UID']):
            raise SettingsError('settings_identity_split_required')
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(10)
            connection.connect(str(path))
            if struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] != 0:
                raise SettingsError('unsafe_settings_listener')
            connection.sendall(json.dumps(value, ensure_ascii=False).encode() + b'\n')
            with connection.makefile('rb') as stream:
                raw = stream.readline(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
            raise SettingsError('invalid_receipt')
        result = json.loads(raw)
        if 'error' in result:
            code = result['error']
            raise SettingsError(code if code in {'request_key_used', 'not_found', 'unsupported_provider',
                                                 'invalid_key', 'invalid_password', 'invalid_args', 'peer_denied',
                                                 'invalid_factors', 'recovery_busy', 'authentication_throttled'} else 'settings_failed')
        return result
    except SettingsError:
        raise
    except Exception:
        raise SettingsError('settings_unavailable') from None


def unavailable_reason():
    try:
        lane_id = installed_id()
        if os.getuid() != int(os.environ.get('BOB_GATEWAY_UID', '-1')):
            return 'settings_identity_split_required'
        _root_path(Path('/run/bob-settings') / lane_id / 'control.sock', socket_file=True)
        return None
    except Exception:
        return 'settings_helper_or_identity_unavailable'


def provider_key(provider, fallback):
    """Read live rotations only at trusted provider construction/dispatch.

    Settings publication enrolls the provider's preset separately. This reader
    only resolves the latest key; it never changes defaults or enables fallbacks.
    """
    if not os.environ.get('BOB_SETTINGS_ID'):
        return fallback
    if provider not in PROVIDERS or os.environ.get('BOB_SERVICE_ROLE') != 'core':
        return fallback
    return vault_key(provider, installed_id(), fallback)


def vault_key(provider, lane_id, fallback):
    """Trusted lane catalog/root reader; no credential bytes enter public rows."""
    if provider not in PROVIDERS or not isinstance(lane_id, str) or not re.fullmatch('[0-9a-f]{12}', lane_id):
        raise SettingsError('invalid_lane')
    from core.worker.auth_file import directory
    path = VAULT_STATE / lane_id / 'vault'
    try:
        with directory(path) as fd:
            child = os.open(provider + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                info = os.fstat(child)
                if (info.st_uid != 0 or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode)
                        or info.st_mode & 0o027 or info.st_size > MAX_REQUEST):
                    raise SettingsError('unsafe_vault')
                value = json.loads(os.read(child, MAX_REQUEST + 1))
            finally:
                os.close(child)
        if value['provider'] != provider or not isinstance(value['key'], str):
            raise SettingsError('unsafe_vault')
        return value['key']
    except FileNotFoundError:
        return fallback
    except Exception:
        raise SettingsError('provider_credentials_unavailable') from None
