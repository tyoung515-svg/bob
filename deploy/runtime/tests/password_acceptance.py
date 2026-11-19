"""3.3 acceptance on two existing harness-created disposable Linux lanes.

Install this script in /var/tmp/bob-s3f (0755 parents, script 0644). No providers
are called. Real Postgres run accounting uses a random schema that is dropped.
Secrets travel only in memory/stdin/HTTP; reports contain fixed probe names.
Journal-boundary crashes are covered by test_password_journal.py, not a runtime
fault-injection API. This harness never accepts an unmarked production lane.
"""
import argparse
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid

sys.path[:0] = ['/opt/bob/bobclaw-core', '/opt/bob/deploy/runtime/lib']
FAMILY = Path('/var/lib/bob/family')


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60)


def http(port, path, body=None, headers=None):
    request = urllib.request.Request(f'http://127.0.0.1:{port}' + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, json.loads(response.read(65537))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read(65537))


def pids(names):
    return {(name, role): command('systemctl', 'show', '-p', 'MainPID', '--value',
                                f'bob-{role}@{name}.service').stdout.strip()
            for name in names for role in ('core', 'gateway', 'bridge')}


def next_code(seed, previous=None):
    import pyotp
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        step = int(time.time()/30)
        if step != previous:
            return pyotp.TOTP(seed).at(step*30), step
        time.sleep(.2)
    raise RuntimeError('factor_window_timeout')


def wait_gateway(port):
    deadline = time.monotonic()+45
    while time.monotonic()<deadline:
        try:
            if http(port, '/auth/generation')[0] == 200:
                return
        except (OSError, ValueError):
            pass
        time.sleep(.2)
    raise RuntimeError('gateway_not_ready')


