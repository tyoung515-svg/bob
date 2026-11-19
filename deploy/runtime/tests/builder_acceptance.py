#!/opt/bob/.venv/bin/python
"""Installed Builder acceptance; run as root ONLY in a marked disposable distro.

Copy this one file into the upgraded guest and use /opt/bob/.venv/bin/python -I.
No provider calls: tools enter Builder directly, with genuine owner-session proof,
the installed database, queue, worker and proxy. The inert provider points at :9.
--offline skips network; only --fetch-embedder permits a model download.

Preservation probe format (SHA256 hashes are lowercase hex):
  {"build_id":"OLD", "lanes":{"alice":{"lane_env_sha256":"..."}},
   "counts":{"alice":{"projects":1,"conversations":2,"messages":3}},
   "projects":{"/var/lib/bob/family/alice/bob-memory/projects/UUID/index.html":"..."}}
Counts are public PostgreSQL table totals, per lane database. Projects maps absolute
regular-file paths to hashes. Take the probe BEFORE upgrade. Supply the new build
using --expect-build-id NEW (or expected_build_id in the probe); otherwise build_id
must identify the old build. The installed version must match runtime-payload.json.
Preservation is observed before any acceptance lane creation or embedder fetch.
Missing --expect-state is an explicit skip, never a preservation pass.
"""

import argparse
import asyncio
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import time
import traceback
import uuid

sys.path.insert(0, '/opt/bob/bobclaw-core')
sys.path.insert(0, '/usr/local/lib/bob_runtime')
PYTHON = '/opt/bob/.venv/bin/python'
FAMILY = Path('/var/lib/bob/family')
MODES = ('modes', 'files', 'shell', 'network', 'workspace', 'session-scope', 'j3-files', 'embedder', 'preserved')
PATH = '/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
HTML = '<!doctype html>\n<meta charset="utf-8"><h1>Builder acceptance: 42 ✓</h1>\n'


def failure(mode, exc):
    # Do not forward credential-bearing envs, subprocess output, or DSNs.
    detail = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
    return {'mode': mode, 'passed': False, 'evidence_ids': [],
            'error': getattr(exc, 'code', type(exc).__name__), 'detail': detail[:1500],
            'trace': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                      for frame in traceback.extract_tb(exc.__traceback__)[-12:]]}


def skip(mode, reason):
    return {'mode': mode, 'passed': None, 'skipped': True,
            'evidence_ids': [], 'reason': reason}


def run(*args, timeout=120, **kwargs):
    return subprocess.run(list(args), capture_output=True, text=True,
                          timeout=timeout, cwd='/', **kwargs)


def service(lane, action):
    result = run('systemctl', action, *['bob-' + role + '@' + lane + '.service'
                                      for role in ('core', 'gateway', 'bridge')])
    assert result.returncode == 0, 'Disposable lane service ' + action + ' failed; inspect journal'


