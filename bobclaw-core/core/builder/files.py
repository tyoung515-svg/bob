"""Bounded no-follow reads shared by tools and the preview route."""
import base64
import fnmatch
import mimetypes
import os
import re
import stat
import heapq
import hashlib
from itertools import islice
from pathlib import Path

from core.portal_resources import relative_parts, safe_open
from core.execution.models import ExecutionError, integer

PREVIEW_CAP = 5 * 1024 * 1024
READ_CAP = 256 * 1024


def browse(root, path=''):
    """Metadata only; share Builder's no-follow traversal, including dot entries.

    Symlinks/reparse points are visible but cannot be traversed. Reserved file
    contents retain their existing read/preview policy.
    """
    parts = relative_parts(path, empty=True, include_reserved=True)
    with safe_open(Path(root), parts, directory=True) as (fd, target, _):
        # Retain only the first 1001 names, rather than an unbounded directory.
        with os.scandir(fd if fd is not None and os.scandir in os.supports_fd else target) as scan:
            names = heapq.nsmallest(1001, (entry.name for entry in islice(scan, 10001)))
        entries = []
        for name in names[:1000]:
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else (target/name).lstat()
            except FileNotFoundError:
                continue  # Concurrent removal, never follow the replacement.
            link = stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            entries.append(dict(name=name, kind='symlink' if link else 'dir' if stat.S_ISDIR(info.st_mode) else 'file',
                                size=info.st_size, mtime=info.st_mtime))
        return dict(path=path, entries=entries, truncated=len(names)>1000)


def checkpoint_snapshot(root):
    from core.execution.workspaces import snapshot
    from core.portal_resources import ResourceError
    def allowed(parts):
        try:
            relative_parts('/'.join(parts))
            return True
        except ResourceError:
            return False
    # Builder commands may create .pytest_cache and other reserved dot paths.
    # Keep these outside undo, just like existing .git/node_modules exclusions.
    return snapshot(root,path_filter=allowed)


def read(root, path, offset=0, limit=READ_CAP):
    integer(offset, 0, 2**40)
    integer(limit, 1, PREVIEW_CAP)
    with safe_open(Path(root), relative_parts(path)) as (fd, _, info):
        if info.st_nlink != 1:
            raise ExecutionError('shared_file_refused', 403)
        with os.fdopen(os.dup(fd), 'rb') as stream:
            stream.seek(offset)
            data = stream.read(limit)
        return data, info.st_size


def preview(root, path):
    # Bytes and provenance come from the same no-follow descriptor, rather than
    # a second path lookup that could describe a replacement file.
    with safe_open(Path(root), relative_parts(path)) as (fd, _, info):
        if info.st_nlink != 1:
            raise ExecutionError('shared_file_refused', 403)
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(PREVIEW_CAP)
        after = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ExecutionError('preview_changed', 409)
        size = info.st_size
    # OS MIME databases commonly label .ts as MPEG transport streams; source
    # previews must stay readable code and use the existing Monaco renderer.
    source = Path(path).suffix.lower() in {'.ts','.tsx','.jsx','.rs','.toml','.yaml','.yml','.sh','.ps1','.go','.kt','.c','.h','.cpp'}
    return dict(path=path, mime='text/plain' if source else mimetypes.guess_type(path)[0] or 'application/octet-stream',
                bytes_b64=base64.b64encode(data).decode() if size <= PREVIEW_CAP else None,
                size=size, truncated=size > PREVIEW_CAP,
                provenance={'source':'workspace', 'modified_at':info.st_mtime,
                            'sha256':hashlib.sha256(data).hexdigest() if size <= PREVIEW_CAP else None})


def listing(root, path=''):
    parts = relative_parts(path, empty=True)
    with safe_open(Path(root), parts, directory=True) as (fd, target, _):
        names = sorted(os.listdir(fd if fd is not None else target))
        if len(names) > 2048:
            raise ExecutionError('listing_too_large', 413)
        items = []
        for name in names:
            try:
                relative_parts('/'.join((*parts, name)))
            except Exception:
                continue
            items.append(name)
        return {'path': path, 'items': items}


def search(root, pattern, glob='*', *, read_scope=None, inspection=False):
    if len(pattern) > 512 or len(glob) > 512:
        raise ExecutionError('search_too_large', 413)
    # Run regex in the isolated worker, with the daemon timeout as the ReDoS
    # boundary. This function is never called in the core event loop.
    regex = re.compile(pattern)
    matches, visited = [], 0
    def visit(path):
        nonlocal visited
        for name in listing(root, path)['items']:
            visited += 1
            if visited > 2048:
                raise ExecutionError('search_too_large', 413)
            child = '/'.join(filter(None, [path, name]))
            if inspection:
                from core.inspection import read_parts
                from core.portal_resources import ResourceError, scope_allows
                try:
                    read_parts(child)
                except ResourceError:
                    continue
                if read_scope is not None and not scope_allows(read_scope, Path(root)/child):
                    continue
            try:
                with safe_open(Path(root), relative_parts(child), directory=True):
                    pass
            except Exception:
                if not fnmatch.fnmatchcase(child, glob):
                    continue
                data, _ = read(root, child, limit=READ_CAP)
                for number, line in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
                    if regex.search(line):
                        matches.append({'path': child, 'line': number, 'text': line[:500]})
                        if len(matches) >= 100:
                            return False
            else:
                if visit(child) is False:
                    return False
        return True
    complete = visit('')
    return {'matches': matches, 'truncated': complete is False}
