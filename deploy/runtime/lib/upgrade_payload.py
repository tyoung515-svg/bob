"""Build and verify the single runtime-owned payload inventory (stdlib only)."""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import stat
import shutil
import tarfile

MANIFEST = 'etc/bob/runtime-payload.json'
VERSION = 'etc/bob/runtime-version'
TREES = ('/opt/bob', '/usr/local/lib/bob_runtime', '/usr/local/libexec/bob', '/opt/llama')


def owned(path):
    p = PurePosixPath(path)
    if str(p) != path or not path.startswith('/') or '..' in p.parts or '\\' in path:
        return False
    if any(path == root or path.startswith(root + '/') for root in TREES):
        return True
    return (path in {'/etc/bob/memory_slots.toml', '/etc/bob/embedder-models.json'}
            or (str(p.parent) == '/usr/local/bin' and
                (re.fullmatch(r'bob-[a-z0-9-]+', p.name) or p.name in
                 {'fetch-checked', 'check-embedder-backend', 'claude'}))
            or (str(p.parent) == '/etc/systemd/system' and
                re.fullmatch(r'bob-[a-z0-9@_.-]+\.(service|socket|timer)', p.name)))


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def inventory(root):
    roots = [p for p in TREES if (root / p.lstrip('/')).exists()]
    for directory, pattern in (('usr/local/bin', 'bob-*'), ('etc/systemd/system', 'bob-*')):
        roots.extend('/' + p.relative_to(root).as_posix() for p in sorted((root / directory).glob(pattern))
                     if owned('/' + p.relative_to(root).as_posix()))
    roots.extend('/' + p for p in ('etc/bob/memory_slots.toml', 'etc/bob/embedder-models.json', 'usr/local/bin/fetch-checked',
                                 'usr/local/bin/check-embedder-backend', 'usr/local/bin/claude') if (root / p).exists())
    rows = []
    def visit(path):
        name = '/' + path.relative_to(root).as_posix()
        if not owned(name):
            raise ValueError('path outside runtime allowlist')
        info = path.lstat()
        row = dict(path=name, mode=stat.S_IMODE(info.st_mode))
        if path.is_symlink():
            row.update(kind='symlink', target=os.readlink(path))
        elif path.is_dir():
            row.update(kind='directory')
        elif path.is_file():
            row.update(kind='file', size=info.st_size, sha256=sha(path))
        else:
            raise ValueError('special file in runtime payload')
        rows.append(row)
        if row['kind'] == 'directory':
            for child in sorted(path.iterdir()):
                visit(child)
    for path in sorted(set(roots)):
        visit(root / path.lstrip('/'))
    return sorted(set(roots)), sorted(rows, key=lambda r: r['path'])


def build(root, build_id, git_rev, build_epoch):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', build_id):
        raise ValueError('invalid build ID')
    roots, entries = inventory(root)
    body = dict(schema_version=1, build_id=build_id, git_rev=git_rev,
                build_epoch=int(build_epoch), roots=roots, entries=entries)
    body['payload_sha256'] = hashlib.sha256(canonical(body)).hexdigest()
    version = {k: body[k] for k in ('build_id', 'git_rev', 'build_epoch', 'payload_sha256')}
    (root / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST).write_bytes(canonical(body) + b'\n')
    (root / VERSION).write_bytes(canonical(version) + b'\n')
    return body


