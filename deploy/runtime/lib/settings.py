"""Root-only settings service: fixed operations and mapped service restarts.

Systemd-activated listener. All persistent opens are beneath
verified no-follow directory descriptors. Journal bodies never contain keys or
key digests; only the published vault generation has credential bytes.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import time

from core.settings_contract import (MAX_REQUEST, SettingsError, fingerprint,
                                    secret_result, status_result, validate, decode)
from core.worker.auth_file import directory
from common import SYSTEMD, private_replace, require_root, root_directory

CONFIG = Path('/etc/bob/settings')
STATE = Path('/var/lib/bob-settings')
RUN = Path('/run/bob-settings')
TMPFILES = Path('/etc/tmpfiles.d')
RESTART_GRACE_SECONDS = 1.5


def checked(fd, *, mode=None):
    st = os.fstat(fd)
    if (st.st_uid != 0 or st.st_nlink != 1 or not stat.S_ISREG(st.st_mode)
            or st.st_mode & 0o022 or st.st_size > MAX_REQUEST):
        raise SettingsError('unsafe_storage')
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        raise SettingsError('unsafe_storage')
    return st


def load(fd, name):
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        checked(child)
        with os.fdopen(child, 'r', closefd=False) as stream:
            return json.load(stream)
    finally:
        os.close(child)


def publish(fd, name, value, *, gid=0, mode=0o600):
    temporary = '.stage-' + secrets.token_hex(16)
    child = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=fd)
    try:
        with os.fdopen(child, 'w', encoding='utf-8') as stream:
            os.fchown(stream.fileno(), 0, gid)
            os.fchmod(stream.fileno(), mode)
            if isinstance(value, str):
                stream.write(value)
            else:
                json.dump(value, stream, separators=(',', ':'), ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


@contextmanager
def locked(path):
    with directory(path) as fd:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SettingsError('unsafe_storage')
        lock = os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=fd)
        try:
            checked(lock, mode=0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(lock)


def config(lane_id):
    if not isinstance(lane_id, str) or not __import__('re').fullmatch('[0-9a-f]{12}', lane_id):
        raise SettingsError('invalid_lane')
    with directory(CONFIG) as fd:
        value = load(fd, lane_id + '.json')
    if (value['lane_id'] != lane_id or len({value[k] for k in
            ('gateway_uid', 'service_uid', 'worker_uid', 'bridge_uid', 'broker_uid')}) != 5):
        raise SettingsError('identity_split_required')
    return value


def receipt(fd, request):
    if 'operation_id' in request:
        # Bounded filename, no caller paths. A key index is never enumerated.
        return status_result(load(fd, 'op-' + request['operation_id'] + '.json'))
    index = hashlib.sha256(request['request_key'].encode()).hexdigest()
    return status_result(load(fd, 'key-' + index + '.json'))


def recover(cfg, fd, vaultfd, value, *, resume=False, after_reply=None):
    """The per-provider pointer is the commit point, including lost replies."""
    if value['phase'] not in {'prepared', 'committed', 'restart-pending'}:
        return value
    try:
        active = load(vaultfd, value['provider'] + '.json')
    except FileNotFoundError:
        active = {}
    if value['phase'] == 'prepared':
        if active.get('operation_id') == value['operation_id']:
            value.update(phase='committed', result=secret_result(active))
        else:
            value.update(phase='failed')
        save_receipt(fd, value)
    if resume and value['phase'] in {'committed', 'restart-pending'}:
        try:
            from model import enroll_provider, restart_provider
            if not value.get('enrollment_complete'):
                # An interrupted/old enrollment may already have changed envs.
                # Repeating it with identical bytes must not erase restart debt.
                owed = value['phase'] == 'restart-pending'
                value.update(phase='restart-pending', restart_required=True)
                save_receipt(fd, value)
                changed = enroll_provider(cfg['lane'], value['provider'])
                value.update(enrollment_complete=True, restart_required=owed or changed)
                save_receipt(fd, value)
            if value['restart_required']:
                if after_reply is not None:
                    after_reply.append(value['operation_id'])
                    return value
                restart_provider(cfg['lane'])
        except Exception:
            # Credential stays committed. Never include provider/helper output.
            raise SettingsError('settings_failed') from None
        value['phase'] = 'ready'
        save_receipt(fd, value)
    return value


def save_receipt(fd, value):
    publish(fd, 'op-' + value['operation_id'] + '.json', value)
    publish(fd, 'key-' + value['key_index'] + '.json', value)


def reconcile(cfg, fd, vaultfd, value, *, resume=False, after_reply=None):
    if value.get('operation') in {'password.change', 'factors.commit'}:
        from credentials import recover as recover_password
        with directory(STATE / cfg['lane_id'] / 'credentials') as cfd:
            return recover_password(cfg, fd, cfd, value, resume=resume)
    if value.get('operation') in {'factors.prepare', 'factors.cancel'}:
        if value['phase'] == 'prepared':
            value.update(phase='failed')
            save_receipt(fd, value)
        return value
    return recover(cfg, fd, vaultfd, value, resume=resume, after_reply=after_reply)


def journals(fd):
    import re
    return [load(fd, name) for name in os.listdir(fd)
            if re.fullmatch(r'op-[0-9a-f]{32}\.json', name)]


def recover_all(cfg):
    home = STATE / cfg['lane_id']
    with locked(home) as fd, directory(home / 'vault') as vaultfd:
        for value in journals(fd):
            reconcile(cfg, fd, vaultfd, value, resume=True)


def repair(cfg):
    """Boot prerequisite: repair env/pointer disagreement, never restart a unit."""
    from credentials import environment
    home = STATE / cfg['lane_id']
    with locked(home) as fd, directory(home / 'credentials') as cfd, directory(home / 'vault') as vaultfd:
        active = load(cfd, 'active.json')
        publish(cfd, 'current.env', environment(load(cfd, active['generation'] + '.json')))
        from recovery import pending
        pending(cfd)  # Remove expired private material before gateway startup.
        for value in journals(fd):
            reconcile(cfg, fd, vaultfd, value, resume=False)


def handle(cfg, peer_uid, request, *, readonly=False, after_reply=None):
    """An internal after_reply queue defers secret restarts; CLI uses None."""
    if peer_uid != cfg['broker_uid' if readonly else 'gateway_uid']:
        raise SettingsError('peer_denied')
    validate(request, receipt=readonly)
    if request['op'] in {'embedder.status', 'embedder.install'}:
        # Peer authorization and the closed schema above apply before any root action.
        # Installation runs in a separate fixed systemd job, never on the socket thread.
        from common import writer_guard
        from embedder import settings_operation, SetupError
        writer_guard(cfg['lane'])
        try:
            return settings_operation(request['op'], request.get('model'))
        except SetupError:
            raise SettingsError('embedder_setup_failed') from None
    if request['op'] in {'secrets.set', 'password.change', 'factors.prepare', 'factors.commit', 'factors.cancel'}:
        from common import writer_guard
        writer_guard(cfg['lane'])
    if request['op'] == 'factors.binding':
        from recovery import binding
        return binding(cfg)
    home = STATE / cfg['lane_id']
    with locked(home) as fd, directory(home / 'vault') as vaultfd:
        # Resolve any interrupted journal before serving status or reusing a key.
        lookup = ('op-' + request['operation_id'] if 'operation_id' in request else
                  'key-' + hashlib.sha256(request['request_key'].encode()).hexdigest()) + '.json'
        try:
            previous = load(fd, lookup)
            # The operation journal is canonical if a crash split the two receipts.
            previous = load(fd, 'op-' + previous['operation_id'] + '.json')
        except FileNotFoundError:
            previous = next((v for v in journals(fd) if
                ('op-' + v['operation_id'] + '.json' == lookup or
                 'key-' + v['key_index'] + '.json' == lookup)), None)
        if previous:
            previous = reconcile(cfg, fd, vaultfd, previous, resume=not readonly,
                                 after_reply=after_reply)
        if request['op'] == 'status':
            if previous is None:
                raise SettingsError('not_found')
            return status_result(previous)
        if previous:
            # Single-use admission, never compare a credential body or its hash.
            raise SettingsError('request_key_used')
        # Finish a committed key enrollment before another key can supersede its
        # pointer; no credential replay or digest comparison is needed.
        for pending in journals(fd):
            if pending.get('operation') in {'secrets.set', 'password.change', 'factors.commit'} and pending['phase'] not in {'ready', 'failed'}:
                reconcile(cfg, fd, vaultfd, pending, resume=True, after_reply=after_reply)
        value = dict(operation_id=secrets.token_hex(16), phase='prepared',
                     key_index=hashlib.sha256(request['request_key'].encode()).hexdigest(),
                     operation=request['op'])
        if request['op'] == 'secrets.set':
            value['provider'] = request['provider']
        save_receipt(fd, value)
        if request['op'] in {'factors.prepare', 'factors.cancel'}:
            from recovery import prepare, challenge, clear
            try:
                with directory(home / 'credentials') as cfd:
                    if request['op'] == 'factors.prepare':
                        result = prepare(cfg, cfd, request)
                    else:
                        challenge(cfd, request)
                        clear(cfd)
                        result = {'changed': False, 'restarted': False}
                value.update(phase='ready', result=result)
                save_receipt(fd, value)
                return result
            except Exception:
                value.update(phase='failed')
                if request['op'] == 'factors.cancel':
                    value['result'] = {'changed': False, 'restarted': False}
                save_receipt(fd, value)
                raise
        if request['op'] in {'password.change', 'factors.commit'}:
            from credentials import change
            if request['op'] == 'factors.commit':
                from recovery import commit as change
            try:
                with directory(home / 'credentials') as cfd:
                    return change(cfg, fd, cfd, request, value)
            except Exception:
                # Commit can precede an I/O exception. Inspect the pointer first.
                recovered = reconcile(cfg, fd, vaultfd, value, resume=True)
                return recovered['result']
        result = dict(provider=request['provider'], stored=True,
                      fingerprint=fingerprint(request['key']), operation_id=value['operation_id'])
        try:
            # Immutable generation first; pointer includes the same operation ID.
            from model import storage_key
            generation = {**result, 'key': storage_key(request['provider'], request['key'])}
            publish(vaultfd, value['operation_id'] + '.json', generation,
                    gid=cfg['service_gid'], mode=0o640)
            publish(vaultfd, request['provider'] + '.json', generation,
                    gid=cfg['service_gid'], mode=0o640)
            recovered = reconcile(cfg, fd, vaultfd, value, resume=True, after_reply=after_reply)
            return secret_result(recovered['result'])
        except Exception:
            # A failure after pointer publication remains committed and recoverable.
            recover(cfg, fd, vaultfd, value)
            raise SettingsError('settings_failed') from None


def provision(ids, root, *, activate=True):
    """Only called by quiesced root provisioning/migration, never over RPC."""
    import pwd
    from common import systemctl
    cfg = {**ids, 'broker_uid': pwd.getpwnam('bobapp').pw_uid}
    from common import read_env
    lane_env = read_env(root / 'lane.env', safe=True)
    cfg['gateway_port'] = int(lane_env['BOBCLAW_GATEWAY_PORT'])
    for target, gid, mode in ((CONFIG, 0, 0o755), (STATE, 0, 0o711),
                              (STATE / ids['lane_id'], 0, 0o711),
                              (STATE / ids['lane_id'] / 'vault', ids['service_gid'], 0o750)):
        with directory(target, create=True) as fd:
            os.fchown(fd, 0, gid)
            os.fchmod(fd, mode)
            os.fsync(fd)
    with directory(CONFIG) as fd:
        try:
            installation = load(fd, 'installation.json')
        except FileNotFoundError:
            installation = {'installation_id': secrets.token_hex(16)}
            publish(fd, 'installation.json', installation)
    cfg.update(installation_id=installation['installation_id'], owner=lane_env['BOB_DEFAULT_OWNER'])
    private_replace(CONFIG / (ids['lane_id'] + '.json'), json.dumps(cfg), 0, 0)
    # Public descriptor contains no credentials and grants no authority.
    descriptor = dict(version=1, lane_id=ids['lane_id'], gateway_uid=ids['gateway_uid'],
                      installation_id=cfg['installation_id'], owner=cfg['owner'])
    private_replace(root / 'settings.json', json.dumps(descriptor), 0, 0)
    os.chmod(root / 'settings.json', 0o644)
    folder = SYSTEMD
    for kind, user in (('control', ids['gateway_user']), ('receipt', 'bobapp')):
        target = folder / f'bob-settings-{kind}@{ids["lane_id"]}.socket.d'
        root_directory(target)
        private_replace(target / 'identity.conf', f'[Socket]\nSocketGroup={user}\n', 0, 0)
    provision_enrollment(ids, root)
    from credentials import provision as provision_credentials
    provision_credentials(cfg, root)
    systemctl('daemon-reload')
    if activate:
        systemctl('enable', '--now', f'bob-settings-control@{ids["lane_id"]}.socket',
                  f'bob-settings-receipt@{ids["lane_id"]}.socket')


def provision_enrollment(ids, root):
    """Root provisioning/upgrade only; no credential changes or unit restart."""
    # Only the root control helper may update this lane's model configuration.
    # It does not write gateway envs or gain access to other lanes' workspaces.
    target = SYSTEMD / f'bob-settings-control@{ids["lane_id"]}.service.d'
    root_directory(target)
    locks = [RUN.parent / 'bob-lane.lock', RUN.parent / f'bob-model-{ids["lane"]}.lock']
    # Named files, rather than writable /run. tmpfiles recreates the lock inodes
    # before socket activation after a reboot; never replace a live lock inode.
    for path in locks:
        with directory(path.parent, create=True) as runfd:
            child = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                            0o600, dir_fd=runfd)
            try:
                checked(child)
            finally:
                os.close(child)
    root_directory(TMPFILES)
    private_replace(TMPFILES / f'bob-settings-{ids["lane_id"]}.conf',
        ''.join(f'f {path} 0600 root root -\n' for path in locks), 0, 0)
    private_replace(target / 'enrollment.conf',
        f'[Service]\nReadWritePaths={root} ' + ' '.join(map(str, locks)) + '\n', 0, 0)


def main():
    import sys
    require_root()
    # Socket instance is root-installed, not supplied by the remote request.
    cfg = config(sys.argv[1])
    if sys.argv[2] not in {'control', 'receipt', 'repair'}:
        raise SettingsError('unsupported_operation')
    if sys.argv[2] == 'repair':
        repair(cfg)
        return
    readonly = sys.argv[2] == 'receipt'
    if not readonly:
        recover_all(cfg)
    listener = socket.socket(fileno=0)
    while True:
        connection, _ = listener.accept()
        with connection:
            serve(cfg, connection, readonly=readonly)


def serve(cfg, connection, *, readonly=False):
    connection.settimeout(10)
    after_reply = []
    try:
        peer = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if peer != cfg['broker_uid' if readonly else 'gateway_uid']:
            raise SettingsError('peer_denied')
        with connection.makefile('rb') as stream:
            raw = stream.readline(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
            raise SettingsError('invalid_args')
        result = handle(cfg, peer, decode(raw), readonly=readonly,
                        after_reply=after_reply)
    except Exception as exc:
        result = {'error': exc.code if isinstance(exc, SettingsError) else 'settings_failed'}
    try:
        connection.sendall(json.dumps(result).encode() + b'\n')
    except OSError:
        return  # Lost reply is reconciled through the durable receipt, never replayed.
    if after_reply and not readonly:
        # Same single-threaded helper, strictly after sendall returns. Allow the
        # gateway to finish HTTP before the core dependency restarts it too.
        time.sleep(RESTART_GRACE_SECONDS)
        try:
            home = STATE / cfg['lane_id']
            with locked(home) as fd, directory(home / 'vault') as vaultfd:
                for operation_id in dict.fromkeys(after_reply):
                    value = load(fd, 'op-' + operation_id + '.json')
                    recover(cfg, fd, vaultfd, value, resume=True)
        except Exception:
            # The reply is already sent. Keep restart-pending for control
            # startup/status/the next key entry; never emit a second reply.
            pass


if __name__ == '__main__':
    main()
