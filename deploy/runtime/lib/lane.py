"""Provision systemd lanes; pure planning functions are usable without root."""
from __future__ import annotations

import argparse
import base64
import fcntl
import getpass
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import jwt
from urllib.parse import quote, urlencode

from common import DATA, SOURCE, ensure_codex_home, private_replace, private_write, read_env, render_env, require_root, systemctl
LOCK_PATH = '/run/bob-lane.lock'

FAMILY = DATA / 'family'
PORTAL = Path('/var/lib/bob-portal')
FIXED_PORTS = {5432, 6379, 6353, 6354, 58081}


def validate_name(name: str) -> str:
    if re.fullmatch(r'[a-z][a-z0-9-]{0,31}', name) is None:
        raise ValueError('lane name must match [a-z][a-z0-9-]{0,31}')
    return name


def allocate_ports(used: set[int]) -> tuple[int, int]:
    for core in range(7835, 65535, 2):
        if core not in used and core + 1 not in used:
            return core, core + 1
    raise ValueError('no free lane port pair')


def allocate_bridge(used: set[int]) -> int:
    for port in range(8902, 65536):
        if port not in used:
            return port
    raise ValueError('no free bridge port')


def lane_values(name: str, core: int, gateway: int, bridge: int,
                password: str, totp: str, secret: str) -> dict[str, str]:
    validate_name(name)
    root = FAMILY / name / 'bob-memory'
    return {
        'BOB_FAMILY_ROOT': str(FAMILY), 'BOB_DEFAULT_OWNER': name,
        'BOB_LANE_ROOT': str(root),
        'BOBCLAW_CORE_HOST': '127.0.0.1', 'BOBCLAW_CORE_PORT': str(core),
        'BOBCLAW_GATEWAY_HOST': '127.0.0.1', 'BOBCLAW_GATEWAY_PORT': str(gateway),
        'CORE_URL': f'http://127.0.0.1:{core}',
        'BOBCLAW_GATEWAY_URL': f'http://127.0.0.1:{gateway}',
        'BOB_BRIDGE_ENGINE': f'http://127.0.0.1:{core}', 'BOB_BRIDGE_PORT': str(bridge),
        'BOBCLAW_PASSWORD': password, 'TOTP_SECRET': totp,
        'BOB_OWNER_WORK_APPROVAL': 'signed_in_user',
        'BOBCLAW_SECRET': secret, 'JWT_SECRET': secret,
        'GATEWAY_SQLITE_PATH': str(root / 'gateway.db'),
        'MEMORY_ENABLED': 'true', 'MEMORY_L1_EXTRACTION_ENABLED': 'false',
        'MEMORY_SLOTS_FILE': '/etc/bob/memory_slots.toml',
        'MEMORY_QDRANT_URL': 'http://127.0.0.1:6353',
        'MEMORY_SQLITE_PATH': str(root / 'bobclaw_memory.db'),
        'MEMORY_STORES_CONFIG_PATH': str(root / 'memory_stores.toml'),
        'MEMORY_DEFAULT_STORE_ID': f'{name}_bob',
        'MEMORY_WRITE_FENCE_ENABLED': '1', 'MEMORY_WRITER_ENABLED': 'true',
        'MEMORY_T0_RECALL_ENABLED': 'true', 'MEMORY_LKS_FIRST': 'false',
        'MEMORY_SINGLE_QDRANT': 'false',
        'BOBCLAW_LEDGER_INSTANCES': str(root / 'ledger_instances.json'),
        'PROFILE_SCHEDULE_DEFAULT_OWNER': name,
        'CC_PROJECT_DIR': str(root / 'projects' / 'cc'),
        'CC_SCRATCH_ROOT': str(root / 'scratch' / 'cc'),
        'CC_SIDECAR_PATH': str(root / 'cc_session_sidecar.jsonl'),
        'CC_CONFIG_DIR': str(root / 'cli' / 'claude'),
        'AGY_PROJECT_DIR': str(root / 'projects' / 'agy'),
        'AGY_SCRATCH_ROOT': str(root / 'scratch' / 'agy'),
        'AGY_HOME': str(root / 'cli' / 'agy'),
        'AGY_SETTINGS_PATH': str(root / 'cli' / 'agy' / '.gemini' / 'antigravity-cli' / 'settings.json'),
        'CODEX_PROJECT_DIR': str(root / 'projects' / 'codex'),
        'CODEX_SCRATCH_ROOT': str(root / 'scratch' / 'codex'),
        'CODEX_HOME': str(root / 'codex-home'),
        'SPAWN_CONTEXT_DIR': str(root / 'spawn-context'),
        'MCP_SCOPED_DIR': str(root / 'scratch'),
        'BUILD_WORKSPACE_ROOT': str(root / 'scratch' / 'build'),
        'BUILD_SANDBOX': 'worker',
    }


