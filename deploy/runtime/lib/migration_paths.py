"""Offline ownership transfer with pinned descriptors and journaled link breaks."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import secrets
import stat
import subprocess

from core.worker.auth_file import directory


WORKER_TREES = {'projects', 'scratch', 'codex-home', 'cli'}


def refuse(reason, relative):
    # repr escapes control characters in hostile filenames. Never include data.
    raise ValueError(f'migration {reason}: {"/".join(relative) or "."!r}')


def check_inode(info, device, relative=(), *, worker_trees=True):
    worker = worker_trees and relative and relative[0] in WORKER_TREES
    if info.st_dev != device:
        refuse('refuses mount crossing', relative)
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1 and not worker:
            refuse('refuses service hardlinks', relative)
    elif not stat.S_ISDIR(info.st_mode):
        if not worker or not (stat.S_ISLNK(info.st_mode) or stat.S_ISFIFO(info.st_mode)
                              or stat.S_ISSOCK(info.st_mode)):
            refuse('refuses symlink or special file', relative)


def mount_points():
    # st_dev alone misses same-device bind mounts. Decode kernel mountinfo escapes.
    def decode(value):
        import re
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    return {decode(line.split()[4]) for line in Path('/proc/self/mountinfo').read_text().splitlines()}


def walk(path, visit, *, worker_trees=True, temporary=None):
    """Visit an offline tree using openat; verify the opened inode before mutation.

    Caller has stopped ALL processes sharing the old owner and removed write
    access to the lane ancestor. Mountpoints (including bind mounts) are refused.
    No resolve(), path-based chown, or subprocess recursive chown is used.
    """
    mounts = mount_points()
    absolute = str(path.absolute())
    for mount in sorted(mounts):
        if mount == absolute or mount.startswith(absolute + '/'):
            refuse('refuses mounted tree', Path(mount).relative_to(path.absolute()).parts)
    with directory(path) as rootfd:
        device = os.fstat(rootfd).st_dev

        def descend(fd, relative, parent=None, name=None):
            info = os.fstat(fd)
            check_inode(info, device, relative, worker_trees=worker_trees)
            visit(fd, relative, info, parent, name)
            if not stat.S_ISDIR(info.st_mode):
                return
            for name in sorted(os.listdir(fd)):
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                rel = relative + (name,)
                if temporary and temporary(rel, before):
                    continue
                check_inode(before, device, rel, worker_trees=worker_trees)
                # Never open a link, FIFO or socket, even with O_NONBLOCK.
                if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
                    visit(None, rel, before, fd, name)
                    continue
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if stat.S_ISDIR(before.st_mode):
                    flags |= os.O_DIRECTORY
                child = os.open(name, flags, dir_fd=fd)
                try:
                    after = os.fstat(child)
                    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                        refuse('tree changed while opening', rel)
                    descend(child, rel, fd, name)
                finally:
                    os.close(child)
        descend(rootfd, ())


def describe(info):
    return dict(uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode),
                dev=info.st_dev, ino=info.st_ino, kind=stat.S_IFMT(info.st_mode), nlink=info.st_nlink)


def ownership(relative, info, ids):
    worker = relative and relative[0] in WORKER_TREES
    uid = ids['worker_uid' if worker else 'service_uid']
    gid = ids['worker_gid' if worker else 'service_gid']
    mode = 0o700 if stat.S_ISDIR(info.st_mode) else (0o700 if info.st_mode & 0o111 else 0o600)
    if relative and relative[0] in {'projects', 'scratch'}:
        gid = ids['service_gid']
        mode = 0o2770 if stat.S_ISDIR(info.st_mode) else (0o770 if info.st_mode & 0o111 else 0o660)
    if relative == ('codex-home',):
        mode = 0o2750
    if relative in {('codex-home', 'auth.json'), ('codex-home', 'config.toml')}:
        mode = 0o660 if relative[-1] == 'auth.json' else 0o640
    if len(relative) == 1 and relative[0] in {'lane.env', 'providers.env', 'core.env', 'gateway.env',
                                             'bridge.env', 'project-folders.json'}:
        uid, gid, mode = 0, ids['service_gid'], 0o640
    return uid, gid, mode


def apply(fd, uid, gid, mode):
    # Two syscalls instead of one/two subprocesses per inode. Unsupported ACLs
    # mean there are no POSIX ACL grants to strip; other errors fail closed.
    for attr in ('system.posix_acl_access', 'system.posix_acl_default'):
        try:
            os.removexattr(fd, attr)
        except OSError as exc:
            if exc.errno not in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
    os.fchown(fd, uid, gid)
    os.fchmod(fd, mode)


def same_inode(info, expected):
    return (info.st_dev, info.st_ino) == (expected['dev'], expected['ino'])


def recheck(parent, name, expected, relative):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not same_inode(info, expected) or stat.S_IFMT(info.st_mode) != expected.get('kind', stat.S_IFMT(info.st_mode)):
        refuse('inventory changed; operator inspection required', relative)
    return info


def check_temporary(info, record, relative):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or ('copy' in record and not same_inode(info, record['copy']))
            or ('copy' not in record and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600))):
        refuse('link-break temporary changed; operator inspection required', relative)


def break_link(fd, parent, name, relative, info, target, record, save):
    """Copy then rename, with a durable prepared-inode receipt before rename.

    A kill before rename leaves the source name intact; a kill after rename is
    recognized by the prepared copy's dev/ino. Incomplete copies are overwritten.
    The original/shared inode is never chowned, chmodded or stripped of ACLs.
    """
    if not record:
        record['temporary'] = '.bob-migrate-' + secrets.token_hex(16)
        save()
    temp = record['temporary']
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        copyfd = os.open(temp, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
    except FileExistsError:
        copyfd = os.open(temp, flags, dir_fd=parent)
    try:
        copied = os.fstat(copyfd)
        check_temporary(copied, record, relative)
        record['copy'] = describe(copied)
        record['ready'] = False
        os.fsync(parent)  # Persist the temporary name before its journal receipt.
        save()
        os.ftruncate(copyfd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        while data := os.read(fd, 1024 * 1024):
            remaining = memoryview(data)
            while remaining:
                written = os.write(copyfd, remaining)
                if not written:
                    raise OSError('migration copy made no progress')
                remaining = remaining[written:]
        apply(copyfd, *target)
        os.fsync(copyfd)
        record['ready'] = True
        save()
        recheck(parent, name, describe(info), relative)
        recheck(parent, temp, record['copy'], relative)
        os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        record['completed'] = True
        save()
    finally:
        os.close(copyfd)


def transfer(path, ids, manifest, save, *, worker_trees=True, checkpoint=None):
    if manifest.get('applied'):
        return
    # Finish the complete inventory BEFORE the first ownership change. A crash
    # preserves the original metadata even when ownership is partly transferred.
    if not manifest.get('inventory_complete'):
        entries = {}
        walk(path, lambda fd, rel, info, parent, name: entries.update({'/'.join(rel): describe(info)}),
             worker_trees=worker_trees)
        manifest['entries'] = entries
        manifest['inventory_complete'] = True
        save()
    breaks = manifest.setdefault('link_breaks', {})
    temporaries = {'/'.join(source.split('/')[:-1] + [record['temporary']]): record
                   for source, record in breaks.items() if 'temporary' in record}
    def save_break(key, record):
        # Production writes a small independent receipt per break. Avoid
        # serializing a 100k-inode inventory several times for every hardlink.
        if checkpoint is None:
            save()
        else:
            checkpoint(key, record)
    seen = set()
    def temporary(rel, info):
        # Only journaled, validated temporaries are excluded from the inventory.
        key = '/'.join(rel)
        if key in temporaries:
            check_temporary(info, temporaries[key], rel)
            return True
        return False

    def change(fd, rel, info, parent, name):
        key = '/'.join(rel)
        seen.add(key)
        original = manifest['entries'].get(key)
        record = breaks.get(key, {})
        replaced_control = (len(rel) == 1 and rel[0] in {'lane.env', 'core.env', 'gateway.env', 'bridge.env'}
                            and info.st_uid == 0 and stat.S_ISREG(info.st_mode))
        if original is None and replaced_control:
            return  # A role env generated by an interrupted provisioning phase.
        copied = record.get('ready') and same_inode(info, record['copy'])
        if copied and info.st_nlink != 1:
            refuse('prepared copy acquired hardlinks; operator inspection required', rel)
        if original is None or (not same_inode(info, original) and not replaced_control and not copied):
            refuse('inventory changed; operator inspection required', rel)
        if original.get('kind', stat.S_IFMT(info.st_mode)) != stat.S_IFMT(info.st_mode):
            refuse('inventory type changed; operator inspection required', rel)
        target = ownership(rel, info, ids)
        if fd is None:
            recheck(parent, name, original, rel)
            os.chown(name, *target[:2], dir_fd=parent, follow_symlinks=False)
            # Symlink modes are immaterial; never chmod through the link.
        elif stat.S_ISREG(info.st_mode) and not copied and (info.st_nlink > 1 or original.get('nlink', 1) > 1):
            record = breaks.setdefault(key, {})
            break_link(fd, parent, name, rel, info, target, record, lambda: save_break(key, record))
        else:
            apply(fd, *target)
            if copied and not record.get('completed'):
                os.fsync(parent)
                record['completed'] = True
                save_break(key, record)
        if rel and rel[0] in {'projects', 'scratch'} and stat.S_ISDIR(info.st_mode):
            subprocess.run(['setfacl', '-m',
                            f'd:u:{ids["worker_uid"]}:rwx,d:g:{ids["service_gid"]}:rwx,d:m:rwx',
                            f'/proc/self/fd/{fd}'], pass_fds=(fd,), check=True)
    walk(path, change, worker_trees=worker_trees, temporary=temporary)
    missing = manifest['entries'].keys() - seen
    if missing:
        refuse('inventory entry disappeared; operator inspection required', tuple(sorted(missing)[0].split('/')))
    manifest['applied'] = True
    save()
