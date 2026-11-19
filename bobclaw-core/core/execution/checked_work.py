"""Durable checked-work orchestration atop the existing admission and queue.

The intent/attempt link commits before waiting. Recovery inspects the SAME ID;
it cannot create another attempt for an interrupted step.
"""
import asyncio
import hashlib
from pathlib import Path
import time
import uuid

from .checked import CONTRACT, PROFILE, accepted_manifest, edit_scope, check_source_unchanged
from .models import Authority, ExecutionError, canonical, digest
from .store import decoded
from .workspaces import materialize, proposal, snapshot


async def load_context(store, row):
    raw = await store.pool.fetchval('SELECT definition FROM checked_run_contracts WHERE lane=$1 AND owner=$2 AND run_id=$3', row['lane'], row['owner'], row['id'])
    if not raw:
        raise ExecutionError('checked_contract_missing', 409)
    context = decoded(raw)
    from .checked import CHECKS
    if any(cid not in CHECKS or definition['command_spec_digest'] != digest(CHECKS[cid])
           for cid,definition in context['checks'].items()):
        raise ExecutionError('registered_check_revision_changed',409)
    return context


async def freeze(c, jobs, job, row, frozen):
    for sha, data in frozen['blobs'].items():
        await jobs.blob(c, job, data, expected=sha)
    await c.execute('INSERT INTO checked_snapshots VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING',
                    row['lane'], row['owner'], row['id'], frozen['digest'], canonical(frozen['manifest']))


async def thaw(c, row, sha):
    manifest = await c.fetchval('SELECT manifest FROM checked_snapshots WHERE lane=$1 AND owner=$2 AND run_id=$3 AND digest=$4', row['lane'], row['owner'], row['id'], sha)
    if manifest is None:
        raise ExecutionError('snapshot_unavailable', 409)
    manifest = decoded(manifest)
    if digest(manifest) != sha:
        raise ExecutionError('snapshot_digest_conflict', 409)
    blobs = {}
    for item in manifest:
        if item['kind'] == 'file':
            raw = await c.fetchval('SELECT content FROM tool_job_blobs WHERE lane=$1 AND owner=$2 AND sha256=$3', row['lane'], row['owner'], item['sha256'])
            if raw is None or hashlib.sha256(bytes(raw)).hexdigest() != item['sha256']:
                raise ExecutionError('artifact_digest_conflict', 409)
            blobs[item['sha256']] = bytes(raw)
    return {'digest': sha, 'manifest': manifest, 'blobs': blobs}


async def source(c, row, step):
    spec = step['spec']
    reference = spec['snapshot']
    if reference == row['checked_context']['base_digest']:
        return reference
    raw = await c.fetchval("SELECT output FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (spec->>'cycle')::bigint=$4 AND spec->>'id'=$5 AND state='completed'", row['lane'], row['owner'], row['id'], spec['cycle'], reference)
    if not raw:
        raise ExecutionError('snapshot_dependency_unavailable', 409)
    return decoded(raw)['snapshot_digest']


async def source_workspace(c, row, step):
    spec = step['spec']
    if spec['snapshot'] != row['checked_context']['base_digest']:
        raw = await c.fetchval("SELECT output FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (spec->>'cycle')::bigint=$4 AND spec->>'id'=$5 AND state='completed'", row['lane'], row['owner'], row['id'], spec['cycle'], spec['snapshot'])
        if raw and decoded(raw).get('workspace_id'):
            return decoded(raw)['workspace_id']
    return await c.fetchval('SELECT workspace_id FROM tool_jobs WHERE lane=$1 AND owner=$2 AND run_id=$3', row['lane'], row['owner'], row['id'])


