"""Nonsecret root-published epoch; the active pointer is the commit point."""
import json
import os
from pathlib import Path
import re
import stat

from .settings_contract import SettingsError


def enabled():
    return bool(os.environ.get('BOB_SETTINGS_ID'))


def active():
    lane = os.environ.get('BOB_SETTINGS_ID', '')
    if not re.fullmatch('[0-9a-f]{12}', lane):
        raise SettingsError('credential_state_unavailable')
    path = Path('/var/lib/bob-settings') / lane / 'credentials' / 'active.json'
    try:
        for parent in reversed(path.parents):
            info = parent.lstat()
            if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
                raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (info.st_uid != 0 or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o022 or info.st_size > 4096):
                raise ValueError()
            value = json.loads(os.read(fd, 4097))
        finally:
            os.close(fd)
        if type(value['epoch']) is not int or value['epoch'] < 1:
            raise ValueError()
        # A gateway must never vouch a newly-read epoch using authority proved
        # under its old loaded generation. Core still reads the current pointer
        # independently to fence already-durable work; it has a different role.
        if os.environ.get('BOB_SERVICE_ROLE') == 'gateway' and loaded_epoch() != value['epoch']:
            raise ValueError()
        return value
    except Exception:
        raise SettingsError('credential_state_unavailable') from None


def loaded_epoch():
    try:
        value = int(os.environ.get('BOB_CREDENTIAL_EPOCH', '0'))
        if value < 1:
            raise ValueError()
        return value
    except ValueError:
        raise SettingsError('credential_generation_mismatch') from None


def require_loaded():
    if enabled() and loaded_epoch() != active()['epoch']:
        raise SettingsError('credential_generation_mismatch')
