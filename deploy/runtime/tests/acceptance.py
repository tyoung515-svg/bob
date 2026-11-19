#!/opt/bob/.venv/bin/python
"""Operator-run tests on TWO DISPOSABLE lanes; never shipped in image.

Run as root in an imported test distro. Emits JSONL, exits nonzero on any failed
probe. No provider calls unless --codex-positive is explicitly supplied.
--embedder is read-only, needs no disposable-lane provisioning, and uses only
--lane-a for the optional model-backed client check.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import threading
import uuid

PYTHON = '/opt/bob/.venv/bin/python'
FAMILY = Path('/var/lib/bob/family')
WORKER_CONFIGS = Path('/etc/bob/workers')
OSRELEASE = Path('/proc/sys/kernel/osrelease')
sys.path.insert(0, '/opt/bob/bobclaw-core')
sys.path.insert(0, '/usr/local/lib/bob_runtime')


def emit(probe, passed, **evidence):
    print(json.dumps(dict(probe=probe, passed=bool(passed), **evidence)), flush=True)
    return bool(passed)


def admin(*args, **kwargs):
    # cwd=/ so runuser'd children never inherit an unreadable root working directory.
    kwargs.setdefault('cwd', '/')
    return subprocess.run(list(args), capture_output=True, text=True,
                          timeout=kwargs.pop('timeout', 120), **kwargs)


def wait_until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)
    return True


def host_listener(value):
    """Parse every endpoint before main can provision either disposable lane."""
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit('tcp://' + value)
        address, port = str(ipaddress.ip_address(parsed.hostname)), parsed.port
        if (not port or not 1 <= port <= 65535 or parsed.username is not None or
                parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('invalid endpoint')
        return address, port
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError('host listener requires a numeric IP and port') from exc


def boot_workers(timeout=60):
    """Observe ALL provisioned lanes before Harness restarts or repairs anything."""
    configs = [(path.stem, json.loads(path.read_text())) for path in sorted(WORKER_CONFIGS.glob('*.json'))]
    if not configs:
        return [('lifecycle.boot-workers', False, {'reason': 'no provisioned lanes'})]
    deadline = time.monotonic() + timeout
    while True:
        rows = []
        for lane_id, cfg in configs:
            units = [f'bob-worker-policy@{lane_id}.service', f'bob-worker@{lane_id}.socket',
                     f'bob-worker-auth@{lane_id}.socket', f'bob-worker-journal@{lane_id}.socket']
            active = {unit: admin('systemctl', 'is-active', '--quiet', unit).returncode == 0 for unit in units}
            policy = Path(f'/run/bob-workers/{lane_id}/policy.json').is_file()
            rows.append(('lifecycle.boot-workers.' + cfg['lane'], all(active.values()) and policy,
                         {'lane_id': lane_id, 'active': active, 'policy_receipt': policy}))
        if all(passed for _, passed, _ in rows) or time.monotonic() >= deadline:
            return rows
        time.sleep(.2)


def child(lane, mode):
    """Invoked with runuser as the real service identity, using the real client."""
    from common import read_env
    from core.worker import client
    root = Path('/var/lib/bob/family') / lane / 'bob-memory'
    os.environ.clear()
    os.environ.update({'PATH': '/usr/local/bin:/usr/bin:/bin', 'BOB_LANE_ROOT': str(root)})
    # Same service-readable storage configuration as core; never export to jobs.
    os.environ.update(read_env(root / 'core.env'))
    os.environ['BOB_SERVICE_ROLE'] = 'core'
    payload = json.load(sys.stdin)
    if mode == 'checked-edits':
        print(json.dumps(asyncio.run(checked_edits_fixture(client.configuration(), payload))))
        return
    if mode == 'execution-mode':
        from core.worker import mode as execution_mode
        marker = Path('/etc/bob/execution-boundary')
        checks = {'initial': execution_mode() == 'worker'}
        for value in ('legacy', '', 'off', '0'):
            os.environ['BOB_EXECUTION_BOUNDARY'] = value
            checks['env-' + repr(value)] = execution_mode() == 'worker'
        os.environ.pop('BOB_EXECUTION_BOUNDARY', None)
        checks['env-unset'] = execution_mode() == 'worker'
        # Run as the actual service UID. All three mutation attempts must fail.
        for label, operation in (
            ('write', lambda: marker.write_text('legacy\n')),
            ('rename', lambda: marker.rename(marker.with_name('execution-boundary.disabled'))),
            ('unlink', marker.unlink),
        ):
            try:
                operation()
            except PermissionError:
                checks[label + '-denied'] = True
            else:
                checks[label + '-denied'] = False
        checks['after'] = execution_mode() == 'worker'
        print(json.dumps(checks))
        return
    if mode == 'availability':
        print(json.dumps({'reason': client.unavailable_reason()}))
        return
    cfg = client.configuration()
    if mode == 'scoped-api-fixture':
        print(json.dumps(asyncio.run(scoped_api_fixture(cfg,payload))))
        return
    if mode in {'crash-window', 'job-audit', 'queue-start'}:
        async def ledger_probe():
            import asyncpg
            from core.config import config
            from core.execution.api import lane_id
            from core.execution.jobs import JobContext
            from core.execution.job_store import JobStore
            from core.execution.worker_queue import WorkerQueue
            pool = await asyncpg.create_pool(config.POSTGRES_URL, min_size=1, max_size=4)
            store = JobStore(pool, lane_id())
            await store.initialize()
            try:
                if mode == 'job-audit':
                    row = await store.get(payload['execution_id'])
                    return {k: row[k] for k in ('execution_id','outcome','cleanup','reconciliation','request_digest')}
                if mode == 'queue-start':
                    row = await store.admit(JobContext('acceptance','acceptance',payload['execution_id'],'runtime-acceptance'),
                        payload['execution_id'],payload['request'])
                    return {'execution_id':row['execution_id'],'outcome':row['outcome']}
                async with pool.acquire() as connection:
                    await connection.execute('SELECT pg_advisory_lock($1)', store.controller_lock)
                    execution_id = payload['execution_id']
                    controller = await store.takeover(connection, uuid.uuid4().hex,
                        client.Transport(cfg).hello()['lane_generation'] + 1)
                    queue = WorkerQueue(store, client.Transport(cfg))
                    hello = await queue.recover(connection, controller)
                    await store.admit(JobContext('acceptance','acceptance',execution_id,'runtime-acceptance'), execution_id, payload['request'])
                    row = await store.select(connection, controller, hello['profiles'])
                    assert row['execution_id'] == execution_id
                    if payload['phase'] == 'after_reply':
                        await asyncio.to_thread(client.Transport(cfg).submit, row['request'], threading.Event())
                    print(json.dumps({'execution_id': execution_id, 'phase':payload['phase']}), flush=True)
                    os._exit(86)  # DB connection death releases controller lock, no commit.
            finally:
                await pool.close()
        print(json.dumps(asyncio.run(ledger_probe())))
        return
    if mode == 'ladder-controls':
        print(json.dumps(asyncio.run(ladder_controls(cfg, payload))))
        return
    if mode == 'protocol':
        transport = client.Transport(cfg)
        action = payload.pop('action')
        if action == 'hello':
            value = transport.hello()
        elif action == 'submit':
            value = transport.submit(payload, threading.Event())
        else:
            value = transport.control(payload)
        print(json.dumps(value), flush=True)
        return
    if mode == 'queue-contention':
        from core.execution.jobs import JobContext, job_context
        from core.execution.api import lane_id
        async def contention():
            import asyncpg
            from core.config import config
            from core.execution.job_store import JobStore
            pool = await asyncpg.create_pool(config.POSTGRES_URL, min_size=1, max_size=4)
            run_ids = [uuid.uuid4().hex, uuid.uuid4().hex]
            # Disposable deterministic runs, no model or owner wait owns a slot.
            team = 's43a-acceptance-' + uuid.uuid4().hex
            await pool.execute('INSERT INTO agent_team_heads VALUES($1,$2,$3,1)', lane_id(), 'acceptance', team)
            await pool.execute("INSERT INTO agent_team_versions(lane,owner,id,revision,actor,boundary,definition) VALUES($1,$2,$3,1,'acceptance','acceptance','{}')", lane_id(), 'acceptance', team)
            for run_id in run_ids:
                await pool.execute("INSERT INTO agent_runs(lane,owner,id,actor,boundary,run_authority,team,team_revision,snapshot,request,status,generation,controller,lease_until) VALUES($1,'acceptance',$2,'acceptance','runtime-acceptance','{}',$3,1,'{}','{}','running',1,'runtime-acceptance',clock_timestamp()+interval '1 hour')", lane_id(), run_id, team)
            lockfile = Path(cfg['workspaces']['scratch']) / ('s43a-overlap-' + uuid.uuid4().hex)
            code = "import os,time; p=" + repr(str(lockfile)) + "; fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); time.sleep(.15); os.close(fd); os.unlink(p); print('ok')"
            labels = ['run-a','run-b','chat','build','git','mcp']
            async def one(index):
                label = labels[index]
                run_id = run_ids[index] if index < 2 else None
                with job_context(JobContext('acceptance','acceptance',label,'runtime-acceptance',run_id,1 if run_id else None)):
                    if label == 'git':
                        result = await client.execute_async('git', args=['--version'])
                    elif label == 'mcp':
                        result = await client.execute_async('mcp', input=json.dumps({'server':'filesystem','operation':'list'}))
                    else:
                        result = await client.execute_async('python', args=['-c',code])
                    return dict(group=label, ok=result['returncode']==0, execution_id=result['execution_id'])
            try:
                store = JobStore(pool,lane_id())
                await store.initialize()
                with job_context(JobContext('acceptance','acceptance','waiting-for-owner','runtime-acceptance')):
                    await asyncio.sleep(.2)
                    async with pool.acquire() as connection:
                        waiting_releases_slot = (await store.slot(connection))['active_execution'] is None
                ready = lockfile.with_suffix('.ready')
                async def hold():
                    with job_context(JobContext('acceptance','acceptance','acceptance-blocker','runtime-acceptance')):
                        return await client.execute_async('python', args=['-c',
                            'import pathlib,time; pathlib.Path('+repr(str(ready))+').touch(); time.sleep(10)'])
                holding = asyncio.create_task(hold())
                deadline = time.monotonic()+30
                while not ready.exists():
                    if holding.done() or time.monotonic()>deadline:
                        raise RuntimeError('contention blocker did not start')
                    await asyncio.sleep(.05)
                bounded = JobContext('bounds-user','bounds-user','bounds','runtime-acceptance')
                admitted_ids = [uuid.uuid4().hex for _ in range(32)]
                bound_enforced = False
                try:
                    for execution_id in admitted_ids:
                        await store.admit(bounded,execution_id,client.request('python',args=['-c','print("bound")']))
                    try:
                        await store.admit(bounded,uuid.uuid4().hex,client.request('python',args=['-c','print("excess")']))
                    except client.WorkerUnavailable:
                        bound_enforced = True
                    visible = await store.visible(admitted_ids[0])
                finally:
                    await asyncio.gather(*(store.cancel(execution_id,bounded) for execution_id in admitted_ids))
                results = await asyncio.gather(*(one(index) for _ in range(2) for index in range(6)))
                await holding
                rows = await pool.fetch('SELECT a.execution_id,a.caller_group,a.outcome FROM worker_job_attempts a WHERE lane=$1 AND execution_id=ANY($2::text[]) ORDER BY dispatched_at', lane_id(), [r['execution_id'] for r in results])
                return dict(results=results, dispatch_order=[dict(r) for r in rows], no_overlap=not lockfile.exists(),
                            bound_enforced=bound_enforced,waiting_releases_slot=waiting_releases_slot,
                            wait_state=visible['state'],wait_observed_at=visible['queue_position_observed_at'])
            finally:
                await pool.execute("UPDATE agent_runs SET status='completed',controller=NULL,lease_until=NULL WHERE lane=$1 AND owner='acceptance' AND id=ANY($2::text[])", lane_id(), run_ids)
                await pool.close()
        print(json.dumps(asyncio.run(contention())))
        return
    if mode == 'socket-access':
        results = {}
        for label, path in payload.items():
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.settimeout(3)
                    connection.connect(path)
                results[label] = True
            except OSError:
                results[label] = False
        results['policy-readable'] = client.unavailable_reason() is None
        print(json.dumps(results))
        return
    if mode == 'auth-refresh':
        import httpx
        from core.backends.openai_codex_auth import LaneCodexAuth
        auth = LaneCodexAuth()
        initial = auth.load()
        if not initial.access.startswith('S2D_GRANT.') or not initial.refresh.startswith('S2D_GRANT_'):
            raise ValueError('base auth probe requires synthetic grants; use fresh disposable lanes')
        access = 'S2D_GRANT.' + base64.urlsafe_b64encode(json.dumps({
            'exp': time.time() + 3600, 'nonce': uuid.uuid4().hex}).encode()).decode().rstrip('=') + '.synthetic'
        async def refresh():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                    200, json={'access_token': access, 'refresh_token': initial.refresh}))) as transport:
                result = await auth.credentials(transport, previous=initial)
                return result.access == access and result.refresh == initial.refresh
        print(json.dumps({'ok': asyncio.run(refresh())}))
        return
    if mode == 'core-cases':
        from core.build import skeleton
        from core.harness.sandbox import SubprocessSandbox
        from core.ledger.gitdag import _git
        workspace = Path(cfg['workspaces']['scratch']) / 'core-cases'
        workspace.mkdir(mode=0o770, exist_ok=True)
        unit = {'name': 'identity', 'signature': 'identity()', 'cases': [{'args': [], 'expect': cfg['worker_uid']}]}
        skeleton.write_app(workspace, [unit], {'identity': 'def identity():\n    return __import__("os").getuid()\n'})
        shell = SubprocessSandbox().run(['/usr/bin/id', '-u'], cwd=str(workspace))
        results = {'shell': shell.returncode == 0 and shell.stdout.strip() == str(cfg['worker_uid']),
                   'import': skeleton.build_empty_ok(workspace), 'collection': skeleton.collect_tests(workspace) == 1,
                   'pytest': skeleton.run_pytest(workspace)['passed'] == 1,
                   'cli': skeleton.run_cli(workspace)[0], 'git': _git(workspace, 'init', '-q').returncode == 0}
        print(json.dumps(results))
        return
    if mode == 'cancel':
        # Cancel the scheduler observer; its durable intent cancels the owning IO.
        async def cancel_job():
            from core.execution.jobs import operation_context
            context = operation_context()
            ready_path = Path(payload.pop('_ready_path'))
            bridge = await asyncio.to_thread(client.queue_bridge)
            future = await asyncio.to_thread(bridge.submit, context, payload)
            observed = asyncio.wrap_future(future)
            # The job's readiness marker precedes its first output.
            deadline = time.monotonic() + 30
            while not ready_path.exists():
                if observed.done():
                    await observed
                    raise RuntimeError('job exited before readiness')
                if time.monotonic() >= deadline:
                    raise RuntimeError('job readiness timeout')
                await asyncio.sleep(.05)
            observed.cancel()
            try:
                await observed
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(.5)  # Let durable cancellation reach daemon before process exit.
        asyncio.run(cancel_job())
        print(json.dumps({'cancel_requested': True}))
        return
    if mode == 'wire':
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(30)
            sock.connect(cfg['socket'])
            sock.sendall(json.dumps(payload).encode() + b'\n')
            with sock.makefile('rb') as stream:
                event = json.loads(stream.readline(65537))
                print(json.dumps(event), flush=True)
            # Closing after the first output chunk is a real caller cancellation.
        return
    if mode == 'codex':
        from core.backends.codex_code import CodexCodeClient
        from core import config
        config.config.CODEX_SCRATCH_ROOT = cfg['workspaces']['scratch']
        result = asyncio.run(CodexCodeClient(conversation_id='s2d-acceptance').chat(
            prompt='Reply exactly S2D_CODEX_OK. Do not call any tools.', posture={}))
        print(json.dumps({'ok': result['text'].strip() == 'S2D_CODEX_OK'}))
        return
    receipt = client.exchange(payload)
    receipt['stdout'] = receipt['stdout'].decode('utf-8', 'replace')
    receipt['stderr'] = receipt['stderr'].decode('utf-8', 'replace')
    print(json.dumps(receipt))


def payload(profile='acceptance', code='', *, timeout=20, output_limit=1048576):
    return dict(version=1, job_id=uuid.uuid4().hex, profile=profile, workspace='scratch', cwd='.',
                args=['-c', code] if code else [], input='', timeout=timeout,
                output_limit=output_limit, expires=time.time() + timeout + 20)


def legacy_migration_fixture(names):
    """Synthetic, unmarked pre-boundary lanes in an EMPTY throwaway distro."""
    import pwd
    import jwt
    from common import DATA, private_write, render_env
    from lane import lane_values, render_stores, allocate_ports, allocate_bridge, units
    from core.portal_resources import register_project_folder
    if Path('/etc/bob/execution-boundary').exists() or list(FAMILY.glob('*/bob-memory/lane.env')):
        raise ValueError('--migrate-existing requires an unmarked empty disposable runtime')
    bob = pwd.getpwnam('bob')
    # Restore the old image's shared-bob ancestor/helper ownership as well as
    # lane ownership. This catches migrations that only work on fresh images.
    admin('systemctl', 'stop', 'bob-qdrant.service', 'bob-embedder.service').check_returncode()
    for target in (DATA, FAMILY):
        target.chmod(0o700)
        os.chown(target, bob.pw_uid, bob.pw_gid)
    from migration_paths import transfer
    fixture_ids = {role + suffix: number for role in ('service', 'worker')
                   for suffix, number in (('_uid', bob.pw_uid), ('_gid', bob.pw_gid))}
    for leaf, username in (('models', 'bob-embedder'), ('qdrant', 'bob-qdrant')):
        transfer(DATA / leaf, fixture_ids, {}, lambda: None)
        unit = Path('/etc/systemd/system') / (username + '.service')
        source = unit.read_text()
        unit.write_text(source.replace('User=' + username + '\n', 'User=bob\n')
                              .replace('Group=' + username + '\n', 'Group=bob\n'))
    admin('systemctl', 'daemon-reload').check_returncode()
    admin('systemctl', 'start', 'bob-qdrant.service', 'bob-embedder.service').check_returncode()
    snapshots = {}
    used = {5432, 6379, 6353, 6354, 58081}
    for name in names:
        root = FAMILY / name / 'bob-memory'
        root.mkdir(parents=True, mode=0o700)
        os.chown(root.parent, bob.pw_uid, bob.pw_gid)
        os.chown(root, bob.pw_uid, bob.pw_gid)
        for leaf in ('projects/codex', 'scratch', 'codex-home', 'cli', 'spawn-context'):
            path = root / leaf
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        core, gateway = allocate_ports(used)
        used.update((core, gateway))
        bridge = allocate_bridge(used)
        used.add(bridge)
        values = lane_values(name, core, gateway, bridge, 'synthetic-migration-password',
                             'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP', secrets.token_urlsafe(32))
        private_write(root / 'lane.env', render_env(values), bob.pw_uid, bob.pw_gid)
        (root / 'memory_stores.toml').write_text(render_stores(name))
        (root / 'ledger_instances.json').write_text('{"version":1,"instances":{}}\n')
        (root / 'acceptance-disposable.json').write_text(json.dumps({'purpose': 's2d-disposable', 'lane': name}))
        (root / 'codex-home/config.toml').write_text('cli_auth_credentials_store = "file"\n[profiles.gpt]\nmodel_provider = "openai"\n')
        access = 'S2D_GRANT.' + base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip('=') + '.synthetic'
        (root / 'codex-home/auth.json').write_text(json.dumps({'tokens': {
            'access_token': access, 'refresh_token': 'S2D_GRANT_' + secrets.token_hex(16)}}))
        (root / 'migration-service-data').write_bytes(b'S2E_SERVICE_DATA\x00preserved')
        (root / 'projects/codex/migration-project-data').write_bytes(b'S2E_PROJECT_DATA\x00preserved')
        (root / 'synthetic-secret').write_text('S2E_SECRET_' + secrets.token_hex(24))
        project_id = str(uuid.uuid4())
        register_project_folder(root, name, project_id, 'projects/codex', 'Preserved project')
        # This fixture is entirely harness-created, contains no links/mounts,
        # and has never been exposed to a worker. Production uses the fd walker.
        for path in root.rglob('*'):
            os.chown(path, bob.pw_uid, bob.pw_gid)
            path.chmod(0o700 if path.is_dir() else 0o600)
        snapshots[name] = dict(values=values, project_id=project_id,
            files={str(path.relative_to(root)): path.read_bytes() for path in (
                root / 'migration-service-data', root / 'projects/codex/migration-project-data',
                root / 'project-folders.json', root / 'codex-home/auth.json', root / 'codex-home/config.toml')},
            old_token=jwt.encode({'sub': name, 'iat': time.time(), 'exp': time.time() + 3600},
                                 values['JWT_SECRET'], algorithm='HS256'))
        # Use the actual shipped lane service templates with the old shared UID
        # and unsplit envs, as in a pre-boundary installation.
        for unit in units(name):
            folder = Path('/etc/systemd/system') / (unit + '.d')
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'identity.conf').write_text(
                '[Service]\nUser=bob\nGroup=bob\nEnvironment=HOME=/home/bob\n'
                'Environment=BOB_EXECUTION_BOUNDARY=legacy\nEnvironmentFile=\n'
                f'EnvironmentFile={DATA}/secrets/bobclaw.env\nEnvironmentFile={root}/lane.env\n')
    admin('systemctl', 'daemon-reload').check_returncode()
    # A real worker project, created under the legacy identity. Do this after
    # the fixture-only mode normalization so executable bits and links survive.
    a = FAMILY / names[0] / 'bob-memory'
    b = FAMILY / names[1] / 'bob-memory'
    project = a / 'projects/codex'
    admin('runuser', '-u', 'bob', '--', PYTHON, '-m', 'venv',
          str(project / '.venv'), timeout=120).check_returncode()
    bins = project / 'node_modules/.bin'
    bins.mkdir(parents=True)
    tool = project / 'node_modules/local-tool'
    tool.write_text('#!/bin/sh\necho S2E_NODE_TOOL\n')
    tool.chmod(0o700)
    (bins / 'local-tool').symlink_to('../local-tool')
    source = a / 'scratch/local-origin'
    source.mkdir()
    os.chown(source, bob.pw_uid, bob.pw_gid)
    source.chmod(0o700)
    admin('runuser', '-u', 'bob', '--', 'git', 'init', str(source)).check_returncode()
    (source / 'tracked').write_text('S2E_GIT_DATA\n')
    os.chown(source / 'tracked', bob.pw_uid, bob.pw_gid)
    for args in (('add', 'tracked'), ('-c', 'user.name=S2E', '-c', 'user.email=s2e@example.invalid',
                                    'commit', '-m', 'synthetic offline fixture')):
        admin('runuser', '-u', 'bob', '--', 'git', '-C', str(source), *args).check_returncode()
    clone = project / 'local-clone'
    admin('runuser', '-u', 'bob', '--', 'git', 'clone', '--local', str(source), str(clone)).check_returncode()
    objects = [p for p in (clone / '.git/objects').glob('*/*') if p.is_file()]
    if not objects or not any(p.stat().st_nlink > 1 for p in objects):
        raise ValueError('local clone fixture did not create hardlinked objects')
    foreign = b / 'synthetic-secret'
    os.link(foreign, a / 'scratch/foreign-service-link')
    (a / 'scratch/foreign-service-symlink').symlink_to(foreign)
    # Normalize only the new harness-created entries, without following links.
    for top in (project / 'node_modules', source):
        os.chown(top, bob.pw_uid, bob.pw_gid)
        for path in top.rglob('*'):
            os.chown(path, bob.pw_uid, bob.pw_gid, follow_symlinks=False)
    (project / 'node_modules').chmod(0o700)
    bins.chmod(0o700)
    source.chmod(0o700)
    snapshots[names[0]]['foreign'] = dict(path=str(foreign), ino=foreign.stat().st_ino,
        uid=foreign.stat().st_uid, mode=stat.S_IMODE(foreign.stat().st_mode), data=foreign.read_bytes())
    snapshots[names[0]]['objects'] = {str(p.relative_to(a)): p.read_bytes() for p in objects}
    for name in names:
        admin('systemctl', 'enable', '--now', *units(name)).check_returncode()
        for unit in units(name):
            pid = int(admin('systemctl', 'show', '-p', 'MainPID', '--value', unit).stdout.strip())
            if not pid or Path(f'/proc/{pid}').stat().st_uid != bob.pw_uid:
                raise ValueError('legacy fixture did not start with shared bob identity')
    return snapshots


def migrate_existing(names):
    from common import read_env
    snapshots = legacy_migration_fixture(names)
    # Kill a real operator process after its durable verify-phase receipt. No
    # production failpoint or test-only bypass is installed in the runtime.
    process = subprocess.Popen(['bob-runtime', 'migrate-workers', '--lane', names[0]],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    interrupted = False
    try:
        import selectors
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 180
            while process.poll() is None and time.monotonic() < deadline:
                if not selector.select(.2):
                    continue
                line = process.stdout.readline()
                if line and json.loads(line).get('phase') == 'verify':
                    process.kill()
                    process.wait(timeout=10)
                    journal = json.loads((Path('/var/lib/bob-worker-migrations') / names[0] / 'state.json').read_text())
                    interrupted = process.returncode == -signal.SIGKILL and journal['phase'] != 'completed'
                    # B has not migrated: breaking A's hardlink must not have
                    # changed the legacy service inode's owner or mode.
                    foreign = snapshots[names[0]]['foreign']
                    info = Path(foreign['path']).stat()
                    if not emit('migration.foreign-inode-untouched',
                                (info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)) ==
                                (foreign['ino'], foreign['uid'], foreign['mode'])
                                and Path(foreign['path']).read_bytes() == foreign['data']):
                        raise ValueError('A migration changed B shared service inode')
                    break
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    if not emit('migration.interrupted', interrupted):
        raise ValueError('migration interruption was not observed before completion')
    root = FAMILY / names[0] / 'bob-memory'
    if not emit('migration.disabled-before-resume',
                (Path('/var/lib/bob-worker-migrations') / names[0] / 'pending').is_file()
                and read_env(root / 'lane.env').get('BOB_WORKER_ID', '').startswith('pending-')
                and Path('/etc/bob/execution-boundary').is_file()):
        raise ValueError('interrupted migration did not preserve the admission gate')
    journal = json.loads((Path('/var/lib/bob-worker-migrations') / names[0] / 'state.json').read_text())
    result = admin('runuser', '-u', journal['identity']['service_user'], '--', PYTHON, '-I',
                   str(Path(__file__).resolve()), '--child', names[0], 'availability', input='{}')
    reason = json.loads(result.stdout).get('reason') if result.returncode == 0 else None
    if not emit('migration.core-reports-unavailable', isinstance(reason, str) and reason.startswith('unavailable:')):
        raise ValueError('incomplete migration was not reported unavailable by the real core client')
    result = admin('bob-runtime', 'migrate-workers', timeout=600)
    if result.returncode:
        raise ValueError('migration retry failed; inspect root-private journals')
    for name in names:
        root = FAMILY / name / 'bob-memory'
        snapshot = snapshots[name]
        values = read_env(root / 'lane.env')
        preserved = all((root / path).read_bytes() == data for path, data in snapshot['files'].items())
        preserved &= all(values[k] == v for k, v in snapshot['values'].items()
                         if k not in {'JWT_SECRET', 'BOBCLAW_SECRET'})
        if not emit('migration.data-and-config.' + name, preserved):
            raise ValueError('migration did not preserve original data/configuration')
        journal = json.loads((Path('/var/lib/bob-worker-migrations') / name / 'state.json').read_text())
        if not emit('migration.rotation-scrubbed.' + name,
                    not {'jwt', 'vouch'} & journal['rotation'].keys()
                    and journal['rotation']['keys_published'] and journal['rotation']['portal_reissued']):
            raise ValueError('completed journal retains signing material or lost receipts')
        for label, token, expected in (
            ('old', snapshot['old_token'], 401),
            ('new', __import__('jwt').encode({'sub': name, 'iat': time.time(), 'exp': time.time()+120},
                                             values['JWT_SECRET'], algorithm='HS256'), 200)):
            connection = http.client.HTTPConnection('127.0.0.1', int(values['BOBCLAW_GATEWAY_PORT']), timeout=10)
            try:
                connection.request('GET', '/conversations', headers={'Authorization': 'Bearer ' + token})
                response = connection.getresponse()
                ok = response.status == expected
                response.read()
            finally:
                connection.close()
            if not emit('migration.jwt-' + label + '.' + name, ok):
                raise ValueError('gateway rotation check failed')
    paths = [Path('/var/lib/bob-worker-migrations') / name / 'state.json' for name in names]
    paths += [FAMILY / name / 'bob-memory/lane.env' for name in names]
    before = {path: path.read_bytes() for path in paths}
    result = admin('bob-runtime', 'migrate-workers', timeout=180)
    if not emit('migration.second-run-noop', result.returncode == 0 and
                all(path.read_bytes() == data for path, data in before.items())):
        raise ValueError('completed migration replay changed state')
    return snapshots


class Harness:
    def __init__(self, names, host_endpoints=()):
        from common import read_env
        self.names = names
        self.host_endpoints = list(host_endpoints)
        self.configs = {}
        self.envs = {}
        self.originals = {}
        self.secret_envs = {}
        self.failed = []
        self.script = str(Path(__file__).resolve())
        try:
            for name in names:
                root = Path('/var/lib/bob/family') / name / 'bob-memory'
                self.envs[name] = read_env(root / 'lane.env')
                lane_id = self.envs[name]['BOB_WORKER_ID']
                config_path = Path('/etc/bob/workers') / (lane_id + '.json')
                self.originals[config_path] = config_path.read_bytes()
                cfg = json.loads(config_path.read_text())
                # Temporary, root-enrolled network probe profile: same UID, service,
                # socket, mount namespace, seccomp and nft policy as Codex/MCP.
                cfg['profiles']['acceptance'] = {'argv': [PYTHON], 'network': True}
                config_path.write_text(json.dumps(cfg))
                config_path.chmod(0o644)
                self.configs[name] = cfg
                admin('systemctl', 'restart', f'bob-worker@{lane_id}.service').check_returncode()
        except BaseException:
            self.close()
            raise

    def record(self, name, passed, **detail):
        if not emit(name, passed, **detail):
            self.failed.append(name)

    def call(self, request, lane=None, mode='job'):
        lane = lane or self.names[0]
        cfg = self.configs[lane]
        result = admin('runuser', '-u', cfg['service_user'], '--', PYTHON, '-I',
                       self.script, '--child', lane, mode, input=json.dumps(request),
                       timeout=max(720 if mode == 'core-cases' else 120, request.get('timeout', 0) + 60))
        if result.returncode:
            raise RuntimeError('service-side worker call failed: stdout=' + result.stdout[-2000:] +
                               ' stderr=' + result.stderr[-1000:])
        return json.loads(result.stdout)

    def code(self, code, **kwargs):
        return self.call(payload(code=code, **kwargs))

    def services_ready(self):
        for lane, env in self.envs.items():
            deadline = time.monotonic() + 60
            while True:
                ready = True
                for key in ('BOBCLAW_CORE_PORT', 'BOBCLAW_GATEWAY_PORT', 'BOB_BRIDGE_PORT'):
                    with socket.socket() as connection:
                        connection.settimeout(.2)
                        ready &= connection.connect_ex(('127.0.0.1', int(env[key]))) == 0
                if ready or time.monotonic() >= deadline:
                    self.record('services.ready.' + lane, ready)
                    if not ready:
                        raise RuntimeError('disposable lane services did not become ready')
                    break
                time.sleep(.2)

    def migration_project(self, snapshot):
        root = FAMILY / self.names[0] / 'bob-memory'
        project = root / 'projects/codex'
        cfg = self.configs[self.names[0]]
        foreign = Path(snapshot['foreign']['path'])
        copied = root / 'scratch/foreign-service-link'
        self.record('migration.hardlinks-broken',
                    copied.stat().st_ino != foreign.stat().st_ino and copied.stat().st_nlink == 1
                    and copied.read_bytes() == snapshot['foreign']['data']
                    and all((root / p).read_bytes() == data and (root / p).stat().st_nlink == 1
                            for p, data in snapshot['objects'].items()))
        symlink = root / 'scratch/foreign-service-symlink'
        self.record('migration.symlink-preserved', symlink.is_symlink()
                    and symlink.lstat().st_uid == cfg['worker_uid'] and os.readlink(symlink) == str(foreign))
        code = f'''import os,subprocess
for p in ({str(foreign)!r}, {str(symlink)!r}):
 try: open(p).read()
 except (PermissionError,FileNotFoundError): pass
 else: raise AssertionError('foreign service data readable')
assert subprocess.check_output([{str(project / '.venv/bin/python')!r},'-c',
 'import sys; print(sys.prefix)'],text=True).strip()=={str(project / '.venv')!r}
assert subprocess.check_output(['/usr/bin/git','-C',{str(project / 'local-clone')!r},
 'status','--porcelain'],text=True)==''
assert subprocess.check_output([{str(project / 'node_modules/.bin/local-tool')!r}],text=True).strip()=='S2E_NODE_TOOL'
print('S2E_REAL_PROJECT_OK')
'''
        result = self.call(payload(code=code, timeout=30))
        self.record('migration.real-project-through-worker', result['returncode'] == 0
                    and result['stdout'].strip() == 'S2E_REAL_PROJECT_OK', stderr=result.get('stderr', '')[-1000:])

    def socket_access(self, phase='initial'):
        for lane in self.names:
            cfg = self.configs[lane]
            other = self.configs[next(name for name in self.names if name != lane)]
            paths = {'own-exec': cfg['socket'], 'own-auth': cfg['auth_socket'],
                     'foreign-exec': other['socket'], 'foreign-auth': other['auth_socket']}
            results = self.call(paths, lane=lane, mode='socket-access')
            self.record('sockets.service-access.' + phase + '.' + lane, results == {
                'own-exec': True, 'own-auth': True, 'foreign-exec': False,
                'foreign-auth': False, 'policy-readable': True}, checks=results)

    def startup_failure(self):
        # The real SQLite connection used to leave a non-daemon thread alive.
        source = '''import start
from aiohttp import web
from core.db import init_sqlite
async def fail():
 raise RuntimeError('S2D_FATAL_STARTUP')
start.init_sqlite=lambda: init_sqlite(':memory:')
start.init_postgres=fail
start.build_app=web.Application
start.config.validate=lambda: None
start.main(['--host','127.0.0.1','--port','0'])
'''
        result = admin('runuser', '-u', self.configs[self.names[0]]['service_user'], '--',
                       PYTHON, '-I', '-c', source, timeout=20)
        self.record('startup.fatal-exits', result.returncode != 0 and 'S2D_FATAL_STARTUP' in result.stderr)

    def close(self):
        for path, data in self.secret_envs.items():
            path.write_bytes(data)
            path.chmod(0o640)
        for path, data in self.originals.items():
            path.write_bytes(data)
            path.chmod(0o644)
        for cfg in self.configs.values():
            results = [admin('systemctl', 'restart', f'bob-worker-policy@{cfg["lane_id"]}.service'),
                       admin('systemctl', 'start', f'bob-worker@{cfg["lane_id"]}.socket', f'bob-worker-auth@{cfg["lane_id"]}.socket'),
                       admin('systemctl', 'restart', f'bob-worker@{cfg["lane_id"]}.service')]
            recovered = all(result.returncode == 0 for result in results)
            if recovered:
                try:
                    receipt = self.call(payload('python', 'import os; print(os.getuid())'), lane=cfg['lane'])
                    recovered = receipt['returncode'] == 0 and receipt['stdout'].strip() == str(cfg['worker_uid'])
                except RuntimeError:
                    recovered = False
            self.record('cleanup.worker-recovered.' + cfg['lane'], recovered)
        if self.secret_envs:
            for lane in self.names:
                for role in ('core', 'gateway', 'bridge'):
                    admin('systemctl', 'restart', f'bob-{role}@{lane}.service')

    def codex_positive(self):
        """Normal core HTTP dispatch, observed independently through root /proc."""
        lane = self.names[0]
        cfg, env = self.configs[lane], self.envs[lane]
        response = {}
        def chat():
            try:
                connection = http.client.HTTPConnection('127.0.0.1', int(env['BOBCLAW_CORE_PORT']), timeout=900)
                connection.request('POST', '/api/chat', body=json.dumps({
                    'conversation_id': str(uuid.uuid4()), 'backend': 'codex_code',
                    'content': 'In your allocated scratch workspace, use your shell tool to run id -u and pwd. '
                               'Then reply exactly S2D_CODEX_OK.'}), headers={'Content-Type': 'application/json'})
                result = connection.getresponse()
                response.update(status=result.status, body=result.read().decode())
                connection.close()
            except Exception as exc:
                response['error'] = type(exc).__name__
        thread = threading.Thread(target=chat, daemon=True)
        thread.start()
        observed = False
        while thread.is_alive():
            for path in Path('/proc').glob('[0-9]*'):
                try:
                    if path.stat().st_uid != cfg['worker_uid'] or (path / 'exe').resolve() != Path('/usr/local/bin/codex'):
                        continue
                    values = (path / 'environ').read_bytes().split(b'\0')
                    observed |= (('CODEX_HOME=' + cfg['codex_home']).encode() in values and
                                 not any(value.startswith((b'JWT_SECRET=', b'BOBCLAW_SECRET=', b'TOTP_SECRET=')) for value in values))
                except OSError:
                    pass
            time.sleep(.05)
        body = response.get('body', '')
        events = []
        for line in body.splitlines():
            if line.startswith('data: '):
                try:
                    events.append(json.loads(line[6:]))
                except ValueError:
                    pass
        self.record('positive.codex-normal-dispatch', response.get('status') == 200 and
                    'S2D_CODEX_OK' in body and any(event.get('type') == 'message_complete' for event in events) and
                    not any(event.get('type') == 'error' for event in events))
        self.record('positive.codex-observed-worker-home', observed)

    def run(self):
        a, b = self.names
        ca, cb = self.configs[a], self.configs[b]
        marker = Path('/etc/bob/execution-boundary')
        info, parent = marker.lstat(), marker.parent.stat()
        self.record('execution-mode.root-marker', marker.is_file() and not marker.is_symlink() and
                    info.st_uid == parent.st_uid == 0 and not (info.st_mode & 0o222) and
                    not (parent.st_mode & 0o022))
        for name in self.names:
            checks = self.call({}, lane=name, mode='execution-mode')
            self.record('execution-mode.service-cannot-disable.' + name, all(checks.values()), checks=checks)
        self.socket_access()
        # Exercise both directories again after the policy recreates its receipt.
        for cfg in (ca, cb):
            admin('systemctl', 'restart', f'bob-worker-policy@{cfg["lane_id"]}.service').check_returncode()
            admin('systemctl', 'start', f'bob-worker@{cfg["lane_id"]}.socket',
                  f'bob-worker-auth@{cfg["lane_id"]}.socket').check_returncode()
        self.socket_access('policy-restart')
        self.startup_failure()
        roots = [Path('/var/lib/bob/family') / name / 'bob-memory' for name in self.names]
        canary = 'S2D_SECRET_' + secrets.token_hex(24)
        for root, cfg in zip(roots, (ca, cb)):
            secret = root / 'acceptance-secret'
            secret.write_text(canary)
            secret.chmod(0o600)
            os.chown(secret, cfg['service_uid'], cfg['service_gid'])
            for role in ('core', 'gateway', 'bridge'):
                # Split-identity lanes keep gateway/bridge env under root-owned service-env.
                split = Path('/etc/bob/service-env') / f'{cfg["lane_id"]}-{role}.env'
                path = split if split.exists() else root / (role + '.env')
                self.secret_envs[path] = path.read_bytes()
                with path.open('a') as stream:
                    stream.write(f'BOB_ACCEPTANCE_CANARY="{canary}"\n')
            auth = Path(cfg['codex_home']) / 'auth.json'
            if not auth.exists():
                token = 'S2D_GRANT.' + base64.urlsafe_b64encode(b'{"exp":1}').decode().rstrip('=') + '.synthetic'
                auth.write_text(json.dumps({'tokens': {'access_token': token,
                    'refresh_token': 'S2D_GRANT_' + secrets.token_hex(16)}}))
                os.chown(auth, cfg['worker_uid'], cfg['worker_gid'])
                auth.chmod(0o660)
        # One transaction starts both cores concurrently, as on a fresh WSL boot.
        admin('systemctl', 'restart', *[f'bob-{role}@{name}.service'
              for name in self.names for role in ('core', 'gateway', 'bridge')]).check_returncode()
        self.services_ready()
        auth_path = Path(ca['codex_home']) / 'auth.json'
        before_auth = auth_path.stat()
        result = self.call({}, mode='auth-refresh')
        after_auth = auth_path.stat()
        self.record('auth.http-refresh-real-worker-publication', result.get('ok') and
                    after_auth.st_uid == ca['worker_uid'] and after_auth.st_nlink == 1 and
                    before_auth.st_ino != after_auth.st_ino and after_auth.st_mode & 0o777 == 0o660)
        r = self.call({**payload('shell'), 'args': ['-c', 'printf S2D_SHELL_OK']})
        self.record('positive.shell', r['returncode'] == 0 and r['stdout'] == 'S2D_SHELL_OK' and r.get('uid') == ca['worker_uid'])
        group = admin('systemctl', 'show', '--property=ControlGroup', '--value', f'bob-worker@{ca["lane_id"]}.service').stdout.strip()
        control = Path('/sys/fs/cgroup') / group.lstrip('/')
        memory = (control / 'memory.max').read_text().strip()
        tasks = (control / 'pids.max').read_text().strip()
        quota, period = (control / 'cpu.max').read_text().split()
        self.record('resources.cgroup-limits', memory != 'max' and int(memory) <= 2147483648 and
                    tasks != 'max' and int(tasks) <= 256 and quota != 'max' and int(quota) / int(period) <= 2)
        for operation, passed in self.call({}, mode='core-cases').items():
            self.record('positive.core.' + operation, passed)

        code = '''import os,json,subprocess,sys
def details():
 status=open('/proc/self/status').read()
 fds=[]
 for fd in os.listdir('/proc/self/fd'):
  try: fds.append(os.readlink('/proc/self/fd/'+fd))
  except OSError: pass
 return dict(uid=os.getuid(),groups=os.getgroups(),status=status,env=dict(os.environ),fds=fds)
d=details()
d['grandchild']=json.loads(subprocess.check_output([sys.executable,'-c',"import os,json; print(json.dumps(dict(uid=os.getuid(),groups=os.getgroups(),env=dict(os.environ),status=open('/proc/self/status').read())))"]))
print(json.dumps(d))'''
        r = self.code(code)
        info = json.loads(r['stdout'])
        for label, item in (('child', info), ('grandchild', info['grandchild'])):
            good = (item['uid'] == ca['worker_uid'] and set(item['groups']) <= {ca['worker_gid']} and
                    'CapEff:\t0000000000000000' in item['status'] and 'NoNewPrivs:\t1' in item['status'] and
                    not any(k in item['env'] for k in ('JWT_SECRET', 'BOBCLAW_SECRET', 'TOTP_SECRET',
                        'POSTGRES_URL', 'BOBCLAW_PASSWORD', 'SSH_AUTH_SOCK', 'WSL_INTEROP', 'PYTHONPATH', 'LD_PRELOAD', 'BOB_ACCEPTANCE_CANARY')))
            self.record('identity.' + label, good)
        r = self.code('import os,pathlib; p=pathlib.Path(os.environ["HOME"])/"acceptance-home"; '
                      'p.write_text("ok"); print(p.read_text()); p.unlink()')
        self.record('filesystem.worker-home-writable', r['returncode'] == 0 and r['stdout'].strip() == 'ok')
        self.record('descriptors', not any('socket:[' in p or 'acceptance-secret' in p or 'lane.env' in p for p in info['fds']))
        self.record('canaries.output', canary not in json.dumps(info))
        paths = [str(root / leaf) for root in roots for leaf in ('lane.env', 'providers.env', 'acceptance-secret', 'gateway.db')]
        paths += ['/var/lib/bob/secrets/bobclaw.env', '/var/lib/bob-portal/' + a + '.token',
                  str(roots[1] / 'codex-home/auth.json'), '/etc/bob/workers/' + ca['lane_id'] + '.json']
        code = '''import os,json
paths=PATHS
out={}
for path in paths:
 row={}
 for mode in ('read','write','replace','list','symlink'):
  try:
   if mode=='read': open(path,'rb').read(1)
   elif mode=='write': open(path,'r+b').close()
   elif mode=='list':
    entries=os.listdir(os.path.dirname(path))
    row[mode]=not any(x in entries for x in ('lane.env','providers.env','acceptance-secret','gateway.db'))
    continue
   elif mode=='symlink':
    os.symlink(path,'probe-link')
    try: open('probe-link','rb').read(1)
    finally: os.unlink('probe-link')
   else:
    # Only synthetic canary files: never replace an actual configuration/database.
    if not path.endswith('acceptance-secret'): continue
    open('probe-replace','w').write('attempt')
    os.replace('probe-replace',path)
   row[mode]=False
  except OSError: row[mode]=True
 out[path]=row
print(json.dumps(out))'''.replace('PATHS', repr(paths))
        evidence = json.loads(self.code(code)['stdout'])
        for path, row in evidence.items():
            # Root-owned installed profiles are intentionally readable, immutable data.
            expected = [v for key, v in row.items() if not (path.startswith('/etc/bob/workers/') and key in {'read', 'list', 'symlink'})]
            # Traversal/listing the own lane root is denied in the worker's empty mount tree.
            self.record('filesystem.' + path, bool(expected) and all(expected))

        pids = []
        for name in self.names:
            for role in ('core', 'gateway', 'bridge'):
                result = admin('systemctl', 'show', '-p', 'MainPID', '--value', f'bob-{role}@{name}.service')
                pid = int(result.stdout.strip() or '0')
                if pid:
                    pids.append(pid)
                    self.record(f'canary.live.{name}.{role}', canary.encode() in Path(f'/proc/{pid}/environ').read_bytes())
        broker = subprocess.Popen(['runuser', '-u', 'bobapp', '--', '/usr/local/bin/bob-broker', a],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        try:
            import pwd
            broker_uid = pwd.getpwnam('bobapp').pw_uid
            def broker_ready():
                if broker.poll() is not None:
                    return True
                children = Path(f'/proc/{broker.pid}/task/{broker.pid}/children').read_text().split()
                return bool(children) and all(Path(f'/proc/{pid}').stat().st_uid == broker_uid and
                    b'/portal-broker/portal-broker.py' in Path(f'/proc/{pid}/cmdline').read_bytes() for pid in children)
            wait_until(broker_ready)
            children = Path(f'/proc/{broker.pid}/task/{broker.pid}/children').read_text().split()
            pids.extend(map(int, children or [str(broker.pid)]))
            self.record('portal.live-broker', bool(children) and all(Path(f'/proc/{pid}').stat().st_uid == broker_uid for pid in map(int, children)))
            code = '''import os,json,ctypes
out={}
for pid in PIDS:
 for leaf in ('environ','fd','mem','root/var/lib/bob/secrets/bobclaw.env'):
  path=f'/proc/{pid}/{leaf}'
  try:
   if leaf=='fd': os.listdir(path)
   else: open(path,'rb').read(1)
   out[path]=False
  except OSError: out[path]=True
 out[f'ptrace:{pid}']=ctypes.CDLL(None,use_errno=True).ptrace(16,pid,0,0)==-1
print(json.dumps(out))'''.replace('PIDS', repr(pids))
            rows = json.loads(self.code(code)['stdout'])
            for path, denied in rows.items():
                self.record('process.' + path, denied)
            self.record('process.live-services', len(pids) >= 7, count=len(pids))
        finally:
            try:
                broker.communicate(timeout=10)  # EOF closes the broker as well as runuser.
            except subprocess.TimeoutExpired:
                os.killpg(broker.pid, signal.SIGKILL)
                broker.communicate(timeout=10)

        # Keep real listeners open while the worker tries them. A refusal from a
        # closed port is not accepted as firewall evidence.
        listeners = []
        endpoints = []
        try:
            ports = {5432, 6379, 6353, 6354, 58081}
            for env in self.envs.values():
                ports.update(int(env[k]) for k in ('BOBCLAW_CORE_PORT', 'BOBCLAW_GATEWAY_PORT', 'BOB_BRIDGE_PORT'))
            addresses = ['127.0.0.1', '127.0.0.2', '::1']
            rows = json.loads(admin('ip', '-j', 'address', 'show').stdout)
            addresses += [ip['local'] for row in rows for ip in row.get('addr_info', [])
                          if ip['family'] in {'inet', 'inet6'} and ip.get('scope') == 'global']
            # Resolve aliases as root, then exercise their numeric addresses with
            # live listeners. A fresh distro need not resolve its own hostname;
            # worker DNS failure must never masquerade as firewall enforcement.
            for hostname in ('localhost', socket.gethostname()):
                try:
                    addresses += [row[4][0] for row in socket.getaddrinfo(hostname, 5432, type=socket.SOCK_STREAM)]
                except socket.gaierror:
                    pass
            for address in sorted(set(addresses)):
                family = socket.AF_INET6 if ':' in address else socket.AF_INET
                for port in sorted(ports):
                    listener = socket.socket(family)
                    try:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        if family == socket.AF_INET6:
                            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                        listener.bind((address, port))
                        listener.listen(128)
                        listeners.append(listener)
                    except OSError:
                        listener.close()
                    with socket.socket(family) as check:
                        check.settimeout(1)
                        if check.connect_ex((address, port)) != 0:
                            self.record(f'network.listener.{address}:{port}', False, reason='no live listener')
                            continue
                    endpoints.append((address, port))
            live_hosts = 0
            for address, port in self.host_endpoints:
                family = socket.AF_INET6 if ':' in address else socket.AF_INET
                with socket.socket(family) as check:
                    check.settimeout(2)
                    live = check.connect_ex((address, port)) == 0
                self.record(f'network.host-positive-control.{address}:{port}', live,
                            reason='live operator host listener required')
                if live:
                    endpoints.append((address, port))
                    live_hosts += 1
            is_wsl = 'microsoft' in OSRELEASE.read_text().lower()
            self.record('network.windows-host-covered', bool(live_hosts) if is_wsl else True,
                        applicable=is_wsl, reason='WSL requires --host-listener; never infer denial from a closed host port')
            code = '''import socket,json
out={}
for host,port in ENDPOINTS:
 family=socket.AF_INET6 if ':' in host else socket.AF_INET
 with socket.socket(family) as s:
  s.settimeout(.4)
  out[f'{host}:{port}']=s.connect_ex((host,port))!=0
print(json.dumps(out))'''.replace('ENDPOINTS', repr(endpoints))
            rows = json.loads(self.code(code, timeout=90)['stdout'])
            for target, denied in rows.items():
                self.record('network.' + target, denied)
            self.record('network.ipv6-covered', any('::1' in e for e in rows))
            self.record('network.host-address-covered', len(set(addresses)) > 3)
            # Offline profiles may not even create Internet sockets.
            r = self.call(payload('python', 'import socket; socket.socket(socket.AF_INET)'))
            self.record('network.offline-profile', r['returncode'] != 0)
        finally:
            for listener in listeners:
                listener.close()

        sockets = ['/run/postgresql/.s.PGSQL.5432', '/run/redis/redis-server.sock',
                   '/run/docker.sock', '/run/containerd/containerd.sock', '/run/systemd/private',
                   '/run/dbus/system_bus_socket', ca['socket'], cb['socket']]
        code = '''import socket,json
out={}
for path in SOCKETS:
 try:
  with socket.socket(socket.AF_UNIX) as s: s.connect(path)
  out[path]=False
 except OSError: out[path]=True
print(json.dumps(out))'''.replace('SOCKETS', repr(sockets))
        for path, denied in json.loads(self.code(code)['stdout']).items():
            self.record('unix.' + path, denied)
        with socket.socket(socket.AF_UNIX) as listener:
            abstract = '\0bob-s2d-' + secrets.token_hex(8)
            listener.bind(abstract)
            listener.listen()
            code = "import socket; s=socket.socket(socket.AF_UNIX); s.connect(" + repr(abstract) + ')'
            self.record('unix.abstract', self.code(code)['returncode'] != 0)

        commands = [
            ['bob-lane', 'remove', b, '--keep-data'], ['bob-model', 'set-default', b, 'codex_code'],
            ['bob-project-folder', b, str(uuid.uuid4()), 'projects/codex', 'forged'],
            ['bob-token-renew'], ['bob-embedder', 'fetch'], ['bob-broker', a],
            ['sudo', '-n', 'id', '-u'], ['su', '-s', '/bin/sh', '-c', 'id -u', 'root'],
            ['runuser', '-u', 'root', '--', 'id', '-u'],
            ['systemd-run', '--system', '--wait', 'id', '-u'],
            ['dbus-send', '--system', '--dest=org.freedesktop.systemd1', '/org/freedesktop/systemd1',
             'org.freedesktop.systemd1.Manager.Reload']]
        # Root preflight ensures missing utilities cannot pass as access denial.
        commands = [[shutil.which(command[0]) or command[0], *command[1:]] for command in commands]
        before = hashlib.sha256((roots[1] / 'lane.env').read_bytes()).hexdigest()
        code = '''import subprocess,json
out={}
for cmd in COMMANDS:
 try: out[' '.join(cmd[:2])]=subprocess.run(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode!=0
 except (OSError,subprocess.TimeoutExpired): out[' '.join(cmd[:2])]=True
print(json.dumps(out))'''.replace('COMMANDS', repr(commands))
        for command, denied in json.loads(self.code(code, timeout=90)['stdout']).items():
            self.record('helper.' + command, denied)
        self.record('helper.state-unchanged', before == hashlib.sha256((roots[1] / 'lane.env').read_bytes()).hexdigest())
        # A real setuid-root executable: no-new-privileges must retain worker UID.
        c = roots[0] / 'scratch/acceptance-setuid'
        shutil.copyfile('/usr/bin/id', c)
        os.chown(c, 0, 0)
        c.chmod(0o4755)
        r = self.code(f'import subprocess; print(subprocess.check_output([{str(c)!r},"-u"]).decode().strip())')
        self.record('privilege.setuid', r['stdout'].strip() == str(ca['worker_uid']))
        c.unlink()

        for field in ('lane', 'uid', 'gid', 'env', 'executable', 'mounts', 'fds', 'unit', 'socket', 'capabilities'):
            event = self.call({**payload(), field: 'forged'}, mode='wire')
            self.record('forgery.' + field, event.get('type') == 'error')
        for label, changes in [('profile', {'profile': 'not-installed'}), ('workspace', {'workspace': 'foreign'}),
                ('traversal', {'cwd': '../'}), ('expired', {'expires': 1}), ('oversized', {'input': 'x' * 1100000})]:
            try:
                event = self.call({**payload(), **changes}, mode='wire')
                denied = event.get('type') == 'error'
            except RuntimeError:
                denied = True  # Oversized frame can be reset without an error frame.
            self.record('forgery.' + label, denied)
        foreign = admin('runuser', '-u', cb['service_user'], '--', PYTHON, '-c',
                        'import socket; s=socket.socket(socket.AF_UNIX); s.connect(' + repr(ca['socket']) + ')')
        self.record('forgery.foreign-peer', foreign.returncode != 0)
        root_peer = socket.socket(socket.AF_UNIX)
        try:
            root_peer.settimeout(10)
            root_peer.connect(ca['socket'])
            root_peer.sendall(json.dumps(payload()).encode() + b'\n')
            self.record('forgery.root-peer', json.loads(root_peer.makefile('rb').readline())['type'] == 'error')
        finally:
            root_peer.close()

        # Malicious hooks/plugins/imports run through real git/python profiles.
        hook = 'import os; open("worker-hook-result","w").write(str(os.getuid()))'
        code = '''import os,pathlib,subprocess,json,sys
p=pathlib.Path('hookrepo'); p.mkdir(exist_ok=True)
subprocess.run(['git','init','-q',str(p)],check=True)
h=p/'.git/hooks/pre-commit'; h.write_text('#!'+sys.executable+'\\n'+HOOK+'\\n'); h.chmod(0o755)
subprocess.run(['git','-C',str(p),'-c','user.name=probe','-c','user.email=p@invalid','commit','--allow-empty','-qm','probe'],check=True)
print((p/'worker-hook-result').read_text())'''.replace('HOOK', repr(hook))
        self.record('workspace.git-hook', self.code(code)['stdout'].strip() == str(ca['worker_uid']))
        code = '''import pathlib,subprocess,sys,os
p=pathlib.Path('pytest-probe'); p.mkdir(exist_ok=True)
(p/'conftest.py').write_text('import os\\nopen("worker-plugin-result","w").write(str(os.getuid()))\\n')
(p/'test_probe.py').write_text('def test_ok(): assert True\\n')
subprocess.run([sys.executable,'-m','pytest','-q','-p','no:cacheprovider'],cwd=p,stdout=subprocess.DEVNULL,check=True)
print((p/'worker-plugin-result').read_text())'''
        self.record('workspace.pytest-plugin', self.code(code)['stdout'].strip() == str(ca['worker_uid']))
        self.record('workspace.generated-import', self.call(payload('python', 'import os; print(os.getuid())'))['stdout'].strip() == str(ca['worker_uid']))
        for server in ('filesystem', 'browser'):
            r = self.call({**payload('mcp'), 'input': json.dumps({'server': server, 'operation': 'list'})}, mode='job')
            self.record('workspace.mcp.' + server, r['returncode'] == 0 and bool(json.loads(r['stdout'])))
        request = {'server': 'filesystem', 'operation': 'call', 'name': 'read_text_file',
                   'arguments': {'path': str(roots[0] / 'scratch/core-cases/minikit/functions.py')}}
        r = self.call({**payload('mcp'), 'input': json.dumps(request)})
        self.record('workspace.mcp.filesystem-call', r['returncode'] == 0 and
                    not json.loads(r['stdout']).get('isError') and 'getuid' in r['stdout'])
        request = {'server': 'browser', 'operation': 'sequence', 'calls': [
            {'name': 'browser_navigate', 'arguments': {'url': 'data:text/html,<h1>S2D_BROWSER_OK</h1>'}},
            {'name': 'browser_snapshot', 'arguments': {}}]}
        r = self.call({**payload('mcp', timeout=90), 'input': json.dumps(request)})
        self.record('workspace.mcp.browser-navigation', r['returncode'] == 0 and
                    all(not result.get('isError') for result in json.loads(r['stdout'])) and 'S2D_BROWSER_OK' in r['stdout'])

        # Submit the worker's opaque attempted JWT from the trusted harness.
        code = '''import base64,hmac,hashlib,json,time
enc=lambda x:base64.urlsafe_b64encode(json.dumps(x).encode()).decode().rstrip('=')
t=enc({'alg':'HS256','typ':'JWT'})+'.'+enc({'sub':LANE,'exp':time.time()+300,'iat':time.time()})
print(t+'.'+base64.urlsafe_b64encode(hmac.new(b'guessed-worker-key',t.encode(),hashlib.sha256).digest()).decode().rstrip('='))'''.replace('LANE', repr(a))
        forged = self.code(code)['stdout'].strip()
        connection = http.client.HTTPConnection('127.0.0.1', int(self.envs[a]['BOBCLAW_GATEWAY_PORT']), timeout=10)
        connection.request('GET', '/conversations', headers={'Authorization': 'Bearer ' + forged})
        response = connection.getresponse()
        self.record('token-mint.gateway-rejection', response.status == 401, status=response.status)
        response.read()
        connection.close()
        import jwt
        from common import read_env
        claims = {'sub': a, 'iat': time.time(), 'exp': time.time() + 120}
        generation = Path('/var/lib/bob-settings') / self.configs[a]['lane_id'] / 'credentials/current.env'
        if generation.exists():
            # Credential generations (3.3): human tokens must carry the loaded epoch.
            stale = jwt.encode(claims, self.envs[a]['JWT_SECRET'], algorithm='HS256')
            connection = http.client.HTTPConnection('127.0.0.1', int(self.envs[a]['BOBCLAW_GATEWAY_PORT']), timeout=10)
            connection.request('GET', '/conversations', headers={'Authorization': 'Bearer ' + stale})
            response = connection.getresponse()
            self.record('token-mint.missing-epoch-rejected', response.status == 401, status=response.status)
            response.read()
            connection.close()
            claims['credential_epoch'] = int(read_env(generation)['BOB_CREDENTIAL_EPOCH'])
        valid = jwt.encode(claims, self.envs[a]['JWT_SECRET'], algorithm='HS256')
        connection = http.client.HTTPConnection('127.0.0.1', int(self.envs[a]['BOBCLAW_GATEWAY_PORT']), timeout=10)
        connection.request('GET', '/conversations', headers={'Authorization': 'Bearer ' + valid})
        response = connection.getresponse()
        self.record('token-mint.trusted-positive-control', response.status == 200, status=response.status)
        response.read()
        connection.close()

        code = '''import os,subprocess,json
out={}
for path in ('/mnt/c','/mnt/wsl','/mnt/wslg','/run/WSL','/proc/sys/fs/binfmt_misc/WSLInterop'):
 try: os.listdir(path) if os.path.isdir(path) else open(path,'rb').read(1); out[path]=False
 except OSError: out[path]=True
for exe in ('/mnt/c/Windows/System32/cmd.exe','/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'):
 try: out[exe]=subprocess.run([exe,'/c','exit','0'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode!=0
 except (OSError,subprocess.TimeoutExpired): out[exe]=True
print(json.dumps(out))'''
        for path, denied in json.loads(self.code(code)['stdout']).items():
            self.record('wsl.' + path, denied)

        descendant_file = roots[0] / 'scratch/acceptance-descendant'
        code = f'''import os,time,pathlib,ctypes
if os.fork()==0:
 os.setsid()
 ctypes.CDLL(None).prctl(4,0,0,0,0)
 pathlib.Path({str(descendant_file)!r}).write_text(str(os.getpid()))
 while True: time.sleep(1)
time.sleep(30)'''
        r = self.code(code, timeout=2)
        pid = int(descendant_file.read_text())
        self.record('lifecycle.timeout-descendants', r.get('killed') and
                    wait_until(lambda: not Path(f'/proc/{pid}').exists()))
        descendant_file.unlink()
        code = code.replace('time.sleep(30)',
            f'while not pathlib.Path({str(descendant_file)!r}).exists(): time.sleep(.01)\n'
            'print("ready",flush=True)\ntime.sleep(30)')
        self.call({**payload(code=code), '_ready_path':str(descendant_file)}, mode='cancel')
        pid = int(descendant_file.read_text())
        self.record('lifecycle.cancel-descendants', wait_until(lambda: not Path(f'/proc/{pid}').exists()))
        r = self.code('import sys; sys.stdout.write("x"*20000)', output_limit=1024)
        self.record('lifecycle.output-ceiling', r.get('killed') and len(r['stdout'].encode()) <= 1024)
        self.record('lifecycle.detached-child-killed', not Path(f'/proc/{pid}').exists())
        uid_before = ca['worker_uid']
        admin('systemctl', 'restart', f'bob-worker@{ca["lane_id"]}.service').check_returncode()
        self.record('lifecycle.restart-identity', self.code('import os; print(os.getuid())')['stdout'].strip() == str(uid_before))
        admin('nft', 'delete', 'table', 'inet', 'bob_w_' + ca['lane_id']).check_returncode()
        wait_until(lambda: not Path(ca['socket']).exists() and not Path(ca['auth_socket']).exists())
        try:
            self.code('print("must not run")')
            denied = False
        except RuntimeError:
            denied = True
        self.record('lifecycle.deleted-kernel-policy-disabled',
                    denied and not Path(ca['socket']).exists() and not Path(ca['auth_socket']).exists())
        admin('systemctl', 'restart', f'bob-worker-policy@{ca["lane_id"]}.service').check_returncode()
        admin('systemctl', 'start', f'bob-worker@{ca["lane_id"]}.socket').check_returncode()
        admin('systemctl', 'restart', f'bob-worker@{ca["lane_id"]}.service').check_returncode()
        admin('systemctl', 'stop', f'bob-worker-auth@{ca["lane_id"]}.socket').check_returncode()
        try:
            self.code('print("must not run")')
            denied = False
        except RuntimeError:
            denied = True
        self.record('lifecycle.missing-auth-socket-disabled',
                    denied and not Path(ca['socket']).exists() and not Path(ca['auth_socket']).exists())
        admin('systemctl', 'start', f'bob-worker@{ca["lane_id"]}.socket').check_returncode()
        admin('systemctl', 'stop', f'bob-worker@{ca["lane_id"]}.socket', f'bob-worker@{ca["lane_id"]}.service').check_returncode()
        try:
            self.code('print("must not run")')
            denied = False
        except RuntimeError:
            denied = True
        self.record('lifecycle.missing-worker-disabled', denied)
        admin('systemctl', 'start', f'bob-worker@{ca["lane_id"]}.socket').check_returncode()
        admin('systemctl', 'stop', f'bob-worker-policy@{ca["lane_id"]}.service').check_returncode()
        try:
            self.code('print("must not run")')
            denied = False
        except RuntimeError:
            denied = True
        self.record('lifecycle.missing-policy-disabled',
                    denied and not Path(ca['socket']).exists() and not Path(ca['auth_socket']).exists())
        # close() re-enables policy/socket after this negative test.


async def ladder_controls(cfg, options):
    """Disposable fixture authority; exercise the actual store and daemon.

    This is not a production owner-auth bypass: --child is a root-run harness
    mode on marked disposable lanes. It makes no model/provider calls.
    """
    import asyncpg
    from core.config import config
    from core.execution.api import lane_id
    from core.execution.models import Authority, canonical, TOPOLOGY
    from core.execution.store import Store
    from core.execution.job_store import JobStore
    from core.execution.jobs import JobContext
    from core.execution.job_policy import JobPolicy, Reconciliation
    from core.execution.worker_queue import WorkerQueue
    from core.worker.client import Transport
    from core.worker.contract import profile_revision, WorkerUnavailable
    pool = await asyncpg.create_pool(config.POSTGRES_URL,min_size=1,max_size=5)
    store, jobs = Store(pool,lane_id()), JobStore(pool,lane_id())
    await store.initialize()
    await jobs.initialize()
    rows = []
    owner = 'acceptance-ladder-' + uuid.uuid4().hex
    auth = Authority(lane_id(),owner,owner,{},'human')
    definition = dict(schema_version=1,id='ladder-'+uuid.uuid4().hex,topology=TOPOLOGY,
                      roles={'manager':'fixture','worker':'fixture','critic':'fixture'})
    saved = await store.save_team(auth,definition,None,'save',{'definition':definition})
    team = saved['team']
    snapshot = dict(team=team,models={'fixture':{'model':'deterministic-no-provider'}},review_independence='same_model')
    scope = dict(profiles={'acceptance':profile_revision(cfg['profiles']['acceptance'])},
                 workspaces={'scratch':['.']},attachments=[],max_job_attempts=8,
                 job_seconds_budget=20000,allow_cli=False)
    transport = Transport(cfg)
    queue = WorkerQueue(jobs,transport)
    async def new_run(seconds, granted=False):
        req = dict(contract_version=2,team=team['id'],team_revision=1,task='deterministic ladder fixture',
                   evidence_ids=[],request_key=uuid.uuid4().hex,job_coverage=scope,
                   budget=dict(start_calls=10,wall_mode='none',wall_seconds=None,max_job_attempts=8,job_seconds_budget=20000))
        grant = None
        if granted:
            proposal = await store.prepare_grant(auth,{'request_key':uuid.uuid4().hex},req,snapshot)
            issued = await store.issue_grant(auth,dict(proposal_id=proposal['proposal_id'],proposal_revision=1,
                expected_revision=None,request_key=uuid.uuid4().hex,coverage_kind='task',allow_long_runs=True,
                auto_escalate=True,call_ceiling=None,expires_at=None,allow_extended_jobs=True,allow_custom_job_limit=True))
            grant = issued['grant']
            req['intent_id'] = proposal['intent_id']
        started = await store.start(auth,req,snapshot)
        run_id = started['run_id']
        await pool.execute("UPDATE agent_runs SET status='running',generation=1,controller='runtime-acceptance',lease_until=clock_timestamp()+interval '1 day' WHERE lane=$1 AND owner=$2 AND id=$3",lane_id(),owner,run_id)
        return JobContext(owner,owner,'run:'+run_id,auth.boundary,run_id,1,JobPolicy(seconds,'installed acceptance fixture')), grant
    def job(code):
        result = payload('acceptance',code,timeout=900)
        return result
    async def record(label,passed,**evidence):
        rows.append(dict(probe=label,passed=bool(passed),**evidence))
        if not passed:
            raise RuntimeError('ladder control failed: '+label)
    try:
        async with pool.acquire() as c:
            # Suppress the independently running core controller for these
            # deliberate fixture windows. The kernel lock is released on death.
            await c.execute('SELECT pg_advisory_lock($1)',jobs.controller_lock)
            hello = await asyncio.to_thread(transport.hello)
            controller = await jobs.takeover(c,uuid.uuid4().hex,hello['lane_generation']+1)
            hello = await queue.recover(c,controller)
            async def select(execution_id):
                row = await jobs.select(c,controller,hello['profiles'],max_timeout=hello['max_timeout'])
                if row and row['execution_id']!=execution_id:
                    raise RuntimeError('unexpected disposable queue contender')
                return row
            async def execute(row):
                receipt = await asyncio.to_thread(transport.submit,row['request'],threading.Event())
                await jobs.settle(c,row,receipt,controller)
                return receipt
            for seconds in (600,7200):
                ctx,_ = await new_run(seconds)
                execution_id = uuid.uuid4().hex
                try:
                    await jobs.admit(ctx,execution_id,job('raise RuntimeError("unauthorized launch")'))
                    denied = False
                except WorkerUnavailable:
                    denied = True
                await record('deny-'+str(seconds),denied,execution_id=execution_id)
                ctx,grant = await new_run(seconds,True)
                execution_id = uuid.uuid4().hex
                await jobs.admit(ctx,execution_id,job('print("authorized")'))
                row = await select(execution_id)
                receipt = await execute(row)
                account = dict(await pool.fetchrow('SELECT * FROM run_job_accounts WHERE lane=$1 AND owner=$2 AND run_id=$3',lane_id(),owner,ctx.run_id))
                await record('allow-'+str(seconds),row['selected_seconds']==seconds and receipt['outcome']=='succeeded' and account['dispatched']==account['completed']==1 and account['held']==0,
                    execution_id=execution_id,selected_seconds=row['selected_seconds'],effective_seconds=row['effective_seconds'],grant_id=grant['id'],receipt=receipt,account=account)
            for phase in ('queued','active','expired'):
                ctx,grant = await new_run(900,True)
                marker = Path(cfg['workspaces']['scratch'])/('s43c-revoke-'+uuid.uuid4().hex)
                heartbeat = marker.with_suffix('.heartbeat')
                code = "import os,time,pathlib; pathlib.Path("+repr(str(marker))+").write_text(str(os.getpid())+' '+pathlib.Path('/proc/self/stat').read_text().split()[21]); "+"exec("+repr("while True:\n pathlib.Path("+repr(str(heartbeat))+").write_text(str(time.monotonic())); time.sleep(.05)")+")"
                execution_id = uuid.uuid4().hex
                await jobs.admit(ctx,execution_id,job(code))
                if phase=='active':
                    row = await select(execution_id)
                    running = asyncio.create_task(asyncio.to_thread(transport.submit,row['request'],threading.Event()))
                    for _ in range(300):
                        if marker.exists(): break
                        await asyncio.sleep(.05)
                    if not marker.exists(): raise RuntimeError('active grant job never launched')
                if phase=='expired':
                    await pool.execute("UPDATE agent_execution_grants SET expires_at=clock_timestamp()-interval '1 second' WHERE lane=$1 AND owner=$2 AND id=$3",lane_id(),owner,grant['id'])
                else:
                    await store.revoke_grant(auth,dict(grant_id=grant['id'],expected_revision=1,request_key=uuid.uuid4().hex))
                if phase=='active':
                    await jobs.enforce_active(c,row)
                    await queue.control('cancel',controller,execution_id)
                    receipt = await asyncio.wait_for(running,30)
                    await jobs.settle(c,row,receipt,controller)
                    pid,start_time=marker.read_text().split()
                    await record('revoke-active',receipt['outcome']=='cancelled' and receipt['cleanup']=='idle_proved',execution_id=execution_id,pid=pid,pid_start_time=start_time,heartbeat=str(heartbeat))
                else:
                    refused = await select(execution_id)
                    await record('grant-'+phase,refused is None and not marker.exists(),execution_id=execution_id)
            ctx,_ = await new_run(1800)
            await pool.execute("UPDATE agent_runs SET deadline=clock_timestamp()+interval '2 seconds' WHERE lane=$1 AND owner=$2 AND id=$3",lane_id(),owner,ctx.run_id)
            execution_id=uuid.uuid4().hex
            await jobs.admit(ctx,execution_id,job('import time; time.sleep(100)'))
            row=await select(execution_id)
            receipt=await execute(row)
            try:
                await jobs.continue_timeout(ctx,execution_id,Reconciliation('a'*64,'b'*64,True,True),current_workspace_digest='a'*64)
                denied=False
            except WorkerUnavailable:
                denied=True
            await record('remaining-run-wall',row['selected_seconds']==1800 and 0<row['effective_seconds']<=2 and row['limiting_reason']=='run_wall' and receipt['outcome']=='timed_out' and denied,
                execution_id=execution_id,effective_seconds=row['effective_seconds'],receipt=receipt)
            # End fixture roots so a future real executor does not claim them.
            # Also release the synthetic 1-day leases: claim() counts live leases
            # toward the two-controller limit, which would block later modes.
            await pool.execute("UPDATE agent_runs SET status='cancelled',controller=NULL,lease_until=NULL WHERE lane=$1 AND owner=$2",lane_id(),owner)
            await c.execute('SELECT pg_advisory_unlock($1)',jobs.controller_lock)
        return rows
    finally:
        await pool.close()


def job_ladder(h, tiers):
    """Real elapsed deadlines, root-observed descendant identities and heartbeat.

    A shortened clock is not accepted for the preset proof. The three tiers can
    be invoked separately so the normal iteration gate need not wait 105 minutes.
    """
    from core.worker.contract import request_digest, profile_revision
    from core.worker import daemon as installed_daemon
    name=h.names[0]
    cfg=h.configs[name]
    for row in h.call({},mode='ladder-controls'):
        probe=row.pop('probe'); passed=row.pop('passed')
        h.record('s43c.'+probe,passed,**row)
        if 'pid' in row:
            pid=row['pid']; start=row['pid_start_time']
            current=Path('/proc',pid,'stat')
            alive=current.exists() and current.read_text().split()[21]==start
            heartbeat=Path(row['heartbeat'])
            before=heartbeat.read_bytes() if heartbeat.exists() else None
            time.sleep(.2)
            h.record('s43c.revoke-external-cleanup',not alive and before==(heartbeat.read_bytes() if heartbeat.exists() else None),pid=pid,pid_start_time=start)
    transport=lambda action,value=None: h.call(dict(action=action,**(value or {})),mode='protocol')
    hello=transport('hello')
    generation=hello['lane_generation']+1
    controller=uuid.uuid4().hex
    def control(op):
        return transport('control',dict(version=2,op=op,lane_generation=generation,controller_session=controller))
    h.record('s43c.installed-ladder',hello.get('job_time_presets')==[900,1800,3600] and hello['max_timeout']>=3600,
        kernel=OSRELEASE.read_text().strip(),profiles=hello['profiles'],max_timeout=hello['max_timeout'],
        daemon_sha256=hashlib.sha256(Path(installed_daemon.__file__).read_bytes()).hexdigest())
    for tier in tiers:
        if control('idle_barrier').get('cleanup')!='idle_proved':
            raise RuntimeError('preset probe needs proved idle')
        prefix=Path(cfg['workspaces']['scratch'])/('s43c-tier-'+uuid.uuid4().hex)
        daemon_pid=admin('systemctl','show','--property=MainPID','--value',f'bob-worker@{cfg["lane_id"]}.service').stdout.strip()
        cgroup=admin('systemctl','show','--property=ControlGroup','--value',f'bob-worker@{cfg["lane_id"]}.service').stdout.strip()
        code='''import os,time,pathlib,threading,ctypes
began=time.monotonic()
prefix=PREFIX
def heartbeat(index):
 os.setsid()
 if os.fork()!=0: os._exit(0)
 ctypes.CDLL(None).prctl(4,0,0,0,0)
 p=pathlib.Path(prefix+'.'+str(index))
 p.write_text(str(os.getpid())+' '+pathlib.Path('/proc/self/stat').read_text().split()[21])
 while True:
  pathlib.Path(str(p)+'.heartbeat').write_text(str(time.monotonic())); time.sleep(.05)
def fork_thread(index):
 if os.fork()==0: heartbeat(index)
threads=[threading.Thread(target=fork_thread,args=(i,)) for i in range(4)]
for t in threads: t.start()
for t in threads: t.join()
spawned=0
while True:
 if time.monotonic()-began>=TIER-3 and spawned<32:
  if os.fork()==0:
   time.sleep(120); os._exit(0)
  spawned+=1
 time.sleep(.01)
'''.replace('PREFIX',repr(str(prefix))).replace('TIER',str(tier))
        now=time.time(); execution_id=uuid.uuid4().hex
        wire=dict(version=2,op='submit',execution_id=execution_id,logical_job_id=execution_id,
            lane_generation=generation,controller_session=controller,profile='acceptance',
            profile_revision=profile_revision(cfg['profiles']['acceptance']),workspace='scratch',cwd='.',
            args=['-c',code],input='',timeout=tier,not_after=now+tier,submit_expires_at=now+20,output_limit=4194304)
        wire['request_digest']=request_digest(wire)
        result={}
        def run():
            try: result['receipt']=transport('submit',wire)
            except Exception as exc: result['error']=str(exc)[:500]
        started=time.monotonic()
        thread=threading.Thread(target=run); thread.start()
        markers=[Path(str(prefix)+'.'+str(i)) for i in range(4)]
        if not wait_until(lambda:all(p.exists() for p in markers),timeout=30):
            raise RuntimeError('preset descendants never became ready')
        identities=[p.read_text().split() for p in markers]
        alive_until_deadline=True
        observations=0
        # Observe externally at intervals, including the final predeadline seconds.
        while thread.is_alive() and time.monotonic()-started<tier-2:
            for pid,start in identities:
                stat=Path('/proc',pid,'stat')
                alive_until_deadline &= stat.exists() and stat.read_text().split()[21]==start
            observations+=1
            thread.join(timeout=min(1,max(.01,tier-2-(time.monotonic()-started))))
        thread.join(timeout=60)
        elapsed=time.monotonic()-started
        receipt=result.get('receipt',{})
        dead=all(not Path('/proc',pid).exists() or Path('/proc',pid,'stat').read_text().split()[21]!=start for pid,start in identities)
        beats=[p.with_name(p.name+'.heartbeat') for p in markers]
        before=[p.read_bytes() if p.exists() else None for p in beats]
        time.sleep(.2)
        stopped=before==[p.read_bytes() if p.exists() else None for p in beats]
        members=set((Path('/sys/fs/cgroup')/cgroup.lstrip('/')/'cgroup.procs').read_text().split())
        cgroup_clean=members <= {daemon_pid}
        h.record('s43c.preset.'+str(tier),not thread.is_alive() and alive_until_deadline and observations>0 and tier-2<=elapsed<=tier+30 and dead and stopped and cgroup_clean and receipt.get('outcome')=='timed_out' and receipt.get('cleanup')=='idle_proved',
            execution_id=execution_id,selected_seconds=tier,elapsed_seconds=elapsed,descendants=identities,
            external_samples=observations,alive_until_deadline=alive_until_deadline,heartbeat_stopped=stopped,
            worker_cgroup=cgroup,cgroup_members=sorted(members),cgroup_clean=cgroup_clean,result=result)
        generation+=1
        h.record('s43c.preset-idle.'+str(tier),control('idle_barrier').get('cleanup')=='idle_proved')


def worker_protocol(h):
    """4.3a installed gate. Includes one actual 900-second elapsed-time run."""
    from core.worker.contract import request_digest, profile_revision
    from core.worker import daemon as installed_daemon
    from core.execution import worker_queue as installed_queue
    name = h.names[0]
    cfg = h.configs[name]
    config_path = WORKER_CONFIGS / (cfg['lane_id'] + '.json')
    controller = uuid.uuid4().hex
    def protocol(action, value=None):
        return h.call({'action': action, **(value or {})}, mode='protocol')
    hello = protocol('hello')
    generation = max(2, hello['lane_generation'] + 1)
    def command(op, execution_id=None, gen=None):
        value = dict(version=2, op=op, lane_generation=generation if gen is None else gen, controller_session=controller)
        if execution_id:
            value['execution_id'] = execution_id
        return protocol('control', value)
    def barrier():
        deadline = time.monotonic() + 30
        while True:
            result = command('idle_barrier')
            if result['type'] == 'idle_barrier':
                return result
            if time.monotonic() >= deadline:
                raise RuntimeError('idle barrier timeout')
            time.sleep(.1)
    def submission(source, *, execution_id=None, timeout=900, output_limit=1048576):
        now = time.time()
        value = dict(version=2, op='submit', execution_id=execution_id or uuid.uuid4().hex,
                     logical_job_id=uuid.uuid4().hex, lane_generation=generation, controller_session=controller,
                     profile='acceptance', profile_revision=profile_revision(cfg['profiles']['acceptance']),
                     workspace='scratch', cwd='.', args=['-c', source], input='', timeout=timeout,
                     not_after=now+timeout, submit_expires_at=now+min(20,timeout), output_limit=output_limit)
        value['request_digest'] = request_digest(value)
        return value
    h.record('s43a.hello', hello['version'] == 2 and hello['max_timeout'] >= 3600 and hello.get('job_time_presets') == [900,1800,3600] and hello['max_output'] == 4194304,
             epoch=hello['daemon_epoch'], kernel=OSRELEASE.read_text().strip(), profiles=hello['profiles'],
             daemon_sha256=hashlib.sha256(Path(installed_daemon.__file__).read_bytes()).hexdigest(),
             queue_sha256=hashlib.sha256(Path(installed_queue.__file__).read_bytes()).hexdigest())
    barrier()
    missing = command('inspect', uuid.uuid4().hex)
    h.record('s43a.unknown-history', missing['state'] == 'unknown_history')
    try:
        command('cancel', 'old', gen=generation-1)
        denied = False
    except RuntimeError:
        denied = True
    h.record('s43a.stale-generation', denied)

    effect = Path(cfg['workspaces']['scratch']) / ('s43a-effect-' + uuid.uuid4().hex)
    source = 'from pathlib import Path; p=Path(' + repr(str(effect)) + "); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('ok')"
    request = submission(source)
    first = protocol('submit', request)
    second = protocol('submit', request)
    h.record('s43a.dedup-receipt', first == second and effect.read_text() == 'x', execution_id=request['execution_id'])
    conflict = {**request, 'input': 'changed'}
    conflict['request_digest'] = request_digest(conflict)
    try:
        protocol('submit', conflict)
        denied = False
    except RuntimeError:
        denied = True
    h.record('s43a.digest-conflict', denied and effect.read_text() == 'x')
    import sqlite3
    with sqlite3.connect(cfg['journal_path']) as database:
        database.execute('DELETE FROM executions WHERE id=?',(request['execution_id'],))
    missing = command('inspect',request['execution_id'])
    try:
        protocol('submit',request)
        refused = False
    except RuntimeError:
        refused = True
    h.record('s43a.partial-history-no-replay',refused and missing['state']=='unknown_history' and effect.read_text()=='x')
    unknown = command('cancel', 'previous-execution')
    h.record('s43a.stale-cancel-id', unknown['state'] == 'unknown_history')

    # Root-controlled deterministic fault points, never request flags. The live
    # core sees no queued job during these direct wire adversarial probes.
    for phase in ('after_admission','before_terminal','after_terminal'):
        fault_id = uuid.uuid4().hex
        effect.unlink(missing_ok=True)
        updated = {**cfg, 'acceptance_fault': {'phase': phase, 'execution_id': fault_id}}
        config_path.write_text(json.dumps(updated)); config_path.chmod(0o644)
        # Deliberate fault restarts must not trip systemd's start-rate limit.
        admin('systemctl','reset-failed',f'bob-worker@{cfg["lane_id"]}.service')
        admin('systemctl','restart',f'bob-worker@{cfg["lane_id"]}.service').check_returncode()
        generation = protocol('hello')['lane_generation'] + 1
        barrier()
        request = submission(source, execution_id=fault_id)
        try:
            protocol('submit', request)
        except RuntimeError:
            pass
        config_path.write_text(json.dumps(cfg)); config_path.chmod(0o644)
        # Deliberate fault restarts must not trip systemd's start-rate limit.
        admin('systemctl','reset-failed',f'bob-worker@{cfg["lane_id"]}.service')
        admin('systemctl','restart',f'bob-worker@{cfg["lane_id"]}.service').check_returncode()
        generation += 1
        barrier()
        inspected = command('inspect', fault_id)
        receipt = inspected.get('receipt', {})
        expected = 'succeeded' if phase == 'after_terminal' else 'unknown'
        h.record('s43a.crash.'+phase, receipt.get('outcome') == expected and receipt.get('cleanup') == 'idle_proved' and
                 (not effect.exists() if phase == 'after_admission' else effect.read_text() == 'x'), execution_id=fault_id, receipt=receipt)
        # Same execution ID remains a lookup, including after its expiry.
        try:
            protocol('submit', request)
            stale_rejected = False
        except RuntimeError:
            stale_rejected = True
        replay = command('inspect', fault_id)['receipt']
        h.record('s43a.crash.no-replay.'+phase, stale_rejected and replay['outcome'] == expected and
                 (not effect.exists() if phase == 'after_admission' else effect.read_text() == 'x'))

    # Every exit path at the current admitted bound. Observe PID/start-time and
    # heartbeat from root, beyond the worker namespace and its own claims.
    for exit_path in ('parent_exit','cancel','output_limit','daemon_crash','policy_loss','timeout'):
        marker = Path(cfg['workspaces']['scratch']) / ('s43a-descendant-' + uuid.uuid4().hex)
        heartbeat = marker.with_suffix('.heartbeat')
        code = '''import os,time,pathlib,ctypes,threading
marker=pathlib.Path(MARKER); heartbeat=pathlib.Path(HEARTBEAT)
def fork_thread():
 if os.fork()==0:
  os.setsid()
  if os.fork()!=0: os._exit(0)
  ctypes.CDLL(None).prctl(4,0,0,0,0)
  marker.write_text(str(os.getpid())+' '+pathlib.Path('/proc/self/stat').read_text().split()[21])
  while True:
   heartbeat.write_text(str(time.monotonic())); time.sleep(.05)
thread=threading.Thread(target=fork_thread); thread.start(); thread.join()
while not marker.exists(): time.sleep(.01)
print('ready',flush=True)
ACTION
'''.replace('MARKER',repr(str(marker))).replace('HEARTBEAT',repr(str(heartbeat)))
        action = "print('x'*5000000,flush=True)" if exit_path == 'output_limit' else 'pass' if exit_path == 'parent_exit' else 'time.sleep(1000)'
        code = code.replace('ACTION', action)
        request = submission(code, output_limit=4096 if exit_path == 'output_limit' else 1048576)
        results = {}
        def run():
            try:
                results['receipt'] = protocol('submit', request)
            except RuntimeError as exc:
                results['error'] = str(exc)[:200]
        thread = threading.Thread(target=run)
        started = time.monotonic()
        thread.start()
        if not wait_until(marker.exists, timeout=30):
            raise RuntimeError('descendant readiness missing: ' + exit_path)
        pid, start_time = marker.read_text().split()
        if exit_path == 'cancel':
            stale = command('cancel','previous-execution')
            try:
                command('cancel',request['execution_id'],gen=generation-1)
                stale_denied = False
            except RuntimeError:
                stale_denied = True
            h.record('s43a.active-stale-cancel',stale.get('state')=='unknown_history' and stale_denied and Path('/proc',pid).exists())
            command('cancel', request['execution_id'])
        elif exit_path == 'daemon_crash':
            admin('systemctl','kill','--kill-whom=main','--signal=SIGKILL',f'bob-worker@{cfg["lane_id"]}.service').check_returncode()
        elif exit_path == 'policy_loss':
            admin('nft','delete','table','inet','bob_w_'+cfg['lane_id']).check_returncode()
        thread.join(timeout=950)
        elapsed = time.monotonic() - started
        if thread.is_alive():
            raise RuntimeError('execution did not terminate: ' + exit_path)
        dead = wait_until(lambda: not Path('/proc',pid).exists(), timeout=15)
        before = heartbeat.read_bytes() if heartbeat.exists() else None
        time.sleep(.2)
        stopped = before == (heartbeat.read_bytes() if heartbeat.exists() else None)
        if exit_path == 'policy_loss':
            admin('systemctl','restart',f'bob-worker-policy@{cfg["lane_id"]}.service').check_returncode()
            admin('systemctl','start',f'bob-worker@{cfg["lane_id"]}.socket',f'bob-worker-auth@{cfg["lane_id"]}.socket').check_returncode()
        if exit_path in {'daemon_crash','policy_loss'}:
            # Deliberate fault restarts must not trip systemd's start-rate limit.
            admin('systemctl','reset-failed',f'bob-worker@{cfg["lane_id"]}.service')
            admin('systemctl','restart',f'bob-worker@{cfg["lane_id"]}.service').check_returncode()
        generation += 1
        idle = barrier()
        receipt = command('inspect',request['execution_id']).get('receipt',{})
        expected = 'succeeded' if exit_path == 'parent_exit' else 'cancelled' if exit_path == 'cancel' else 'timed_out' if exit_path == 'timeout' else 'unknown' if exit_path in {'daemon_crash','policy_loss'} else 'failed'
        h.record('s43a.exit.'+exit_path, dead and stopped and receipt.get('outcome') == expected and
                 idle['cleanup'] == 'idle_proved' and (898 <= elapsed <= 930 if exit_path == 'timeout' else True),
                 execution_id=request['execution_id'], pid=pid, pid_start_time=start_time, elapsed_seconds=elapsed,
                 outcome=receipt.get('outcome'), cleanup=receipt.get('cleanup'))

    # Journal protection includes /proc aliases into the helper namespace.
    helper_pid = admin('systemctl','show','--property=MainPID','--value',f'bob-worker-journal@{cfg["lane_id"]}.service').stdout.strip()
    executor_pid = admin('systemctl','show','--property=MainPID','--value',f'bob-worker@{cfg["lane_id"]}.service').stdout.strip()
    code = '''import pathlib,json
paths=PATHS; denied={}
for path in paths:
 try: pathlib.Path(path).read_bytes(); denied[path]=False
 except OSError: denied[path]=True
print(json.dumps(denied))'''.replace('PATHS',repr([cfg['journal_path'],f'/proc/{helper_pid}/root'+cfg['journal_path'],
    f'/proc/{executor_pid}/root/etc/bob/workers/{cfg["lane_id"]}.json']))
    request = submission(code)
    result = protocol('submit',request)
    retained_execution = request['execution_id']
    denied = json.loads(base64.b64decode(result['stdout_b64']))
    h.record('s43a.journal-child-denial', all(denied.values()), checks=denied)
    code = '''import os,errno,json
try: os.kill(os.getppid(),0); denied=False
except OSError as exc: denied=exc.errno==errno.EPERM
print(json.dumps({'supervisor_signal_denied':denied}))'''
    result = protocol('submit',submission(code))
    h.record('s43a.supervisor-signal-denial',json.loads(base64.b64decode(result['stdout_b64']))['supervisor_signal_denied'])

    # Core intent/reply windows use the real ledger and process death; recovery
    # is performed by the independently running core queue after lock loss.
    for phase in ('before_send','after_reply'):
        execution_id = uuid.uuid4().hex
        result = admin('runuser','-u',cfg['service_user'],'--',PYTHON,'-I',h.script,'--child',name,'crash-window',
                       input=json.dumps({'execution_id':execution_id,'phase':phase,'request':payload('python','print("effect")')}),timeout=60)
        h.record('s43a.core-crash.'+phase+'.injected',result.returncode == 86,execution_id=execution_id)
        row = {}
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            row = h.call({'execution_id':execution_id},mode='job-audit')
            if row['outcome'] in {'succeeded','unknown'}:
                break
            time.sleep(.1)
        h.record('s43a.core-crash.'+phase+'.reconciled',row.get('outcome') == ('unknown' if phase == 'before_send' else 'succeeded') and row.get('cleanup') == 'idle_proved',row=row)

    daemon_pid = admin('systemctl','show','--property=MainPID','--value',f'bob-worker@{cfg["lane_id"]}.service').stdout.strip()
    observations = {'max_direct_children':0, 'samples_with_children':0, 'identities':set(), 'daemon_missing':False}
    stop_observer = threading.Event()
    def observe_processes():
        while not stop_observer.is_set():
            try:
                children = Path(f'/proc/{daemon_pid}/task/{daemon_pid}/children').read_text().split()
                identities = set()
                for pid in children:
                    try:
                        fields = Path('/proc',pid,'stat').read_text().rsplit(')',1)[1].split()
                        if fields[1] == daemon_pid:
                            identities.add((pid,fields[19]))
                    except FileNotFoundError:
                        pass
                observations['max_direct_children'] = max(observations['max_direct_children'],len(identities))
                observations['samples_with_children'] += bool(identities)
                observations['identities'].update(identities)
            except FileNotFoundError:
                observations['daemon_missing'] = True
            stop_observer.wait(.01)
    observer = threading.Thread(target=observe_processes)
    observer.start()
    try:
        contention = h.call({}, mode='queue-contention')
    finally:
        stop_observer.set(); observer.join(timeout=5)
    order = [r['caller_group'] for r in contention['dispatch_order']]
    h.record('s43a.admission-and-visible-wait',contention['bound_enforced'] and contention['waiting_releases_slot'] and
             contention['wait_state']=='waiting_for_lane_worker', wait_observed_at=contention['wait_observed_at'])
    observations['identities'] = sorted(observations['identities'])
    h.record('s43a.contention',all(r['ok'] for r in contention['results']) and contention['no_overlap'] and len(order)==12 and len(set(order[:6]))==6 and
             observations['max_direct_children']==1 and observations['samples_with_children']>0 and not observations['daemon_missing'],
             dispatch_order=order, executions=contention['results'], external_observations=observations)
    for role in ('gateway','core'):
        admin('systemctl','restart',f'bob-{role}@{name}.service').check_returncode()
        h.record('s43a.restart.'+role,h.call(payload('python','print("ok")'))['stdout'].strip() == 'ok')
    active_marker = Path(cfg['workspaces']['scratch']) / ('s43a-core-owned-'+uuid.uuid4().hex)
    active_heartbeat = active_marker.with_suffix('.heartbeat')
    code = '''import pathlib,os,time
pathlib.Path(MARKER).write_text(str(os.getpid()))
while True:
 pathlib.Path(HEARTBEAT).write_text(str(time.monotonic())); time.sleep(.05)
'''.replace('MARKER',repr(str(active_marker))).replace('HEARTBEAT',repr(str(active_heartbeat)))
    execution_id = uuid.uuid4().hex
    h.call({'execution_id':execution_id,'request':payload('python',code,timeout=900)},mode='queue-start')
    if not wait_until(lambda: active_marker.exists() and active_heartbeat.exists(),timeout=30):
        raise RuntimeError('core did not dispatch durable queue admission')
    pid = active_marker.read_text()
    before = active_heartbeat.read_bytes()
    admin('systemctl','restart',f'bob-gateway@{name}.service').check_returncode()
    time.sleep(.2)
    h.record('s43a.active-gateway-restart',Path('/proc',pid).exists() and active_heartbeat.read_bytes()!=before,execution_id=execution_id,pid=pid)
    admin('systemctl','restart',f'bob-core@{name}.service').check_returncode()
    deadline = time.monotonic()+30
    while True:
        row = h.call({'execution_id':execution_id},mode='job-audit')
        if row['outcome'] in {'cancelled','unknown'} or time.monotonic()>deadline:
            break
        time.sleep(.1)
    h.record('s43a.active-core-restart',row['outcome'] in {'cancelled','unknown'} and row['cleanup']=='idle_proved' and
             not Path('/proc',pid).exists(),row=row,pid=pid)
    marker = FAMILY / name / 'bob-memory/acceptance-worker-protocol.json'
    marker.write_text(json.dumps({'execution_id':retained_execution, 'daemon_epoch':protocol('hello')['daemon_epoch']}))
    marker.chmod(0o600)


def arm_protocol_reboot(h):
    """Leave one admitted job queued with no controller until the real reboot."""
    name = h.names[0]
    cfg = h.configs[name]
    admin('systemctl','stop',f'bob-core@{name}.service').check_returncode()
    effect = Path(cfg['workspaces']['scratch']) / ('s43a-reboot-effect-'+uuid.uuid4().hex)
    source = 'from pathlib import Path; p=Path('+repr(str(effect))+"); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    execution_id = uuid.uuid4().hex
    row = h.call({'execution_id':execution_id,'request':payload('python',source)},mode='queue-start')
    marker = FAMILY / name / 'bob-memory/acceptance-worker-protocol.json'
    saved = json.loads(marker.read_text())
    saved.update(queued_execution_id=execution_id,queued_effect=str(effect))
    marker.write_text(json.dumps(saved)); marker.chmod(0o600)
    h.record('s43a.reboot.queue-armed',row['outcome']=='queued' and not effect.exists(),execution_id=execution_id,
             core_stopped_until_reboot=name)


def settings_acceptance(names):
    """S3d, synthetic credentials only; works on fresh or old isolated test lanes.

    Does not downgrade a split lane to manufacture migration evidence. To prove
    upgrade, run against harness-created lanes from the previous isolated image.
    """
    from common import read_env
    from worker import load_identity
    failures = []
    def record(name, passed, **details):
        if not emit('s3d.'+name, passed, **details):
            failures.append(name)
    # A subprocess receives secrets through stdin, never argv, and prints only
    # projected replies. It runs under the installed identity with no root env.
    source = '''
import json,os,socket,sys
sys.path.insert(0,'/opt/bob/bobclaw-core')
from core.settings_client import call
p=json.load(sys.stdin)
os.environ.update(BOB_SETTINGS_ID=p['lane_id'],BOB_GATEWAY_UID=str(p['gateway_uid']),BOB_EXECUTION_BOUNDARY='worker')
try:
 if p.get('raw'):
  path='/run/bob-settings/'+p['lane_id']+('/receipt.sock' if p.get('receipt') else '/control.sock')
  with socket.socket(socket.AF_UNIX) as s:
   s.settimeout(10); s.connect(path)
   s.sendall(json.dumps(p['request']).encode()+b'\\n')
   with s.makefile('rb') as stream: result=json.loads(stream.readline(32769))
  print(json.dumps({'denied':True} if 'error' in result else result))
 else:
  print(json.dumps(call(p['request'],receipt=p.get('receipt',False))))
except Exception:
 print(json.dumps({'denied':True}))
'''
    for name in names:
        root = FAMILY / name / 'bob-memory'
        ids = load_identity(name)
        before = {k: ids[k] for k in ('service_uid', 'worker_uid')}
        upgraded = 'gateway_uid' not in ids
        if upgraded:
            result = admin('bob-runtime', 'migrate-workers', '--lane', name, timeout=600)
            record('migration.'+name, result.returncode == 0)
            if result.returncode:
                continue
            ids = load_identity(name)
            record('migration-retained-identities.'+name, all(ids[k] == v for k,v in before.items()))
            journal = json.loads((Path('/var/lib/bob-worker-migrations') / name / 'state.json').read_text())
            record('migration-journal.'+name, journal.get('settings_version') == 1 and journal.get('activation_complete') is True)
        record('identity-split.'+name, len({ids[k+'_uid'] for k in ('service','worker','gateway','bridge')}) == 4,
               migration_exercised=upgraded)
        key = 'S3D-CANARY-'+secrets.token_hex(20)
        request_key = uuid.uuid4().hex
        body = dict(version=1, op='secrets.set', provider='deepseek', key=key, request_key=request_key)
        def rpc(user, request, receipt=False, raw=False):
            result = admin('runuser','-u',user,'--',PYTHON,'-I','-c',source,
                           input=json.dumps(dict(lane_id=ids['lane_id'],gateway_uid=ids['gateway_uid'],request=request,receipt=receipt,raw=raw)))
            assert key not in result.stdout+result.stderr
            if result.returncode:
                return {'failed': True}
            return json.loads(result.stdout)
        # Observe service identities independently via /proc, not config alone.
        pids = {}
        for role in ('core','gateway','bridge'):
            pid = int(admin('systemctl','show','-p','MainPID','--value',f'bob-{role}@{name}.service').stdout.strip())
            pids[role] = pid
            expected = ids[('service' if role == 'core' else role)+'_uid']
            record('installed-uid.'+name+'.'+role, pid > 0 and Path('/proc/'+str(pid)).stat().st_uid == expected)
        providers = (root / 'providers.env').read_bytes() if (root / 'providers.env').exists() else None
        lane_env = (root / 'lane.env').read_bytes()
        result = rpc(ids['gateway_user'], body)
        record('enrollment-no-gateway-restart.'+name,
            int(admin('systemctl','show','-p','MainPID','--value',f'bob-gateway@{name}.service').stdout.strip()) == pids['gateway'])
        record('enrollment-core-restart.'+name,
            int(admin('systemctl','show','-p','MainPID','--value',f'bob-core@{name}.service').stdout.strip()) != pids['core'])
        record('stored.'+name, result.get('stored') is True and result.get('fingerprint') == hashlib.sha256(key.encode()).hexdigest()[:16]
               and set(result) == {'provider','stored','fingerprint','operation_id'})
        record('single-use.'+name, rpc(ids['gateway_user'], body).get('denied') is True)
        for role in ('service','bridge','worker'):
            record('control-denied.'+name+'.'+role, rpc(ids[role+'_user'], body, raw=True).get('denied') is True)
        record('control-denied.'+name+'.bobapp', rpc('bobapp', body, raw=True).get('denied') is True)
        vault = Path('/var/lib/bob-settings') / ids['lane_id'] / 'vault/deepseek.json'
        record('vault-exact-value.'+name, json.loads(vault.read_text()).get('key') == key)
        read_source = 'import json,sys; p=sys.argv[1]\ntry:\n json.load(open(p)); print("read")\nexcept PermissionError:\n print("denied")'
        for role in ('service','gateway','bridge','worker','bobapp'):
            probe = admin('runuser','-u',role if role == 'bobapp' else ids[role+'_user'],'--',PYTHON,'-I','-c',read_source,str(vault))
            record('vault.'+name+'.'+role, probe.returncode == 0 and probe.stdout.strip() == ('read' if role == 'service' else 'denied'))
        code = ('import json,socket\nresults=[]\n'
                'for p in '+repr([str(vault), '/run/bob-settings/'+ids['lane_id']+'/control.sock'])+':\n'
                ' try:\n'
                '  if p.endswith(".sock"):\n   s=socket.socket(socket.AF_UNIX); s.connect(p); s.close()\n'
                '  else:\n   open(p).read()\n'
                ' except (PermissionError,FileNotFoundError):\n  results.append(True)\n'
                ' else:\n  results.append(False)\n'
                'print(json.dumps(results))')
        probe = admin('runuser','-u',ids['service_user'],'--',PYTHON,'-I',str(Path(__file__).resolve()),
                      '--child',name,'job',input=json.dumps(payload('python',code)),timeout=90)
        try:
            worker_result = json.loads(probe.stdout)
            denied = worker_result['returncode'] == 0 and json.loads(worker_result['stdout']) == [True,True]
        except (ValueError,KeyError):
            denied = False
        record('installed-worker-denied.'+name, probe.returncode == 0 and denied)
        # Worker namespace/child seccomp exclusion in addition to UID denial.
        # Existing baseline probes exercise the real sandbox; direct UID check
        # here ensures the control socket itself does not grant that identity.
        record('receipt-no-write.'+name, rpc('bobapp', body, True, raw=True).get('denied') is True)
        status = rpc('bobapp', dict(version=1,op='status',request_key=request_key), True)
        record('receipt.'+name, status.get('phase') == 'ready' and status.get('result') == result)
        # Gateway outage reconciliation proves the receipt path is independent.
        admin('systemctl','stop',f'bob-gateway@{name}.service').check_returncode()
        try:
            record('gateway-down-receipt.'+name, rpc('bobapp',dict(version=1,op='status',request_key=request_key),True) == status)
        finally:
            admin('systemctl','start',f'bob-gateway@{name}.service').check_returncode()
            from migration import wait_ready
            wait_ready(read_env(root / 'lane.env'))
        from core.lane_config import read_lane, read_env as parse_env, provider_setup
        enrolled = read_lane(root, values=parse_env(root / 'lane.env'))
        prior = dict(line.split('=', 1) for line in lane_env.decode().splitlines() if '=' in line)
        expected_default = prior.get('BOB_LANE_DEFAULT_BACKEND', '').strip('"') or 'deepseek_v4_flash'
        record('preset-enrolled.'+name, enrolled.backends['deepseek_v4_flash'].available
            and enrolled.default_backend == expected_default
            and enrolled.backends['deepseek_v4_flash'].model == (prior.get('DEEPSEEK_MODEL', '').strip('"') or provider_setup()['deepseek']['model']))
        record('vault-only-no-env-copy.'+name,
            ((root / 'providers.env').read_bytes() if (root / 'providers.env').exists() else None) == providers)
        record('enrollment-no-bridge-restart.'+name,
            int(admin('systemctl','show','-p','MainPID','--value',f'bob-bridge@{name}.service').stdout.strip()) == pids['bridge'])
        # Exercise actual HTTP owner step-up and write-only projection as well.
        # Never print captured response bodies or factor-bearing requests.
        import pyotp
        env = read_env(root / 'lane.env')
        portal = Path('/var/lib/bob-portal') / (name+'.token')
        headers = {'Authorization':'Bearer '+portal.read_text().strip(),
                   'X-Bob-Broker-Link':uuid.uuid4().hex+uuid.uuid4().hex, 'Content-Type':'application/json'}
        def http_request(path, body, request_headers):
            connection = http.client.HTTPConnection('127.0.0.1',int(env['BOBCLAW_GATEWAY_PORT']),timeout=15)
            try:
                connection.request('POST',path,body=json.dumps(body),headers=request_headers)
                response = connection.getresponse()
                raw = response.read(65536)
                assert key.encode() not in raw
                return response.status,json.loads(raw)
            finally:
                connection.close()
        code, challenge = http_request('/auth/owner/begin', {'operation':'secrets.set','resource':{'provider':'deepseek'}},headers)
        if code == 200:
            code, confirmation = http_request('/auth/owner/confirm', {'challenge_id':challenge['challenge_id'],
                'password':env['BOBCLAW_PASSWORD'],'totp':pyotp.TOTP(env['TOTP_SECRET']).now()},headers)
        if code == 200 and confirmation.get('owner_bearer'):
            import jwt
            claims = jwt.decode(headers['Authorization'][7:],options={'verify_signature':False})
            owner = {**headers,'Authorization':'Bearer '+confirmation['owner_bearer'],
                     'X-Bob-Portal-Credential':claims['jti']}
            code, stored = http_request('/settings/secrets',{'contract_version':2,'provider':'deepseek','key':key,
                                                    'request_key':uuid.uuid4().hex},owner)
            record('http-stored.'+name, code == 200 and stored.get('stored') is True and
                   set(stored) == {'provider','stored','fingerprint','operation_id'})
            code, refused = http_request('/settings/secrets',{'contract_version':2,'provider':'deepseek','key':key,
                                                      'request_key':uuid.uuid4().hex,'unit':key},owner)
            record('http-safe-error.'+name, code == 422 and key not in json.dumps(refused))
        else:
            record('http-owner-stepup.'+name, False, status=code)
        # Canary in a malformed body must not enter helper errors or journals.
        record('override-refused.'+name, rpc(ids['gateway_user'],{**body,'request_key':uuid.uuid4().hex,'path':key},raw=True).get('denied') is True)
        log = admin('journalctl','--no-pager','--since','-10min','-u',f'bob-settings-control@{ids["lane_id"]}.service',
                    '-u',f'bob-gateway@{name}.service')
        record('logs-scrubbed.'+name, key not in log.stdout+log.stderr)
        state = Path('/var/lib/bob-settings') / ids['lane_id']
        record('journals-scrubbed.'+name, all(key not in p.read_text() for p in state.glob('*.json')))
    emit('overall',not failures,failures=failures)
    return int(bool(failures))


def scoped_workspaces(h):
    """4.3c real installed-child confinement and immutable API/broker readback.

    Uses a synthetic paused/completed run for readback, never a provider or
    project-code execution. The existing queue/ladder suites remain required.
    """
    from core.worker.contract import request_digest, profile_revision
    from core.execution.workspaces import snapshot
    name, other_name = h.names
    cfg, other = h.configs[name], h.configs[other_name]
    root = FAMILY / name / 'bob-memory'
    parent = Path(cfg.get('run_workspace_root', '/nonexistent'))
    if not parent.is_dir() or not parent.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('installed run_workspace_root required; reprovision disposable lanes')
    identifier, sibling_id = uuid.uuid4().hex, uuid.uuid4().hex
    selected, sibling = parent / identifier, parent / sibling_id
    for directory in (selected, sibling):
        directory.mkdir(mode=0o770)
        os.chown(directory,cfg['service_uid'],cfg['worker_gid']);directory.chmod(0o2770)
    project = root / 'projects' / ('s43d-' + identifier)
    project.mkdir(mode=0o770)
    os.chown(project,cfg['service_uid'],cfg['worker_gid']);project.chmod(0o2770)
    for directory in (project,sibling):
        (directory/'protected.txt').write_text('S43D_PROTECTED_'+identifier)
        os.chown(directory/'protected.txt',cfg['service_uid'],cfg['worker_gid'])
        (directory/'protected.txt').chmod(0o660)
    (selected/'base.txt').write_text('registered immutable input')
    os.chown(selected/'base.txt',cfg['service_uid'],cfg['worker_gid']);(selected/'base.txt').chmod(0o660)
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (project/'protected.txt',sibling/'protected.txt')}
    # Positive DAC control outside the confined service namespace. Both files
    # really are readable/writable by this UID before Landlock is inherited.
    positive=admin('runuser','-u',cfg['worker_user'],'--',PYTHON,'-c',
                   'import os,sys; assert all(os.access(p,os.R_OK|os.W_OK) for p in sys.argv[1:])',*before)
    h.record('s43d.dac-positive-control',positive.returncode==0)
    readonly = parent / uuid.uuid4().hex
    readonly.mkdir(mode=0o770)
    os.chown(readonly,cfg['service_uid'],cfg['worker_gid']);readonly.chmod(0o2770)
    (readonly/'input.txt').write_text('declared read-only')
    os.chown(readonly/'input.txt',cfg['service_uid'],cfg['worker_gid']);(readonly/'input.txt').chmod(0o660)
    cfg.setdefault('readonly_inputs',{})['run_'+identifier]=[str(readonly)]
    installed=WORKER_CONFIGS/(cfg['lane_id']+'.json')
    installed.write_text(json.dumps(cfg));installed.chmod(0o644)
    admin('systemctl','restart','bob-worker@'+cfg['lane_id']+'.service').check_returncode()
    source = '''import json,os,socket,sys
from pathlib import Path
w=Path.cwd(); results={}
results['declared-read']=Path(READONLY,'input.txt').read_text()=='declared read-only'
try: Path(READONLY,'input.txt').write_text('forbidden')
except OSError: results['declared-write-denied']=True
else: results['declared-write-denied']=False
(w/'allowed.txt').write_text('allowed')
results['selected-write']=(w/'allowed.txt').read_text()=='allowed'
(w/'escape-link').symlink_to(PROJECT+'/protected.txt')
for label,path in {'host-project':PROJECT+'/protected.txt','other-workspace':SIBLING+'/protected.txt',
                   'parent':str(w/'..'/SIBLING_ID/'protected.txt'),'symlink':str(w/'escape-link'),
                   'proc-root':'/proc/self/root'+PROJECT+'/protected.txt','other-lane':OTHER,
                   'journal':JOURNAL}.items():
 for mode in ('r','w'):
  try:
   with open(path,mode) as f: f.read(1) if mode=='r' else f.write('ESCAPED')
  except OSError: results[label+'-'+mode]=True
  else: results[label+'-'+mode]=False
try: os.link(PROJECT+'/protected.txt',w/'hardlink')
except OSError: results['hardlink']=True
else: results['hardlink']=False
for op in ('decide','apply'):
 try:
  with socket.create_connection(('127.0.0.1',GATEWAY),timeout=2) as sock:
   sock.sendall(('POST /api/tool-jobs/'+JOB+('/proposal/decision' if op=='decide' else '/apply')+' HTTP/1.0\\r\\n\\r\\n').encode())
   results['worker-cannot-'+op]=False
 except OSError: results['worker-cannot-'+op]=True
(w/'escape-link').unlink()
print(json.dumps(results))'''
    replacements={'PROJECT':str(project),'SIBLING':str(sibling),'SIBLING_ID':sibling_id,
                  'OTHER':other['run_workspace_root']+'/private/protected.txt',
                  'READONLY':str(readonly),'JOURNAL':cfg['journal_path'],'GATEWAY':int(h.envs[name]['BOBCLAW_GATEWAY_PORT']),'JOB':identifier}
    # Replace whole identifiers only; values are fixture data, never code input.
    import re
    source=re.sub(r'\b(PROJECT|SIBLING_ID|SIBLING|OTHER|JOURNAL|GATEWAY|JOB|READONLY)\b',lambda m:repr(replacements[m[0]]),source)
    hello=h.call({'action':'hello'},mode='protocol')
    h.record('s43d.landlock-abi',hello.get('filesystem',{}).get('available') and hello['filesystem']['abi']>=3,
             filesystem=hello.get('filesystem'),kernel=OSRELEASE.read_text().strip())
    generation,controller=hello['lane_generation']+1,uuid.uuid4().hex
    control=dict(version=2,lane_generation=generation,controller_session=controller)
    barrier=h.call({'action':'control',**control,'op':'idle_barrier'},mode='protocol')
    h.record('s43d.idle-before-probe',barrier.get('cleanup')=='idle_proved')
    now=time.time()
    wire=dict(version=2,op='submit',execution_id=uuid.uuid4().hex,logical_job_id=identifier,
              lane_generation=generation,controller_session=controller,profile='acceptance',
              profile_revision=profile_revision(cfg['profiles']['acceptance']),workspace='run_'+identifier,cwd='',
              args=['-c',source],input='',timeout=30,output_limit=65536,not_after=now+30,submit_expires_at=now+20)
    wire['request_digest']=request_digest(wire)
    # A request-local filesystem refusal must be terminal and durable, never a
    # daemon restart/unknown outcome. The valid confined job below must still run.
    epoch=hello['daemon_epoch']
    for label,workspace,cwd in [('missing-run','run_'+uuid.uuid4().hex,''),
                                ('invalid-run','run_invalid',''),
                                ('missing-cwd','run_'+identifier,'missing'),
                                ('foreign-workspace','foreign','')]:
        now=time.time()
        refused={**wire,'execution_id':uuid.uuid4().hex,'workspace':workspace,'cwd':cwd,
                 'not_after':now+30,'submit_expires_at':now+20,
                 'args':['-c',"raise AssertionError('refused request launched')"]}
        refused['request_digest']=request_digest(refused)
        receipt=h.call({'action':'submit',**refused},mode='protocol')
        replay=h.call({'action':'submit',**refused},mode='protocol')
        inspection=h.call({'action':'control',**control,'op':'inspect',
                           'execution_id':refused['execution_id']},mode='protocol')
        current=h.call({'action':'hello'},mode='protocol')
        h.record('s43d.resolution-refused.'+label,
                 receipt.get('outcome')=='failed' and receipt.get('cleanup')=='idle_proved'
                 and receipt.get('execution_seconds')==0 and bool(receipt.get('reason'))
                 and replay==receipt and inspection.get('receipt',{}).get('request_digest')==refused['request_digest']
                 and current['daemon_epoch']==epoch,execution_id=refused['execution_id'])
    # Keep the real confinement probe's short submission window independent of
    # the refusal/replay checks' runuser and service-start overhead.
    now=time.time();wire.update(not_after=now+30,submit_expires_at=now+20)
    wire['request_digest']=request_digest(wire)
    result=h.call({'action':'submit',**wire},mode='protocol')
    # protocol mode returns the terminal receipt and accumulated stdout.
    stdout=result.get('stdout','')
    if not stdout and result.get('stdout_b64'): stdout=base64.b64decode(result['stdout_b64']).decode()
    checks=json.loads(stdout)
    h.record('s43d.real-confinement',result.get('outcome')=='succeeded' and bool(checks) and all(checks.values()),
             checks=checks,execution_id=wire['execution_id'])
    h.record('s43d.protected-hashes',all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==sha for p,sha in before.items()))
    # Install a dated operator-only project registration for the synthetic readback.
    from core.portal_resources import register_project_folder
    pid=str(uuid.uuid4())
    folder=register_project_folder(root,h.envs[name]['BOB_DEFAULT_OWNER'],pid,
                                    str(project.relative_to(root)),'S43d disposable fixture')
    fixture=h.call({'folder':folder,'workspace':identifier},mode='scoped-api-fixture')
    h.record('s43d.immutable-service-readback',fixture['immutable'] and fixture['digest_bound'],
             job_id=fixture['id'],base_digest=fixture['base_digest'],output_digest=fixture['output_digest'])
    # Use the real shipped broker as bobapp. No bearer is put in argv or files.
    from lane import PORTAL
    token=(PORTAL/(name+'.token')).read_text().strip()
    broker_code='''import importlib.util,json,sys
from types import SimpleNamespace
value=json.load(sys.stdin)
spec=importlib.util.spec_from_file_location('installed_broker','/opt/bob/portal-broker/portal-broker.py')
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
t=m.LiveTransport(value['gateway'],value['token'])
b=m.Broker(t,{'sub':value['owner'],'token_type':'portal'},SimpleNamespace())
j=value['id'];name=value['artifact'];out={}
for op,args in [('attachments',{}),('list',{}),('audit',{'job_id':j}),('proposal',{'job_id':j}),
                ('artifact',{'job_id':j,'filename':name,'max_bytes':32}),('cancel',{'job_id':j})]:
 out[op]=b.handle({'id':1,'op':'jobs.'+op,'args':args}).value
for op,args in [('start',{'role':'coder','task':'must not execute','wall_seconds':120}),
                ('decide',{'contract_version':2,'job_id':j,'decision':'approve','proposal_digest':value['digest'],
                           'request_key':'acceptance-denied','expected_revision':1})]:
 try: b.handle({'id':1,'op':'jobs.'+op,'args':args})
 except m.BrokerError as e: out[op]={'error':e.code}
 else: raise AssertionError('ordinary broker obtained mutation authority')
print(json.dumps({'operations':sorted(out),'artifact':out['artifact'],'proposal':out['proposal']['digest'],
                  'start':out['start'],'decide':out['decide']}))'''
    reply=admin('runuser','-u','bobapp','--',PYTHON,'-I','-c',broker_code,input=json.dumps({
        'gateway':'http://127.0.0.1:'+h.envs[name]['BOBCLAW_GATEWAY_PORT'],'token':token,'owner':folder['owner'],
        'id':fixture['id'],'artifact':fixture['artifact'],'digest':fixture['proposal_digest']}))
    if reply.returncode: raise RuntimeError('installed broker fixture failed: '+reply.stderr[-600:])
    data=json.loads(reply.stdout)
    h.record('s43d.broker-api-parity',len(data['operations'])==8 and data['proposal']==fixture['proposal_digest']
             and data['artifact']['sha256']==fixture['artifact_sha256'] and data['artifact']['truncated']
             and data['decide']['error']=='owner_confirmation_required' and data['start']['error']=='client_upgrade_required')


async def scoped_api_fixture(cfg, options):
    """Synthetic immutable resource in the real lane DB; no executable run admitted."""
    import asyncpg
    from core.config import config
    from core.execution.api import lane_id,policy_epoch
    from core.execution.admission import Admission
    from core.execution.models import Authority,TOPOLOGY,canonical
    from core.execution.store import Store
    from core.execution.workspaces import snapshot,proposal
    from core.tool_jobs.service import Jobs
    folder=options['folder'];owner=folder['owner'];lane=lane_id()
    pool=await asyncpg.create_pool(config.POSTGRES_URL,min_size=1,max_size=6)
    store=Store(pool,lane)
    await store.initialize()
    from core.execution.owner import current_epoch
    auth=Authority(lane,owner,owner,{},'human',policy_epoch=policy_epoch(config.BOBCLAW_SECRET),
                   credential_epoch=await current_epoch(lane,owner))
    team_id='s43d-'+uuid.uuid4().hex[:12]
    team={'schema_version':1,'id':team_id,'topology':TOPOLOGY,'roles':{'manager':'fixture','worker':'fixture','critic':'fixture'}}
    jobs=Jobs(Admission(store,None))
    identifier=uuid.uuid4().hex
    run_workspace=Path(cfg['run_workspace_root'])/options['workspace']
    base=snapshot(Path(os.environ['BOB_LANE_ROOT'])/folder['path'])
    output=snapshot(run_workspace)
    try:
        async with store.transaction() as c:
            await c.execute('INSERT INTO projects(id,user_id,name) VALUES($1,$2,$3)',uuid.UUID(folder['project_id']),owner,'S43d acceptance')
            await store.save_team(auth,team,None,'s43d-'+identifier,{})
            pinned=await store._team(c,auth,team_id,1)
            req={'task':'Synthetic immutable readback fixture; no inference','request_key':'s43d-'+identifier,
                 'team':team_id,'team_revision':1,'project_id':folder['project_id'],
                 'budget':{'start_calls':4,'wall_seconds':120,'max_job_attempts':1,'job_seconds_budget':120}}
            snap={'team':pinned,'models':{'fixture':{'model':'fixture','backend':'fixture'}},'review_independence':'same_model'}
            admitted=await store.start(auth,req,snap,connection=c)
            # Status changes in this same transaction: the production pump can
            # never observe an accepted fixture and dispatch a model.
            await c.execute("UPDATE agent_runs SET status='completed' WHERE lane=$1 AND owner=$2 AND id=$3",lane,owner,admitted['run_id'])
            await c.execute('INSERT INTO tool_jobs(lane,owner,id,run_id,actor,boundary,attachment_id,project_id,registration,workspace_id,role,request,base_digest,output_digest,operation_id) '
                            'VALUES($1,$2,$3,$4,$2,$5,$6,$7,$8::jsonb,$9,$10,$11::jsonb,$12,$13,$14)',
                            lane,owner,identifier,admitted['run_id'],auth.boundary,folder['attachment_id'],folder['project_id'],canonical(folder),
                            'run_'+options['workspace'],'researcher',canonical({'task':req['task']}),base['digest'],output['digest'],str(uuid.uuid4()))
            row={'lane':lane,'owner':owner,'id':identifier}
            await jobs.save_snapshot(c,row,'base',base);await jobs.save_snapshot(c,row,'output',output)
            prop=proposal(base,output,{'job_id':identifier,'project_id':folder['project_id'],'provenance':'acceptance_fixture'})
            await c.execute('INSERT INTO job_proposals VALUES($1,$2,$3,$4,$5::jsonb)',lane,owner,identifier,prop['digest'],canonical(prop))
            data=canonical(prop).encode();sha=await jobs.blob(c,row,data);name='proposal-'+identifier+'.json'
            await c.execute('INSERT INTO tool_job_artifacts VALUES($1,$2,$3,$4,$5)',lane,owner,identifier,name,sha)
        first=await jobs.command(auth,'jobs.artifact',{'job_id':identifier,'filename':name})
        (run_workspace/'allowed.txt').write_text('changed after snapshot')
        second=await jobs.command(auth,'jobs.artifact',{'job_id':identifier,'filename':name})
        immutable=first==second
        try: await pool.execute('DELETE FROM job_proposals WHERE job_id=$1',identifier)
        except Exception: pass
        else: immutable=False
        return {'id':identifier,'artifact':name,'artifact_sha256':sha,'proposal_digest':prop['digest'],
                'base_digest':base['digest'],'output_digest':output['digest'],'immutable':immutable,
                'digest_bound':first['sha256']==hashlib.sha256(data).hexdigest()}
    finally: await pool.close()


def checked_edits_acceptance(h):
    """Real installed worker jobs; model transport is deterministic supplied text."""
    from core.portal_resources import register_project_folder
    name = h.names[0]
    cfg = h.configs[name]
    root = FAMILY / name / 'bob-memory'
    if 'checked' not in cfg['profiles']:
        raise RuntimeError('installed checked-edits/1 profile required; reprovision disposable lanes')
    core_unit = 'bob-core@' + name + '.service'
    admin('systemctl', 'stop', core_unit).check_returncode()
    try:
        for case in ('success', 'stale', 'failed_check', 'restart_prepare', 'cancel_build'):
            identifier = uuid.uuid4().hex
            project = root / 'projects' / ('s43e-' + identifier)
            project.mkdir(mode=0o770)
            (project / 'file.txt').write_text('registered base')
            checks = ['python.pytest'] if case == 'cancel_build' else ['files.nonempty']
            if case == 'cancel_build':
                (project / 'tests').mkdir()
                # Detached child keeps a heartbeat. Only the worker supervisor,
                # observed from this root harness, may establish its death.
                child_code = ("import os,json,time;from pathlib import Path;"
                              "Path('child.json').write_text(json.dumps({'pid':os.getpid()}));"
                              "exec(\"while True:\\n Path('heartbeat').write_text(str(time.time()))\\n time.sleep(.05)\")")
                test = ('import subprocess,sys,time\n'
                        'def test_hold():\n'
                        '    subprocess.Popen([sys.executable,"-c",' + repr(child_code) + '],start_new_session=True)\n'
                        '    time.sleep(300)\n')
                (project / 'tests' / 'test_hold.py').write_text(test)
            for path in (project, *project.rglob('*')):
                os.chown(path, cfg['service_uid'], cfg['worker_gid'])
                path.chmod(0o2770 if path.is_dir() else 0o660)
            folder = register_project_folder(root, h.envs[name]['BOB_DEFAULT_OWNER'], str(uuid.uuid4()),
                                             str(project.relative_to(root)), 'S43e disposable fixture')
            registry = root / 'project-folders.json'
            rows = json.loads(registry.read_text())
            folder = next(r for r in rows if r['attachment_id'] == folder['attachment_id'])
            folder['checked_edits'] = {'checks': checks, 'write_paths': ['file.txt']}
            registry.write_text(json.dumps(rows))
            os.chown(registry,cfg['service_uid'],cfg['service_gid'])
            registry.chmod(0o640)
            before = hashlib.sha256((project / 'file.txt').read_bytes()).hexdigest()
            options = {'folder': folder, 'case': case, 'timeout': 90}
            if case == 'cancel_build':
                result, errors = {}, []
                def invoke():
                    try: result.update(h.call(options, mode='checked-edits'))
                    except BaseException as exc: errors.append(exc)
                import threading
                task = threading.Thread(target=invoke)
                task.start()
                observations = {}
                deadline = time.monotonic() + 90
                while task.is_alive() and time.monotonic() < deadline:
                    for marker in Path(cfg['run_workspace_root']).glob('*/child.json'):
                        try:
                            pid = json.loads(marker.read_text())['pid']
                            stat = Path('/proc') / str(pid) / 'stat'
                            if stat.exists(): observations[pid] = stat.read_text().rsplit(')',1)[1].split()[19]
                        except (OSError, ValueError, KeyError): pass
                    time.sleep(.05)
                task.join(timeout=1)
                if errors: raise errors[0]
                if task.is_alive(): raise RuntimeError('cancelled build did not settle')
                marker = result.get('heartbeat')
                heartbeat = Path(marker).read_bytes() if marker and Path(marker).exists() else None
                time.sleep(.25)
                dead = bool(observations)
                for pid, started in observations.items():
                    stat = Path('/proc') / str(pid) / 'stat'
                    if stat.exists() and stat.read_text().rsplit(')',1)[1].split()[19] == started:
                        dead = False
                stopped = heartbeat is not None and Path(marker).read_bytes() == heartbeat
                h.record('s43e.cancel-build-descendants', result.get('status') == 'cancelled' and dead and stopped,
                         observations=observations, cleanup=result.get('cleanup'), heartbeat=marker)
            else:
                result = h.call(options, mode='checked-edits')
                if case == 'restart_prepare':
                    prepared = result
                    result = h.call({'folder':folder, 'case':'restart_resume', 'job_id':result['job_id'],
                                     'run_id':result['run_id'], 'timeout':90}, mode='checked-edits')
                    h.record('s43e.restart-without-replay', result.get('status')=='completed'
                             and result.get('model_calls')==2 and result.get('job_attempts')==3
                             and set(prepared['execution_ids']) <= set(result['execution_ids']),
                             before=prepared, after=result)
                else:
                    expected = 'completed' if case == 'success' else 'paused'
                    h.record('s43e.'+case, result.get('status')==expected
                             and result.get('accepted_outputs')==(1 if case=='success' else 0)
                             and (case!='stale' or result.get('stale_preimage_rejected'))
                             and (case!='failed_check' or result.get('registered_check_failed')), **result)
                if case == 'success':
                    h.record('s43e.accepted-output-accounting', result.get('model_calls')==2
                             and result.get('job_attempts')==3 and result.get('held')==0
                             and result.get('digest_bound') and result.get('checks_passed'), **result)
            h.record('s43e.protected-source.'+case, hashlib.sha256((project/'file.txt').read_bytes()).hexdigest()==before)
    finally:
        # Stopping core also stopped its Requires= dependents; start all three.
        admin('systemctl', 'start', core_unit, 'bob-gateway@' + name + '.service',
              'bob-bridge@' + name + '.service').check_returncode()
        h.services_ready()


async def checked_edits_fixture(cfg, options):
    """Service-UID executor process; actual DB, queue, daemon and Landlock."""
    import asyncpg
    from core.config import config
    from core.execution.api import lane_id
    from core.execution.admission import Admission
    from core.execution.checked import CONTRACT
    from core.execution.executor import Executor
    from core.execution.job_store import JobStore
    from core.execution.models import Authority, canonical
    from core.execution.store import Store, decoded
    from core.execution.worker_queue import WorkerQueue
    from core.execution.workspaces import snapshot
    from core.tool_jobs.service import Jobs
    from core.worker.client import Transport
    from core.worker.contract import profile_revision
    folder, case = options['folder'], options['case']
    root = Path(os.environ['BOB_LANE_ROOT'])
    pool = await asyncpg.create_pool(config.POSTGRES_URL,min_size=1,max_size=8)
    store = Store(pool,lane_id())
    await store.initialize()
    # The real lane owner has a credential epoch (3.4); runs without it are fenced.
    from core.execution.api import policy_epoch
    from core.execution.owner import current_epoch
    auth = Authority(lane_id(),folder['owner'],folder['owner'],{'may_touch':[str(root)+'*']},'human',
                     policy_epoch=policy_epoch(config.BOBCLAW_SECRET),
                     credential_epoch=await current_epoch(lane_id(),folder['owner']))
    model = {'fixture':{'backend':'fixture','model':'deterministic-checked-text','transport':'text','enabled':True,'exact_calls':True}}
    async def catalog(): return model
    admission = Admission(store,catalog)
    jobs = Jobs(admission)
    check_id = folder['checked_edits']['checks'][0]
    calls = []
    async def dispatch(binding,prompt):
        calls.append(prompt)
        if 'tasks\":[typed steps]' in prompt:
            base = snapshot(root/folder['path'])['digest']
            common = {'instruction':'bounded deterministic work','criteria_ids':[]}
            timed = {'proposed_seconds':900,'rationale':'small isolated acceptance job'}
            output = {'tasks':[
                {**common,**timed,'id':'read','kind':'workspace_read','dependencies':[],'snapshot':base,'operation_id':'workspace.read','paths':['file.txt']},
                {**common,'id':'model','kind':'model_task','dependencies':['read'],'snapshot':'read','operation_id':'edits.plan'},
                {**common,**timed,'id':'edit','kind':'workspace_edit','dependencies':['model'],'snapshot':'model','operation_id':'workspace.edit','edits_from':'model'},
                {**common,**timed,'id':'check','kind':'registered_check','dependencies':['edit'],'snapshot':'edit','check_id':check_id},
                {**common,'id':'publish','kind':'proposal_publish','dependencies':['check'],'snapshot':'check','criteria_ids':['deliverable']},
            ]}
        else:
            output = {'edits':[{'operation':'delete' if case=='failed_check' else 'replace','path':'file.txt',
                               'pre_sha256':'0'*64 if case=='stale' else hashlib.sha256(b'registered base').hexdigest(),
                               **({} if case=='failed_check' else {'content':'checked deliverable'})}]}
        return {'text':canonical(output),'usage':{}}
    async def checkpoint(row,ordinal):
        if case=='restart_prepare' and ordinal==3: raise asyncio.CancelledError()
    worker = JobStore(pool,lane_id())
    await worker.initialize()
    queue = WorkerQueue(worker,Transport(cfg),poll=.05)
    queue.start()
    executor = Executor(store,admission,dispatch,poll=.05,after_checkpoint=checkpoint)
    started = None
    heartbeat = None
    try:
        if case=='restart_resume':
            started = {'id':options['job_id'],'run_id':options['run_id']}
        else:
            await pool.execute('INSERT INTO projects(id,user_id,name) VALUES($1,$2,$3)',uuid.UUID(folder['project_id']),folder['owner'],'S43e acceptance')
            team_id = 's43e-' + uuid.uuid4().hex[:16]
            await admission.save(auth,{'definition':{'schema_version':1,'id':team_id,'topology':'agent-loop/v1',
                'roles':{'manager':'fixture','worker':'fixture','critic':'fixture'},'max_parallelism':1},
                'expected_revision':None,'request_key':'save-'+team_id})
            started = await jobs.start(auth,{'contract_version':2,'role':'coder','worker_contract':CONTRACT,
                'task':'Read, edit, check and publish a bounded checked proposal','attachment':folder['attachment_id'],
                'request_key':'start-'+team_id,'team':team_id,'team_revision':1,
                'acceptance':{'criteria':[{'id':'deliverable','output_kind':'proposal','checks':{'required_check_ids':[check_id]}}]},
                'budget':{'start_calls':2,'wall_mode':'fixed','wall_seconds':180,'max_job_attempts':8,'job_seconds_budget':3600},
                'job_policy':{'proposed_seconds':900,'rationale':'acceptance fixture','auto_escalate':False},
                'job_coverage':{'profiles':{'checked':profile_revision(cfg['profiles']['checked'])},
                    'workspaces':{'projects':[folder['path'][len('projects/'):]]},'attachments':[folder['attachment_id']],
                    'max_job_attempts':8,'job_seconds_budget':3600,'allow_cli':False}})
        row = await store.claim(executor.controller)
        if not row or row['id']!=started['run_id']: raise RuntimeError('disposable lane has another active run')
        task = asyncio.create_task(executor.execute(row))
        if case=='cancel_build':
            async with asyncio.timeout(60):
                while not task.done():
                    links = await pool.fetch('SELECT workspace_id FROM checked_step_jobs WHERE lane=$1 AND owner=$2 AND run_id=$3',auth.lane,auth.owner,started['run_id'])
                    markers = [Path(cfg['run_workspace_root'])/link['workspace_id'][4:]/'child.json' for link in links]
                    marker = next((p for p in markers if p.exists()),None)
                    if marker:
                        heartbeat = str(marker.parent/'heartbeat')
                        await asyncio.sleep(1)  # root-side PID/start-time observation window
                        await jobs.command(auth,'jobs.cancel',{'job_id':started['id'],'request_key':'cancel-build-'+started['id']})
                        break
                    await asyncio.sleep(.05)
        try: await asyncio.wait_for(task,90)
        except asyncio.CancelledError:
            if case!='restart_prepare': raise
        view = (await store.get(auth,started['run_id']))['run']
        receipts = [decoded(r['payload']) for r in await pool.fetch('SELECT payload FROM checked_step_receipts WHERE lane=$1 AND owner=$2 AND run_id=$3',auth.lane,auth.owner,started['run_id'])]
        links = await pool.fetch('SELECT execution_id FROM checked_step_jobs WHERE lane=$1 AND owner=$2 AND run_id=$3 ORDER BY ordinal',auth.lane,auth.owner,started['run_id'])
        manifest = view['accepted_outputs'][0]['check_receipts'] if view['accepted_outputs'] else None
        attempts = await pool.fetch('SELECT receipt FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3',auth.lane,auth.owner,started['run_id'])
        errors = [base64.b64decode((decoded(a['receipt']) or {}).get('stderr_b64','')).decode('utf-8','replace') for a in attempts]
        return {'job_id':started['id'],'run_id':started['run_id'],'status':view['status'],
                'accepted_outputs':view['accepted_output_count'],'model_calls':view['calls_used'],
                'job_attempts':view['job_account']['dispatched'],'held':view['job_account']['held'],
                'execution_ids':[r['execution_id'] for r in links],
                'checks_passed':bool(manifest and all(r['passed'] for r in manifest['checks'])),
                'stale_preimage_rejected':any('stale_preimage' in e for e in errors),
                'registered_check_failed':any('check_failed' in e for e in errors),
                'digest_bound':bool(manifest and all(r['input_digest']==manifest['output_digest'] for r in manifest['checks'])),
                'cleanup':[r['cleanup'] for r in receipts], 'heartbeat':heartbeat}
    finally:
        await executor.close()
        await queue.close()
        await pool.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        child(sys.argv[2], sys.argv[3])
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane-a', default='s2d-test-a')
    parser.add_argument('--lane-b', default='s2d-test-b')
    parser.add_argument('--create', action='store_true', help='create disposable lanes; refuses any existing lane')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--embedder', action='store_true', help='PA2 read-only CPU backend, missing model, boot failures and optional lane-A client probes')
    modes.add_argument('--codex-positive', action='store_true', help='operator-approved real provider call on lane A, separate invocation')
    modes.add_argument('--after-reboot', action='store_true', help='verify retained identity and restored policy after an actual reboot')
    modes.add_argument('--worker-protocol', action='store_true', help='4.3a durable protocol/queue gate; includes a real 900-second timeout')
    modes.add_argument('--settings', action='store_true', help='S3d helper/identity/storage acceptance; upgrades old isolated disposable lanes')
    modes.add_argument('--workspaces', action='store_true', help='4.3c real Landlock, immutable Jobs API and broker gate; no providers')
    modes.add_argument('--checked-edits', action='store_true', help='4.3d real text-planned edit/check/proposal jobs, cancellation and restart; no providers')
    modes.add_argument('--job-ladder', action='store_true', help='4.3b grants, revocation, wall and real deadline/descendant gate')
    parser.add_argument('--job-tier', type=int, action='append', choices=(900,1800,3600),
                        help='with --job-ladder: repeatable actual preset seconds; default 900; run long tiers separately')
    modes.add_argument('--migrate-existing', action='store_true',
                       help='with --create: build two synthetic legacy lanes, kill/resume migration, then run every baseline probe')
    parser.add_argument('--host-listener', action='append', type=host_listener, default=[], metavar='IP:PORT',
                        help='repeatable live host positive control; required for the WSL baseline, bracket IPv6')
    args = parser.parse_args()
    if args.embedder:
        if os.geteuid() != 0 or args.create or args.job_tier or args.host_listener:
            parser.error('--embedder requires root, an existing --lane-a, and no provisioning/worker options')
        from lane import validate_name
        validate_name(args.lane_a)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from embedder_acceptance import run
        return run(args.lane_a, emit)
    if os.geteuid() != 0 or args.lane_a == args.lane_b:
        parser.error('root and two distinct disposable lanes required')
    if args.job_tier and not args.job_ladder:
        parser.error("--job-tier requires --job-ladder")
    if args.create and (args.after_reboot or args.codex_positive or args.worker_protocol or args.job_ladder or args.workspaces or args.checked_edits):
        parser.error('--create is only valid for the baseline run')
    if args.migrate_existing and not args.create:
        parser.error('--migrate-existing requires --create in an empty unmarked throwaway runtime')
    from lane import validate_name
    try:
        names = [validate_name(args.lane_a), validate_name(args.lane_b)]
    except ValueError as exc:
        parser.error(str(exc))
    baseline = not any((args.after_reboot, args.codex_positive, args.worker_protocol, args.settings, args.job_ladder, args.workspaces, args.checked_edits))
    if baseline and 'microsoft' in OSRELEASE.read_text().lower() and not args.host_listener:
        parser.error('WSL baseline requires --host-listener')
    for address, port in args.host_listener:
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        with socket.socket(family) as check:
            check.settimeout(2)
            if check.connect_ex((address, port)) != 0:
                parser.error('host listener must be reachable before lane creation')
    # Preflight BOTH lane paths before creating either one.
    for name in names:
        root = FAMILY / name / 'bob-memory'
        marker = root / 'acceptance-disposable.json'
        if args.create:
            if root.parent.exists():
                parser.error('refusing to reuse any existing lane with --create')
        elif not marker.is_file() or json.loads(marker.read_text()).get('purpose') != 's2d-disposable':
            parser.error('only harness-created disposable lanes are allowed')
        if args.after_reboot and not (root / 'acceptance-reboot.json').is_file():
            parser.error('--after-reboot requires a successful baseline receipt for both lanes')
    os.environ['PATH'] = '/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    required = ('systemctl', 'runuser', 'nft', 'ip', 'bob-lane', 'bob-model', 'bob-project-folder',
                'bob-token-renew', 'bob-embedder', 'bob-broker', 'bob-runtime', 'pgrep', 'pkill',
                'setfacl', 'sudo', 'su', 'systemd-run', 'dbus-send')
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        parser.error('runtime tools missing: ' + ', '.join(missing))
    # Children run as unprivileged identities; /root and private temp directories
    # are unsuitable script locations even when root can execute this file.
    import stat
    script = Path(__file__).resolve()
    if not script.stat().st_mode & stat.S_IROTH or any(
            not parent.stat().st_mode & stat.S_IXOTH for parent in script.parents):
        parser.error('install the harness at /var/tmp/bob-s2d/acceptance.py, file 0644 and directories 0755')
    if args.migrate_existing:
        migration_snapshots = migrate_existing(names)
    elif args.create:
        for name in names:
            root = FAMILY / name / 'bob-memory'
            marker = root / 'acceptance-disposable.json'
            result = admin('bob-lane', 'add', name, '--password-stdin', input=secrets.token_urlsafe(32) + '\n')
            result.check_returncode()  # Never print captured TOTP setup URI.
            marker.write_text(json.dumps({'purpose': 's2d-disposable', 'lane': name}))
            marker.chmod(0o600)
    # No Harness construction before this check: it restarts workers, masking boot failures.
    if args.after_reboot:
        rows = boot_workers()
        for probe, passed, evidence in rows:
            emit(probe, passed, **evidence)
        if not all(passed for _, passed, _ in rows):
            emit('overall', False, failures=[probe for probe, passed, _ in rows if not passed])
            return 1
        # Before Harness can restart a worker or a temporary client can pump,
        # observe the actual core's recovery of the admission saved pre-reboot.
        for name in names:
            marker = FAMILY / name / 'bob-memory/acceptance-worker-protocol.json'
            if not marker.exists():
                continue
            saved = json.loads(marker.read_text())
            if 'queued_execution_id' not in saved:
                continue
            from common import read_env
            lane_id = read_env(FAMILY / name / 'bob-memory/lane.env')['BOB_WORKER_ID']
            cfg = json.loads((WORKER_CONFIGS / (lane_id+'.json')).read_text())
            deadline = time.monotonic()+60
            state = {}
            while time.monotonic()<deadline:
                result = admin('runuser','-u',cfg['service_user'],'--',PYTHON,'-I',str(script),'--child',name,'job-audit',
                    input=json.dumps({'execution_id':saved['queued_execution_id']}))
                if result.returncode==0:
                    state = json.loads(result.stdout)
                    if state.get('outcome')=='succeeded':
                        break
                time.sleep(.2)
            effect = Path(saved['queued_effect'])
            passed = state.get('outcome')=='succeeded' and effect.is_file() and effect.read_text()=='x'
            emit('s43a.reboot.queue-recovered.'+name,passed,execution_id=saved['queued_execution_id'],state=state)
            if not passed:
                emit('overall',False,failures=['s43a.reboot.queue-recovered.'+name])
                return 1
    if args.settings:
        return settings_acceptance(names)
    harness = Harness(names, args.host_listener)
    try:
        if args.migrate_existing:
            harness.migration_project(migration_snapshots[names[0]])
        if args.codex_positive:
            harness.codex_positive()
        elif args.worker_protocol:
            worker_protocol(harness)
        elif args.workspaces:
            scoped_workspaces(harness)
        elif args.checked_edits:
            checked_edits_acceptance(harness)
        elif args.job_ladder:
            job_ladder(harness, sorted(set(args.job_tier or [900])))
        elif args.after_reboot:
            harness.socket_access('boot')
            for name, cfg in harness.configs.items():
                marker = Path('/var/lib/bob/family') / name / 'bob-memory/acceptance-reboot.json'
                state = json.loads(marker.read_text())
                harness.record('lifecycle.reboot.' + name,
                    state['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip() and
                    state['worker_uid'] == cfg['worker_uid'] and state['service_uid'] == cfg['service_uid'] and
                    harness.call(payload(code='import os; print(os.getuid())'), lane=name)['stdout'].strip() == str(cfg['worker_uid']))
                protocol_marker = FAMILY / name / 'bob-memory/acceptance-worker-protocol.json'
                if protocol_marker.exists():
                    saved = json.loads(protocol_marker.read_text())
                    hello = harness.call({'action':'hello'},lane=name,mode='protocol')
                    inspection = harness.call(dict(action='control',version=2,op='inspect',
                        lane_generation=hello['lane_generation']+1,controller_session=uuid.uuid4().hex,
                        execution_id=saved['execution_id']),lane=name,mode='protocol')
                    harness.record('s43a.reboot.history.'+name, hello['daemon_epoch'] != saved['daemon_epoch'] and
                        inspection.get('receipt',{}).get('outcome') == 'succeeded', execution_id=saved['execution_id'])
        else:
            harness.run()
    except Exception as exc:
        harness.record('harness.error', False, error=type(exc).__name__, detail=str(exc)[:500])
    finally:
        harness.close()
    if not args.codex_positive and not args.after_reboot and not args.job_ladder and not args.workspaces and not args.checked_edits and not harness.failed:
        for name, cfg in harness.configs.items():
            marker = Path('/var/lib/bob/family') / name / 'bob-memory/acceptance-reboot.json'
            marker.write_text(json.dumps({'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                                         'service_uid': cfg['service_uid'], 'worker_uid': cfg['worker_uid']}))
            marker.chmod(0o600)
        print(json.dumps({'probe': 'lifecycle.reboot', 'passed': None, 'status': 'pending --after-reboot'}))
        if args.worker_protocol:
            arm_protocol_reboot(harness)
    emit('overall', not harness.failed, failures=harness.failed)
    return int(bool(harness.failed))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit('harness.setup-or-cleanup', False, error=type(exc).__name__, detail=str(exc)[:500])
        emit('overall', False)
        raise SystemExit(1)