async def model_prompt(store, row, step):
    async with store.transaction() as c:
        sha = await source(c, row, step)
        frozen = await thaw(c, row, sha)
        dependency = await c.fetchval("SELECT output FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (spec->>'cycle')::bigint=$4 AND spec->>'id'=$5 AND state='completed'",row['lane'],row['owner'],row['id'],step['spec']['cycle'],step['spec']['snapshot'])
        preferred = {item['path'] for item in decoded(dependency).get('files',[])} if dependency else set()
        baseline = decoded(dependency).get('baseline') if dependency else None
    chunks, remaining = [], 32000
    for item in sorted(frozen['manifest'],key=lambda i:(i['path'] not in preferred,i['path'])):
        if item['kind'] != 'file':
            continue
        raw = frozen['blobs'][item['sha256']]
        text = raw.decode('utf-8', 'replace')
        content = text[:min(16000,remaining)]
        while True:
            entry = {'path': item['path'], 'sha256': item['sha256'], 'content': content, 'truncated': len(content)<len(text)}
            size = len(canonical(entry).encode()) + 1
            if size<=remaining or not content: break
            content=content[:len(content)//2]
        if size>remaining: break
        chunks.append(entry)
        remaining-=size
    return ('Return ONLY JSON {"edits":[{"operation":"create|replace|delete","path":"relative/path",'
            '"pre_sha256":"exact SHA256 or null for create","content":"complete new UTF-8 file; omit for delete"}]}. '
            'No commands, tests-passed claims or authority. '
            'Use exact source preimages. Existing tests and control files are protected. '
            'You may create new app/test_*.py unittest files; preserve all existing tests. '
            'Use importable modules in app/ and unittest discovery from app/.\nTASK: ' + canonical(step['spec'])
            + '\nBASELINE RECEIPT: ' + canonical(baseline)
            + '\nORIGINAL TASK: ' + row['request']['task']
            + '\nACCEPTANCE: ' + canonical(row['request']['acceptance'])
            + '\nFROZEN SNAPSHOT: ' + sha + '\nWRITE PATHS: ' + canonical(row['checked_context']['write_paths'])
            + '\nUNTRUSTED FILE DATA: ' + canonical(chunks))


async def execute_step(executor, row, step):
    from core.tool_jobs.service import Jobs
    from core.worker.client import configuration
    from core.worker.contract import validate
    from .job_policy import JobPolicy
    from .job_store import JobStore, TERMINAL
    from .jobs import JobContext
    jobs, store = Jobs(executor.admission), executor.store
    auth = Authority(**row['run_authority'])
    cfg = configuration()
    context = row['checked_context']
    keys = (row['lane'], row['owner'], row['id'])
    worker = JobStore(store.pool, row['lane'])
    spec = step['spec']
    async with store.transaction() as c:
        current = await store._fenced(c, row)
        await store._dispatchable(c, current)
        raw = await c.fetchrow('SELECT * FROM tool_jobs WHERE lane=$1 AND owner=$2 AND run_id=$3', *keys)
        job = dict(raw)
        sha = await source(c, row, step)
        source_ws = await source_workspace(c, row, step)
        frozen = await thaw(c, row, sha)
    folder = await jobs.attachment(auth, job['attachment_id'])
    if folder != decoded(job['registration']):
        raise ExecutionError('registration_changed', 409)
    if spec['kind'] == 'proposal_publish':
        return await publish(executor, row, step, job, frozen)

    job_request = decoded(job['request'])
    policy = JobPolicy(spec['proposed_seconds'], spec['rationale'], auto_escalate=job_request['job_policy'].get('auto_escalate',False))
    jc = JobContext(auth.owner, auth.actor, 'run:' + row['id'], auth.boundary, row['id'], row['generation'], policy)
    link = await store.pool.fetchrow('SELECT * FROM checked_step_jobs WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4', *keys, step['ordinal'])
    if link is None:
        operation = spec.get('check_id') if spec['kind'] == 'registered_check' else spec['operation_id']
        payload = {'operation': operation, 'input_digest': sha}
        if context.get('direct_mode') == 'fixer' and operation == 'python.unittest':
            payload['test_evidence'] = True
        workspace = source_ws
        # Commands/checks always get fresh copies of the immutable input. Their
        # scratch writes can neither change the proposal nor its registered tests.
        if spec['kind'] in {'registered_check', 'worker_command', 'workspace_read'}:
            identifier = uuid.uuid4().hex
            await asyncio.to_thread(materialize, cfg['run_workspace_root'], identifier, frozen, worker_gid=cfg['worker_gid'])
            workspace = 'run_' + identifier
        if spec['kind'] == 'workspace_read':
            payload['paths'] = spec['paths']
        elif spec['kind'] == 'workspace_edit':
            value = {'edits': spec['edits']} if 'edits' in spec else None
            if value is None:
                async with store.transaction() as c:
                    raw = await c.fetchval("SELECT output FROM agent_run_steps WHERE lane=$1 AND owner=$2 AND run_id=$3 AND (spec->>'cycle')::bigint=$4 AND spec->>'id'=$5 AND state='completed'", *keys, spec['cycle'], spec['edits_from'])
                value = decoded(raw)
                if value['snapshot_digest'] != sha:
                    raise ExecutionError('stale_edit_source', 409)
            edit_scope({'edits': value['edits']},context['write_paths'],context.get('allow_new_tests',False))
            payload.update(edits=value['edits'], write_paths=context['write_paths'],
                           allow_new_tests=context.get('allow_new_tests',False),
                           operation_key=digest([row['id'],step['ordinal'],sha,value['edits']]))
        if operation == 'python.scaffold':
            payload['build_contracts'] = context['build_contracts']
        execution = uuid.uuid4().hex
        request = validate(dict(version=1, job_id=execution, profile=PROFILE, workspace=workspace, cwd='', args=[],
                                input=canonical(payload), timeout=policy.selected_seconds, output_limit=262144,
                                expires=time.time() + policy.selected_seconds + 20), max_timeout=2147483647)
        async with store.transaction() as c:
            # Always queue -> run -> step. The immutable link and queue admission
            # are one transaction, including reply-loss/crash windows.
            await c.execute('SELECT pg_advisory_xact_lock($1)', worker.lock)
            await store._lock_pools(c,keys)
            current = await store._fenced(c, row)
            await store._dispatchable(c, current)
            link = await c.fetchrow('SELECT * FROM checked_step_jobs WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4', *keys, step['ordinal'])
            if link is None:
                # coverage_request can resolve this input workspace before admit.
                # The FK to the attempt is deferred for this atomic pair.
                await c.execute('SET CONSTRAINTS checked_step_jobs_attempt DEFERRED')
                await c.execute('INSERT INTO checked_step_jobs VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)', *keys, step['ordinal'], execution, workspace, sha, digest(spec), digest(request['input']))
                await worker.admit(jc, execution, request, _connection=c)
                await c.execute('UPDATE agent_runs SET phase=$4 WHERE lane=$1 AND owner=$2 AND id=$3',*keys,spec['kind'])
                link = {'execution_id': execution, 'workspace_id': workspace, 'input_digest': sha, 'spec_digest': digest(spec)}
                await store._event(c, keys, 'worker_step_admitted', {'step': step['ordinal'], 'execution_id': execution, 'input_digest': sha})
    if link['spec_digest'] != digest(spec) or link['input_digest'] != sha:
        raise ExecutionError('step_intent_conflict', 409)
    attempt = None
    continuation_reason = None
    active_execution = link['execution_id']
    try:
        while True:
            latest = await store.pool.fetchval('SELECT execution_id FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND logical_job_id=$4 ORDER BY timeout_episode DESC LIMIT 1', *keys, link['execution_id'])
            active_execution = latest or active_execution
            attempt = await worker.get(active_execution)
            if attempt['outcome'] == 'timed_out' and policy.auto_escalate:
                from .job_reconciliation import WorkspaceReconciler, verify_installed_workspace
                worker.workspace_verifier = verify_installed_workspace
                def effects_check(parent, inspected):
                    actual = snapshot(Path(cfg['run_workspace_root']) / link['workspace_id'][4:])
                    unchanged = actual['digest'] == sha
                    pending = None
                    if spec['kind'] == 'workspace_edit':
                        from core.tool_jobs.checked_runner import edit_progress
                        intention = decoded(parent['request']['input'])
                        pending = edit_progress(frozen,actual,{'edits':intention['edits']})
                        safe = True
                    else:
                        # Build/check scratch with other partial writes cannot
                        # be safely rerun merely because the worker is idle.
                        safe = unchanged
                    return {'established': safe, 'resumable': safe,
                            'evidence': {'input_digest':sha, 'current_digest':actual['digest'], 'adapter':CONTRACT,
                                         'pending_edits_digest':digest(pending) if pending is not None else None}}
                try:
                    proof, current_digest = await asyncio.to_thread(WorkspaceReconciler(effects_check), attempt)
                    child = await worker.continue_timeout(jc, active_execution, proof, current_workspace_digest=current_digest)
                    active_execution = child['execution_id']
                    continue
                except Exception as exc:
                    continuation_reason = 'job_reconciliation_required:' + getattr(exc,'code',str(exc))
                    await worker.block_timeout(jc,active_execution,continuation_reason)
                    break
            if attempt['outcome'] in TERMINAL:
                break
            state = await store.heartbeat(row)
            if state != 'running':
                await worker.cancel(active_execution, JobContext(auth.owner, auth.actor, 'run:' + row['id'], auth.boundary, row['id'], attempt['run_generation']))
            await asyncio.sleep(executor.poll)
    except asyncio.CancelledError:
        async def drain():
            async with asyncio.timeout(35):
                while True:
                    ids = await store.pool.fetch('SELECT execution_id FROM worker_job_attempts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND logical_job_id=$4',*keys,link['execution_id'])
                    states = []
                    for item in ids:
                        state = await worker.get(item['execution_id'])
                        await worker.cancel(item['execution_id'],JobContext(auth.owner,auth.actor,'run:'+row['id'],auth.boundary,row['id'],state['run_generation']))
                        states.append(state)
                    if states and all(s['outcome'] in TERMINAL and s['cleanup']=='idle_proved' for s in states):
                        return
                    await asyncio.sleep(executor.poll)
        try:
            await asyncio.shield(drain())
        except Exception as exc:
            raise ExecutionError('worker_cleanup_not_established',409) from exc
        raise
    receipt = {'kind': spec['kind'], 'run_id':row['id'],'step':step['ordinal'],'plan_step':spec['plan_step'],
               'execution_id': active_execution, 'input_digest': sha,
               'snapshot_digest': sha, 'workspace_id': source_ws, 'outcome': attempt['outcome'], 'cleanup': attempt['cleanup'],
               'worker_receipt': {k:v for k,v in (attempt['receipt'] or {}).items() if k not in {'stdout_b64','stderr_b64'}},
               'log_artifact': 'worker-' + active_execution + '.json',
               'started_at': attempt['dispatched_at'].isoformat() if attempt.get('dispatched_at') else None,
               'finished_at': attempt['finished_at'].isoformat() if attempt.get('finished_at') else None,
               'execution_seconds': attempt.get('execution_seconds'), 'selected_seconds':attempt['selected_seconds'],
               'effective_seconds':attempt.get('effective_seconds'), 'limiting_reason':attempt.get('limiting_reason'),
               'passed': (attempt['outcome'] == 'succeeded' and attempt['cleanup'] == 'idle_proved'
                          and (attempt['receipt'] or {}).get('returncode') == 0
                          and not (attempt['receipt'] or {}).get('truncated',False))}
    if continuation_reason:
        receipt['continuation_blocked_reason'] = continuation_reason
    if spec.get('operation_id') == 'python.baseline' and receipt['passed']:
        import base64, json
        try:
            raw=base64.b64decode(attempt['receipt']['stdout_b64']).decode('utf-8')
            last=json.loads(raw.strip().splitlines()[-1])
            baseline=last['result']
            if last['adapter']!='checked-edits/1' or baseline.get('baseline') is not True or baseline.get('tests_run',0)<1:
                raise ValueError()
            receipt['baseline']=baseline
        except (KeyError,ValueError,TypeError):
            raise ExecutionError('baseline_receipt_unavailable',409) from None
    failure_reason = continuation_reason or 'worker_' + attempt['outcome']
    if not continuation_reason and attempt['outcome']=='timed_out' and attempt.get('limiting_reason') not in (None,'selected_limit'):
        failure_reason = attempt['limiting_reason'] + '_exhausted'
    if spec['kind'] == 'registered_check':
        definition = context['checks'][spec['check_id']]
        receipt.update(check_id=spec['check_id'], definition_digest=digest(definition), definition=definition,
                       command_spec_digest=definition['command_spec_digest'],tool_revision=definition['profile_revision'],
                       exit_status=(attempt['receipt'] or {}).get('returncode'),
                       timed_out=attempt['outcome']=='timed_out',assertion_summary='registered_adapter_exit_zero' if receipt['passed'] else 'registered_adapter_failed',
                       truncated=bool((attempt['receipt'] or {}).get('truncated', False)))
        if context.get('direct_mode') == 'fixer' and spec['check_id'] == 'python.unittest':
            import base64, json
            from .checked import compare_baseline
            raw = await store.pool.fetchval("SELECT payload FROM checked_step_receipts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND payload ? 'baseline' ORDER BY ordinal LIMIT 1", *keys)
            baseline = decoded(raw).get('baseline') if raw else None
            final = None
            try:
                adapter = json.loads(base64.b64decode((attempt['receipt'] or {})['stdout_b64']).decode('utf-8').strip().splitlines()[-1])
                if adapter['adapter'] == CONTRACT:
                    final = adapter['result']
            except (KeyError, ValueError, TypeError, IndexError):
                pass
            reason = compare_baseline(baseline, final)
            if final is None and baseline and (attempt['receipt'] or {}).get('returncode') not in (None, 0):
                reason = 'baseline_regression_not_repaired'
            receipt.update(baseline_comparison={'baseline': baseline, 'final': final, 'failure': reason},
                           baseline_already_passing=bool(baseline and baseline.get('passed')))
            if reason:
                receipt['passed'] = False
                receipt['assertion_summary'] = reason
                failure_reason = reason
    if receipt['passed']:
        if spec['kind'] == 'workspace_edit':
            output = await asyncio.to_thread(snapshot, Path(cfg['run_workspace_root']) / link['workspace_id'][4:])
            from core.tool_jobs.checked_runner import edit_progress
            intention = decoded(attempt['request']['input'])
            if edit_progress(frozen,output,{'edits':intention['edits']}):
                raise ExecutionError('edit_output_not_established',409)
            receipt['snapshot_digest'] = output['digest']
            receipt['workspace_id'] = link['workspace_id']
        else:
            output = frozen
        if spec['kind'] == 'workspace_read':
            files = {i['path']: i for i in frozen['manifest'] if i['kind'] == 'file'}
            receipt['files'] = [{**files[p], 'content': frozen['blobs'][files[p]['sha256']].decode('utf-8')} for p in spec['paths']]
        if spec['kind'] in {'registered_check', 'worker_command'}:
            scratch = await asyncio.to_thread(snapshot, Path(cfg['run_workspace_root']) / link['workspace_id'][4:])
            receipt['scratch_digest'] = scratch['digest']
            if spec['kind']=='registered_check':
                receipt['source_unchanged'] = check_source_unchanged(frozen,scratch)
                if not receipt['source_unchanged']:
                    receipt['passed'] = False
                    receipt['assertion_summary'] = 'source_changed_during_check'
                    failure_reason = 'registered_check_source_changed'
            # Scaffold is the only build command whose output is a new source
            # snapshot. It remains isolated; subsequent edits use its own copy.
            if spec.get('operation_id') == 'python.scaffold':
                output = scratch
                receipt['snapshot_digest'] = output['digest']
                receipt['workspace_id'] = link['workspace_id']
    else:
        output = frozen
        if attempt['cleanup'] == 'idle_proved':
            observed = await asyncio.to_thread(snapshot,Path(cfg['run_workspace_root']) / link['workspace_id'][4:])
            receipt['observed_snapshot_digest'] = observed['digest']
    async with store.transaction() as c:
        await store._lock_pools(c,keys)
        current = await store._fenced(c, row)
        await freeze(c, jobs, job, row, output)
        if 'observed_snapshot_digest' in receipt:
            await freeze(c,jobs,job,row,observed)
        if 'scratch_digest' in receipt:
            await freeze(c, jobs, job, row, scratch)
        log_sha = await jobs.blob(c,job,canonical(attempt['receipt'] or {}).encode())
        await c.execute('INSERT INTO tool_job_artifacts VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',row['lane'],row['owner'],job['id'],receipt['log_artifact'],log_sha)
        await c.execute('INSERT INTO checked_step_receipts VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING', *keys, step['ordinal'], canonical(receipt))
        checks = {'passed': receipt['passed'], 'failures': [] if receipt['passed'] else [failure_reason]}
        await c.execute("UPDATE agent_run_steps SET state='completed',output=$5,output_digest=$6,checks=$7::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4 AND state='ready'", *keys, step['ordinal'], canonical(receipt), digest(receipt), canonical(checks))
        await store._event(c, keys, 'worker_step_checked', {'step': step['ordinal'], **receipt})
        if current['status'] == 'cancelling' and attempt['cleanup'] == 'idle_proved' and attempt['outcome'] != 'unknown':
            await store._refund_unsent(c, keys)
            await store._release_holds(c, keys)
            await c.execute("UPDATE agent_runs SET status='cancelled',controller=NULL,lease_until=NULL WHERE lane=$1 AND owner=$2 AND id=$3", *keys)
            await store._event(c, keys, 'cancelled', {'cleanup': 'idle_proved'})
            return 'paused'
        if not receipt['passed']:
            # 4.4 flags own the pause. A mutating job is never fed into the
            # read-only model retry ladder or silently rerun with a new ID.
            await store._pause(c, current, failure_reason,
                               owner_flag=True,ordinal=step['ordinal'],detail={'execution_id':active_execution,'effect_class':spec['effect_class']})
            return 'paused'
        await store._dispatchable(c, current)
        return 'completed' if await store._complete_loop(c, current) else 'checkpointed'


async def publish(executor, row, step, job, frozen):
    from core.tool_jobs.service import Jobs
    store, jobs = executor.store, Jobs(executor.admission)
    keys = (row['lane'], row['owner'], row['id'])
    async with store.transaction() as c:
        await store._lock_pools(c,keys)
        current = await store._fenced(c, row)
        await store._dispatchable(c, current)
        base = await jobs.frozen(c, job, 'base')
        receipts = [decoded(r['payload']) for r in await c.fetch('SELECT payload FROM checked_step_receipts WHERE lane=$1 AND owner=$2 AND run_id=$3 ORDER BY ordinal', *keys)]
        checks = [r for r in receipts if r.get('check_id')]
        identity = {'job_id': job['id'], 'run_id': row['id'], 'project_id': job['project_id'],
                    'attachment_id': job['attachment_id'], 'registration_revision': digest(decoded(job['registration']))}
        prop = proposal(base, frozen, identity)
        prop.update(required_checks=sorted(row['checked_context']['checks']), checks_revision=CONTRACT,
                    check_runs=checks, checks_passed=True, work_mode=row['checked_context'].get('direct_mode'),
                    recipe=row['checked_context'].get('recipe'))
        if row['checked_context'].get('direct_mode') == 'fixer':
            prop['baseline_already_passing'] = any(r.get('baseline', {}).get('passed') is True for r in receipts)
        prop['digest'] = digest({k: v for k, v in prop.items() if k not in {'digest', 'diff', 'diff_truncated'}})
        manifest = accepted_manifest(run_id=row['id'], step={**step['spec'], 'ordinal': step['ordinal']},
                                     proposal=prop, receipts=checks, contract=row['request']['acceptance'], context=row['checked_context'])
        # One immutable proposal per job; multiple publish steps cannot replace it.
        await jobs.save_snapshot(c, job, 'output', frozen)
        await c.execute('INSERT INTO job_proposals VALUES($1,$2,$3,$4,$5::jsonb)', row['lane'], row['owner'], job['id'], prop['digest'], canonical(prop))
        for prefix, value in [('proposal', prop), ('receipt', manifest)]:
            sha = await jobs.blob(c, job, canonical(value).encode())
            await c.execute('INSERT INTO tool_job_artifacts VALUES($1,$2,$3,$4,$5)', row['lane'], row['owner'], job['id'], prefix + '-' + job['id'] + '.json', sha)
        await c.execute('UPDATE tool_jobs SET output_digest=$4,revision=revision+1 WHERE lane=$1 AND owner=$2 AND id=$3', row['lane'], row['owner'], job['id'], frozen['digest'])
        output_id = 'output-' + str(step['ordinal'])
        await c.execute('INSERT INTO agent_accepted_outputs VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb)', *keys, output_id, step['ordinal'], manifest['digest'], canonical(manifest), canonical(step['spec']['criteria_ids']), canonical(manifest))
        receipt = {'passed': True, 'checks': ['checked_proposal_published'], 'failures': []}
        await c.execute("UPDATE agent_run_steps SET state='completed',output=$5,output_digest=$6,checks=$7::jsonb WHERE lane=$1 AND owner=$2 AND run_id=$3 AND ordinal=$4", *keys, step['ordinal'], canonical({'snapshot_digest': frozen['digest'], 'manifest': manifest}), manifest['digest'], canonical(receipt))
        await store._event(c, keys, 'output_accepted', {'output_id': output_id, **manifest})
        return 'completed' if await store._complete_loop(c, current) else 'checkpointed'
