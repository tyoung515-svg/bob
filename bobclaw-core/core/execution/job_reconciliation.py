"""Service-side current-workspace evidence for trusted resumable adapters.

This hashes registered directories; it is not a new workspace/Jobs API and does
not establish external effects. A tool-specific trusted checker must do that.
"""
from pathlib import Path
import hashlib
import os
import stat

from .job_policy import Reconciliation
from .models import digest, canonical
from core.worker.contract import WorkerUnavailable, resolve_profile


def workspace_snapshot(root, *, max_files=10000, max_bytes=256*1024*1024):
    root = Path(root).resolve(strict=True)
    entries, total = [], 0
    def visit(directory):
        nonlocal total
        before = directory.stat()
        with os.scandir(directory) as children:
            children = sorted(children, key=lambda entry:entry.name)
        for entry in children:
            item = Path(entry.path)
            info = item.lstat()
            relative = item.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                raise WorkerUnavailable('workspace reconciliation refuses links')
            if stat.S_ISDIR(info.st_mode):
                entries.append([relative,'directory',stat.S_IMODE(info.st_mode)])
                visit(item)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if len(entries) >= max_files or total > max_bytes:
                    raise WorkerUnavailable('workspace reconciliation bound exceeded')
                flags = os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_BINARY',0)
                fd = os.open(item,flags)
                with os.fdopen(fd,'rb') as source:
                    opened = os.fstat(source.fileno())
                    if (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns) != (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns):
                        raise WorkerUnavailable('workspace changed during reconciliation')
                    content = hashlib.sha256()
                    remaining = info.st_size
                    while chunk := source.read(min(65536,remaining+1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise WorkerUnavailable('workspace changed during reconciliation')
                        content.update(chunk)
                    after = os.fstat(source.fileno())
                    if remaining or (after.st_size,after.st_mtime_ns,after.st_ctime_ns) != (opened.st_size,opened.st_mtime_ns,opened.st_ctime_ns):
                        raise WorkerUnavailable('workspace changed during reconciliation')
                current = item.lstat()
                if (current.st_dev,current.st_ino,current.st_mode,current.st_nlink,current.st_size,current.st_mtime_ns,current.st_ctime_ns) != (info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns):
                    raise WorkerUnavailable('workspace changed during reconciliation')
                entries.append([relative,'file',stat.S_IMODE(info.st_mode),content.hexdigest()])
            else:
                raise WorkerUnavailable('workspace reconciliation refuses special/shared files')
            if len(entries) > max_files:
                raise WorkerUnavailable('workspace reconciliation bound exceeded')
        after = directory.stat()
        if (before.st_ino,before.st_dev,before.st_mtime_ns) != (after.st_ino,after.st_dev,after.st_mtime_ns):
            raise WorkerUnavailable('workspace changed during reconciliation')
    visit(root)
    return dict(digest=digest(entries),files=len(entries),bytes=total,manifest=entries)


def installed_snapshot(row):
    from core.worker.client import configuration
    cfg = configuration()
    _, directory, _ = resolve_profile(cfg,row['request'])
    return workspace_snapshot(directory)


def verify_installed_workspace(row):
    return installed_snapshot(row)['digest']


class WorkspaceReconciler:
    def __init__(self, effects_check):
        self.effects_check = effects_check

    def __call__(self, row):
        if row['outcome'] != 'timed_out' or row['cleanup'] != 'idle_proved':
            raise WorkerUnavailable('known timeout and cleanup required')
        snapshot = installed_snapshot(row)
        # A model response is never an effects checker. This callable is installed
        # service adapter code checking actual files/acknowledgements.
        effects = self.effects_check(row,snapshot)
        if not isinstance(effects,dict) or effects.get('established') is not True or effects.get('resumable') is not True or not effects.get('evidence'):
            raise WorkerUnavailable('adapter cannot establish resumable effects')
        if len(canonical([snapshot['manifest'],effects]).encode()) > 1024*1024:
            raise WorkerUnavailable('reconciliation evidence bound exceeded')
        proof = Reconciliation(snapshot['digest'],digest(effects),True,True,snapshot['manifest'],effects)
        if verify_installed_workspace(row) != snapshot['digest']:
            raise WorkerUnavailable('workspace changed during effects inspection')
        return proof,snapshot['digest']