def wait_core(env, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = HTTPConnection('127.0.0.1', int(env['BOBCLAW_CORE_PORT']), timeout=2)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            if response.status == 200 and json.loads(response.read()).get('status') == 'ok':
                return
        except (OSError, HTTPException, ValueError, AttributeError):
            pass
        finally:
            connection.close()
        time.sleep(.2)
    raise AssertionError('Disposable core /health did not become ready')


def wait_gateway(env):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', int(env['BOBCLAW_GATEWAY_PORT'])), timeout=.5):
                return
        except OSError:
            time.sleep(.2)
    raise AssertionError('Disposable gateway did not become ready')


def configure_lane(lane, root, http, owner_headers):
    """Same inert enrollment and loopback pin as day_one_acceptance.py."""
    from common import read_env
    from model import save_env
    from core.lane_config import read_lane
    inert = 'bob-acceptance-inert-key-not-a-secret'
    receipt = http('/settings/secrets', {'contract_version': 2, 'provider': 'deepseek',
        'key': inert, 'request_key': 'builder-inert-' + uuid.uuid4().hex}, owner_headers)
    assert receipt['provider'] == 'deepseek' and receipt['stored'] is True, 'Inert key not stored'
    values = read_env(root / 'lane.env')
    values['DEEPSEEK_BASE_URL'] = 'http://127.0.0.1:9'
    save_env(root / 'lane.env', values)
    for arguments, key in ((['set-key', lane, 'deepseek'], inert + '\n'),
                           (['set-default', lane, 'deepseek_v4_flash'], None)):
        result = run('bob-model', *arguments, input=key)
        assert result.returncode == 0, 'Inert bob-model enrollment failed; output withheld'
    values = {**read_env(root / 'lane.env'), **read_env(root / 'providers.env')}
    configured = read_lane(root, values=values)
    assert configured.default_error() is None, 'Disposable model default not enrolled'
    assert configured.default_backend == 'deepseek_v4_flash', 'Incorrect inert backend'
    assert configured.values['DEEPSEEK_BASE_URL'] == 'http://127.0.0.1:9', 'Provider transport not inert'
    service(lane, 'restart')
    return {'default_backend': configured.default_backend,
            'model': configured.backends[configured.default_backend].model,
            'base_url': 'http://127.0.0.1:9', 'secret_operation_id': receipt['operation_id']}


class Gateway:
    def __init__(self, lane, env):
        import jwt
        self.env = env
        token = (Path('/var/lib/bob-portal') / (lane + '.token')).read_text().strip()
        # Metadata only. The gateway verifies the actual bearer on every request.
        self.claims = jwt.decode(token, options={'verify_signature': False})
        self.link = uuid.uuid4().hex + uuid.uuid4().hex
        self.headers = {'Authorization': 'Bearer ' + token, 'X-Bob-Broker-Link': self.link,
                        'Content-Type': 'application/json'}
        self.last_step = -1

    def http(self, path, body, auth=None, expected=(200, 201, 202)):
        connection = HTTPConnection('127.0.0.1', int(self.env['BOBCLAW_GATEWAY_PORT']), timeout=120)
        try:
            connection.request('POST', path, body=json.dumps(body), headers=auth or self.headers)
            response = connection.getresponse()
            value = json.loads(response.read())
            assert response.status in expected, 'Unexpected HTTP status at ' + path + ': ' + str(response.status)
            return value
        finally:
            connection.close()

    def owner(self, operation, resource):
        from core.execution.owner import WORK_APPROVAL_OPERATIONS
        if operation in WORK_APPROVAL_OPERATIONS:
            challenge=self.http('/auth/owner/begin',{'contract_version':2,'operation':operation,'resource':resource})
            assert challenge.get('confirmation_mode')=='signed_in_user','Disposable work route did not offer signed-in confirmation'
            confirmed=self.http('/auth/owner/confirm',{'contract_version':2,'challenge_id':challenge['challenge_id'],'confirmation_mode':'signed_in_user'})
            bearer=confirmed['owner_bearer']
            return bearer,confirmed['owner_session'],{**self.headers,'Authorization':'Bearer '+bearer,'X-Bob-Portal-Credential':self.claims['jti']}
        import pyotp
        deadline = time.monotonic() + 35
        while int(time.time() / 30) <= self.last_step:
            assert time.monotonic() < deadline, 'TOTP clock did not advance'
            time.sleep(.2)
        challenge = self.http('/auth/owner/begin', {'contract_version': 2,
            'operation': operation, 'resource': resource})
        proof_time = time.time()
        confirmed = self.http('/auth/owner/confirm', {'contract_version': 2,
            'challenge_id': challenge['challenge_id'], 'password': self.env['BOBCLAW_PASSWORD'],
            'totp': pyotp.TOTP(self.env['TOTP_SECRET']).at(proof_time)})
        self.last_step = int(proof_time / 30)
        bearer = confirmed['owner_bearer']
        headers = {**self.headers, 'Authorization': 'Bearer ' + bearer,
                   'X-Bob-Portal-Credential': self.claims['jti']}
        return bearer, confirmed['owner_session'], headers


def http_modes(gateway, conversation, project):
    body = {'contract_version': 2, 'conversation_id': conversation}
    current = gateway.http('/api/builder/mode.get', body)
    assert current['mode'] == 'approve' and current['project_id'] == project, 'New Builder default/binding incorrect'
    assert current['revision'] == 1 and current['expires_at'] is None, 'Incorrect initial mode revision'
    _, _, headers = gateway.owner('builder.mode.set', {'conversation_id': conversation})
    receipts = []
    for target in ('suggest', 'auto', 'yolo'):
        args = {**body, 'mode': target, 'expected_revision': current['revision'], 'request_key': uuid.uuid4().hex}
        if target == 'suggest':
            denial = gateway.http('/api/builder/mode.set', args, expected=(403,))
            assert denial['error']['code'] == 'owner_session_invalid', 'Portal changed mode without owner proof'
        if target == 'yolo':
            denial = gateway.http('/api/builder/mode.set', args, headers, expected=(422,))
            assert denial['error']['code'] == 'yolo_ack_required', 'Missing yolo secondary acknowledgement was accepted'
            unchanged = gateway.http('/api/builder/mode.get', body)
            assert unchanged['mode'] == 'auto' and unchanged['revision'] == current['revision'], 'Rejected yolo mutated mode'
            args['yolo_acknowledged'] = True
        previous = current
        current = gateway.http('/api/builder/mode.set', args, headers)
        assert current['mode'] == target and current['revision'] == previous['revision'] + 1, 'Owner mode change failed'
        assert gateway.http('/api/builder/mode.get', body)['mode'] == target, 'Mode not durable on readback'
        if target == 'yolo':
            assert time.time() < current['expires_at'] <= time.time() + 7200, 'Yolo expiry outside two-hour bound'
        receipts.append({key: current[key] for key in ('mode', 'revision', 'expires_at', 'operation_id')})
    return receipts


def child_environment(lane):
    from common import read_env
    root = FAMILY / lane / 'bob-memory'
    os.environ.clear()
    os.environ.update({'PATH': PATH, 'BOB_LANE_ROOT': str(root), 'BOB_SERVICE_ROLE': 'core'})
    os.environ.update(read_env(root / 'core.env'))
    return root


async def child(options):
    assert re.fullmatch(r'builder-acc-[0-9a-f]{8}', options['lane']), 'Refusing non-acceptance child lane'
    root = child_environment(options['lane'])
    import asyncpg
    from core.config import config
    from core.builder.service import Builder, turn_id
    from core.execution.api import lane_id, policy_epoch
    from core.execution.models import Authority, ExecutionError
    from core.execution.owner import current_epoch
    from core.worker.client import close_queues, configuration, Transport
    from core.lane_config import get_lane
    lane = get_lane()
    assert lane.default_error() is None, 'Inert model enrollment missing'
    assert lane.default_backend == options['lane_setup']['default_backend'], 'Default backend mismatch'
    assert lane.values['DEEPSEEK_BASE_URL'] == 'http://127.0.0.1:9', 'Provider transport is not loopback discard'
    pool = await asyncpg.create_pool(config.POSTGRES_URL, min_size=1, max_size=8, command_timeout=20)
    tasks = []
    try:
        epoch = await current_epoch(lane_id(), options['lane'])
        normal = Authority(lane_id(), options['lane'], options['lane'], options['scope'],
            'portal', options['portal_id'], policy_epoch(config.BOBCLAW_SECRET), epoch)
        owner = Authority(lane_id(), options['lane'], options['lane'], {}, 'human', options['owner_hash'],
            policy_epoch(config.BOBCLAW_SECRET), epoch, 'owner_stepup', 'bob-owner-controls',
            tuple(options['owner_operations']), options['portal_id'], options['link'])
        builder = Builder(pool)
        conversation = options['conversation_id']
        workspace = root / 'projects' / options['project_id']
        evidence = {'mode': options['mode'], 'passed': False, 'evidence_ids': [conversation, options['project_id']],
                    'conversation_id': conversation, 'project_id': options['project_id'], 'model_calls': 0,
                    'dispatch': 'direct Builder service tools; no model transport invoked'}

        async def mode(target):
            row = await builder.command(normal, 'builder.mode.get', {'conversation_id': conversation})
            return await builder.command(owner, 'builder.mode.set', {'conversation_id': conversation,
                'mode': target, 'expected_revision': row['revision'], 'request_key': uuid.uuid4().hex})

        async def tool(operation, args, **kwargs):
            return await builder.tool(normal, conversation, operation, args, **kwargs)

        async def decide(card, decision='allow_once'):
            # Session minted via begin/confirm; require_owner checks its actual SQLite row.
            result = await Builder(pool).command(owner, 'builder.approve', {
                'approval_id': card['approval_id'], 'decision': decision, 'request_key': uuid.uuid4().hex})
            assert result['state'] == ('denied' if decision == 'deny' else 'allowed'), 'Owner decision not durable'
            evidence['evidence_ids'].append(card['approval_id'])
            return result

        async def pending(operation, args, kind):
            frames = []
            task = asyncio.create_task(tool(operation, args, writer=frames.append))
            tasks.append(task)
            async with asyncio.timeout(15):
                while not frames:
                    if task.done():
                        await task
                        raise AssertionError('Mutation executed without required approval')
                    await asyncio.sleep(.05)
            card = json.loads(frames[0]['content'].split('\n')[1])
            assert card['kind'] == kind and card['conversation_id'] == conversation, 'Incorrect approval card'
            assert not task.done(), 'Approval did not suspend execution'
            # A second service and SQL read prove this is not model text or transient UI state.
            record = await Builder(pool).command(normal, 'builder.approval.get', {'approval_id': card['approval_id']})
            assert record['state'] == 'pending' and record['execution_state'] == 'pending', 'Pending row missing'
            assert await pool.fetchval('SELECT state FROM builder_approvals WHERE id=$1 AND lane=$2 AND owner=$3',
                                      card['approval_id'], normal.lane, normal.owner) == 'pending', 'Approval not in DB'
            view = await Builder(pool).command(normal, 'builder.mode.get', {'conversation_id': conversation})
            assert card in view['approvals'], 'Approval missing after reconnect'
            return task, card

        async def approved(operation, args, kind='command', decision='allow_once'):
            task, card = await pending(operation, args, kind)
            await decide(card, decision)
            return await asyncio.wait_for(asyncio.shield(task), 90)

        async def receipt(result, success=True):
            assert result.get('cleanup') == 'idle_proved' and result.get('execution_id'), 'Missing worker cleanup/execution proof'
            assert (result['exit_code'] == 0) is success, 'Unexpected worker exit code'
            row = await pool.fetchrow('''SELECT outcome,cleanup,receipt FROM worker_job_attempts
                WHERE lane=$1 AND owner=$2 AND execution_id=$3''', normal.lane, normal.owner, result['execution_id'])
            assert row and row['cleanup'] == 'idle_proved', 'Worker attempt not durable'
            assert row['outcome'] == ('succeeded' if success else 'failed'), 'Worker did not finish cleanly'
            from core.execution.store import decoded
            actual = decoded(row['receipt'])
            assert actual['returncode'] == result['exit_code'], 'Tool/worker receipt mismatch'
            evidence['evidence_ids'].append(result['execution_id'])
            return {key: result[key] for key in ('exit_code', 'stdout', 'stderr', 'cleanup', 'execution_id')}

        selected = options['mode']
        if selected == 'modes':
            args = {'conversation_id': conversation, 'mode': 'auto', 'expected_revision': 4,
                    'request_key': uuid.uuid4().hex}
            try:
                await builder.command(normal, 'builder.mode.set', args)
            except ExecutionError as exc:
                assert exc.status in (403, 404), 'Model authority rejection has wrong status'
            else:
                raise AssertionError('Model authority changed mode')
            try:
                await tool('builder.mode.set', args)
            except ExecutionError as exc:
                assert exc.code == 'unsupported_builder_operation', 'Mode setter exposed as model tool'
            else:
                raise AssertionError('Model tool changed mode')
            from dataclasses import asdict
            from core.execution.chat_context import call, current
            context = current.set({'face_id': 'builder', 'conversation_id': conversation,
                                   'authority': asdict(normal), 'exp': time.time() + 60})
            try:
                denied = await call('builder.mode.set', args)
            finally:
                current.reset(context)
            assert denied.get('status') == 'refused', 'Generic chat work path exposed mode setter'
            from core.tools.builder_tools import BUILDER_TOOLS
            assert all('mode' not in item.name for item in BUILDER_TOOLS), 'Model catalog contains mode setter'
            assert (await builder.command(normal, 'builder.mode.get', {'conversation_id': conversation}))['mode'] == 'yolo', 'Model path mutated mode'
            evidence.update(owner_routes=options['http_modes'], model_mode_change='refused')
        elif selected == 'workspace':
            task, card = await pending('workspace_create', {'name':'Acceptance workspace','purpose':'S8 acceptance'}, 'workspace')
            before = await builder.store.mode(normal,conversation)
            assert str(before['project_id']) == options['project_id'], 'Workspace changed before approval'
            decision = await decide(card)
            switched = await asyncio.wait_for(asyncio.shield(task), 60)
            target = switched['project_id']
            assert target != options['project_id'], 'Workspace create reused the old project'
            assert decision['result']['project_id'] == target, 'Tool and owner receipts disagree'
            current = await builder.command(normal,'builder.mode.get',{'conversation_id':conversation})
            assert current['project_id'] == target and current['mode'] == 'approve', 'Binding/mode reset failed'
            assert await pool.fetchval('SELECT project_id::text FROM conversations WHERE id=$1',uuid.UUID(conversation)) == target, 'Conversation not rebound'
            written = await approved('fs_write', {'path':'index.html','content':HTML}, 'write')
            evidence['write'] = await receipt(written)
            listing = await builder.command(normal,'workspace.browse',{'project_id':target})
            assert any(e['name']=='index.html' and e['kind']=='file' for e in listing['entries']), 'Browse missed written file'
            assert not (workspace/'index.html').exists(), 'Old workspace changed'
            evidence.update(workspace=switched,browse=listing,new_project_id=target)
            evidence['evidence_ids'].append(target)
        elif selected == 'session-scope':
            await mode('approve')
            await receipt(await approved('fs_write', {'path':'scope.txt','content':'version 0'}, 'write'))
            await receipt(await approved('fs_edit', {'path':'scope.txt','old':'version 0','new':'version 1'}, 'edit', decision='allow_session'))
            frames = []
            edits = []
            for n in range(2,5):
                result = await tool('fs_edit', {'path':'scope.txt','old':f'version {n-1}','new':f'version {n}'}, writer=frames.append)
                edits.append(await receipt(result))
            result = await tool('shell_run', {'command':'pwd; ls -la; cat scope.txt | head -20'}, writer=frames.append)
            evidence['read_only'] = await receipt(result)
            assert not frames, 'Session scope or read-only checks requested another approval'
            assert (workspace/'scope.txt').read_text() == 'version 4', 'Different edits were not applied'
            assert await pool.fetchval("SELECT count(*) FROM builder_approvals WHERE conversation_id=$1 AND state='pending'", uuid.UUID(conversation)) == 0
            evidence.update(edits=edits,additional_approvals=0)
        elif selected == 'j3-files':
            # Diagnostic first: all three requests go through the installed
            # queue/worker, never a direct builder_runner import. This is not
            # the separate fake-provider/chat journey acceptance.
            await mode('approve')
            evidence['writes']=[]
            async def write_probe(path):
                result=await approved('fs_write',{'path':path,'content':'C6 nested write probe\n'},'write')
                if 'diagnostics' not in result:
                    try:result['diagnostics']=json.loads(result.get('stdout','{}')).get('diagnostics')
                    except ValueError:pass
                evidence['evidence_ids'].append(result.get('execution_id'))
                evidence['writes'].append({'path':path,**{key:result.get(key) for key in
                    ('status','exit_code','execution_id','cleanup','error','diagnostics')}})
                if result.get('exit_code')!=0 or result.get('error'):
                    evidence.update(passed=False,reason='Installed write failed; inspect sanitized stage/errno above.')
                    return False
                readback=await tool('fs_read',{'path':path})
                assert readback['content']=='C6 nested write probe\n','Installed write readback mismatch'
                return True
            writes_ok=[await write_probe('root-probe.txt')]
            shell=await approved('shell_run',{'command':'mkdir shell-parent; id -u; id -g; stat -c "%u %g %a" . shell-parent'})
            evidence['shell_identity']=await receipt(shell)
            writes_ok.append(await write_probe('shell-parent/probe.txt'))
            writes_ok.append(await write_probe('missing-parent/deep/probe.txt'))
            evidence['parent_metadata']={name:{'uid':(workspace/name).stat().st_uid,
                'gid':(workspace/name).stat().st_gid,'mode':oct(stat.S_IMODE((workspace/name).stat().st_mode))}
                for name in ('shell-parent','missing-parent') if (workspace/name).exists()}
            if not all(writes_ok):return evidence
        elif selected == 'files':
            tid = 'acceptance-turn-' + uuid.uuid4().hex
            token = turn_id.set(tid)
            try:
                task, card = await pending('fs_write', {'path': 'index.html', 'content': HTML}, 'write')
                assert card['mode'] == 'approve', 'File probe did not start in approve mode'
                assert not (workspace / 'index.html').exists(), 'File exists before approval'
                await decide(card)
                written = await asyncio.wait_for(asyncio.shield(task), 60)
            finally:
                turn_id.reset(token)
            evidence['write'] = await receipt(written)
            assert (workspace / 'index.html').read_bytes() == HTML.encode(), 'File bytes differ'
            read = await tool('fs_read', {'path': 'index.html'})
            assert read['content'] == HTML and read['size'] == len(HTML.encode()) and not read['truncated'], 'fs_read differs'
            search = await tool('fs_search', {'pattern': 'Builder acceptance: 42', 'glob': '*.html'})
            await receipt(search)
            assert any(hit['path'] == 'index.html' and 'Builder acceptance: 42' in hit['text'] for hit in search['matches']), 'fs_search missed written HTML'
            checkpoints = await tool('checkpoint_list', {})
            checkpoint = next(item for item in checkpoints['items'] if item['turn_id'] == tid)
            restored = await approved('checkpoint_restore', {'checkpoint_id': checkpoint['checkpoint_id']}, 'edit')
            evidence['restore'] = await receipt(restored)
            assert restored['restored'] and not (workspace / 'index.html').exists(), 'Checkpoint did not undo file creation'
            evidence.update(checkpoint=checkpoint, bytes=len(HTML.encode()), sha256=hashlib.sha256(HTML.encode()).hexdigest())
            evidence['evidence_ids'].append(checkpoint['checkpoint_id'])
        elif selected == 'shell':
            await mode('auto')
            # python -c is deliberately outside the conservative auto safe set.
            result = await approved('shell_run', {'command': 'python3 -c "print(6*7)"', 'timeout_s': 30})
            evidence['calculation'] = await receipt(result)
            assert result['stdout'].strip() == '42', 'Worker calculation did not return 42'
            # Sibling project is DAC-writable to the same worker group, so Landlock
            # (rather than an ordinary missing Unix permission) must contain it.
            outside = Path(options['outside_path'])
            result = await approved('shell_run', {'command': 'python3 -c ' + shlex.quote(
                'from pathlib import Path; Path(' + repr(str(outside)) + ').write_text("escaped")'), 'timeout_s': 30})
            evidence['outside'] = await receipt(result, success=False)
            assert outside.read_bytes() == b'outside workspace sentinel\n', 'Worker changed sibling project file'
            assert 'PermissionError' in result['stderr'] or 'Operation not permitted' in result['stderr'], 'Outside write failed for an unrelated reason'
            # Also exercise a safe command with no card/owner decision at all.
            safe = await tool('shell_run', {'command': 'ls', 'timeout_s': 30})
            evidence['auto_safe'] = await receipt(safe)
        elif selected == 'network':
            def fetch(url):
                # HEAD exercises real HTTPS without downloading PyPI's entire
                # multi-megabyte index or aborting a partially-read TLS tunnel.
                code = ('import json,time,urllib.request; r=urllib.request.urlopen('
                        'urllib.request.Request(' + repr(url) + ',method="HEAD"),timeout=25); '
                        'print(json.dumps({"status":r.status,"content_type":r.headers.get("Content-Type")})); '
                        'r.close(); time.sleep(0.2)')
                return {'command': 'python3 -c ' + shlex.quote(code), 'network': True, 'timeout_s': 40}

            async def logs(result, domain, decision, approval_id=None):
                view = await Builder(pool).command(normal, 'builder.network_log', {'job_id': result['job_id']})
                matches = [item for item in view['items'] if item['domain'] == domain and item['decision'] == decision]
                assert matches and not view['truncated'], 'Expected durable proxy decision missing: ' + domain + '/' + decision
                if approval_id:
                    assert any(item['approval_id'] == approval_id for item in matches), 'Network denial not linked to approval'
                assert all(type(item['bytes_in']) is int and type(item['bytes_out']) is int for item in matches), 'Network byte accounting missing'
                evidence.setdefault('network_log', []).extend(matches)

            # Approve mode: no HTTP request before the command owner decision.
            args = fetch('https://pypi.org/simple/')
            task, card = await pending('shell_run', args, 'command')
            assert card['mode'] == 'approve', 'Network probe did not start in approve mode'
            before = await builder.command(normal, 'builder.network_log', {'conversation_id': conversation})
            assert not before['items'], 'Network used before command approval'
            await decide(card, 'allow_session')
            first = await asyncio.wait_for(asyncio.shield(task), 75)
            evidence['pypi_approved'] = await receipt(first)
            payload = json.loads(first['stdout'])
            assert payload['status'] == 200 and payload['content_type'], 'Approved PyPI HTTPS HEAD fetch failed'
            await logs(first, 'pypi.org', 'allow')
            await mode('auto')
            # Changing mode closes the old task/session allowance. Review this
            # exact network command again; the destination remains allowlisted.
            automatic = await approved('shell_run', args)
            evidence['pypi_auto'] = await receipt(automatic)
            assert not automatic['approvals'] and json.loads(automatic['stdout'])['status'] == 200, 'Allowlisted auto fetch asked/failed'
            await logs(automatic, 'pypi.org', 'allow')
            unknown_args = fetch('https://example.com/')
            unknown = await approved('shell_run', unknown_args, decision='allow_session')
            await receipt(unknown, success=False)
            network_card = next(card for card in unknown['approvals'] if card['kind'] == 'network' and card['detail']['domain'] == 'example.com')
            await logs(unknown, 'example.com', 'ask', network_card['approval_id'])
            await decide(network_card, 'deny')
            denied = await tool('shell_run', unknown_args)
            evidence['unknown_denied'] = await receipt(denied, success=False)
            await logs(denied, 'example.com', 'deny', network_card['approval_id'])
            record = await Builder(pool).command(normal, 'builder.approval.get', {'approval_id': network_card['approval_id']})
            assert record['state'] == 'denied', 'Network denial not durable after retry'
            # No outbound connection to example.com can occur: policy asks/denies
            # before DNS/upstream connect. Direct egress uses only a numeric IP.
            bypass = {'command': 'python3 -c ' + shlex.quote(
                'import socket; socket.create_connection(("1.1.1.1",443),timeout=3); print("BYPASS")'),
                'network': True, 'timeout_s': 15}
            blocked = await approved('shell_run', bypass)
            evidence['direct_egress'] = await receipt(blocked, success=False)
            assert 'BYPASS' not in blocked['stdout'] and not blocked['network_log'], 'Direct egress escaped or used proxy'
            assert any(word in blocked['stderr'] for word in ('PermissionError', 'TimeoutError', 'ConnectionRefusedError', 'Network is unreachable', 'Operation not permitted')), 'Direct egress failed for unrelated reason'
        else:
            raise AssertionError('Unsupported child mode')
        idle = await asyncio.to_thread(Transport(configuration()).hello)
        assert idle['active_execution'] is None, 'Worker not idle at end of mode'
        evidence.update(passed=True, idle_execution=idle['active_execution'])
        return evidence
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.to_thread(close_queues)
        finally:
            await pool.close()


async def bounded_child(options):
    async with asyncio.timeout(240):
        return await child(options)


async def counts_child(options):
    child_environment(options['lane'])
    import asyncpg
    from core.config import config
    connection = await asyncpg.connect(config.POSTGRES_URL, timeout=10, command_timeout=20)
    try:
        result = {}
        async with connection.transaction(readonly=True):
            for table, previous in options['counts'].items():
                assert re.fullmatch(r'[a-z][a-z0-9_]{0,62}', table), 'Unsafe count table identifier'
                assert type(previous) is int and previous >= 0, 'Invalid baseline count'
                result[table] = await connection.fetchval('SELECT count(*) FROM public."' + table + '"')
                assert result[table] >= previous, 'Count decreased: ' + table
        return result
    finally:
        await connection.close()


def preserved(args):
    if not args.expect_state:
        return skip('preserved', '--expect-state not supplied; no pre-upgrade evidence')
    state = json.loads(Path(args.expect_state).read_text())
    assert all(isinstance(state.get(key), dict) and state[key] for key in ('lanes', 'counts', 'projects')), 'Expected nonempty lanes/counts/projects maps; see script docstring'
    assert set(state['counts']) == set(state['lanes']), 'Counts must cover every baseline lane'
    from common import read_env
    from lane import lanes
    existing = dict(lanes())
    counts = {}
    for name, entry in state['lanes'].items():
        assert name in existing, 'Baseline lane missing: ' + name
        expected = entry['lane_env_sha256']
        assert re.fullmatch('[0-9a-f]{64}', expected), 'Invalid lane.env hash'
        assert hashlib.sha256(existing[name].read_bytes()).hexdigest() == expected, 'lane.env changed: ' + name
        values = read_env(existing[name])
        cfg = json.loads((Path('/etc/bob/workers') / (values['BOB_WORKER_ID'] + '.json')).read_text())
        assert cfg['service_uid'] != 0, 'Lane service unexpectedly root'
        assert isinstance(state['counts'][name], dict) and state['counts'][name], 'Missing baseline table counts'
        result = run('runuser', '-u', cfg['service_user'], '--', PYTHON, '-I', str(Path(__file__).resolve()),
                     '--counts-child', input=json.dumps({'lane': name, 'counts': state['counts'][name]}))
        assert result.returncode == 0, 'Preserved database count probe failed: ' + name + '; output withheld'
        counts[name] = json.loads(result.stdout)
    for raw, expected in state['projects'].items():
        path = Path(raw)
        assert path.is_absolute() and path.resolve().is_relative_to(FAMILY), 'Project hash path outside family root'
        lane = path.relative_to(FAMILY).parts
        assert len(lane) >= 5 and lane[0] in state['lanes'] and lane[1:3] == ('bob-memory', 'projects'), 'Hash path is not a baseline project file'
        assert path.resolve() == path and stat.S_ISREG(path.lstat().st_mode), 'Project file missing/symlink'
        assert re.fullmatch('[0-9a-f]{64}', expected), 'Invalid project file hash'
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'Previous project file changed: ' + raw
    version = json.loads(Path('/etc/bob/runtime-version').read_text())
    manifest = json.loads(Path('/etc/bob/runtime-payload.json').read_text())
    assert version['build_id'] == manifest['build_id'], 'Version does not match installed payload manifest'
    expected_build = args.expect_build_id or state.get('expected_build_id')
    if expected_build:
        assert version['build_id'] == expected_build, 'Installed build differs from expected new build'
    else:
        assert state.get('build_id') and version['build_id'] != state['build_id'], 'Supply old build_id or --expect-build-id to prove upgrade'
    if state.get('build_id'):
        assert version['build_id'] != state['build_id'], 'Upgrade retained old build ID'
    units = run('systemctl', '--failed', '--no-legend', '--plain', '--no-pager')
    assert units.returncode == 0 and not units.stdout.strip(), 'Failed systemd units after upgrade'
    return {'mode': 'preserved', 'passed': True, 'evidence_ids': [version['build_id']],
            'build_id': version['build_id'], 'lanes': sorted(state['lanes']), 'counts': counts,
            'project_files_checked': len(state['projects']), 'failed_units': []}


def embedder(args):
    result = run('bob-embedder', 'status', timeout=180)
    assert result.returncode == 0, 'bob-embedder status failed'
    status = json.loads(result.stdout)
    assert status['selected_model'] == '4b' and status['embedding_dimension'] == 2560, 'Embedder default is not 4b/2560'
    assert all(type(status[key]) is bool for key in ('file_present', 'sha_ok', 'ready', 'service_active')), 'Malformed embedder status booleans'
    assert {item['model'] for item in status['options']} == {'4b', '0.6b'}, 'Embedder choices missing'
    if args.fetch_embedder:
        result = run('bob-embedder', 'fetch', '--model', '4b', timeout=2100)
        assert result.returncode == 0, 'Explicit embedder fetch failed; inspect setup/journal'
        status = json.loads(result.stdout)
        assert status['ready'] and status['sha_ok'], 'Fetched embedder not ready/verified'
    if status['file_present']:
        embedder_probe(status)
    health_result = run('bob-health', timeout=120)
    assert health_result.returncode in (0, 1), 'bob-health command failed'
    health = json.loads(health_result.stdout)
    assert type(health['healthy']) is bool and isinstance(health['lanes'], dict), 'Malformed health report'
    entry = health['embedder']
    assert entry['selected_model'] == '4b' and entry['embedding_dimension'] == 2560, 'Health selection differs'
    assert entry['status'] in {'setup-needed', 'starting', 'ready'}, 'Health reports failed embedder'
    assert type(entry['ready']) is bool and type(entry['setup_needed']) is bool, 'Malformed health embedder state'
    if not status['file_present']:
        assert entry['status'] == 'setup-needed' and entry['setup_needed'] and not entry['ready'], 'Missing model state incorrect'
    if args.fetch_embedder:
        assert entry['ready'], 'Health did not report fetched embedder ready'
    return {'mode': 'embedder', 'passed': True, 'evidence_ids': [], 'status': status,
            'health_embedder': entry, 'health_overall': health['healthy'],
            'download_requested': args.fetch_embedder}


def embedder_probe(status):
    """Loopback only; a green /health does not prove a model was loaded."""
    import math
    connection = HTTPConnection('127.0.0.1', 58081, timeout=30)
    try:
        connection.request('GET', '/v1/models')
        response = connection.getresponse()
        models = json.loads(response.read(1024 * 1024))
        assert response.status == 200, 'Embedder models endpoint failed'
        assert any(row.get('id') == status['alias'] for row in models['data']), 'Configured embedder alias missing'
        connection.request('POST', '/v1/embeddings',
                           body=json.dumps({'model': status['alias'], 'input': 'hello'}),
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        result = json.loads(response.read(1024 * 1024))
        assert response.status == 200, 'Embedder probe failed'
        vector = result['data'][0]['embedding']
        assert len(vector) == status['embedding_dimension'], 'Embedder dimension mismatch'
        assert all(type(v) in (int, float) and math.isfinite(v) for v in vector), 'Invalid embedding values'
    finally:
        connection.close()


def root(args, modes):
    assert os.name == 'posix' and os.geteuid() == 0, 'Run as root inside the disposable Linux guest'
    marker = Path('/var/lib/bob/.dayone-disposable.json')
    info = marker.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, 'Unsafe disposable marker'
    assert json.loads(marker.read_text())['purpose'] == 'day-one-disposable', 'Distro is not marked disposable'
    os.environ['PATH'] = PATH
    # Never take a post-test snapshot and call it upgrade preservation evidence.
    preservation = None
    if 'preserved' in modes:
        try:
            preservation = preserved(args)
        except Exception as exc:
            preservation = failure('preserved', exc)
    from common import read_env
    lane = 'builder-acc-' + uuid.uuid4().hex[:8]
    lane_root = FAMILY / lane / 'bob-memory'
    setup = gateway = cfg = None
    records = []
    try:
        for selected in modes:
            try:
                if selected == 'preserved':
                    result = preservation
                elif selected == 'network' and args.offline:
                    result = skip('network', '--offline: real PyPI proxy/egress probes disabled')
                elif selected == 'embedder':
                    result = embedder(args)
                else:
                    if setup is None or cfg is None:
                        assert not lane_root.exists(), 'Refusing to reuse an existing acceptance lane'
                        created = run('bob-lane', 'add', lane, '--password-stdin',
                                      input=secrets.token_urlsafe(32) + '\n', timeout=180)
                        assert created.returncode == 0, 'Disposable lane provisioning failed; output withheld'
                        env = read_env(lane_root / 'lane.env')
                        wait_core(env)
                        wait_gateway(env)
                        gateway = Gateway(lane, env)
                        _, _, headers = gateway.owner('secrets.set', {'provider': 'deepseek'})
                        setup = configure_lane(lane, lane_root, gateway.http, headers)
                        wait_core(env)
                        wait_gateway(env)
                        cfg = json.loads((Path('/etc/bob/workers') / (env['BOB_WORKER_ID'] + '.json')).read_text())
                        assert cfg['service_uid'] != 0, 'Acceptance service must not run as root'
                    wait_core(gateway.env)
                    pid = gateway.http('/projects', {'name': 'Disposable Builder ' + selected})['id']
                    conversation = gateway.http('/conversations', {'title': 'Builder acceptance ' + selected,
                                               'face_id': 'builder', 'project_id': pid})['id']
                    bearer, metadata, headers = gateway.owner('work.workspace.create', {'project_id': pid})
                    workspace = gateway.http('/api/work/workspace.create', {'contract_version': 2,
                        'project_id': pid, 'request_key': uuid.uuid4().hex}, headers)
                    assert workspace['target'] == 'bob-lane-workspace', 'Workspace registration failed'
                    mode_receipts = http_modes(gateway, conversation, pid) if selected == 'modes' else None
                    # A fresh proof supplies the child with up to five minutes.
                    bearer, metadata, _ = gateway.owner('builder.mode.set', {'conversation_id': conversation})
                    options = {'mode': selected, 'lane': lane, 'project_id': pid, 'conversation_id': conversation,
                        'scope': gateway.claims['scope'], 'portal_id': gateway.claims['jti'],
                        'owner_hash': hashlib.sha256(bearer.encode()).hexdigest(),
                        'owner_operations': metadata['allowed_operations'], 'link': gateway.link,
                        'lane_setup': setup, 'http_modes': mode_receipts}
                    if selected == 'shell':
                        sibling = lane_root / 'projects' / str(uuid.uuid4())
                        sibling.mkdir(mode=0o2770)
                        os.chown(sibling, cfg['service_uid'], cfg['worker_gid'])
                        outside = sibling / 'sentinel.txt'
                        outside.write_bytes(b'outside workspace sentinel\n')
                        os.chown(outside, cfg['service_uid'], cfg['worker_gid'])
                        outside.chmod(0o660)
                        options['outside_path'] = str(outside)
                    try:
                        stopped = run('systemctl', 'stop', 'bob-core@' + lane + '.service')
                        assert stopped.returncode == 0, 'Could not stop real model executor'
                        completed = run('runuser', '-u', cfg['service_user'], '--', PYTHON, '-I',
                                        str(Path(__file__).resolve()), '--child', input=json.dumps(options), timeout=300)
                    finally:
                        service(lane, 'start')
                    assert completed.returncode == 0, 'Acceptance child failed; subprocess output withheld'
                    result = json.loads(completed.stdout)
                    assert result['mode'] == selected and result.get('project_id', pid) == pid, 'Child receipt scope mismatch'
                    result['lane_setup'] = setup
            except Exception as exc:
                result = failure(selected, exc)
            records.append(result)
    finally:
        if lane_root.exists():
            try:
                service(lane, 'start')
            except Exception as exc:
                # Keep exactly one JSON line per mode, including restoration
                # failures. No successful mode hides a failed cleanup.
                for record in records:
                    record.update(passed=False, restoration_error=failure(record['mode'], exc)['error'])
    for record in records:
        print(json.dumps(record), flush=True)
    return int(any(record['passed'] is False for record in records))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--mode', default=','.join(MODES), help='Comma-separated: ' + ','.join(MODES))
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--fetch-embedder', action='store_true')
    parser.add_argument('--expect-state')
    parser.add_argument('--expect-build-id')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--counts-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child or args.counts_child:
        options = json.load(sys.stdin)
        try:
            result = asyncio.run(counts_child(options) if args.counts_child else bounded_child(options))
        except Exception as exc:
            print(json.dumps(failure(options.get('mode', 'preserved'), exc)), flush=True)
            return 1 if args.counts_child else 0
        print(json.dumps(result), flush=True)
        return 0
    modes = list(dict.fromkeys(item.strip() for item in args.mode.split(',')))
    if not modes or any(item not in MODES for item in modes):
        parser.error('Unknown mode; choose from ' + ','.join(MODES))
    if args.offline and args.fetch_embedder:
        parser.error('--offline and --fetch-embedder cannot be combined')
    try:
        return root(args, modes)
    except Exception as exc:
        for selected in modes:
            print(json.dumps(failure(selected, exc)), flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