def render_stores(name: str) -> str:
    validate_name(name)
    # TOML keys must be quoted because lane names can contain '-'.
    # The unique prefix prevents lanes sharing the same Qdrant collection.
    return (f'[meta]\nspec_version = "1.0"\n\n[stores."{name}_bob"]\n'
            'acl_allowed_providers = ["qdrant_local"]\n\n[providers.qdrant_local]\n'
            f'locality = "local"\ncollection_prefix = "{name}_bob"\n'
            'capability_classes = ["text_dense"]\n')


def lanes() -> list[tuple[str, Path]]:
    return [(validate_name(p.parent.parent.name), p)
            for p in sorted(FAMILY.glob('*/bob-memory/lane.env'))]


def reserved_ports() -> set[int]:
    ports = set(FIXED_PORTS)
    for _, path in lanes():
        env = read_env(path)
        for key in ('BOBCLAW_CORE_PORT', 'BOBCLAW_GATEWAY_PORT', 'BOB_BRIDGE_PORT'):
            port = int(env[key])
            if not 1 <= port <= 65535:
                raise ValueError('invalid reserved lane port')
            ports.add(port)
    for journal in (DATA / 'lane-provisioning').glob('*.json'):
        values = json.loads(journal.read_text())['values']
        ports.update(int(values[k]) for k in ('BOBCLAW_CORE_PORT', 'BOBCLAW_GATEWAY_PORT', 'BOB_BRIDGE_PORT'))
    return ports


