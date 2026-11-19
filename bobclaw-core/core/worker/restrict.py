"""Mandatory x86_64 seccomp filter inherited by all task descendants."""
import ctypes
import errno
import platform


def install(*, network: bool, protected_pid=None, protected_pgid=None):
    if platform.machine().lower() not in {'x86_64', 'amd64'}:
        raise RuntimeError('worker seccomp architecture unavailable')
    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]
    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]
    # Audit arch check, then syscall number. x32 ABI and compat syscalls fail closed.
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xc000003e), (0x06, 0, 0, 0x80000000),
                    (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    # No task can create sockets via io_uring, change namespaces, ptrace the
    # daemon, or use process_vm/pidfd to steal fds. Browser launchers may setsid;
    # the daemon is a subreaper and kills the entire descendant tree, not only PGID.
    blocked = [101, 310, 311, 424, 434, 438, 425, 426, 427, 272, 308, 165, 166, 155]
    if protected_pid is not None:
        # Same-UID children must not stop/kill the deadline/control supervisor.
        # Deny direct/thread/queued signals and broad kill(-1)/supervisor-group
        # signals; job-local child/process-group management remains available.
        targets = {int(protected_pid), -1}
        if protected_pgid is not None:
            targets.add(-int(protected_pgid))
        for syscall in (62, 129, 200, 234, 297):
            body = [(0x20, 0, 0, 16)]
            for target in sorted(targets):
                body += [(0x15, 0, 1, target & 0xffffffff),
                         (0x06, 0, 0, 0x50000 | errno.EPERM)]
            body += [(0x20, 0, 0, 0)]
            instructions += [(0x15, 0, len(body), syscall), *body]
    # Tasks use stdio/pipe transport, never host pathname or abstract Unix sockets.
    instructions += [(0x15, 0, 4, 41), (0x20, 0, 0, 16),
                     (0x15, 0, 1, 1), (0x06, 0, 0, 0x50000 | errno.EPERM),
                     (0x20, 0, 0, 0)]
    # socketpair(2) of any type stays allowed: the pair is anonymous and connected only
    # to itself, so it reaches nothing outside the job. Chromium's crashpad/IPC needs
    # SOCK_SEQPACKET pairs (a stream-only rule killed the browser at launch).
    if not network:
        blocked.append(42)  # connect; no inherited/alternate socket can restore networking.
        # The rule above already denies AF_UNIX socket creation. Deny every
        # other family too; anonymous socketpair(2) remains available.
        for syscall in (41,):
            instructions += [(0x15, 0, 4, syscall), (0x20, 0, 0, 16),
                             (0x15, 1, 0, 1), (0x06, 0, 0, 0x50000 | errno.EPERM),
                             (0x20, 0, 0, 0)]
    for syscall in blocked:
        instructions += [(0x15, 0, 1, syscall), (0x06, 0, 0, 0x50000 | errno.EPERM)]
    instructions += [(0x06, 0, 0, 0x7fff0000)]
    array = (Filter * len(instructions))(*(Filter(*row) for row in instructions))
    program = Program(len(instructions), array)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(22, 2, ctypes.byref(program), 0, 0):
        raise RuntimeError('mandatory worker seccomp unavailable')
