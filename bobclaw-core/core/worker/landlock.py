"""Inherited Linux filesystem confinement. ABI 3 is mandatory (REFER + TRUNCATE).

Landlock mediates file access, not stat metadata. No filesystem root or shared
home is granted. /proc is read-only (no write/execute): the worker unit already
limits it to the worker's own processes (ProtectProc=invisible, ProcSubset=pid),
tools need /proc/self, and Landlock checks magic-link targets such as
/proc/self/root/... against their real location, so those stay denied. Only the selected workspace/private tmp are writable.
The existing seccomp, identity, namespace and network controls remain required.
"""
import ctypes
import os
from pathlib import Path
import platform

MIN_ABI = 3
EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1, 2, 4, 8
REFER, TRUNCATE = 1 << 13, 1 << 14
HANDLED = (1 << 15) - 1
READ = EXECUTE | READ_FILE | READ_DIR
# Special nodes may be opened but never created, nor removed outside scratch.
FILE_RW = READ_FILE | WRITE_FILE | TRUNCATE
RUNTIME_READ = ('/usr', '/opt/bob', '/opt/python', '/opt/bob-mcp', '/opt/bob-browsers', '/etc/ld.so.cache',
                '/etc/ld.so.conf', '/etc/ld.so.conf.d', '/etc/ssl/certs', '/etc/ssl/openssl.cnf',
                '/etc/hosts', '/etc/resolv.conf', '/etc/nsswitch.conf',
                '/etc/passwd', '/etc/group', '/etc/localtime')


class Ruleset(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64)]


class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def _libc():
    if platform.system() != 'Linux' or platform.machine().lower() not in {'x86_64', 'amd64'}:
        raise RuntimeError('landlock_abi_3_required')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def abi():
    result = _libc().syscall(444, 0, 0, 1)  # LANDLOCK_CREATE_RULESET_VERSION
    if result < MIN_ABI:
        raise RuntimeError('landlock_abi_3_required')
    return result


def rules(workspace, temporary, *, readonly=(), writable=(), runtime=RUNTIME_READ):
    """Paths come only from installed configuration or service-selected IDs."""
    result = [(str(Path(workspace).resolve(strict=True)), HANDLED),
              (str(Path(temporary).resolve(strict=True)), HANDLED)]
    for name in writable:
        result.append((str(Path(name).resolve(strict=True)), HANDLED))
    for name in (*runtime, *readonly):
        # Inside the worker unit's sandbox some runtime paths are unreachable (for
        # example /etc/resolv.conf links into a read-only, empty /run), so stat()
        # raises instead of reporting absence. An unreachable runtime path simply
        # gets no rule; a declared input must exist and stays fail-closed.
        try:
            path = Path(name).resolve(strict=True)
            directory = path.is_dir()
        except OSError:
            if name in readonly:
                raise RuntimeError('declared_input_unavailable') from None
            continue
        result.append((str(path), READ if directory else EXECUTE | READ_FILE))
    for name in ('/dev/null', '/dev/zero', '/dev/random', '/dev/urandom'):
        if Path(name).exists():
            result.append((name, FILE_RW))
    if Path('/proc/self').exists():
        result.append(('/proc', READ_FILE | READ_DIR))
    return result


def install(workspace, temporary, *, readonly=(), writable=(), runtime=RUNTIME_READ):
    abi()
    libc = _libc()
    attr = Ruleset(HANDLED)
    fd = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if fd < 0:
        raise RuntimeError('landlock_ruleset_unavailable')
    try:
        for path, access in rules(workspace, temporary, readonly=readonly, writable=writable, runtime=runtime):
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathBeneath(access, parent)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0):
                    raise RuntimeError('landlock_rule_unavailable')
            finally:
                os.close(parent)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, fd, 0):
            raise RuntimeError('landlock_restrict_unavailable')
    finally:
        os.close(fd)