def port_available(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


def units(name: str) -> list[str]:
    validate_name(name)
    return [f'bob-{service}@{name}.service' for service in ('core', 'gateway', 'bridge')]


def broker_scope(name: str) -> dict[str, list[str]]:
    validate_name(name)
    return {
        'may_touch': [f'{FAMILY}/{name}/**', f'{FAMILY}/shared/**'],
        'may_not_touch': [f'{FAMILY}/{other}/**' for other, _ in lanes() if other != name],
    }


# A fresh process avoids importing gateway config with the admin's environment.
# The bearer goes directly to a private inherited fd, never stdout or argv.
_MINT_BROKER_TOKEN = '''
import json
import os
import sys
sys.path.insert(0, sys.argv[1])
from auth import create_portal_token
request = json.load(sys.stdin)
token = create_portal_token(user_id=request['name'], scope=request['scope'])
with os.fdopen(int(sys.argv[2]), 'w') as stream:
    stream.write(token + '\\n')
'''


def rotate_token(name: str) -> None:
    validate_name(name)
    root = FAMILY / name / 'bob-memory'
    if not (root / 'lane.env').is_file():
        raise ValueError('unknown lane')
    lane_env = read_env(root / 'lane.env')
    if not lane_env.get('JWT_SECRET'):
        raise ValueError('lane.env must contain its own JWT_SECRET')
    port = int(lane_env['BOBCLAW_GATEWAY_PORT'])
    if not 1 <= port <= 65535:
        raise ValueError('invalid lane gateway port')
    # Same environment-file order and working directory as bob-gateway@.service.
    from common import execution_mode
    home = str(root / 'service-home') if execution_mode() == 'worker' else '/home/bob'
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': home,
           'PYTHONPATH': str(SOURCE / 'bobclaw-core'), 'PYTHONUNBUFFERED': '1'}
    env.update(read_env(DATA / 'secrets' / 'bobclaw.env'))
    env.update(lane_env)
    generation_epoch = None
    if execution_mode() == 'worker':
        from worker import load_identity
        ids = load_identity(name)
        if 'gateway_uid' in ids:
            from settings import STATE, load
            from core.worker.auth_file import directory
            with directory(STATE / ids['lane_id'] / 'credentials') as cfd:
                active = load(cfd, 'active.json')
                generation_epoch = active['epoch']
                env.update(read_env(STATE / ids['lane_id'] / 'credentials' / 'current.env', safe=True))
                env['BOB_SETTINGS_ID'] = ids['lane_id']
    account = pwd.getpwnam('bobapp')
    PORTAL.mkdir(mode=0o700, exist_ok=True)
    if PORTAL.is_symlink():
        raise ValueError('portal directory must not be a symlink')
    os.chmod(PORTAL, 0o700)
    os.chown(PORTAL, account.pw_uid, account.pw_gid)
    fd, temporary = tempfile.mkstemp(prefix=f'.{name}.token-', dir=PORTAL)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), account.pw_uid, account.pw_gid)
            result = subprocess.run(
                [str(SOURCE / '.venv/bin/python'), '-I', '-c', _MINT_BROKER_TOKEN,
                 str(SOURCE / 'bobclaw-gateway'), str(stream.fileno())],
                input=json.dumps({'name': name, 'scope': broker_scope(name)}), text=True,
                env=env, cwd=root, pass_fds=(stream.fileno(),),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode:
                # Import/config errors can contain secrets; do not forward output.
                raise ValueError('broker token mint failed; gateway output withheld to protect credentials')
            os.fsync(stream.fileno())
        descriptor = {'gateway_port': port}
        if execution_mode() == 'worker':
            from worker import load_identity
            ids = load_identity(name)
            if 'gateway_uid' in ids:
                descriptor['settings_id'] = ids['lane_id']
        private_replace(PORTAL / f'{name}.json', json.dumps(descriptor) + '\n',
                        account.pw_uid, account.pw_gid)
        if generation_epoch is not None:
            with directory(STATE / ids['lane_id'] / 'credentials') as cfd:
                if load(cfd, 'active.json')['epoch'] != generation_epoch:
                    raise ValueError('credential generation changed during portal publication')
        os.replace(temporary, PORTAL / f'{name}.token')
        portal_fd = os.open(PORTAL, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(portal_fd)
        finally:
            os.close(portal_fd)
        # Recovery runs with the lane tree read-only. unlink on a read-only
        # mount can return EROFS even for a missing legacy file; do not issue a
        # cleanup write when provisioning already removed it.
        legacy = root / 'broker.token'
        if legacy.exists():
            legacy.unlink()
    finally:
        Path(temporary).unlink(missing_ok=True)


def add(name: str, password: str) -> None:
    validate_name(name)
    if not password or password.lower() in {'changeme', 'secret', 'password', 'bobclaw', 'none'}:
        raise ValueError('provide a nonempty, non-placeholder password')
    # Validate before creating anything; passwords are never evaluated by a shell.
    render_env({'BOBCLAW_PASSWORD': password})
    from common import writer_guard
    writer_guard(name)
    if not (DATA / '.firstboot-complete').is_file():
        raise ValueError('firstboot must complete before adding lanes')
    from worker import identity, load_identity, provision
    from core.worker.auth_file import directory as safe_directory
    directory = FAMILY / name
    root = directory / 'bob-memory'
    journal_dir = DATA / 'lane-provisioning'
    journal_dir.mkdir(mode=0o700, exist_ok=True)
    journal = journal_dir / (name + '.json')
    if (root / '.disabled').exists():
        raise ValueError('retained lane requires an explicit later migration/re-enablement')
    if directory.exists() and not journal.exists():
        # Existing shared-UID lanes are outside this slice; never adopt their files.
        load_identity(name)
        raise ValueError('lane has no fresh-provision journal; migration required')
    if journal.exists():
        values = json.loads(journal.read_text())['values']
        ids = identity(name)
    else:
        used = reserved_ports()
        while True:
            core, gateway = allocate_ports(used)
            busy = {port for port in (core, gateway) if not port_available(port)}
            if not busy:
                break
            used.update(busy)
        used.update((core, gateway))
        bridge = allocate_bridge(used)
        while not port_available(bridge):
            used.add(bridge)
            bridge = allocate_bridge(used)
        totp = base64.b32encode(secrets.token_bytes(20)).decode('ascii')
        values = lane_values(name, core, gateway, bridge, password, totp, secrets.token_urlsafe(32))
        values['BOBCLAW_SECRET'] = secrets.token_urlsafe(32)
        ids = identity(name)
        values.update({'BOB_WORKER_ID': ids['lane_id'], 'BOB_WORKER_UID': str(ids['worker_uid'])})
        private_write(journal, json.dumps({'values': values}) + '\n', 0, 0)
    with safe_directory(root, create=True):
        pass
    for leaf in ('service-home', 'spawn-context'):
        with safe_directory(root / leaf, create=True) as fd:
            os.fchown(fd, ids['service_uid'], ids['service_gid'])
            os.fchmod(fd, 0o700)
    for path, content, uid, gid, mode in (
        (root / 'memory_stores.toml', render_stores(name), ids['service_uid'], ids['service_gid'], 0o600),
        (root / 'ledger_instances.json', json.dumps({'version': 1, 'instances': {}}) + '\n', ids['service_uid'], ids['service_gid'], 0o600),
        (root / 'lane.env', render_env(values), 0, ids['service_gid'], 0o640)):
        if path.is_symlink():
            raise ValueError('provisioning refuses a control-file symlink')
        if not path.exists():
            private_write(path, content, uid, gid, mode=mode)
    provision(name, root)
    complete_fresh(name, root)
    # The saved journal makes retries preserve ports, password, TOTP and grants.
    current = read_env(root / 'lane.env')
    uri = 'otpauth://totp/' + quote(f'Bob:{name}', safe='') + '?' + urlencode({
        'secret': current['TOTP_SECRET'], 'issuer': 'Bob', 'algorithm': 'SHA1', 'digits': 6, 'period': 30})
    print(uri, flush=True)
    systemctl('enable', '--now', *units(name))
    print(f'Configured {name}: core={current["BOBCLAW_CORE_PORT"]} gateway={current["BOBCLAW_GATEWAY_PORT"]} bridge={current["BOB_BRIDGE_PORT"]}')


def complete_fresh(name, root):
    """Idempotent finish after a firewall/package/service activation failure."""
    from worker import load_identity, service_environments
    ids = load_identity(name)
    ensure_codex_home(root)
    service_environments(root)
    values = read_env(root / 'lane.env')
    from core.worker.auth_file import directory
    for key in ('CODEX_PROJECT_DIR', 'CODEX_SCRATCH_ROOT'):
        path = Path(values[key])
        with directory(path, create=True) as fd:
            os.fchown(fd, ids['worker_uid'], ids['service_gid'])
            os.fchmod(fd, 0o2770)
    renew_if_due(name)


def token_due(token: str, secret: str, name: str, now: float) -> bool:
    """Verify the local bearer before trusting expiry; migrate old agent tokens."""
    try:
        claims = jwt.decode(token, secret, algorithms=['HS256'],
                            options={'verify_exp': False, 'require': ['exp', 'sub']})
        return (claims['sub'] != name or claims.get('token_type') != 'portal'
                or float(claims['exp']) - now < 7 * 86400)
    except (jwt.InvalidTokenError, ValueError, TypeError):
        return True


def renew_if_due(name: str) -> bool:
    validate_name(name)
    root = FAMILY / name / 'bob-memory'
    env = read_env(root / 'lane.env')
    if not env.get('JWT_SECRET'):
        raise ValueError('lane.env must contain its own JWT_SECRET')
    token = PORTAL / f'{name}.token'
    from common import execution_mode
    if execution_mode() == 'worker':
        from worker import load_identity
        ids = load_identity(name)
        if 'gateway_uid' in ids:
            from settings import STATE, load
            from core.worker.auth_file import directory
            with directory(STATE / ids['lane_id'] / 'credentials') as cfd:
                epoch = load(cfd, 'active.json').get('revocation_epoch', 0)
            if epoch:
                try:
                    claims = jwt.decode(token.read_text().strip(), env['JWT_SECRET'], algorithms=['HS256'],
                                        options={'verify_exp': False})
                except Exception:
                    raise ValueError('revoked portal requires trusted publication') from None
                if claims.get('service_epoch') != epoch:
                    raise ValueError('revoked portal requires trusted publication')
    legacy = root / 'broker.token'
    if (legacy.exists() or legacy.is_symlink() or not (PORTAL / f'{name}.json').is_file()
            or not token.is_file()
            or token_due(token.read_text().strip(), env['JWT_SECRET'], name, time.time())):
        rotate_token(name)
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('add')
    create.add_argument('name', type=validate_name)
    create.add_argument('--password-stdin', action='store_true')
    sub.add_parser('list')
    rotate = sub.add_parser('rotate-token')
    rotate.add_argument('name', type=validate_name)
    renew = sub.add_parser('renew-if-due')
    renew.add_argument('name', type=validate_name)
    remove = sub.add_parser('remove')
    remove.add_argument('name', type=validate_name)
    remove.add_argument('--keep-data', action='store_true', required=True)
    args = parser.parse_args()
    require_root()
    # A password prompt/pipe may wait indefinitely for the owner. Read it
    # before serializing lane operations. Validation and writes stay locked.
    password = None
    if args.command == 'add':
        password = (sys.stdin.read().removesuffix('\n').removesuffix('\r')
                    if args.password_stdin else getpass.getpass('Gateway password: '))
    # Serialize allocation/removal, including retained port reservations.
    with open(LOCK_PATH, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == 'list':
            for name, path in lanes():
                env = read_env(path)
                from common import migration_pending
                state = ('retained' if (path.parent / '.disabled').exists() else
                         'migration-pending' if migration_pending(name) else 'configured')
                print(f'{name}\t{env["BOBCLAW_CORE_PORT"]}\t{env["BOBCLAW_GATEWAY_PORT"]}\t{state}')
        elif args.command == 'add':
            add(args.name, password)
        elif args.command == 'rotate-token':
            from common import writer_guard
            writer_guard(args.name)
            rotate_token(args.name)
            print(f'Rotated broker token for {args.name}')
        elif args.command == 'renew-if-due':
            from common import writer_guard
            writer_guard(args.name)
            renewed = renew_if_due(args.name)
            print(f'{args.name}: ' + ('renewed' if renewed else 'not due'))
        else:
            from common import writer_guard
            writer_guard(args.name)
            root = FAMILY / args.name / 'bob-memory'
            if not (root / 'lane.env').is_file():
                raise ValueError('unknown lane')
            systemctl('disable', '--now', *reversed(units(args.name)))
            from common import execution_mode
            if execution_mode() == 'worker':
                from worker import load_identity
                ids = load_identity(args.name)
                from migration import stop_workers
                systemctl('disable', f'bob-worker@{ids["lane_id"]}.socket',
                           f'bob-worker-auth@{ids["lane_id"]}.socket', f'bob-worker-journal@{ids["lane_id"]}.socket',
                            f'bob-claude-turn@{ids["lane_id"]}.socket')
                stop_workers(ids)
            for suffix in ('token', 'json'):
                (PORTAL / f'{args.name}.{suffix}').unlink(missing_ok=True)
            (root / 'broker.token').unlink(missing_ok=True)
            (root / '.disabled').touch(mode=0o600, exist_ok=True)
            print(f'Removed {args.name} units; data and ports retained at {root}')
    return 0
