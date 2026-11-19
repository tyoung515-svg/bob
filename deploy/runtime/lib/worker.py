"""Root-only fresh-lane worker provisioning. Never exposed through the socket."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess

from common import SYSTEMD, private_replace, require_root, root_directory, systemctl

CONFIG = Path('/etc/bob/workers')
IDENTITIES = Path('/etc/bob/lane-identities')
RUN = Path('/run/bob-workers')
SERVICE_ENVS = Path('/etc/bob/service-env')
GATEWAY_STATE = Path('/var/lib/bob-gateway')


def unused_uid():
    reserved = {value[role + '_uid'] for path in IDENTITIES.glob('*.json')
                for value in [json.loads(path.read_text())] for role in ('service', 'worker', 'gateway', 'bridge', 'egress')
                if role + '_uid' in value}
    for uid in range(61000, 65000):
        if uid in reserved:
            continue
        try:
            pwd.getpwuid(uid)
        except KeyError:
            return uid
    raise ValueError('isolated lane UID range exhausted; retained UIDs cannot be recycled')


def identity(name):
    """Durable mapping, no UID reuse/removal. Hash collisions fail explicitly."""
    require_root()
    root_directory(IDENTITIES.parent)
    root_directory(IDENTITIES, mode=0o700)
    path = IDENTITIES / (name + '.json')
    lane_id = hashlib.sha256(name.encode()).hexdigest()[:12]
    if path.exists():
        plan = json.loads(path.read_text())
        if plan.get('phase') != 'allocating' and all(k in plan for k in ('gateway_uid','bridge_uid','egress_uid')):
            return load_identity(name)
        if plan.get('phase') != 'allocating':
            # Persist the upgrade phase before reserving either new UID. A crash
            # after reservation/useradd must not be mistaken for a ready mapping.
            plan['phase'] = 'allocating'
            private_replace(path, json.dumps(plan) + '\n', 0, 0)
    else:
        for entry in IDENTITIES.glob('*.json'):
            if json.loads(entry.read_text())['lane_id'] == lane_id:
                raise ValueError('internal lane ID collision')
        for prefix in ('s', 'w', 'g', 'b', 'e'):
            try:
                pwd.getpwnam(f'bob-{prefix}-{lane_id}')
            except KeyError:
                continue
            raise ValueError('unmapped lane account exists; refusing adoption')
        plan = dict(lane=name, lane_id=lane_id, phase='allocating')
        private_replace(path, json.dumps(plan) + '\n', 0, 0)
    for prefix, role in (('s', 'service'), ('w', 'worker'), ('g', 'gateway'), ('b', 'bridge'), ('e', 'egress')):
        username = f'bob-{prefix}-{lane_id}'
        comment = f'Bob isolated {lane_id} {role}'
        try:
            account = pwd.getpwnam(username)
            if account.pw_gecos != comment:
                raise ValueError('unexpected account in interrupted identity allocation')
        except KeyError:
            if role + '_uid' not in plan:
                plan[role + '_uid'] = unused_uid()
                private_replace(path, json.dumps(plan) + '\n', 0, 0)
            subprocess.run(['useradd', '--system', '--user-group', '--no-create-home',
                            '--uid', str(plan[role + '_uid']),
                            '--comment', comment, '--home-dir', '/nonexistent',
                            '--shell', '/usr/sbin/nologin', username], check=True)
            account = pwd.getpwnam(username)
        if role + '_uid' in plan and plan[role + '_uid'] != account.pw_uid:
            raise ValueError('reserved lane UID changed; refusing account adoption')
        plan.update({role + '_user': username, role + '_uid': account.pw_uid,
                     role + '_gid': account.pw_gid})
        private_replace(path, json.dumps(plan) + '\n', 0, 0)
    # Only the Codex home uses the worker group. No worker supplementary grants.
    subprocess.run(['usermod', '-aG', plan['worker_user'], plan['service_user']], check=True)
    plan['phase'] = 'ready'
    private_replace(path, json.dumps(plan) + '\n', 0, 0)
    return plan


def load_identity(name):
    path = IDENTITIES / (name + '.json')
    if not path.is_file():
        raise ValueError('existing lane requires bob-runtime migrate-workers; execution unavailable')
    value = json.loads(path.read_text())
    if value.get('phase') != 'ready':
        raise ValueError('lane identity allocation incomplete; retry fresh-lane provisioning')
    for role in ('service', 'worker', 'gateway', 'bridge', 'egress'):
        if role + '_user' not in value:
            if role in ('gateway', 'bridge', 'egress'):
                continue  # Read-only inspection of v1 mapping; writes require migration.
            raise ValueError('incomplete lane identity')
        account = pwd.getpwnam(value[role + '_user'])
        if (account.pw_uid, account.pw_gid) != (value[role + '_uid'], value[role + '_gid']):
            raise ValueError('persistent lane identity changed; execution unavailable')
    return value


def lane_memory(root):
    """Create or repair the lane owner's MEMORY.md; existing content is never rewritten.

    The gateway appends owner notes to it and reads it back. The lane folder is
    root-owned, so no service account can create the file itself: without this a
    fresh lane's Memory view could list nothing and every save was refused.
    Gateway-writable, core-readable, never core-writable (same rule as gateway SQLite).
    """
    require_root()
    ids = load_identity(root.parent.name)
    if 'gateway_uid' not in ids:
        raise ValueError('lane memory requires the split gateway identity; run bob-runtime migrate-workers')
    from core.worker.auth_file import directory
    with directory(root.parent) as parent:
        fd = os.open('MEMORY.md', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('unsafe lane memory file')
            import fcntl
            from core.starter_memory import seed_empty
            # Serialize repair with other provisioning/upgrade calls. Re-check
            # file size under lock; never truncate or replace an existing note.
            fcntl.flock(fd, fcntl.LOCK_EX)
            seed_empty(fd)
            os.fchown(fd, ids['gateway_uid'], ids['service_gid'])
            os.fchmod(fd, 0o640)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent)


def provision(name, root, *, activate=True, admit=True):
    require_root()
    ids = identity(name)
    lane_id = ids['lane_id']
    from core.worker.auth_file import directory
    # mkdir(mode=) is masked by the caller's umask (migration and firstboot use
    # 077), which left profiles unreadable to the worker. Set it explicitly.
    root_directory(CONFIG.parent)
    root_directory(CONFIG)
    worker_home = Path('/var/lib/bob-workers') / lane_id / 'home'
    journal_home = Path('/var/lib/bob-worker-journals') / lane_id
    from core.worker.journal import Journal
    with directory(journal_home.parent, create=True) as fd:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o711)
    with directory(journal_home, create=True) as fd:
        os.fchown(fd, ids['worker_uid'], ids['worker_gid'])
        os.fchmod(fd, 0o700)
    journal_path = journal_home / 'journal.sqlite3'
    # An installed sentinel survives a missing DB and forbids silent recreation.
    sentinel = journal_home / 'initialized'
    if not sentinel.exists():
        journal = Journal(journal_path, create=True)
        journal.close()
        for entry in journal_home.iterdir():
            os.chown(entry, ids['worker_uid'], ids['worker_gid'])
            os.chmod(entry, 0o600)
        private_replace(sentinel, 'v2\n', 0, 0)
    for leaf in ('service-home', 'spawn-context'):
        with directory(root / leaf, create=True) as fd:
            os.fchown(fd, ids['service_uid'], ids['service_gid'])
            os.fchmod(fd, 0o700)
    for parent in (worker_home.parent.parent, worker_home.parent):
        with directory(parent, create=True) as fd:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o711)
    with directory(worker_home, create=True) as fd:
        os.fchown(fd, ids['worker_uid'], ids['worker_gid'])
        os.fchmod(fd, 0o700)
    for path in (root.parent, root):
        # Workers get traversal only via an exact ACL; no directory listing.
        with directory(path) as fd:
            os.fchown(fd, ids['service_uid'] if path == root else 0, ids['service_gid'])
            os.fchmod(fd, 0o750)
            subprocess.run(['setfacl', '-m', f'u:{ids["worker_uid"]}:--x', f'/proc/self/fd/{fd}'],
                           pass_fds=(fd,), check=True)
            for role in ('gateway', 'bridge'):
                subprocess.run(['setfacl', '-m', f'u:{ids[role + "_uid"]}:--x', f'/proc/self/fd/{fd}'],
                               pass_fds=(fd,), check=True)
    lane_memory(root)
    # Gateway SQLite is gateway-writable, core-readable, never core-writable.
    gateway_state = GATEWAY_STATE / ids['lane_id']
    with directory(gateway_state.parent, create=True) as fd:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o711)
    with directory(gateway_state, create=True) as fd:
        os.fchown(fd, ids['gateway_uid'], ids['service_gid'])
        os.fchmod(fd, 0o2750)
    for leaf in ('gateway-home', 'bridge-home'):
        role = leaf.split('-')[0]
        with directory(root / leaf, create=True) as fd:
            os.fchown(fd, ids[role + '_uid'], ids[role + '_gid'])
            os.fchmod(fd, 0o700)
    # Move the offline SQLite authority once. Never copy a live WAL database.
    # Inspect every sidecar on EVERY retry. The main DB may already have moved
    # when power was lost before its WAL/shm siblings.
    with directory(root) as sourcefd, directory(gateway_state) as targetfd:
        for suffix in ('', '-wal', '-shm', '-journal'):
            source = root / ('gateway.db' + suffix)
            if source.exists():
                if source.is_symlink() or source.stat().st_nlink != 1:
                    raise ValueError('unsafe gateway database migration')
                destination = gateway_state / source.name
                if destination.exists():
                    raise ValueError('ambiguous gateway database migration')
                os.rename(source.name, destination.name, src_dir_fd=sourcefd, dst_dir_fd=targetfd)
                os.fsync(targetfd)
                os.fsync(sourcefd)
    for path in gateway_state.iterdir():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('unsafe gateway state')
        os.chown(path, ids['gateway_uid'], ids['service_gid'])
        os.chmod(path, 0o640)
    for leaf in ('projects', 'scratch'):
        path = root / leaf
        with directory(path, create=True) as fd:
            os.fchown(fd, ids['worker_uid'], ids['service_gid'])
            os.fchmod(fd, 0o2770)
            subprocess.run(['setfacl', '-m', f'd:u:{ids["worker_uid"]}:rwx,d:g:{ids["service_gid"]}:rwx,d:m:rwx', f'/proc/self/fd/{fd}'],
                           pass_fds=(fd,), check=True)
        with directory(path / 'codex', create=True) as fd:
            os.fchown(fd, ids['worker_uid'], ids['service_gid'])
            os.fchmod(fd, 0o2770)
    # Service-owned parents prevent a worker from renaming other job roots.
    run_root = root / 'run-workspaces'
    with directory(run_root, create=True) as fd:
        os.fchown(fd, ids['service_uid'], ids['worker_gid'])
        os.fchmod(fd, 0o2710)
    claude_home = provision_claude(ids, root)
    config = {**ids, 'egress_port': ids['egress_uid'] - 40000,
              'run_workspace_root': str(run_root), 'max_timeout': 86400, 'socket': f'/run/bob-workers/{lane_id}/exec.sock',
              'journal_socket': f'/run/bob-workers/{lane_id}/journal.sock',
              'journal_path': str(journal_path),
              'auth_socket': f'/run/bob-workers/{lane_id}/auth.sock',
              'worker_home': str(worker_home), 'codex_home': str(root / 'codex-home'),
              'claude_home': str(claude_home),
              'workspaces': {'scratch': str(root / 'scratch'), 'projects': str(root / 'projects')},
              'profiles': {
                  'builder': {'argv': ['/opt/bob/.venv/bin/python', '-I', '-m', 'core.worker.builder_runner'], 'network': True,
                              'egress': 'lane-proxy/1',
                              'adapter_revision': 'builder-command/1', 'filesystem_min_abi': 3},
                  'checked': {'argv': ['/opt/bob/.venv/bin/python', '-I', '-m', 'core.tool_jobs.checked_runner'], 'network': False,
                              'adapter_revision': 'checked-edits/1', 'filesystem_min_abi': 3},
                  'proposal': {'argv': ['/opt/bob/.venv/bin/python', '-I', '-m', 'core.tool_jobs.runner'], 'network': False,
                               'adapter_revision': 'proposal-report/1', 'filesystem_min_abi': 3},
                  'shell': {'argv': ['/bin/sh'], 'network': False},
                  'python': {'argv': ['/opt/bob/.venv/bin/python'], 'network': False},
                  'git': {'argv': ['/usr/bin/git'], 'network': False},
                  'codex': {'argv': ['/usr/local/bin/codex'], 'network': True, 'egress': 'lane-proxy/1'},
                  'claude': {'argv': ['/usr/local/bin/claude'], 'network': True, 'egress': 'lane-proxy/1'},
                  'mcp': {'argv': ['/opt/bob/.venv/bin/python', '-I', '-m', 'core.worker.mcp_runner'],
                          'network': True, 'egress': 'lane-proxy/1'},
                  'auth-replace': {'argv': ['/bin/false'], 'network': False}}}
    provision_claude_turn(ids, root, config)
    path = CONFIG / (lane_id + '.json')
    if not admit:
        config['socket'] = f'/run/bob-workers/{lane_id}/migration-disabled'
        config['auth_socket'] = f'/run/bob-workers/{lane_id}/migration-disabled-auth'
        config['claude_turn_socket'] = f'/run/bob-workers/{lane_id}/migration-disabled-claude'
    config['admission'] = 'enabled' if admit else 'migration-pending'
    private_replace(path, json.dumps(config, indent=2) + '\n', 0, 0)
    os.chmod(path, 0o644)
    proxy_folder = SYSTEMD / f'bob-egress-proxy@{lane_id}.service.d'
    root_directory(proxy_folder)
    private_replace(proxy_folder / 'identity.conf',
        f'[Service]\nUser={ids["egress_user"]}\nGroup={ids["egress_user"]}\n', 0, 0)
    core_folder = SYSTEMD / f'bob-core@{name}.service.d'
    root_directory(core_folder)
    private_replace(core_folder / 'egress.conf',
        f'[Unit]\nRequires=bob-egress-proxy@{lane_id}.service\nAfter=bob-egress-proxy@{lane_id}.service\n', 0, 0)
    # Numeric identities are root-owned unit configuration, never environment expansion in User=.
    for role in ('core', 'gateway', 'bridge'):
        folder = SYSTEMD / f'bob-{role}@{name}.service.d'
        root_directory(folder)
        principal = 'service' if role == 'core' else role
        home = 'service-home' if role == 'core' else role + '-home'
        text = (f'[Service]\nUser={ids[principal + "_user"]}\nGroup={ids[principal + "_user"]}\n'
                f'Environment=HOME={root}/{home}\nEnvironment=BOB_SERVICE_ROLE={role}\n'
                'NoNewPrivileges=yes\n')
        if role == 'core':
            text += f'SupplementaryGroups={ids["worker_user"]}\n'
        else:
            text += 'SupplementaryGroups=\n'
        if role == 'gateway':
            text += 'UMask=0027\n'
        if role != 'core':
            text += f'EnvironmentFile=\nEnvironmentFile={SERVICE_ENVS}/{ids["lane_id"]}-{role}.env\n'
        private_replace(folder / 'identity.conf', text, 0, 0)
    folder = SYSTEMD / f'bob-worker@{lane_id}.service.d'
    root_directory(folder)
    private_replace(folder / 'identity.conf',
        f'[Service]\nUser={ids["worker_user"]}\nGroup={ids["worker_user"]}\n'
        f'ReadWritePaths={worker_home} {root}/projects {root}/scratch {root}/codex-home {claude_home} {run_root}\n'
        f'BindPaths={root}/projects {root}/scratch {root}/codex-home {claude_home} {run_root}\n'
        f'BindReadOnlyPaths=/run/bob-workers/{lane_id}/policy.json /run/bob-workers/{lane_id}/check.sock /run/bob-workers/{lane_id}/journal.sock\n', 0, 0)
    journal_folder = SYSTEMD / f'bob-worker-journal@{lane_id}.service.d'
    root_directory(journal_folder)
    private_replace(journal_folder / 'identity.conf',
        f'[Service]\nUser={ids["worker_user"]}\nGroup={ids["worker_user"]}\n'
        f'ReadWritePaths={journal_home}\n', 0, 0)
    journal_socket = SYSTEMD / f'bob-worker-journal@{lane_id}.socket.d'
    root_directory(journal_socket)
    private_replace(journal_socket / 'identity.conf', f'[Socket]\nSocketUser={ids["worker_user"]}\n', 0, 0)
    folder = SYSTEMD / f'bob-worker@{lane_id}.socket.d'
    root_directory(folder)
    private_replace(folder / 'identity.conf', f'[Socket]\nSocketUser={ids["service_user"]}\n', 0, 0)
    # Independent auth publication cannot wait behind a 900-second Codex job.
    auth_folder = SYSTEMD / f'bob-worker-auth@{lane_id}.service.d'
    root_directory(auth_folder)
    private_replace(auth_folder / 'identity.conf',
        (folder.parent / f'bob-worker@{lane_id}.service.d/identity.conf').read_text().replace(
            f' /run/bob-workers/{lane_id}/journal.sock', ''), 0, 0)
    auth_folder = SYSTEMD / f'bob-worker-auth@{lane_id}.socket.d'
    root_directory(auth_folder)
    private_replace(auth_folder / 'identity.conf', f'[Socket]\nSocketUser={ids["service_user"]}\n', 0, 0)
    # Credentials bootstrap reads the split gateway env and its relocated SQLite
    # authority. Publish both before settings provisioning, on fresh and upgrade
    # paths alike; later env refreshes must not replace the credential generation.
    service_environments(root)
    from settings import provision as provision_settings
    provision_settings(ids, root, activate=activate)
    if activate:
        systemctl('enable', '--now', f'bob-egress-proxy@{lane_id}.service', f'bob-worker-journal@{lane_id}.socket',
                  f'bob-worker@{lane_id}.socket', f'bob-worker-auth@{lane_id}.socket', f'bob-claude-turn@{lane_id}.socket')
    return config


def proxy_profiles_ready(ids):
    """Old S1b identities alone do not prove Codex/MCP proxy enrollment."""
    if 'egress_uid' not in ids:
        return False
    try:
        config = json.loads((CONFIG / (ids['lane_id'] + '.json')).read_text())
        profiles = config.get('profiles') if isinstance(config, dict) else None
        if not isinstance(profiles, dict):
            return False
        return all(isinstance(profiles.get(name), dict)
                   and profiles[name].get('egress') == 'lane-proxy/1'
                   and profiles[name].get('network') is True
                   for name in ('builder', 'codex', 'claude', 'mcp'))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def set_admission(ids, enabled):
    """Atomically control core's submission addresses, without changing identity.

    The daemon uses systemd's inherited fd, never these addresses. Migration can
    therefore verify the real listener while normal core dispatch is unavailable.
    Publication happens only after the completed-version receipt is durable.
    """
    require_root()
    path = CONFIG / (ids['lane_id'] + '.json')
    config = json.loads(path.read_text())
    base = f'/run/bob-workers/{ids["lane_id"]}/'
    config['socket'] = base + ('exec.sock' if enabled else 'migration-disabled')
    config['auth_socket'] = base + ('auth.sock' if enabled else 'migration-disabled-auth')
    if 'claude_turn_socket' in config:
        config['claude_turn_socket'] = base + ('claude-turn.sock' if enabled else 'migration-disabled-claude')
    config['admission'] = 'enabled' if enabled else 'migration-pending'
    private_replace(path, json.dumps(config, indent=2) + '\n', 0, 0)
    os.chmod(path, 0o644)


def write_lane_env(root, values):
    """Shared operator writer for fresh lanes, migration and later Settings."""
    from common import render_env
    ids = load_identity(root.parent.name)
    private_replace(root / 'lane.env', render_env(values), 0, ids['service_gid'])
    os.chmod(root / 'lane.env', 0o640)


def service_environments(root, *, models_only=False):
    from common import DATA, read_env, render_env
    from core.worker.auth_file import directory
    ids = load_identity(root.parent.name)
    # Upgrade defaults apply to generated roles only. Never rewrite lane.env or
    # rotate an existing password, factor, provider token or credential generation.
    lane = {'BOB_OWNER_WORK_APPROVAL': 'signed_in_user', **read_env(root / 'lane.env')}
    shared = read_env(DATA / 'secrets/bobclaw.env')
    storage = {k: shared[k] for k in ('POSTGRES_URL', 'REDIS_URL') if k in shared}
    split = 'gateway_uid' in ids and 'bridge_uid' in ids
    if split:
        lane['GATEWAY_SQLITE_PATH'] = str(GATEWAY_STATE / ids['lane_id'] / 'gateway.db')
        lane['GATEWAY_SQLITE_JOURNAL_MODE'] = 'DELETE'
        lane['BOB_SETTINGS_ID'] = ids['lane_id']
        lane['BOB_GATEWAY_UID'] = str(ids['gateway_uid'])
    core = {**storage, **{k: v for k, v in lane.items() if k not in
                        {'BOBCLAW_PASSWORD', 'TOTP_SECRET', 'JWT_SECRET', 'BOB_OWNER_WORK_APPROVAL'}}}
    # Desktop defaults only; explicit shared and per-lane settings win.
    gateway = {'RATE_LIMIT_PER_MINUTE': '600', 'RATE_LIMIT_BURST': '300',
               **storage, **{k: shared[k] for k in ('RATE_LIMIT_PER_MINUTE', 'RATE_LIMIT_BURST') if k in shared},
               **lane}
    bridge = {k: lane[k] for k in ('BOB_BRIDGE_ENGINE', 'BOB_BRIDGE_PORT')}
    if split:
        bridge.update(BOB_SETTINGS_ID=ids['lane_id'], BOB_LANE_ROOT=str(root))
    for role, values in (('core', core), ('gateway', gateway), ('bridge', bridge)):
        if models_only and role != 'core':
            continue
        principal = 'service' if role == 'core' or not split else role
        private_replace(root / (role + '.env'), render_env(values), 0, ids[principal + '_gid'])
        os.chmod(root / (role + '.env'), 0o640)
        if split and role != 'core':
            external = SERVICE_ENVS
            with directory(external, create=True) as fd:
                os.fchown(fd, 0, 0)
                os.fchmod(fd, 0o700)
            private_replace(external / (ids['lane_id'] + '-' + role + '.env'), render_env(values), 0, 0)
            # No gateway factors remain beneath the core-writable lane ancestor.
            (root / (role + '.env')).unlink()
    if split:
        public_fields = {'BOB_DEFAULT_OWNER', 'BOB_LANE_ROOT', 'BOB_WORKER_ID', 'BOB_WORKER_UID',
                         'BOB_LANE_DEFAULT_BACKEND', 'BOB_LANE_BACKENDS', 'CODEX_PROFILE', 'CODEX_MODEL',
                         'BOB_SETTINGS_ID', 'BOB_GATEWAY_UID'}
        public = {k: v for k, v in lane.items() if k in public_fields}
        from core.lane_config import read_lane
        provider_values = read_env(root / 'providers.env') if (root / 'providers.env').exists() else {}
        catalog = read_lane(root, values={**lane, **provider_values})
        public['BOB_PUBLIC_BACKENDS'] = json.dumps([row.row() for row in catalog.backends.values()
            if row.backend not in {'codex_code', 'claude_code'} or row.model])
        private_replace(root / 'public.env', render_env(public), 0, 0)
        os.chmod(root / 'public.env', 0o644)
        # Root operator configuration contains gateway factors. Core no longer reads it.
        os.chown(root / 'lane.env', 0, 0)
        os.chmod(root / 'lane.env', 0o600)


def nft_rules(config, resolver=None):
    uid, name = config['worker_uid'], 'bob_w_' + config['lane_id']
    port = config.get('egress_port')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('worker unavailable: lane egress proxy not provisioned')
    # A dedicated table per retained UID. Route-local matching covers public host addresses too.
    return f'''table inet {name} {{
 chain output {{
  type filter hook output priority -150; policy accept;
  meta skuid {uid} jump worker
 }}
 chain worker {{
  ip daddr 127.0.0.1 tcp dport {port} accept
  reject
 }}
}}
'''


def policy(lane_id, stop=False):
    require_root()
    config = json.loads((CONFIG / (lane_id + '.json')).read_text())
    directory = runtime_directory(lane_id)
    marker = directory / 'policy.json'
    marker.unlink(missing_ok=True)
    table = 'bob_w_' + lane_id
    if stop:
        # Keep deny rules on stop/removal. UIDs are never recycled in this slice.
        return
    exists = subprocess.run(['nft', 'list', 'table', 'inet', table], capture_output=True).returncode == 0
    rules = ('delete table inet ' + table + '\n' if exists else '') + nft_rules(config)
    # nft batch commit is atomic. Failure never creates a readiness receipt.
    subprocess.run(['nft', '-f', '-'], input=rules, text=True, check=True)
    subprocess.run(['nft', 'list', 'table', 'inet', table], check=True, stdout=subprocess.DEVNULL)
    private_replace(marker, json.dumps({'mode': 'nft-uid', 'worker_uid': config['worker_uid']}) + '\n', 0, 0)
    os.chmod(marker, 0o644)


def runtime_directory(lane_id):
    """Repair both directory modes on every policy start, independent of umask."""
    from core.worker.auth_file import directory
    path = RUN / lane_id
    for target in (RUN, path):
        with directory(target, create=True) as fd:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o711)
    return path


if __name__ == '__main__':
    import sys
    policy(sys.argv[1], stop=len(sys.argv) > 2 and sys.argv[2] == 'stop')


def provision_claude(ids, root):
    """Fixed lane-only configuration; service needs no Claude credential access."""
    from core.worker.auth_file import directory
    with directory(root / 'cli', create=True) as fd:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o711)
    home = root / 'cli/claude'
    with directory(home, create=True) as fd:
        os.fchown(fd, ids['worker_uid'], ids['worker_gid'])
        os.fchmod(fd, 0o700)
    return home


def provision_claude_turn(ids, root, config):
    """Claude's model loop must not hold the queue used by its Bob tool jobs."""
    key = ids['lane_id']
    config['claude_turn_socket'] = f'/run/bob-workers/{key}/claude-turn.sock'
    for kind in ('service', 'socket'):
        folder = SYSTEMD / f'bob-claude-turn@{key}.{kind}.d'
        root_directory(folder)
        if kind == 'socket':
            text = f'[Socket]\nSocketUser={ids["service_user"]}\n'
        else:
            home = config['claude_home']
            text = (f'[Service]\nUser={ids["worker_user"]}\nGroup={ids["worker_user"]}\n'
                    f'ReadWritePaths={home}\nBindPaths={home}\n'
                    f'BindReadOnlyPaths=/run/bob-workers/{key}/policy.json /run/bob-workers/{key}/check.sock\n')
        private_replace(folder / 'identity.conf', text, 0, 0)
