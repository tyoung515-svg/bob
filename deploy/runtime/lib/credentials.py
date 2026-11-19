"""Password generation journal. No caller-selected paths, units or environment.

active.json is the sole commit point. current.env is staged first and repaired
from the immutable active generation after a crash. A gateway loaded from an
uncommitted env fails closed against active.json. No credential is replayed.
"""
import json
import os
import re
import subprocess
import time
import urllib.request

from core.settings_contract import SettingsError, password_result


def checkpoint(name):
    """Fault-injection seam for disposable journal-boundary tests."""


def environment(generation):
    from common import render_env
    values = {'BOBCLAW_PASSWORD': generation['password'],
              'BOB_CREDENTIAL_EPOCH': str(generation['epoch'])}
    if 'totp_secret' in generation:
        values['TOTP_SECRET'] = generation['totp_secret']
    if 'totp_floor' in generation:
        values['BOB_TOTP_FLOOR'] = str(generation['totp_floor'])
    return render_env(values)


def ready(cfg, epoch):
    try:
        if not re.fullmatch('[a-z][a-z0-9-]{0,31}', cfg['lane']):
            return False
        # Another service could bind this unprivileged port while gateway is
        # down. Verify the listener belongs to this unit's mapped MainPID.
        from pathlib import Path
        unit = f'bob-gateway@{cfg["lane"]}.service'
        process = subprocess.run(['/usr/bin/systemctl', 'show', '-p', 'MainPID', '--value', unit],
                                 check=True, timeout=2, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        pid = int(process.stdout.strip())
        if pid <= 0 or Path(f'/proc/{pid}').stat().st_uid != cfg['gateway_uid']:
            return False
        sockets = set()
        for item in Path(f'/proc/{pid}/fd').iterdir():
            try:
                sockets.add(os.readlink(item))
            except FileNotFoundError:
                pass
        address = f'0100007F:{int(cfg["gateway_port"]):04X}'
        owned = any(row[1] == address and row[3] == '0A'
                    and int(row[7]) == cfg['gateway_uid'] and f'socket:[{row[9]}]' in sockets
                    for row in (line.split() for line in Path('/proc/net/tcp').read_text().splitlines()[1:]))
        if not owned:
            return False
        # Installed loopback port, never a URL supplied in the request.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{int(cfg["gateway_port"])}/auth/generation', timeout=1) as response:
            value = json.loads(response.read(4097))
        return value == {'ready': True, 'credential_epoch': epoch}
    except Exception:
        return False


def restart(cfg, epoch):
    if ready(cfg, epoch):
        return True
    if not re.fullmatch('[a-z][a-z0-9-]{0,31}', cfg['lane']):
        raise SettingsError('invalid_lane')
    subprocess.run(['/usr/bin/systemctl', 'restart', f'bob-gateway@{cfg["lane"]}.service'],
                   check=True, timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ready(cfg, epoch):
            return True
        time.sleep(.2)
    return False


def recover(cfg, fd, cfd, value, *, resume=False):
    from settings import load, publish, save_receipt
    active = load(cfd, 'active.json')
    if resume:
        generation = load(cfd, active['generation'] + '.json')
        # Only the control service can repair credential files or restart units.
        publish(cfd, 'current.env', environment(generation))
    factors = value.get('operation') == 'factors.commit'
    if value['phase'] in {'ready', 'failed'}:
        # Retry only an unfinished trusted handoff on control startup. Terminal
        # receipts remain stable and receipt queries cannot publish credentials.
        if resume and factors and not value.get('portal_published') and active.get('operation_id') == value['operation_id']:
            from recovery import finish
            try:
                finish(cfg, fd, cfd, value, publish_portal=True)
                save_receipt(fd, value)
            except Exception:
                pass
        return value
    if active.get('operation_id') != value['operation_id']:
        if value['phase'] != 'prepared':
            raise SettingsError('credential_journal_disagreement')
        value.update(phase='failed', result={'changed': False, 'restarted': False})
        save_receipt(fd, value)
        return value
    if value['phase'] == 'prepared':
        value['phase'] = 'committed'
        save_receipt(fd, value)
        checkpoint('committed')
    if value['phase'] == 'committed':
        value['phase'] = 'restart-pending'
        save_receipt(fd, value)
        checkpoint('restart-pending')
    # Receipt queries may observe readiness but can never restart any unit.
    restarted = ready(cfg, active['epoch'])
    if resume and not restarted:
        try:
            restarted = restart(cfg, active['epoch'])
        except Exception:
            restarted = False
        checkpoint('restarted')
    finished = True
    if factors:
        # A publisher error must never strand a committed generation on the old
        # gateway. Restart and verify its loaded epoch before the portal handoff.
        from recovery import finish
        try:
            finish(cfg, fd, cfd, value, publish_portal=resume)
            finished = bool(value.get('portal_published'))
        except Exception:
            finished = False
        if resume:
            # Private evidence; status_result exposes only the safe contract.
            value['gateway_ready_epoch'] = active['epoch'] if restarted else None
            if not finished:
                value['failure'] = 'portal_publication_failed'
    complete = restarted and finished
    if complete or resume:
        value.update(phase='ready' if complete else 'failed',
                     result={'changed': True, 'restarted': complete})
        save_receipt(fd, value)
        checkpoint('terminal')
    return value


def change(cfg, fd, cfd, request, value):
    from settings import load, publish, save_receipt
    active = load(cfd, 'active.json')
    if active['epoch'] != request['expected_epoch']:
        value.update(phase='failed', result={'changed': False, 'restarted': False})
        save_receipt(fd, value)
        return value['result']
    previous = load(cfd, active['generation'] + '.json')
    if request['new'] == previous['password']:
        raise SettingsError('invalid_password')
    generation = {**previous, 'epoch': active['epoch'] + 1, 'password': request['new']}
    checkpoint('prepared')
    publish(cfd, value['operation_id'] + '.json', generation)
    checkpoint('generation')
    publish(cfd, 'current.env', environment(generation))
    checkpoint('env')
    publish(cfd, 'active.json', dict(epoch=generation['epoch'], generation=value['operation_id'],
                                    operation_id=value['operation_id'],
                                    **{k: active[k] for k in ('reset_at', 'revocation_epoch') if k in active}), mode=0o644)
    checkpoint('pointer')
    return password_result(recover(cfg, fd, cfd, value, resume=True)['result'])


def provision(cfg, root):
    from settings import STATE, load, publish
    from core.worker.auth_file import directory
    from common import SYSTEMD, read_env, private_replace, root_directory
    from worker import SERVICE_ENVS
    from pathlib import Path
    path = STATE / cfg['lane_id'] / 'credentials'
    with directory(path, create=True) as fd:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o711)
        try:
            active = load(fd, 'active.json')
        except FileNotFoundError:
            env = read_env(SERVICE_ENVS / (cfg['lane_id'] + '-gateway.env'), safe=True)
            # Quiesced upgrades preserve the prior durable owner epoch.
            import sqlite3
            import hashlib
            epoch = 1
            if Path(env['GATEWAY_SQLITE_PATH']).exists():
                with sqlite3.connect(Path(env['GATEWAY_SQLITE_PATH']).as_uri()+'?mode=ro', uri=True) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='owner_epochs' AND type='table'").fetchone():
                        lane_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
                        row = db.execute('SELECT epoch FROM owner_epochs WHERE lane=? AND owner=?',
                                         (lane_key, env['BOB_DEFAULT_OWNER'])).fetchone()
                        epoch = max(1, row[0]) if row else 1
            generation = dict(epoch=epoch, password=env['BOBCLAW_PASSWORD'],
                              totp_secret=env.get('TOTP_SECRET', ''),
                              recovery_id=None, recovery_hash=None, totp_enrolled=bool(env.get('TOTP_SECRET')),
                              reset_at=None)
            if not generation['totp_enrolled']:
                capability = __import__('secrets').token_urlsafe(32)
                generation['setup_hash'] = hashlib.sha256(capability.encode()).hexdigest()
                # Trusted provisioning handoff only; no gateway/broker read path.
                publish(fd, 'setup.json', {'capability': capability})
            publish(fd, 'initial.json', generation)
            active = dict(epoch=epoch, generation='initial', operation_id=None)
            publish(fd, 'active.json', active, mode=0o644)
        publish(fd, 'current.env', environment(load(fd, active['generation'] + '.json')))
    target = SYSTEMD / f'bob-gateway@{cfg["lane"]}.service.d'
    root_directory(target)
    # Sorts after identity.conf, which replaces the base EnvironmentFile list.
    private_replace(target / 'zz-credentials.conf',
                    f'[Unit]\nRequires=bob-credentials@{cfg["lane_id"]}.service\n'
                    f'After=bob-credentials@{cfg["lane_id"]}.service\n'
                    f'[Service]\nEnvironmentFile={path}/current.env\n', 0, 0)
