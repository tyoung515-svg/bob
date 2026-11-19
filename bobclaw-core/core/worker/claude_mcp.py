"""Fixed stdio relay. Only this turn's Landlock grant exposes the two FIFOs."""
import json
import os
from pathlib import Path
import stat
import sys

LIMIT = 4 * 1024 * 1024


def relay(requests, responses, source=None, target=None):
    source = source or sys.stdin.buffer
    target = target or sys.stdout.buffer
    paths = [Path(requests), Path(responses)]
    if paths[0].parent != paths[1].parent:
        raise ValueError('invalid turn channel')
    parent = paths[0].parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise ValueError('invalid turn directory')
    fds = []
    try:
        for name, flags in zip(paths, (os.O_WRONLY, os.O_RDONLY)):
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC)
            fds.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ValueError('invalid turn pipe')
        with os.fdopen(fds.pop(), 'rb', buffering=0) as incoming, os.fdopen(fds.pop(), 'wb', buffering=0) as outgoing:
            while True:
                line = source.readline(LIMIT + 1)
                if not line:
                    break
                if len(line) > LIMIT or not line.endswith(b'\n'):
                    raise ValueError('invalid MCP frame')
                request = json.loads(line)
                outgoing.write(line)
                if 'id' in request:
                    response = incoming.readline(LIMIT + 1)
                    if not response or len(response) > LIMIT or not response.endswith(b'\n'):
                        raise ValueError('turn channel closed')
                    target.write(response)
                    target.flush()
    finally:
        for fd in fds:
            os.close(fd)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(2)
    relay(*sys.argv[1:])
