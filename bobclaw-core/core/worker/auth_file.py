"""Checked lane CLI file access. Every POSIX operation is relative to pinned dirfds."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat


@contextmanager
def directory(path: Path, *, create=False):
    # Walk from / without following even an ancestor symlink. Do not resolve first.
    if os.name == 'nt':
        raise OSError('Unix worker file boundary is unavailable on Windows')
    # Ancestors only need search (x) permission, e.g. a 0711 /var/lib/bob: hold them as
    # O_PATH dirfds. O_DIRECTORY|O_NOFOLLOW still refuses a symlinked component. The
    # final directory is a real O_RDONLY fd so callers can fsync it.
    walk = getattr(os, 'O_PATH', os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = path.absolute().parts[1:]
    fd = os.open('/', (os.O_RDONLY if not parts else getattr(os, 'O_PATH', os.O_RDONLY)) | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            if part in {'.', '..'}:
                raise ValueError('invalid auth directory')
            if create:
                try:
                    os.mkdir(part, 0o770, dir_fd=fd)
                except FileExistsError:
                    pass
            last = index == len(parts) - 1
            child = os.open(part, (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) if last else walk, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def checked(fd: int, uid: int):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1 or info.st_size > 1024 * 1024:
        raise ValueError('unsafe shared auth file')
    return info


def read_at(dirfd: int, uid: int, name='auth.json') -> bytes:
    if name not in {'auth.json', 'config.toml', '.credentials.json', '.claude.json'}:
        raise ValueError('unsupported shared-home file')
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
    try:
        checked(fd, uid)
        data = os.read(fd, 1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError('oversized auth')
        return data
    finally:
        os.close(fd)


def read(path: Path, uid: int) -> dict:
    with directory(path.parent) as fd:
        value = json.loads(read_at(fd, uid, path.name))
    return value if isinstance(value, dict) else {}


def configuration_text(home: Path, uid: int) -> str:
    with directory(home) as fd:
        return read_at(fd, uid, 'config.toml').decode('utf-8')


def replace(path: Path, uid: int, expected: str, value: dict) -> bool:
    """Called as the worker UID, preserving ownership on atomic replacement.

    The service cannot chown a new inode to the worker. It submits this bounded
    compare/replace to its authenticated worker instead of writing a service-owned
    inode or following a worker-controlled temporary filename.
    """
    if os.geteuid() != uid:
        raise PermissionError('shared auth replacement requires its worker owner')
    with directory(path.parent) as directory_fd:
        if hashlib.sha256(read_at(directory_fd, uid)).hexdigest() != expected:
            return False
        name = '.auth-bob-' + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                checked(stream.fileno(), uid)
                os.fchmod(stream.fileno(), 0o660)
                stream.write(json.dumps(value, ensure_ascii=False).encode() + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            # Revalidate the destination; rename replaces the entry, never follows it.
            if hashlib.sha256(read_at(directory_fd, uid)).hexdigest() != expected:
                return False
            os.replace(name, 'auth.json', src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
            return True
        finally:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
