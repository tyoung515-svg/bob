"""Runtime-only helpers; never import application modules or load host secrets."""
from __future__ import annotations

from core.worker import mode as execution_mode

import os
import pwd
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile

DATA = Path('/var/lib/bob')
SOURCE = Path('/opt/bob')
MIGRATIONS = Path('/var/lib/bob-worker-migrations')
SYSTEMD = Path('/etc/systemd/system')


def require_root() -> None:
    if os.geteuid() != 0:
        raise ValueError('run this command as root inside the runtime')


def root_directory(path: Path, *, mode=0o755, gid=0) -> None:
    """Create/repair a root-owned directory without following symlinks or umask."""
    from core.worker.auth_file import directory
    with directory(path, create=True) as fd:
        os.fchown(fd, 0, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)


def read_env(path: Path, *, safe=False) -> dict[str, str]:
    """Read our systemd EnvironmentFile subset without shell expansion/execution."""
    if os.name == 'nt' or not safe:  # Preserve legacy operator parsing; migration opts into dirfds.
        payload = path.read_text()
    else:
        from core.worker.auth_file import directory
        with directory(path.parent) as dfd:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                    raise ValueError('unsafe environment file')
                with os.fdopen(fd, 'r', closefd=False) as stream:
                    payload = stream.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    raise ValueError('oversized environment file')
            finally:
                os.close(fd)
    result = {}
    for line in payload.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator:
            raise ValueError(f'invalid environment file: {path}')
        tokens = shlex.split(value, comments=False, posix=True)
        if len(tokens) > 1:
            raise ValueError(f'unquoted environment value: {path}')
        result[key] = tokens[0] if tokens else ''
    return result


def render_env(values: dict[str, str]) -> str:
    lines = []
    for key, value in values.items():
        if not key or not key.replace('_', '').isalnum() or not key[0].isalpha():
            raise ValueError('invalid environment key')
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('environment values cannot contain control characters')
        value = value.replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'{key}="{value}"')
    return '\n'.join(lines) + '\n'


def private_write(path: Path, content: str, uid: int, gid: int, *, mode=0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), mode)
        os.fchown(stream.fileno(), uid, gid)
        stream.write(content)


def systemctl(*args: str) -> None:
    subprocess.run(['systemctl', *args], check=True)


def private_replace(path: Path, content: str, uid: int, gid: int) -> None:
    """Atomically replace a private file without following a destination symlink."""
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt' and execution_mode() == 'worker':
            from core.worker.auth_file import directory
            with directory(path.parent) as parentfd:
                os.fsync(parentfd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def migration_pending(name: str) -> bool:
    return (MIGRATIONS / name / 'pending').exists()


def writer_guard(name: str) -> None:
    if migration_pending(name):
        raise ValueError('lane migration incomplete; retry bob-runtime migrate-workers before settings/enrollment')


def ensure_codex_home(root: Path, *, allow_pending=False) -> Path:
    if not allow_pending:
        writer_guard(root.parent.name)
    if execution_mode() == "legacy":
        account = pwd.getpwnam('bob')
        home = root / 'codex-home'
        home.mkdir(mode=0o700, exist_ok=True)
        if home.is_symlink():
            raise ValueError('codex-home must be a directory, not a symlink')
        os.chmod(home, 0o700)
        os.chown(home, account.pw_uid, account.pw_gid)
        config = home / 'config.toml'
        if not config.exists():
            private_write(config, 'cli_auth_credentials_store = "file"\n\n'
                          '[profiles.gpt]\nmodel_provider = "openai"\n\n', account.pw_uid, account.pw_gid)
        return home
    from worker import load_identity
    ids = load_identity(root.parent.name)
    home = root / 'codex-home'
    from core.worker.auth_file import directory
    if os.name == 'nt':  # Configuration unit tests only; root entrypoints are Linux-only.
        home.mkdir(exist_ok=True)
        if home.is_symlink():
            raise ValueError('codex-home must not be a symlink')
    else:
        with directory(home, create=True) as fd:
            os.fchown(fd, ids['worker_uid'], ids['worker_gid'])
            os.fchmod(fd, 0o2750)
    # Service has traversal/read here, and rw only on checked auth.json (0660).
    # It cannot create/replace entries; atomic publication runs as the worker.
    config = home / 'config.toml'
    if not config.exists() and not config.is_symlink():
        private_write(config, 'cli_auth_credentials_store = "file"\n\n'
                      '[profiles.gpt]\nmodel_provider = "openai"\n\n', ids['worker_uid'], ids['worker_gid'], mode=0o640)
    return home
