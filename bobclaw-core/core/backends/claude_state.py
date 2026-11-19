"""Lane-local, content-bounded Claude observations and bridge transcript.

Core owns this file; the worker cannot use it to grant authority. Credential
state is only an uncertainty marker, scoped to root's sign-in generation.
"""
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time

NAME = 'claude-runtime.json'
_lock = threading.Lock()


def load(root, generation):
    if os.name == 'nt':
        if not (Path(root) / NAME).exists():
            return {}
        raise ValueError('Claude runtime evidence requires Linux inode checks')
    try:
        fd = os.open(Path(root) / NAME, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 2 * 1024 * 1024:
                raise ValueError('invalid Claude observation store')
            value = json.load(source)
        return value if value.get('generation') == generation else {}
    except FileNotFoundError:
        return {}


def update(root, generation, change):
    root = Path(root)
    with _lock:
        fd = os.open(root / (NAME + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('invalid Claude observation lock')
            if os.name != 'nt':
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            value = load(root, generation) or {'generation': generation, 'models': {}, 'turns': []}
            change(value)
            data = json.dumps(value, ensure_ascii=False).encode()
            if len(data) > 2 * 1024 * 1024:
                raise ValueError('Claude observation store too large')
            temporary = root / ('.claude-state-' + secrets.token_hex(16))
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(out, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, root / NAME)
                sync_directory(root)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            os.close(fd)
    return value


def sync_directory(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def record(turn, kind, payload):
    from core.lane_config import get_lane
    from core.tools.projects import _current_user_id, _current_conversation_id
    from core.tools.memory_tools import memory_tool_face_id
    import hashlib
    import re
    if not re.fullmatch('[0-9a-f]{32}', turn):
        raise ValueError('invalid Claude turn identifier')
    lane = get_lane()
    folder = lane.root / 'claude-turns'
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('invalid Claude transcript directory')
    raw = json.dumps(payload, ensure_ascii=False)
    content = payload if len(raw.encode()) <= 32768 else {
        'truncated': True, 'sha256': hashlib.sha256(raw.encode()).hexdigest(), 'preview': raw[:4096]}
    event = {'kind': kind, 'at': time.time(), 'owner': _current_user_id.get(),
        'conversation': _current_conversation_id.get(), 'face': memory_tool_face_id.get(), 'payload': content}
    fd = os.open(folder / (turn + '.jsonl'), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('invalid Claude transcript')
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(folder)
    sync_directory(lane.root)
    def change(value):
        if kind == 'model':
            value.setdefault('models', {}).setdefault(payload['selection'], payload['reported'])
        if kind == 'signed_out':
            value['status'] = 'unknown'
        if kind == 'usage':
            value['last_usage'] = payload
    if kind in {'model', 'signed_out', 'usage'}:
        return update(lane.root, lane.values.get('CLAUDE_AUTH_GENERATION', ''), change)
