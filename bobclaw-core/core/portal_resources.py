"""Owner read services, imported by the authenticated gateway (no worker API).

Folder registration is deployment-local, never a browser-supplied filesystem root.
The gateway resolves project ownership before invoking these domain operations.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager

from core.permissions import Scope, evaluate_path

MAX_READ = 65536
MAX_FILE = 16 * 1024 * 1024
KINDS = ('family_memory', 'bob_event', 'bob_fact')
OPS = ('memory.list', 'files.roots', 'files.list', 'files.read')
OP_CAPABILITIES = {op: {'version': 1, 'implemented': True, 'readable': True,
                        'writable': False, 'runnable': False} for op in OPS}


class ResourceError(Exception):
    def __init__(self, code, status=400, diagnostics=None):
        super().__init__(code)
        self.code, self.status = code, status
        self.diagnostics = diagnostics


def filesystem_error(exc, stage):
    """Keep syscall evidence without exposing exception strings or host paths."""
    number = getattr(exc, 'errno', None)
    if number == errno.ENOENT:
        code, status = 'not_found', 404
    elif number in {errno.EACCES, errno.EPERM, errno.EROFS}:
        code, status = 'permission_denied', 403
    elif number in {errno.EEXIST, errno.ENOTDIR, errno.EISDIR, errno.ELOOP, errno.ESTALE}:
        code, status = 'conflict', 409
    else:
        code, status = 'io_error', 503
    return ResourceError(code, status, {'stage': stage, 'errno': number})


@contextmanager
def filesystem_stage(stage):
    try:
        yield
    except ResourceError:
        raise
    except (OSError, ValueError) as exc:
        raise filesystem_error(exc, stage) from None


def integer(value, default, maximum, minimum=0):
    value = default if value is None else value
    if type(value) is not int or not minimum <= value <= maximum:
        raise ResourceError('invalid_args')
    return value


def relative_parts(path, *, empty=False, include_reserved=False):
    if not isinstance(path, str):
        raise ResourceError('invalid_path')
    try:
        if len(path.encode('utf-8')) > 2048:
            raise ResourceError('invalid_path')
    except UnicodeError:
        raise ResourceError('invalid_path') from None
    if path == '' and empty:
        return ()
    if (not path or '\\' in path or '%' in path or ':' in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path) or PureWindowsPath(path).drive
            or path.startswith('/')):
        raise ResourceError('invalid_path')
    parts = tuple(path.split('/'))
    if len(parts) > 32 or any(p in ('', '.', '..') for p in parts):
        raise ResourceError('invalid_path')
    # Project browsing deliberately excludes credentials and control metadata.
    if not include_reserved and any(p.startswith('.') or p.casefold() in {
        'node_modules', '__pycache__', 'credentials', 'secrets', 'lane.env',
        'providers.env', 'gateway.db', 'project-folders.json',
    } or p.casefold().endswith(('.pem', '.key', '.env')) for p in parts):
        raise ResourceError('scope_denied', 403)
    return parts


def scope_allows(scope, path):
    try:
        return evaluate_path(str(path), Scope.model_validate(scope)) == 'auto'
    except Exception:
        return False


@contextmanager
def safe_open(root: Path, parts, *, directory=False):
    """No symlinks, including ancestors; POSIX uses dirfds throughout traversal.

    Windows lacks openat: reject reparse points and recheck containment after open.
    Linux's descriptor walk is the race-resistant runtime gate.
    """
    root = Path(root).absolute()
    target = root.joinpath(*parts)
    fds = []
    stage = 'lstat'
    try:
        for p in (root, *root.parents):
            info = p.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ResourceError('not_found', 404)
        if os.open in os.supports_dir_fd:
            # Traversal requires search permission, not directory listing. In
            # particular, workers only have --x on the service's run root.
            walk = getattr(os, 'O_PATH', os.O_RDONLY) | os.O_NOFOLLOW | os.O_DIRECTORY
            flags = walk if parts else os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
            stage = 'open-root'
            fd = os.open(root, flags)
            fds.append(fd)
            for i, part in enumerate(parts):
                final = i == len(parts) - 1
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK if final else walk
                if final and directory:
                    flags |= os.O_DIRECTORY
                stage = 'open-dir' if not final or directory else 'open-file'
                fd = os.open(part, flags, dir_fd=fd)
                fds.append(fd)
        else:
            for i in range(1, len(parts) + 1):
                info = root.joinpath(*parts[:i]).lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ResourceError('not_found', 404)
            if not target.resolve().is_relative_to(root.resolve()):
                raise ResourceError('not_found', 404)
            fd = os.open(target, os.O_RDONLY | getattr(os, 'O_BINARY', 0)) if not directory else None
            if fd is not None:
                fds.append(fd)
            if not target.resolve().is_relative_to(root.resolve()):
                raise ResourceError('not_found', 404)
        stage = 'fstat'
        info = os.fstat(fd) if fd is not None else target.stat()
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ResourceError('not_found', 404)
        stage = 'descriptor-operation'
        yield fd, target, info
    except (OSError, ValueError) as exc:
        raise filesystem_error(exc, stage) from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def owned_memory(root, db, owner, scope):
    rows = []
    total_bytes = 0

    def add(row):
        nonlocal total_bytes
        total_bytes += len(row['content'].encode('utf-8'))
        if len(rows) >= 20000 or total_bytes > 32 * 1024 * 1024:
            raise ResourceError('snapshot_too_large', 413)
        row['revision'] = hashlib.sha256(row['content'].encode('utf-8')).hexdigest()
        rows.append(row)

    for name in (owner, 'shared'):
        for filename in (('MEMORY.md',) if name != 'shared' else ('MEMORY.md', 'SHARED_MEMORY.md', 'PINBOARD.md')):
            path = Path(root) / name / filename
            if not scope_allows(scope, path):
                continue
            try:
                with safe_open(Path(root), (name, filename)) as (fd, _, info):
                    if info.st_size > MAX_FILE:
                        raise ResourceError('snapshot_too_large', 413)
                    with os.fdopen(os.dup(fd), 'rb') as stream:
                        text = stream.read(MAX_FILE + 1).decode('utf-8').replace('\r\n', '\n')
            except ResourceError as exc:
                if exc.status == 404:
                    continue
                raise
            except UnicodeError:
                raise ResourceError('invalid_memory_source', 503) from None
            for i, part in enumerate(text.split('\n\n')):
                if part.strip():
                    add({'source_id': f'family:{name}:{filename}:{i}', 'owner': name,
                         'kind': 'family_memory', 'content': part,
                         'provenance': {'path': str(path), 'paragraph': i}})
    # The runtime runs the gateway under its own account, apart from the core that owns the lane
    # database. A file this account cannot read is reported in coverage, not raised as a fault.
    db_available = bool(db and Path(db).is_file() and os.access(db, os.R_OK))
    unattributed = 0
    # The lane DB is server-selected; scope must also authorize the owner's memory.
    db_allowed = scope_allows(scope, Path(root) / owner / 'MEMORY.md')
    if db_available and db_allowed:
        try:
            with sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True) as conn:
                conn.execute('BEGIN')
                events = set()
                for eid, body, ts in conn.execute('SELECT event_id, body_json, ts FROM memory_events ORDER BY insertion_order'):
                    value = json.loads(body)
                    if value.get('user_id') is None:
                        unattributed += 1
                    if value.get('user_id') != owner:
                        continue
                    events.add(eid)
                    add({'source_id': 'event:' + eid, 'owner': owner, 'kind': 'bob_event',
                         'content': body, 'provenance': {'event_id': eid, 'timestamp': ts,
                                                        'ownership': 'explicit_user_id'}})
                for fid, body, eid, ts in conn.execute('SELECT fact_id, body_json, source_event_id, ts FROM memory_facts ORDER BY fact_id'):
                    if eid in events:
                        add({'source_id': 'fact:' + fid, 'owner': owner, 'kind': 'bob_fact',
                             'content': body, 'provenance': {'fact_id': fid, 'source_event_id': eid, 'timestamp': ts}})
        except (sqlite3.Error, ValueError, TypeError):
            raise ResourceError('memory_unavailable', 503) from None
    coverage = {'authority': 'server', 'sources': list(KINDS), 'complete_corpus': False,
                'enumeration_complete': True, 'search_mode': 'enumeration',
                'counts_by_kind': {k: sum(r['kind'] == k for r in rows) for k in KINDS},
                'lane_database': 'included' if db_available and db_allowed else 'unavailable_or_out_of_scope',
                'unattributed_events_excluded': unattributed,
                'excluded': ['unattributed historical records', 'other owners', 'project/corpus files']}
    return rows, coverage


class Snapshots:
    """Bounded, expiring server snapshots; random cursors reveal no source offsets."""
    def __init__(self, ttl=300, clock=time.monotonic):
        self.ttl, self.clock, self.entries = ttl, clock, {}

    def page(self, binding, rows=None, coverage=None, cursor=None, limit=50):
        now = self.clock()
        for key in list(self.entries):
            if self.entries[key]['expires'] <= now:
                del self.entries[key]
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 200:
                raise ResourceError('invalid_cursor')
            entry = next((v for v in self.entries.values() if cursor in v['cursors']), None)
            if entry is None:
                raise ResourceError('snapshot_expired', 410)
            if entry['binding'] != binding:
                raise ResourceError('invalid_cursor')
            offset = entry['cursors'][cursor]
        else:
            if len(self.entries) >= 16:
                raise ResourceError('snapshot_capacity', 503)
            size = len(json.dumps(rows, ensure_ascii=False).encode())
            if size + sum(v['size'] for v in self.entries.values()) > 64 * 1024 * 1024:
                raise ResourceError('snapshot_capacity', 503)
            sid = secrets.token_urlsafe(24)
            entry = {'binding': binding, 'rows': rows, 'coverage': coverage,
                     'expires': now + self.ttl, 'id': sid, 'cursors': {}, 'size': size}
            self.entries[sid] = entry
            offset = 0
        end = offset + limit
        next_cursor = None
        if end < len(entry['rows']):
            next_cursor = next((k for k, v in entry['cursors'].items() if v == end), None)
            if next_cursor is None:
                next_cursor = secrets.token_urlsafe(32)
                entry['cursors'][next_cursor] = end
        return {'items': entry['rows'][offset:end], 'next_cursor': next_cursor,
                'snapshot': entry['id'], 'coverage': entry['coverage']}


def register_project_folder(lane_root, owner, project_id, relative_path, label):
    """Operator-only registration helper. No HTTP/agent registration operation exists.

    Only subdirectories of the lane's projects/ tree may be registered. Project
    ownership is checked live by the gateway on every browse request.
    """
    if not isinstance(owner, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', owner) or owner == 'shared':
        raise ResourceError('invalid_owner')
    uuid.UUID(project_id)
    parts = relative_parts(relative_path)
    if len(parts) < 2 or parts[0] != 'projects':
        raise ResourceError('invalid_path')
    lane_root = Path(lane_root)
    with safe_open(lane_root, parts, directory=True):
        pass
    registry = lane_root / 'project-folders.json'
    items = load_folders(lane_root)
    attachment = {'attachment_id': str(uuid.uuid4()), 'project_id': project_id,
                  'owner': owner, 'path': relative_path, 'label': str(label)[:200]}
    items.append(attachment)
    temp = registry.with_name('project-folders.' + secrets.token_hex(8) + '.tmp')
    try:
        with temp.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(items, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, registry)
    finally:
        temp.unlink(missing_ok=True)
    return attachment


def load_folders(lane_root):
    try:
        with safe_open(Path(lane_root), ('project-folders.json',)) as (fd, _, info):
            if info.st_size > 1024 * 1024:
                raise ResourceError('files_unavailable', 503)
            with os.fdopen(os.dup(fd), 'r', encoding='utf-8') as stream:
                rows = json.load(stream)
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ResourceError('files_unavailable', 503)
        for row in rows:
            if (not isinstance(row, dict) or set(row) - {'attachment_id', 'project_id', 'owner', 'path', 'label', 'checked_edits'}
                    or not {'attachment_id', 'project_id', 'owner', 'path', 'label'} <= set(row)
                    or not all(isinstance(row[key], str) for key in ('attachment_id', 'project_id', 'owner', 'path', 'label'))
                    or ('checked_edits' in row and not isinstance(row['checked_edits'], dict))
                    or len(row['label']) > 200):
                raise ValueError()
            uuid.UUID(row['attachment_id'])
            uuid.UUID(row['project_id'])
            parts = relative_parts(row['path'])
            if parts[0] != 'projects' or len(parts) < 2:
                raise ValueError()
        return rows
    except ResourceError as exc:
        if exc.status == 404:
            return []
        raise
    except (ValueError, KeyError, TypeError):
        raise ResourceError('files_unavailable', 503) from None


def browse_file(lane_root, attachment, scope, path='', *, offset=0, max_bytes=MAX_READ, listing=False):
    offset = integer(offset, 0, MAX_FILE)
    max_bytes = integer(max_bytes, MAX_READ, MAX_READ, 1)
    parts = relative_parts(path, empty=listing)
    registered = relative_parts(attachment['path'])
    root = Path(lane_root)
    target = root.joinpath(*registered, *parts)
    if not scope_allows(scope, target):
        raise ResourceError('scope_denied', 403)
    with safe_open(root, (*registered, *parts), directory=listing) as (fd, target, info):
        if listing:
            entries = []
            scanned = 0
            deadline = time.monotonic() + 5
            with os.scandir(fd if fd is not None and os.scandir in os.supports_fd else target) as scan:
                for item in scan:
                    scanned += 1
                    if scanned > 10000 or time.monotonic() > deadline:
                        raise ResourceError('directory_too_large', 413)
                    try:
                        relative_parts(item.name)
                        meta = item.stat(follow_symlinks=False)
                        if (item.is_symlink() or getattr(meta, 'st_file_attributes', 0) & 0x400
                                or not (stat.S_ISREG(meta.st_mode) or stat.S_ISDIR(meta.st_mode))):
                            continue
                        if not scope_allows(scope, target / item.name):
                            continue
                        entries.append({'name': item.name, 'path': '/'.join((*parts, item.name)),
                                        'dir': stat.S_ISDIR(meta.st_mode), 'size': meta.st_size})
                    except (ResourceError, OSError):
                        continue
            return sorted(entries, key=lambda r: (not r['dir'], r['name']))
        if info.st_size > MAX_FILE:
            raise ResourceError('file_too_large', 413)
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), 'rb') as stream:
            remaining = MAX_FILE + 1
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                digest.update(chunk)
            if remaining == 0:
                raise ResourceError('file_too_large', 413)
            stream.seek(offset)
            raw = stream.read(max_bytes)
            after = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ResourceError('revision_conflict', 409)
        # Every chunk is independently decodable, including UTF-8 byte boundaries.
        try:
            content = raw.decode('utf-8')
            if any(ord(c) < 32 and c not in '\t\n\r' for c in content):
                raise UnicodeError()
            encoding = 'utf-8'
        except UnicodeError:
            content, encoding = base64.b64encode(raw).decode('ascii'), 'base64'
        next_offset = offset + len(raw)
        truncated = next_offset < info.st_size
        return {'content': content, 'encoding': encoding, 'revision': digest.hexdigest(),
                'truncated': truncated, 'next_offset': next_offset if truncated else None}
