"""Versioned packaged-runtime migration. No provider or network operations."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import socket
import subprocess
import sys
import time

from common import DATA, SOURCE, MIGRATIONS, private_replace, read_env as parse_env_file, render_env, require_root, systemctl
import lane
import worker
from migration_paths import apply, describe, transfer
from core.worker.auth_file import directory

VERSION = 1
STATE = MIGRATIONS
BOUNDARY = Path('/etc/bob/execution-boundary')
SYSTEMD = Path('/etc/systemd/system')
PHASES = ('quiesce', 'identities', 'ownership', 'credentials', 'verify', 'publish', 'completed')
NOTICE = ('Password, TOTP and shared database credentials may have been exposed by pre-migration '
          'workers. They were NOT reset. As root, edit BOBCLAW_PASSWORD/TOTP_SECRET in lane.env, '
          'regenerate role envs with bob-runtime refresh-env --lane NAME, restart the lane and '
          're-enroll clients. Coordinate POSTGRES_PASSWORD/POSTGRES_URL rotation with ALTER ROLE '
          'and every lane; regenerate role envs and restart all affected services. Never paste secrets into logs.')


def private_directory(path):
    with directory(path, create=True) as fd:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o700)
        os.fsync(fd)
    with directory(path.parent) as fd:
        os.fsync(fd)


def read_env(path):
    return parse_env_file(path, safe=True)


def save(path, value):
    private_replace(path, json.dumps(value, indent=2) + '\n', 0, 0)
    with directory(path.parent) as fd:
        os.fsync(fd)


def state_for(name):
    path = STATE / name / 'state.json'
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if value.get('version') != VERSION or value.get('lane') != name or value.get('phase') not in PHASES:
        raise ValueError('unsupported migration journal; execution must remain disabled')
    return value


def pending(name):
    return (STATE / name / 'pending').exists()


def writer_guard(name):
    if pending(name):
        raise ValueError('lane migration incomplete; retry bob-runtime migrate-workers before settings/enrollment')


def plan(name):
    lane.validate_name(name)
    root = lane.FAMILY / name / 'bob-memory'
    with directory(root):
        pass
    values = read_env(root / 'lane.env')
    providers = read_env(root / 'providers.env') if (root / 'providers.env').exists() else {}
    for key in values.keys() | providers.keys():
        if key in {'PATH', 'HOME', 'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONUSERBASE',
                   'ENV', 'BASH_ENV', 'NODE_OPTIONS', 'PERL5OPT', 'RUBYOPT'} or key.startswith('LD_'):
            raise ValueError('unsafe process-loader override in lane environment; operator review required')
    if (root / '.disabled').exists():
        raise ValueError('retained/removed lane must be reviewed before migration')
    # Preserve all values, but refuse paths outside the installed worker mounts.
    expected = lane.lane_values(name, 1, 2, 3, 'unused', 'unused', 'unused')
    for key, value in expected.items():
        if key.endswith(('_DIR', '_ROOT', '_HOME', '_PATH')) and key in values:
            if values[key] != value and key not in {'MEMORY_SLOTS_FILE'}:
                raise ValueError(f'nonstandard {key}; move it into the lane layout before migration')
    state = state_for(name)
    # S3d upgrades already-isolated lanes through the same offline journal. Never
    # treat the old BOB_WORKER_ID alone as proof of the settings identity split.
    if state and state['phase'] == 'completed' and not state.get('settings_version'):
        ids = worker.load_identity(name)
        if 'gateway_uid' not in ids or 'bridge_uid' not in ids:
            state = dict(lane=name, version=VERSION, phase='quiesce', ownership={}, helpers={},
                         previous_version=VERSION, settings_upgrade=True)
    if state and state['phase'] == 'completed' and not worker.proxy_profiles_ready(worker.load_identity(name)):
        # Pre-proxy and S1b lanes still need quiesced identity/profile/firewall
        # provisioning. Do not mistake their old completion receipt for support.
        state = dict(lane=name, version=VERSION, phase='quiesce', ownership={}, helpers={},
                     previous_version=VERSION, egress_upgrade=True)
    if state is not None and state['phase'] == 'completed' and (pending(name) or not state.get('activation_complete')):
        state['phase'] = 'verify'  # Interrupted final commit/cleanup is not a no-op.
    if state is None and values.get('BOB_WORKER_ID') and not pending(name):
        ids = worker.load_identity(name)
        if (values['BOB_WORKER_ID'] == ids['lane_id'] and 'gateway_uid' in ids
                and 'bridge_uid' in ids and worker.proxy_profiles_ready(ids)):
            return dict(lane=name, version=VERSION, phase='completed', fresh=True)
        if values['BOB_WORKER_ID'] != ids['lane_id']:
            raise ValueError('worker mapping does not match lane configuration')
    return state or dict(lane=name, version=VERSION, phase='quiesce', ownership={}, helpers={})


def block(name):
    private_directory(STATE)
    private_directory(STATE / name)
    private_replace(STATE / name / 'pending', 'execution disabled; migration v1 required\n', 0, 0)
    identity_path = worker.IDENTITIES / (name + '.json')
    if identity_path.exists():
        mapping = json.loads(identity_path.read_text())
        if mapping.get('phase') == 'ready' and (worker.CONFIG / (mapping['lane_id'] + '.json')).exists():
            worker.set_admission(mapping, False)
    # Persistent boot inhibition also works with the pre-boundary unit templates.
    for unit in lane.units(name):
        folder = SYSTEMD / (unit + '.d')
        folder.mkdir(parents=True, exist_ok=True)
        private_replace(folder / 'migration.conf',
                        f'[Unit]\nConditionPathExists=!{STATE / name / "pending"}\n', 0, 0)


def worker_units(ids):
    key = ids['lane_id']
    units = [f'bob-worker@{key}.socket', f'bob-worker-auth@{key}.socket', f'bob-worker-journal@{key}.socket',
            f'bob-worker@{key}.service', f'bob-worker-auth@{key}.service', f'bob-worker-journal@{key}.service',
            f'bob-worker-policy@{key}.service', f'bob-claude-turn@{key}.socket', f'bob-claude-turn@{key}.service']
    if 'egress_uid' in ids:
        units.append(f'bob-egress-proxy@{key}.service')
    return units


def stop_workers(ids):
    systemctl('stop', *worker_units(ids))


def kill_uid(uid):
    # All old shared-bob processes must stop, including detached descendants and
    # processes in other legacy lanes. No path is mutated while an old owner runs.
    result = subprocess.run(['pkill', '-KILL', '-u', str(uid)], capture_output=True)
    if result.returncode not in (0, 1):
        raise ValueError('could not terminate old-owner processes')
    deadline = time.monotonic() + 10
    while subprocess.run(['pgrep', '-u', str(uid)], capture_output=True).returncode != 1:
        if time.monotonic() >= deadline:
            raise ValueError('old-owner processes remain; migration stopped')
        time.sleep(.05)


def install_marker():
    with directory(BOUNDARY.parent, create=True) as fd:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o755)
    private_replace(BOUNDARY, 'worker\nisolation-version=1\n', 0, 0)
    BOUNDARY.chmod(0o444)


def install_units():
    # Shared by firstboot's opt-in hook and future in-place upgrades. Keep
    # operator drop-ins; replace only the packaged unit templates themselves.
    for path in (SOURCE / 'deploy/runtime/systemd').glob('bob-*'):
        if path.suffix in {'.service', '.socket', '.timer'}:
            target = SYSTEMD / path.name
            private_replace(target, path.read_text(), 0, 0)
            target.chmod(0o644)
    helper = Path('/usr/local/bin/bob-settings')
    private_replace(helper, (SOURCE / 'deploy/runtime/bin/bob-settings').read_text(), 0, 0)
    helper.chmod(0o755)
    for username in ('bob-embedder', 'bob-qdrant'):
        try:
            pwd.getpwnam(username)
        except KeyError:
            subprocess.run(['useradd', '--system', '--user-group', '--no-create-home',
                            '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', username], check=True)


def quiesce(name, root, state, persist):
    # Shared-UID lanes cannot safely remain live during an ownership transfer.
    # Their persistent pending gate remains until individually migrated.
    affected = {name}
    for other, envpath in lane.lanes():
        env = read_env(envpath)
        if not env.get('BOB_WORKER_ID') or pending(other):
            affected.add(other)
    for other in sorted(affected):
        block(other)
    install_marker()  # Monotonic: neither failure nor rollback removes this.
    systemctl('daemon-reload')
    systemctl('stop', 'bob-token-renew.timer', 'bob-token-renew.service')
    for other in sorted(affected):
        systemctl('stop', *lane.units(other))
    systemctl('stop', 'bob-qdrant.service', 'bob-embedder.service')
    for account_name in ('bob',):
        kill_uid(pwd.getpwnam(account_name).pw_uid)
    identity_path = worker.IDENTITIES / (name + '.json')
    if identity_path.exists():
        mapping = json.loads(identity_path.read_text())
        if mapping.get('phase') == 'allocating':
            # An interrupted UID upgrade cannot pass load_identity yet. Stop
            # known old identities before controls resumes account allocation.
            ids = mapping
            for prefix, role in (('s','service'),('w','worker'),('g','gateway'),('b','bridge'),('e','egress')):
                if role+'_uid' in ids:
                    try:
                        account = pwd.getpwuid(ids[role+'_uid'])
                    except KeyError:
                        continue
                    if account.pw_name != f'bob-{prefix}-{ids["lane_id"]}':
                        raise ValueError('reserved lane identity drift during migration')
        else:
            ids = worker.load_identity(name)
        stop_workers(ids)
        for role in ('worker', 'service', 'gateway', 'bridge', 'egress'):
            if role + '_uid' in ids:
                kill_uid(ids[role + '_uid'])
    # Pre-boundary images owned BOTH shared ancestors as bob:0700. Move them
    # only after every old-owner process has stopped; never put the journal
    # beneath that previously writable ancestor.
    for target in (DATA, lane.FAMILY):
        with directory(target) as fd:
            originals = state.setdefault('shared_ancestors', {})
            if str(target) not in originals:
                originals[str(target)] = describe(os.fstat(fd))
                persist()
            apply(fd, 0, 0, 0o711)
    # Freeze the root-owned lane ancestor, saving its ORIGINAL metadata first.
    with directory(root.parent) as fd:
        if 'ancestor' not in state:
            state['ancestor'] = describe(os.fstat(fd))
            persist()
        apply(fd, 0, 0, 0o700)
    install_units()
    systemctl('daemon-reload')


def controls(name, root, state, persist):
    ids = worker.identity(name)
    # Erase supplementary memberships left by earlier provisioning; service gets
    # only its Codex-read group. The worker never receives service/bob groups.
    subprocess.run(['usermod', '-G', '', ids['worker_user']], check=True)
    subprocess.run(['usermod', '-G', ids['worker_user'], ids['service_user']], check=True)
    for role in ('gateway', 'bridge'):
        subprocess.run(['usermod', '-G', '', ids[role + '_user']], check=True)
    state['identity'] = ids
    persist()
    if not state['ownership'].get('inventory_complete'):
        return  # Inventory original control-file metadata before replacing it.
    disable_id(root, ids)


def disable_id(root, ids):
    # Do not expose a functioning worker ID through core until verification.
    values = read_env(root / 'lane.env')
    values.update(BOB_WORKER_ID='pending-' + ids['lane_id'],
                  BOB_WORKER_UID=str(ids['worker_uid']), BOB_ISOLATION_VERSION=str(VERSION))
    worker.write_lane_env(root, values)


def ownership(name, root, state, persist):
    ids = worker.load_identity(name)
    journal = STATE / name / 'link-breaks'
    private_directory(journal)
    manifest = state['ownership']
    if not manifest.get('applied'):
        # Each record is atomically published/fsynced independently of the
        # potentially large inventory. A crash merges these receipts on replay.
        records = manifest.setdefault('link_breaks', {})
        for entry in sorted(journal.glob('*.json')):
            receipt = json.loads(entry.read_text())
            key = receipt['path']
            if (entry.stem != hashlib.sha256(key.encode()).hexdigest()
                    or key not in manifest.get('entries', {})):
                raise ValueError('invalid link-break journal; operator inspection required')
            records[key] = receipt['record']
    def checkpoint(key, record):
        save(journal / (hashlib.sha256(key.encode()).hexdigest() + '.json'),
             dict(path=key, record=record))
    transfer(root, ids, manifest, persist, checkpoint=checkpoint)
    disable_id(root, ids)
    # Migrate the two shared helpers out of the old execution identity too.
    systemctl('stop', 'bob-qdrant.service', 'bob-embedder.service')
    for leaf, username in (('models', 'bob-embedder'), ('qdrant', 'bob-qdrant'), ('secrets', 'root')):
        account = pwd.getpwnam(username)
        target = DATA / leaf
        if target.exists():
            helper_ids = {role + suffix: value for role in ('worker', 'service')
                          for suffix, value in (('_uid', account.pw_uid), ('_gid', account.pw_gid))}
            transfer(target, helper_ids, state['helpers'].setdefault(leaf, {}), persist, worker_trees=False)
    worker.provision(name, root, activate=False, admit=False)
    from common import ensure_codex_home
    ensure_codex_home(root, allow_pending=True)
    # Fresh and migrated layouts share exactly the same profile/ownership writers.
    worker.service_environments(root)
    systemctl('start', 'bob-qdrant.service', 'bob-embedder.service')


def credentials(name, root, state, persist):
    # Persist NEW independent secrets before publication; replay never generates
    # different keys after an interrupted env/token write. Old values are absent.
    if 'rotation' not in state:
        state['rotation'] = dict(jwt=secrets.token_urlsafe(48), vouch=secrets.token_urlsafe(48),
                                 keys_published=False, portal_reissued=False)
        persist()
    rotation = state['rotation']
    values = read_env(root / 'lane.env')
    values.update(JWT_SECRET=rotation['jwt'], BOBCLAW_SECRET=rotation['vouch'])
    worker.write_lane_env(root, values)
    worker.service_environments(root)
    rotation['keys_published'] = True
    persist()
    if not rotation['portal_reissued']:
        lane.rotate_token(name)
        rotation['portal_reissued'] = True
        persist()


# Executed as the actual service identity with a clean environment. Uses the
# shipped socket and protocol, independently of the deliberately disabled core ID.
SELF_CHECK = '''
import base64,json,os,socket,struct,sys,time,uuid
c=json.load(sys.stdin)
assert os.getuid()==c['service_uid']
assert os.stat(c['canary']).st_uid==c['service_uid']
with open(c['canary']) as f: assert f.read(128)
for kind in ('socket','auth_socket'):
 with socket.socket(socket.AF_UNIX) as s:
  s.settimeout(40); s.connect(c[kind])
  assert struct.unpack('3i',s.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]==0
  if kind=='auth_socket':
   continue
  code="import os; p="+repr(c['canary'])+"; denied=False\\ntry: open(p).read()\\nexcept (PermissionError,FileNotFoundError): denied=True\\ncaps=[x for x in open('/proc/self/status') if x.startswith('CapEff:')][0].split()[1]\\nsafe=set(os.getgroups())<="+repr({c['worker_gid']})+" and int(caps,16)==0\\nprint(str(os.getuid())+':'+str(denied and safe))"
  p=dict(version=1,job_id=uuid.uuid4().hex,profile='python',workspace='scratch',cwd='.',args=['-c',code],input='',timeout=20,output_limit=4096,expires=time.time()+30)
  sys.path.insert(0,'/opt/bob/bobclaw-core')
  from core.worker.client import Transport
  from core.worker.contract import request_digest
  import threading
  transport=Transport(c); hello=transport.hello()
  control=dict(version=2,op='idle_barrier',lane_generation=hello['lane_generation']+1,controller_session=uuid.uuid4().hex)
  assert transport.control(control)['cleanup']=='idle_proved'
  p.pop('job_id'); p.pop('expires')
  p.update(version=2,op='submit',execution_id=uuid.uuid4().hex,logical_job_id=uuid.uuid4().hex,
   lane_generation=control['lane_generation'],controller_session=control['controller_session'],
   profile_revision=hello['profiles']['python'],not_after=time.time()+20,submit_expires_at=time.time()+10)
  p['request_digest']=request_digest(p)
  result=transport.submit(p,threading.Event())
  assert result['outcome']=='succeeded' and result['cleanup']=='idle_proved'
  out=base64.b64decode(result['stdout_b64'])
  assert out.strip()==(str(c['worker_uid'])+':True').encode()

print('verified')
'''


def wait_ready(values, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        ready = True
        for key in ('BOBCLAW_CORE_PORT', 'BOBCLAW_GATEWAY_PORT', 'BOB_BRIDGE_PORT'):
            with socket.socket() as connection:
                connection.settimeout(.2)
                ready &= connection.connect_ex(('127.0.0.1', int(values[key]))) == 0
        if ready:
            return
        if time.monotonic() >= deadline:
            raise ValueError('lane services not ready; execution remains disabled')
        time.sleep(.1)


def verify(name, root, state, persist):
    ids = worker.load_identity(name)
    if BOUNDARY.is_symlink() or BOUNDARY.stat().st_uid != 0 or BOUNDARY.stat().st_mode & 0o222:
        raise ValueError('execution marker is not root-controlled')
    # Core reads a nonfunctional ID, so starting it here never opens admission.
    for unit in lane.units(name):
        (SYSTEMD / (unit + '.d') / 'migration.conf').unlink(missing_ok=True)
    systemctl('daemon-reload')
    systemctl('start', *lane.units(name))
    systemctl('enable', '--now', f'bob-worker-journal@{ids["lane_id"]}.socket', f'bob-worker@{ids["lane_id"]}.socket',
              f'bob-worker-auth@{ids["lane_id"]}.socket', f'bob-claude-turn@{ids["lane_id"]}.socket',
              f'bob-egress-proxy@{ids["lane_id"]}.service',
              f'bob-settings-control@{ids["lane_id"]}.socket', f'bob-settings-receipt@{ids["lane_id"]}.socket')
    values = read_env(root / 'lane.env')
    wait_ready(values)
    for unit in [*lane.units(name), f'bob-egress-proxy@{ids["lane_id"]}.service']:
        result = subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', unit],
                                text=True, capture_output=True, check=True)
        pid = int(result.stdout.strip())
        principal = ('gateway' if unit.startswith('bob-gateway@') else 'bridge' if unit.startswith('bob-bridge@')
                     else 'egress' if unit.startswith('bob-egress-proxy@') else 'service')
        if not pid or Path(f'/proc/{pid}').stat().st_uid != ids[principal + '_uid']:
            raise ValueError('lane service did not start under its persistent identity')
    cfg = json.loads((worker.CONFIG / (ids['lane_id'] + '.json')).read_text())
    # The public config intentionally points core at nonexistent addresses until
    # the durable completion receipt. Probe the actual root-created listeners.
    cfg['socket'] = f'/run/bob-workers/{ids["lane_id"]}/exec.sock'
    cfg['auth_socket'] = f'/run/bob-workers/{ids["lane_id"]}/auth.sock'
    canary = root / 'migration-secret-canary'
    private_replace(canary, secrets.token_hex(32) + '\n', ids['service_uid'], ids['service_gid'])
    try:
        cfg['canary'] = str(canary)
        result = subprocess.run([str(SOURCE / '.venv/bin/python'), '-I', '-c', SELF_CHECK],
            input=json.dumps(cfg), text=True, capture_output=True, timeout=60, cwd='/',
            env={'PATH': '/usr/local/bin:/usr/bin:/bin'}, user=ids['service_uid'],
            group=ids['service_gid'], extra_groups=[], check=True)
        if result.stdout.strip() != 'verified':
            raise ValueError('migration worker self-check failed')
        subprocess.run(['nft', 'list', 'table', 'inet', 'bob_w_' + ids['lane_id']],
                       check=True, stdout=subprocess.DEVNULL)
        state['verification'] = dict(service_uid=ids['service_uid'], worker_uid=ids['worker_uid'], egress_uid=ids['egress_uid'],
                                     socket_peer_uid=0, canary_denied=True, policy_loaded=True)
        persist()
    finally:
        canary.unlink(missing_ok=True)


def publish(name, root, state, persist):
    if not state.get('verification') or not state.get('rotation', {}).get('portal_reissued'):
        raise ValueError('cannot enable execution without verification and credential remediation')
    ids = worker.load_identity(name)
    values = read_env(root / 'lane.env')
    values.update(BOB_WORKER_ID=ids['lane_id'], BOB_WORKER_UID=str(ids['worker_uid']),
                  BOB_ISOLATION_VERSION=str(VERSION))
    # Verification is durably recorded before the real ID can reach core.
    worker.write_lane_env(root, values)
    worker.service_environments(root)
    systemctl('restart', *lane.units(name))
    # Report completion only once the restarted services accept connections.
    wait_ready(values)
    state['phase'] = 'completed'
    state['activation_complete'] = False
    state.pop('failure', None)
    state.pop('suspended', None)
    persist()
    (STATE / name / 'pending').unlink(missing_ok=True)
    with directory(STATE / name) as fd:
        os.fsync(fd)
    worker.set_admission(ids, True)
    state['activation_complete'] = True
    state['settings_version'] = 1
    # No replay past publication needs signing material; keep only receipts.
    state['rotation'].pop('jwt', None)
    state['rotation'].pop('vouch', None)
    persist()
    systemctl('start', 'bob-token-renew.timer')


def suspend(name):
    """Service recovery/rollback on retained isolated identities, execution off."""
    state = state_for(name)
    if state is None or not state.get('identity') or not state.get('verification'):
        raise ValueError('service recovery requires a verified isolated-lane journal')
    root = lane.FAMILY / name / 'bob-memory'
    block(name)
    install_marker()
    stop_workers(state['identity'])
    disable_id(root, state['identity'])
    worker.service_environments(root)
    state.update(phase='verify', activation_complete=False, suspended=True)
    save(STATE / name / 'state.json', state)
    for unit in lane.units(name):
        (SYSTEMD / (unit + '.d') / 'migration.conf').unlink(missing_ok=True)
    systemctl('daemon-reload')
    systemctl('restart', *lane.units(name))


def restore_migrated(exclude):
    """Restart lanes that already completed migration.

    Migrating another lane stops the shared Qdrant/embedder helpers, and lane
    cores Require bob-qdrant, so systemd stops completed lanes with them and
    never restarts them. Starting an active unit is a no-op.
    """
    for other, _ in lane.lanes():
        if other == exclude or pending(other):
            continue
        try:
            state = state_for(other)
        except ValueError:
            continue
        if state and state.get('phase') == 'completed' and state.get('activation_complete'):
            systemctl('start', 'bob-qdrant.service', 'bob-embedder.service', *lane.units(other))
            wait_ready(read_env(lane.FAMILY / other / 'bob-memory' / 'lane.env'))


def migrate(name, dry_run=False):
    state = plan(name)
    if dry_run or state['phase'] == 'completed':
        return {k: state[k] for k in ('lane', 'version', 'phase')}
    root = lane.FAMILY / name / 'bob-memory'
    private_directory(STATE)
    private_directory(STATE / name)
    journal = STATE / name / 'state.json'
    persist = lambda: save(journal, state)
    block(name)
    install_marker()
    persist()
    try:
        # Re-quiesce on EVERY replay (including publish). The phase is the first
        # unfinished operation, not a claim that the preceding process survived.
        quiesce(name, root, state, persist)
        if state['phase'] != 'quiesce':
            controls(name, root, state, persist)
        start = PHASES.index(state['phase'])
        if state['phase'] == 'publish':
            # Reboot/kill between verification and publication invalidates live
            # process/socket evidence. Re-establish it before reopening.
            worker.provision(name, root, activate=False, admit=False)
            worker.service_environments(root)
            verify(name, root, state, persist)
        operations = dict(identities=controls, ownership=ownership, credentials=credentials,
                          verify=verify, publish=publish)
        for phase in PHASES[start:-1]:
            state['phase'] = phase
            persist()
            print(json.dumps(dict(lane=name, version=VERSION, phase=phase)), flush=True)
            if phase == 'quiesce':
                continue
            # A replay after ownership must restore profiles/ancestors but never
            # rescan an already live worker-controlled tree.
            if phase == 'verify':
                worker.provision(name, root, activate=False, admit=False)
                worker.service_environments(root)
            operations[phase](name, root, state, persist)
        restore_migrated(name)
        return dict(lane=name, version=VERSION, phase=state['phase'])
    except BaseException as exc:
        state['failure'] = type(exc).__name__  # Never store secret-bearing error text.
        persist()
        try:
            block(name)
            install_marker()
            values = read_env(root / 'lane.env')
            values['BOB_WORKER_ID'] = 'pending-' + state.get('identity', {}).get('lane_id', name)
            if state.get('identity'):
                worker.write_lane_env(root, values)
                worker.service_environments(root)
                stop_workers(state['identity'])
            systemctl('stop', *lane.units(name))
        except Exception:
            pass  # Persistent pending gate + marker survive cleanup failure.
        try:
            restore_migrated(name)
        except Exception:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('migrate-workers')
    command.add_argument('--lane', type=lane.validate_name)
    command.add_argument('--dry-run', action='store_true')
    refresh = sub.add_parser('refresh-env')
    refresh.add_argument('--lane', type=lane.validate_name, required=True)
    disable = sub.add_parser('suspend-workers')
    disable.add_argument('--lane', type=lane.validate_name, required=True)
    args = parser.parse_args()
    require_root()
    os.umask(0o077)
    with open(lane.LOCK_PATH, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == 'suspend-workers':
            suspend(args.lane)
            print(json.dumps(dict(lane=args.lane, version=VERSION, execution='disabled')))
            return 0
        if args.command == 'refresh-env':
            writer_guard(args.lane)
            worker.service_environments(lane.FAMILY / args.lane / 'bob-memory')
            return 0
        names = [args.lane] if args.lane else [n for n, p in lane.lanes() if not (p.parent / '.disabled').exists()]
        if not args.dry_run:
            print(NOTICE, file=sys.stderr, flush=True)
        # Validate every selected lane before making the first change.
        for name in names:
            plan(name)
        for name in names:
            print(json.dumps(migrate(name, args.dry_run)), flush=True)
    return 0