@contextmanager
def broker(name):
    process = subprocess.Popen(['runuser', '-u', 'bobapp', '--', 'bob-broker', name],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    replies = queue.Queue()
    def read():
        for line in process.stdout:
            try:
                replies.put(json.loads(line))
            except ValueError:
                pass
    threading.Thread(target=read, daemon=True).start()
    serial = 0
    def call(operation, args):
        nonlocal serial
        serial += 1
        process.stdin.write(json.dumps({'id': serial, 'op': operation, 'args': args}) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            item = replies.get(timeout=max(.1, deadline-time.monotonic()))
            if item.get('id') == serial:
                return item
        raise RuntimeError('broker_timeout')
    try:
        yield call
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        process.stdout.close()


async def run(names, record):
    from common import read_env
    from worker import load_identity
    from settings import provision
    # Preflight BOTH disposable markers and identities before changing either.
    ids = {}
    for name in names:
        marker = FAMILY / name / 'bob-memory/acceptance-disposable.json'
        if json.loads(marker.read_text()).get('purpose') != 's2d-disposable':
            raise ValueError('disposable_marker_required')
        ids[name] = load_identity(name)
        if not all(k in ids[name] for k in ('gateway_uid', 'bridge_uid')):
            raise ValueError('run_settings_identity_acceptance_first')
    for name in names:
        provision(ids[name], FAMILY / name / 'bob-memory')
        # Installation setup is outside the observed password-change interval.
        command('systemctl', 'restart', f'bob-gateway@{name}.service')
        wait_gateway(int(read_env(FAMILY/name/'bob-memory/lane.env')['BOBCLAW_GATEWAY_PORT']))
    target = names[0]
    root = FAMILY / target / 'bob-memory'
    lane = ids[target]
    env = read_env(Path('/etc/bob/service-env') / (lane['lane_id']+'-gateway.env'))
    current = read_env(Path('/var/lib/bob-settings') / lane['lane_id'] / 'credentials/current.env')
    old, new = current['BOBCLAW_PASSWORD'], 'S3F-' + secrets.token_urlsafe(32)
    epoch = int(current['BOB_CREDENTIAL_EPOCH'])
    port, seed = int(env['BOBCLAW_GATEWAY_PORT']), env['TOTP_SECRET']
    token = (Path('/var/lib/bob-portal') / (target+'.token')).read_text().strip()
    link = uuid.uuid4().hex
    portal = {'Authorization': 'Bearer '+token, 'X-Bob-Broker-Link': link}
    code, step = next_code(seed)
    status, human = http(port, '/auth/login', {'password': old, 'totp_code': code})
    record('initial-login', status == 200)
    status, challenge = http(port, '/auth/owner/begin',
        {'operation': 'secrets.set', 'resource': {'provider': 'deepseek'}}, portal)
    record('owner-challenge', status == 200)
    code, step = next_code(seed, step)
    status, owner = http(port, '/auth/owner/confirm',
        {'challenge_id': challenge['challenge_id'], 'password': old, 'totp': code}, portal)
    record('owner-session', status == 200)
    import jwt
    portal_id = jwt.decode(token, options={'verify_signature': False})['jti']
    owner_headers = {'Authorization': 'Bearer '+owner['owner_bearer'],
                     'X-Bob-Broker-Link': link, 'X-Bob-Portal-Credential': portal_id}
    # A separate temporary schema uses the production Store, untouched by the
    # running executor. No fake provider can ever be dispatched by that core.
    os.environ.update(read_env(root/'core.env'))
    os.environ.update(BOB_EXECUTION_BOUNDARY='worker', BOB_SETTINGS_ID=lane['lane_id'])
    import asyncpg
    from core.execution.store import Store
    from core.execution.models import Authority, LOOP_TOPOLOGY, ExecutionError, canonical
    from core.execution.api import lane_id
    dsn = os.environ['POSTGRES_URL']
    if urlparse(dsn).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('local_postgres_required')
    schema = 's3f_'+uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute('CREATE SCHEMA '+schema)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, server_settings={'search_path':schema})
        await pool.execute('CREATE TABLE projects(id UUID PRIMARY KEY,user_id TEXT,is_archived BOOLEAN DEFAULT FALSE);'
                           'CREATE TABLE conversations(id UUID PRIMARY KEY,user_id TEXT,project_id UUID)')
        store = Store(pool, lane_id())
        await store.initialize()
        authority = Authority(lane_id(), target, target, {}, 'human', credential_epoch=epoch)
        team = dict(schema_version=1, id='password-probe', topology=LOOP_TOPOLOGY,
                    roles={r:'fake' for r in ('manager','worker','critic')})
        await store.save_team(authority, team, None, 'team', {'definition':team})
        team = await store.team(authority, team['id'])
        binding = dict(backend='__never_dispatch__', model='fake', enabled=True, exact_calls=True)
        await store.start(authority, dict(team=team['id'], team_revision=1, task='S3F synthetic evidence',
            request_key='run', budget=dict(start_calls=10, wall_mode='none', wall_seconds=None)),
            dict(team=team, models={'fake':binding}, review_independence='same_model'))
        row = await store.claim('password-acceptance')
        await pool.execute("INSERT INTO agent_run_steps(lane,owner,run_id,ordinal,role,spec) VALUES ($1,$2,$3,0,'worker',$4::jsonb)",
                           row['lane'], row['owner'], row['id'], canonical({'id':'one'}))
        attempt = await store.reserve_attempt(row, 0, binding, 'S3F supplied evidence')
        record('durable-old-epoch', row['run_authority']['credential_epoch'] == epoch)
        before = pids(names)
        record('all-services-running', all(int(pid)>0 for pid in before.values()))
        baseline = {str(p):p.read_bytes() for name in names for p in
            [FAMILY/name/'bob-memory/lane.env', FAMILY/name/'bob-memory/core.env',
             Path('/etc/bob/service-env')/(ids[name]['lane_id']+'-gateway.env')]}
        code, step = next_code(seed, step)
        key = uuid.uuid4().hex
        with broker(target) as call:
            # ONE credential-bearing frame. Every subsequent frame is status only.
            reply = call('auth.change_password', dict(contract_version=2, current=old, new=new, totp=code, request_key=key))
            record('change-reply-safe', (reply.get('ok') is True and reply.get('value') in
                   [dict(changed=True,restarted=True),dict(changed=True,restarted=False)])
                   or (reply.get('ok') is False and reply.get('error',{}).get('code') == 'password_outcome_unknown'))
            deadline = time.monotonic()+90
            receipt = {}
            while time.monotonic()<deadline:
                receipt = call('settings.status', {'request_key':key})
                if receipt.get('ok') and receipt['value']['phase'] in {'ready','failed'}:
                    break
                time.sleep(.2)
            record('ready-receipt', receipt.get('ok') is True and receipt['value'].get('result') == dict(changed=True,restarted=True))
            after = pids(names)
            record('only-target-gateway-restarted', before[(target,'gateway')] != after[(target,'gateway')]
                   and all(before[k] == after[k] for k in before if k != (target,'gateway')))
            # Test the actual broker's Unix receipt path with gateway stopped.
            command('systemctl','stop',f'bob-gateway@{target}.service')
            try:
                down = call('settings.status', {'request_key':key})
                record('broker-receipt-gateway-down', down == {**receipt, 'id': down['id']})
            finally:
                command('systemctl','start',f'bob-gateway@{target}.service')
                wait_gateway(port)
        record('base-envs-unchanged', all(Path(p).read_bytes() == value for p,value in baseline.items()))
        probe = command('runuser','-u',lane['service_user'],'--','/opt/bob/.venv/bin/python','-I','-c',
            "import os,sys; sys.path.insert(0,'/opt/bob/bobclaw-core'); os.environ['BOB_SETTINGS_ID']=sys.argv[1]; "
            "from core.credential_state import active; print(active()['epoch'])", lane['lane_id'])
        record('core-uid-reads-new-epoch', probe.stdout.strip() == str(epoch+1))
        protected = Path('/var/lib/bob-settings')/lane['lane_id']/'credentials/current.env'
        read_probe = "import sys\ntry:\n open(sys.argv[1]).close(); print('read')\nexcept PermissionError:\n print('denied')"
        for role in ('service','gateway','bridge','worker','bobapp'):
            user = role if role == 'bobapp' else lane[role+'_user']
            probe = command('runuser','-u',user,'--','/opt/bob/.venv/bin/python','-I','-c',read_probe,str(protected))
            record('credential-file-denied-'+role, probe.stdout.strip() == 'denied')
        record('old-human-invalid', http(port,'/auth/status',headers={'Authorization':'Bearer '+human['access_token']})[0] == 401)
        record('old-refresh-invalid', http(port,'/auth/refresh',{'refresh_token':human['refresh_token']})[0] == 401)
        record('old-owner-invalid', http(port,'/auth/owner/status',headers=owner_headers)[0] == 403)
        try:
            await store.charge_attempt(row, attempt)
        except ExecutionError as exc:
            record('old-run-dispatch-fenced', exc.code == 'credential_epoch_changed')
        else:
            record('old-run-dispatch-fenced', False)
        record('no-old-run-charge', await pool.fetchval('SELECT consumed FROM agent_budget_accounts WHERE run_id=$1',row['id']) == 0)
        code, step = next_code(seed, step)
        record('old-password-fails', http(port,'/auth/login',{'password':old,'totp_code':code})[0] == 401)
        time.sleep(1.2)
        record('new-password-works', http(port,'/auth/login',{'password':new,'totp_code':code})[0] == 200)
        state = Path('/var/lib/bob-settings')/lane['lane_id']
        record('journal-no-credentials', all(old not in p.read_text() and new not in p.read_text() for p in state.glob('*.json')))
        logs = command('journalctl','--no-pager','--since','-10min','-u',f'bob-gateway@{target}.service',
                       '-u',f'bob-settings-control@{lane["lane_id"]}.service').stdout
        record('logs-no-credentials', old not in logs and new not in logs)
    finally:
        if pool:
            await pool.close()
        await admin.execute('DROP SCHEMA IF EXISTS '+schema+' CASCADE')
        await admin.close()


def main():
    from lane import validate_name
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane-a', required=True, type=validate_name)
    parser.add_argument('--lane-b', required=True, type=validate_name)
    args = parser.parse_args()
    if os.geteuid() != 0 or args.lane_a == args.lane_b:
        parser.error('root and two distinct disposable lanes required')
    failures = []
    def record(name, passed):
        print(json.dumps({'probe':'s3f.'+name,'passed':bool(passed)}), flush=True)
        if not passed:
            failures.append(name)
    try:
        asyncio.run(run([args.lane_a,args.lane_b], record))
    except Exception as exc:
        # Never reflect parser/HTTP/subprocess exception messages or bodies.
        record('harness-'+type(exc).__name__, False)
    record('overall', not failures)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
