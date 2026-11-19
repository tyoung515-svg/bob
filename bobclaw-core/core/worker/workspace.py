"""Service-side data writers for worker-controlled workspaces; never follow paths."""
import os
from pathlib import Path
import secrets
from .auth_file import directory


def mkdir(path):
    path = Path(path)
    if os.name == 'nt':  # Windows is a unit-test host, not the Linux runtime.
        path.mkdir(parents=True, exist_ok=True)
    else:
        with directory(path, create=True):
            pass


def write(path, content):
    path = Path(path)
    if os.name == 'nt':
        path.write_text(content, encoding='utf-8', newline='\n')
        return
    with directory(path.parent) as dfd:
        temporary = '.bob-data-' + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o660, dir_fd=dfd)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            try:
                os.unlink(temporary, dir_fd=dfd)
            except FileNotFoundError:
                pass
