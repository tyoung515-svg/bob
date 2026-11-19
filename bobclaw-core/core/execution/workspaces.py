"""Bounded no-follow snapshots and disposable run copies, never publication.

Blob bytes are committed in the service database before a receipt refers to
these manifests. Workers can change disposable copies, never frozen blobs.
"""
import hashlib
import os
from pathlib import Path
import stat
import time

from core.portal_resources import safe_open, relative_parts, ResourceError, scope_allows
from .models import ExecutionError, digest, canonical

MAX_FILES = 2048
MAX_BYTES = 32 * 1024 * 1024
MAX_FILE = 4 * 1024 * 1024
MAX_MANIFEST = 256 * 1024
# Installed edit progress is private adapter state, never source/proposal data.
# Excluding it also prevents a source tree from injecting a journal into a copy.
EXCLUDED = frozenset({'.git', 'node_modules', '__pycache__', '.bob-checked-edit.json'})


def snapshot(root, *, scope=None, max_files=MAX_FILES, max_bytes=MAX_BYTES, path_filter=None):
    root = Path(root).absolute()
    manifest, blobs, total = [], {}, 0
    manifest_bytes = 2
    def add(item):
        nonlocal manifest_bytes
        manifest_bytes += len(canonical(item).encode()) + 1
        if manifest_bytes > MAX_MANIFEST:
            raise ExecutionError('snapshot_manifest_too_large',413)
        manifest.append(item)
    deadline = time.monotonic() + 15
    def visit(parts, device):
        nonlocal total
        with safe_open(root, parts, directory=True) as (dfd, path, before):
            if before.st_dev != device:
                raise ExecutionError('snapshot_mount_drift', 409)
            names = sorted(os.listdir(dfd if dfd is not None else path))
            if len(names) + len(manifest) > max_files:
                raise ExecutionError('snapshot_too_large', 413)
            for name in names:
                if time.monotonic() > deadline:
                    raise ExecutionError('snapshot_timeout', 409)
                # Even excluded entries cannot be links or device/mount escapes.
                info = os.stat(name, dir_fd=dfd, follow_symlinks=False) if dfd is not None else (path / name).lstat()
                if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                        or info.st_dev != device):
                    raise ExecutionError('snapshot_link_or_mount', 409)
                if name in EXCLUDED:
                    continue
                child = (*parts, name)
                if path_filter is not None and not path_filter(child):
                    continue
                relative_parts('/'.join(child))  # credentials/control roots fail closed
                if scope is not None and not scope_allows(scope, root.joinpath(*child)):
                    raise ExecutionError('scope_denied', 403)
                if stat.S_ISDIR(info.st_mode):
                    add({'path': '/'.join(child), 'kind': 'directory', 'mode': 0o755})
                    visit(child, device)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_FILE:
                    with safe_open(root, child) as (fd, _, opened):
                        if opened.st_nlink != 1 or opened.st_dev != device:
                            raise ExecutionError('snapshot_shared_file', 409)
                        with os.fdopen(os.dup(fd), 'rb') as stream:
                            data = stream.read(MAX_FILE + 1)
                        after = os.fstat(fd)
                        fields = ('st_ino','st_dev','st_size','st_mtime_ns','st_nlink') + (() if os.name == 'nt' else ('st_ctime_ns',))
                        if (len(data) != opened.st_size or len(data) > MAX_FILE
                                or any(getattr(opened, k) != getattr(after, k) or getattr(info, k) != getattr(after, k) for k in fields)):
                            raise ExecutionError('snapshot_changed', 409)
                    total += len(data)
                    if total > max_bytes:
                        raise ExecutionError('snapshot_too_large', 413)
                    sha = hashlib.sha256(data).hexdigest()
                    blobs[sha] = data
                    add({'path': '/'.join(child), 'kind': 'file', 'sha256': sha,
                                     'size': len(data), 'mode': 0o755 if info.st_mode & 0o111 else 0o644})
                else:
                    raise ExecutionError('snapshot_special_or_shared_file', 409)
                if len(manifest) > max_files:
                    raise ExecutionError('snapshot_too_large', 413)
            current = os.fstat(dfd) if dfd is not None else path.stat()
            if (before.st_ino, before.st_dev, before.st_mtime_ns, before.st_ctime_ns) != (current.st_ino, current.st_dev, current.st_mtime_ns, current.st_ctime_ns):
                raise ExecutionError('snapshot_changed', 409)
    try:
        with safe_open(root, (), directory=True) as (_, _, info):
            device = info.st_dev
        visit((), device)
    except ResourceError as exc:
        raise ExecutionError(exc.code, exc.status) from None
    return {'digest': digest(manifest), 'manifest': manifest, 'blobs': blobs,
            'bytes': total, 'excluded': sorted(EXCLUDED)}


def materialize(parent, identifier, frozen, *, worker_gid=None):
    """Parent is service-owned, worker searchable but never worker writable.

    A crash can leave an unreferenced copy; IDs are never reused or auto-replayed.
    """
    import re
    if not re.fullmatch('[0-9a-f]{32}', identifier):
        raise ExecutionError('invalid_workspace')
    parent = Path(parent)
    target = parent / identifier
    with safe_open(parent, (), directory=True) as (_, _, info):
        if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise ExecutionError('workspace_parent_unprotected', 503)
    target.mkdir(mode=0o770)
    if os.name != 'nt' and worker_gid is not None:
        os.chown(target, -1, worker_gid)
        os.chmod(target, 0o2770)
    for item in frozen['manifest']:
        parts = relative_parts(item['path'])
        path = target.joinpath(*parts)
        if item['kind'] == 'directory':
            path.mkdir(mode=0o770)
        else:
            data = frozen['blobs'][item['sha256']]
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ExecutionError('snapshot_digest_conflict', 409)
            with path.open('xb') as stream:
                stream.write(data)
            if os.name != 'nt':
                os.chmod(path, 0o770 if item['mode'] & 0o111 else 0o660)
        if os.name != 'nt' and worker_gid is not None:
            os.chown(path, -1, worker_gid)
            if item['kind'] == 'directory':
                os.chmod(path, 0o2770)
    return target


def proposal(base, output, identity):
    """Digest binds the complete payload; display truncation is separate."""
    import difflib
    old = {r['path']: r for r in base['manifest'] if r['kind'] == 'file'}
    new = {r['path']: r for r in output['manifest'] if r['kind'] == 'file'}
    files, diff = [], []
    for path in sorted(old.keys() | new.keys()):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        operation = 'create' if before is None else 'delete' if after is None else 'replace'
        files.append({'path': path, 'operation': operation, 'pre_sha256': before['sha256'] if before else None,
                      'post_sha256': after['sha256'] if after else None, 'bytes': after['size'] if after else 0,
                      'created': before is None, 'mode': after['mode'] if after else None})
        a = base['blobs'][before['sha256']] if before else b''
        b = output['blobs'][after['sha256']] if after else b''
        diff.extend(difflib.unified_diff(a.decode('utf-8','replace').splitlines(True),
                                       b.decode('utf-8','replace').splitlines(True),
                                       fromfile='a/' + path, tofile='b/' + path))
    payload = {'schema_version': 2, **identity, 'base_digest': base['digest'],
               'output_digest': output['digest'], 'files': files, 'output_manifest': output['manifest'],
               'required_checks': [], 'checks_revision': 'proposal-only/1'}
    rendered = ''.join(diff).encode('utf-8')
    return {**payload, 'digest': digest(payload), 'diff': rendered[:65536].decode('utf-8','replace'),
            'diff_truncated': len(rendered) > 65536, 'check_runs': [], 'checks_passed': False}
