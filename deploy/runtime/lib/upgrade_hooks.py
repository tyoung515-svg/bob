"""Idempotent, reviewed release hooks; no lane secrets/accounts are rewritten."""
import json
from pathlib import Path
import pwd
import subprocess


def c5_builder():
    from common import private_replace
    from lane import lanes
    from worker import CONFIG, load_identity
    for name, _ in lanes():
        ids = load_identity(name)
        path = CONFIG / (ids['lane_id'] + '.json')
        config = json.loads(path.read_text())
        config['profiles']['builder'] = {
            'argv': ['/opt/bob/.venv/bin/python', '-I', '-m', 'core.worker.builder_runner'],
            'network': False, 'adapter_revision': 'builder-command/1', 'filesystem_min_abi': 3}
        private_replace(path, json.dumps(config, indent=2) + '\n', 0, 0)
        path.chmod(0o644)


def c5_egress():
    """Explicit C5 migration matching S1b's dedicated UID, config and unit contract.

    New egress identities are monotonic and retained on rollback; no user account
    or existing service identity is deleted, reassigned or reprovisioned.
    """
    if not Path('/etc/systemd/system/bob-egress-proxy@.service').exists():
        return []
    from common import private_replace, root_directory
    import lane
    import worker
    units = []
    for name, env in lane.lanes():
        ids = worker.load_identity(name)
        key = ids['lane_id']
        username, comment = f'bob-e-{key}', f'Bob isolated {key} egress'
        identity = worker.IDENTITIES / (name + '.json')
        if 'egress_uid' not in ids:
            ids['egress_uid'] = worker.unused_uid()
            private_replace(identity, json.dumps(ids) + '\n', 0, 0)
        try:
            account = pwd.getpwnam(username)
        except KeyError:
            subprocess.run(['useradd', '--system', '--user-group', '--no-create-home',
                            '--uid', str(ids['egress_uid']), '--comment', comment,
                            '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', username], check=True)
            account = pwd.getpwnam(username)
        if account.pw_uid != ids['egress_uid'] or account.pw_gecos != comment:
            raise ValueError('egress identity drift')
        ids.update(egress_user=username, egress_uid=account.pw_uid, egress_gid=account.pw_gid)
        private_replace(identity, json.dumps(ids) + '\n', 0, 0)
        path = worker.CONFIG / (key + '.json')
        config = json.loads(path.read_text())
        config.update({k: ids[k] for k in ('egress_user', 'egress_uid', 'egress_gid')})
        config['egress_port'] = ids['egress_uid'] - 40000
        if not 1024 <= config['egress_port'] <= 65535:
            raise ValueError('unsupported egress UID/port allocation')
        config['profiles']['builder'].update(network=True, egress='lane-proxy/1')
        # Validate the packaged firewall contract before any admission/start.
        rules = worker.nft_rules(config)
        body = ' '.join(rules.partition('chain worker {')[2].partition('}')[0].split())
        expected = f"ip daddr 127.0.0.1 tcp dport {config['egress_port']} accept reject"
        if body != expected:
            raise ValueError('packaged worker lacks the bounded egress policy')
        private_replace(path, json.dumps(config, indent=2) + '\n', 0, 0)
        path.chmod(0o644)
        proxy = worker.SYSTEMD / f'bob-egress-proxy@{key}.service.d'
        root_directory(proxy)
        private_replace(proxy / 'identity.conf',
                        f'[Service]\nUser={username}\nGroup={username}\n', 0, 0)
        (proxy / 'identity.conf').chmod(0o644)
        core = worker.SYSTEMD / f'bob-core@{name}.service.d'
        root_directory(core)
        private_replace(core / 'egress.conf',
                        f'[Unit]\nRequires=bob-egress-proxy@{key}.service\nAfter=bob-egress-proxy@{key}.service\n', 0, 0)
        (core / 'egress.conf').chmod(0o644)
        if not (env.parent / '.disabled').exists():
            units.extend([f'bob-egress-proxy@{key}.service', f'bob-worker-policy@{key}.service'])
    return units


def c5_embedder():
    from embedder import migrate_selection
    return migrate_selection()


def lane_memory():
    """Repair owner memory and seed only missing/empty files; preserve existing notes byte-for-byte."""
    import lane
    import worker
    for name, _ in lane.lanes():
        # A v1 identity has no gateway account yet; migrate-workers provisions the file with it.
        if 'gateway_uid' in worker.load_identity(name):
            worker.lane_memory(lane.FAMILY / name / 'bob-memory')


def settings_enrollment():
    """Give existing split lanes the same scoped enrollment mounts as new lanes."""
    import lane
    import worker
    from settings import provision_enrollment
    for name, _ in lane.lanes():
        ids = worker.load_identity(name)
        if 'gateway_uid' in ids:
            provision_enrollment(ids, lane.FAMILY / name / 'bob-memory')


def claude_setup(only_lane=None):
    """Add only the new profile/mount; leave existing boundary and identities intact."""
    import lane
    import worker
    from common import private_replace, root_directory
    units = []
    for name, env in lane.lanes():
        if only_lane is not None and name != only_lane:
            continue
        ids = worker.load_identity(name)
        path = worker.CONFIG / (ids['lane_id'] + '.json')
        config = json.loads(path.read_text())
        home = worker.provision_claude(ids, env.parent)
        config['claude_home'] = str(home)
        config['profiles']['claude'] = {'argv': ['/usr/local/bin/claude'],
                                      'network': True, 'egress': 'lane-proxy/1'}
        private_replace(path, json.dumps(config, indent=2) + '\n', 0, 0)
        path.chmod(0o644)
        folder = worker.SYSTEMD / f'bob-worker@{ids["lane_id"]}.service.d'
        root_directory(folder)
        private_replace(folder / 'claude.conf',
                        f'[Service]\nReadWritePaths={home}\nBindPaths={home}\n', 0, 0)
        (folder / 'claude.conf').chmod(0o644)
        if not (env.parent / '.disabled').exists():
            units.append(f'bob-worker@{ids["lane_id"]}.service')
    return units


def claude_turns(only_lane=None):
    """Add the independent model supervisor, preserving other worker profiles."""
    import lane
    import worker
    from common import private_replace
    units = []
    for name, env in lane.lanes():
        if only_lane is not None and name != only_lane:
            continue
        ids = worker.load_identity(name)
        filename = worker.CONFIG / (ids['lane_id'] + '.json')
        config = json.loads(filename.read_text())
        worker.provision_claude_turn(ids, env.parent, config)
        if config.get('admission') != 'enabled':
            config['claude_turn_socket'] = f'/run/bob-workers/{ids["lane_id"]}/migration-disabled-claude'
        private_replace(filename, json.dumps(config, indent=2) + '\n', 0, 0)
        filename.chmod(0o644)
        if not (env.parent / '.disabled').exists():
            units.append(f'bob-claude-turn@{ids["lane_id"]}.socket')
    return units


HOOKS = (('c5-builder', c5_builder), ('c5-egress', c5_egress), ('c5-embedder-env', c5_embedder),
         ('lane-memory', lane_memory), ('settings-enrollment', settings_enrollment),
         ('cc1-claude-setup', claude_setup), ('cc2-claude-turns', claude_turns))


def run():
    units = []
    for _, hook in HOOKS:
        units.extend(hook() or [])
    print(json.dumps({'units': units}))