def validate(manifest, version):
    body = dict(manifest)
    digest = body.pop('payload_sha256')
    if manifest['schema_version'] != 1 or hashlib.sha256(canonical(body)).hexdigest() != digest:
        raise ValueError('payload manifest digest mismatch')
    if version != {k: manifest[k] for k in ('build_id', 'git_rev', 'build_epoch', 'payload_sha256')}:
        raise ValueError('version/manifest mismatch')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', version['build_id']):
        raise ValueError('invalid version')
    if type(version['build_epoch']) is not int or version['build_epoch'] < 1:
        raise ValueError('invalid version order')
    roots = manifest['roots']
    if not roots or roots != sorted(set(roots)) or not all(owned(p) for p in roots):
        raise ValueError('invalid payload roots')
    if any(a != b and b.startswith(a + '/') for a in roots for b in roots):
        raise ValueError('overlapping payload roots')
    rows = manifest['entries']
    by_path = {r['path']: r for r in rows}
    if len(rows) != len(by_path) or not all(p in by_path for p in roots):
        raise ValueError('duplicate or missing payload root')
    for row in rows:
        path = row['path']
        if not owned(path) or not any(path == p or path.startswith(p + '/') for p in roots):
            raise ValueError('path outside runtime allowlist')
        if type(row['mode']) is not int or row['mode'] & ~0o777:
            raise ValueError('privileged payload mode')
        if row['kind'] not in {'directory', 'file', 'symlink'}:
            raise ValueError('unsupported payload type')
        if row['kind'] == 'file' and (not re.fullmatch('[a-f0-9]{64}', row['sha256']) or row['size'] < 0):
            raise ValueError('invalid file hash')
        if row['kind'] == 'symlink':
            target = row['target']
            if not target or '\x00' in target or '\\' in target:
                raise ValueError('unsafe link target')
            resolved = posixpath.normpath(str(PurePosixPath(path).parent / target))
            if not owned(resolved) and not resolved.startswith('/opt/python/'):
                raise ValueError('link outside runtime dependencies')
        parent = str(PurePosixPath(path).parent)
        while path not in roots and parent in by_path:
            if by_path[parent]['kind'] != 'directory':
                raise ValueError('payload path beneath a link')
            path, parent = parent, str(PurePosixPath(parent).parent)
        if path not in roots:
            raise ValueError('missing parent directory')
    return manifest


def unpack(archive_path, destination, expected_sha256):
    """Never extract the rootfs: copy only validated inventory entries, no tar.extract."""
    if not re.fullmatch('[a-f0-9]{64}', expected_sha256) or sha(archive_path) != expected_sha256:
        raise ValueError('release archive digest mismatch')
    with tarfile.open(archive_path, 'r:*') as archive:
        members = {}
        for member in archive:
            name = member.name.removeprefix('./')
            if name in members:
                raise ValueError('duplicate archive path')
            members[name] = member
        def metadata(name):
            member = members[name]
            if not member.isfile() or member.size > 64 * 1024 * 1024:
                raise ValueError('invalid release metadata')
            return json.load(archive.extractfile(member))
        version = metadata(VERSION)
        manifest = validate(metadata(MANIFEST), version)
        required = sum(r.get('size', 0) for r in manifest['entries'])
        if shutil.disk_usage(destination).free < required * 3 + 256 * 1024 * 1024:
            raise ValueError('insufficient payload staging space')
        for row in sorted(manifest['entries'], key=lambda r: (r['path'].count('/'), r['path'])):
            member = members[row['path'].lstrip('/')]
            target = destination / row['path'].lstrip('/')
            if member.uid != 0 or member.mode & 0o777 != row['mode']:
                raise ValueError('payload ownership/mode mismatch')
            target.parent.mkdir(parents=True, exist_ok=True)
            kind = row['kind']
            if kind == 'directory' and member.isdir():
                target.mkdir(exist_ok=True)
            elif kind == 'symlink' and member.issym() and member.linkname == row['target']:
                target.symlink_to(row['target'])
            elif kind == 'file' and (member.isfile() or member.islnk()):
                h, count = hashlib.sha256(), 0
                with archive.extractfile(member) as source, target.open('xb') as output:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        count += len(block)
                        if count > row['size']:
                            raise ValueError('oversized payload file')
                        output.write(block)
                        h.update(block)
                if count != row['size'] or h.hexdigest() != row['sha256']:
                    raise ValueError('payload file digest mismatch')
            else:
                raise ValueError('payload type mismatch')
            if kind != 'symlink':
                target.chmod(row['mode'])
        for name, value in ((MANIFEST, manifest), (VERSION, version)):
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(canonical(value) + b'\n')
        return manifest, version


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('/'))
    parser.add_argument('--build-id', required=True)
    parser.add_argument('--git-rev', required=True)
    parser.add_argument('--build-epoch', required=True, type=int)
    args = parser.parse_args()
    build(args.root, args.build_id, args.git_rev, args.build_epoch)
