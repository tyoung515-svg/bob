"""Owner-reviewed publication of checked bytes into a registered lane workspace.

The durable intent precedes the offline worker edit. A lost reply is reconciled
with the same operation key/journal; no fresh proposal or authority is inferred.
"""
import asyncio
import base64
import json
import time

from .models import ExecutionError, canonical, digest, integer, only, request_key, text
from .store import decoded


async def authorize_worker(c,lane,*,context=None,row=None):
    """Revalidate owner session/epoch again at queued publication dispatch."""
    from .models import Authority
    from .owner import require_owner
    group=context.group if context else row['caller_group']
    owner=context.owner if context else row['owner']
    principal=context.principal if context else row['principal']
    boundary=context.boundary if context else row['boundary']
    publication=await c.fetchrow('SELECT authority FROM work_publications WHERE lane=$1 AND owner=$2 AND job_id=$3',lane,owner,group.removeprefix('publish:'))
    if not publication or not publication['authority']:raise ExecutionError('publication_authority_missing',403)
    auth=Authority(**decoded(publication['authority']))
    if auth.owner!=owner or auth.actor!=principal or auth.boundary!=boundary:raise ExecutionError('publication_authority_changed',403)
    await require_owner(auth,'work.publish')


def publication_edits(base, output, declared):
    before={r['path']:r for r in base['manifest'] if r['kind']=='file'}
    after={r['path']:r for r in output['manifest'] if r['kind']=='file'}
    result=[]
    for item in declared:
        name=item['path']
        old,new=before.get(name),after.get(name)
        if item['pre_sha256']!=(old['sha256'] if old else None) or item['post_sha256']!=(new['sha256'] if new else None):
            raise ExecutionError('proposal_digest_conflict',409)
        value={'operation':item['operation'],'path':name,'pre_sha256':item['pre_sha256']}
        if new:
            value['content']=output['blobs'][new['sha256']].decode('utf-8')
        result.append(value)
    if not result:raise ExecutionError('empty_publication',409)
    return result


# Definite rejection of this frozen proposal/source/journal binding. A new job
# and checked proposal are required; replay must never dispatch it again.
# Registration/check/decision/scope failures invalidate the pinned job contract;
# stale source/preimages and journal/output mismatches invalidate its evidence.
# Caller digest/revision, owner/policy/worker availability, snapshot availability
# and another operation's journal blocker are deliberately NOT terminal.
TERMINAL_CONFLICTS = frozenset({
    'proposal_stale', 'stale_snapshot', 'stale_preimage',
    'snapshot_digest_conflict', 'publication_output_conflict',
    'edit_journal_conflict', 'edit_reconciliation_conflict',
    'proposal_denied', 'checked_proposal_required', 'managed_publication_scope',
    'empty_publication', 'registration_changed', 'edit_scope_denied', 'edit_path_alias',
})


async def blocking_publication(c, auth, root, attachment_id, operation_key=None):
    from core.tool_jobs.checked_runner import journal_blocker
    blocker = await asyncio.to_thread(journal_blocker, root, operation_key)
    if blocker:
        # Only resolve identities in this caller's owned workspace/lane.
        row = await c.fetchrow('''SELECT p.job_id,p.request_key FROM work_publications p
            JOIN tool_jobs j ON (j.lane,j.owner,j.id)=(p.lane,p.owner,p.job_id)
            WHERE p.lane=$1 AND p.owner=$2 AND p.operation_key=$3 AND j.attachment_id=$4''',
            auth.lane, auth.owner, blocker['operation_key'], attachment_id)
        if row:
            blocker.update(job_id=row['job_id'], request_key=row['request_key'])
    return blocker


def adapter_message(receipt, stream):
    try:
        raw = base64.b64decode(receipt.get(stream + '_b64', ''), validate=True).decode('utf-8')
        return json.loads(raw.strip().splitlines()[-1])
    except (ValueError, TypeError, IndexError, UnicodeError):
        return None


def worker_output(attempt, auth, job_id):
    """Only a durable, identity-bound terminal attempt can establish an edit."""
    if (not attempt or attempt['owner'] != auth.owner or attempt['principal'] != auth.actor
            or attempt['caller_group'] != 'publish:' + job_id or attempt['boundary'] != auth.boundary
            or attempt['cleanup'] != 'idle_proved'):
        raise ExecutionError('publication_not_established', 409)
    receipt = attempt['receipt'] or {}
    if attempt['outcome'] == 'failed' and not receipt.get('truncated'):
        message = adapter_message(receipt, 'stderr')
        code = message.get('error') if isinstance(message, dict) and message.get('adapter') == 'checked-edits/1' else None
        if isinstance(code,str):
            raise ExecutionError(code, 409)
    if (attempt['outcome'] != 'succeeded' or receipt.get('returncode') != 0
            or receipt.get('truncated')):
        raise ExecutionError('publication_not_established', 409)
    message = adapter_message(receipt, 'stdout')
    if (not isinstance(message, dict) or message.get('adapter') != 'checked-edits/1'
            or not isinstance(message.get('result'), dict)
            or not isinstance(message['result'].get('output_digest'), str)):
        raise ExecutionError('publication_not_established', 409)
    return message['result']['output_digest']


def failure_receipt(exc, operation_key):
    code = getattr(exc, 'code', 'publication_outcome_unknown')
    state = 'conflict' if isinstance(exc, ExecutionError) and code in TERMINAL_CONFLICTS else 'unknown'
    return state, {'code': code, 'operation_key': operation_key, 'detail': getattr(exc, 'detail', None)}


async def prepare_publication(c, jobs, auth, row, folder, root, args, previous, operation_key):
    from core.tool_jobs.service import effective_scope
    from .workspaces import snapshot
    if folder!=row['registration']:raise ExecutionError('registration_changed',409)
    if row['revision']!=args['expected_revision']:raise ExecutionError('revision_conflict',409)
    run=await c.fetchval('SELECT status FROM agent_runs WHERE lane=$1 AND owner=$2 AND id=$3',auth.lane,auth.owner,row['run_id'])
    if run!='completed':raise ExecutionError('work_not_completed',409)
    raw=await c.fetchrow('SELECT digest,payload FROM job_proposals WHERE lane=$1 AND owner=$2 AND job_id=$3',auth.lane,auth.owner,row['id'])
    if not raw or raw['digest']!=args['proposal_digest']:raise ExecutionError('proposal_digest_conflict',409)
    proposal=decoded(raw['payload'])
    if row['role']!='coder' or not proposal['checks_passed'] or not proposal['required_checks']:
        raise ExecutionError('checked_proposal_required',409)
    decision=await c.fetchval('SELECT payload FROM job_decisions WHERE lane=$1 AND owner=$2 AND job_id=$3',auth.lane,auth.owner,row['id'])
    if decision and decoded(decision)['decision']=='deny':raise ExecutionError('proposal_denied',409)
    base=await jobs.frozen(c,row,'base')
    output=await jobs.frozen(c,row,'output')
    if output['digest']!=proposal['output_digest']:raise ExecutionError('snapshot_digest_conflict',409)
    for cid in proposal['required_checks']:
        if not any(r.get('check_id')==cid and r.get('passed') and r.get('source_unchanged')
                   and r.get('snapshot_digest')==output['digest'] for r in proposal['check_runs']):
            raise ExecutionError('checked_proposal_required',409)
    value=publication_edits(base,output,proposal['files'])
    if any(not (i['path']=='app' or i['path'].startswith('app/')) for i in value):
        raise ExecutionError('managed_publication_scope',403)
    blocker=await blocking_publication(c,auth,root,row['attachment_id'],operation_key)
    if blocker:raise ExecutionError('edit_reconciliation_required',409,blocker)
    if not previous:
        current=await asyncio.to_thread(snapshot,root,scope=effective_scope(auth))
        if current['digest']!=base['digest']:raise ExecutionError('proposal_stale',409)
    return base,output,proposal,value


async def publish(admission,auth,args):
    from .owner import require_owner
    await require_owner(auth,'work.publish')
    only(args,{'job_id','proposal_digest','expected_revision','request_key'})
    integer(args.get('expected_revision'))
    key=request_key(args.get('request_key'))
    text(args.get('proposal_digest'),64)
    from core.tool_jobs.service import Jobs,lane_root
    jobs=Jobs(admission)
    row=await jobs.row(auth,args.get('job_id'))
    folder=await jobs.attachment(auth,row['attachment_id'])
    root=lane_root()/folder['path']
    failure=None
    async with admission.store.transaction() as c:
        from .workflow_context import mutation_lock,writer_guard
        if folder.get('project_id'):
            await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',mutation_lock(auth,folder['project_id']))
            await writer_guard(c,auth,folder['project_id'])
        await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',int(digest([auth.lane,auth.owner,'publication',row['id']])[:15],16))
        previous=await c.fetchrow('SELECT * FROM work_publications WHERE lane=$1 AND owner=$2 AND job_id=$3 FOR UPDATE',auth.lane,auth.owner,row['id'])
        if previous:
            if previous['request_key']!=key or previous['proposal_digest']!=args['proposal_digest']:
                raise ExecutionError('publication_already_requested',409)
            if previous['state']=='completed':return {**decoded(previous['receipt']),'replayed':True}
            if previous['state']=='conflict':
                stored=decoded(previous['receipt'])
                raise ExecutionError(stored['code'],409,stored)
        operation_key=previous['operation_key'] if previous else digest(['work.publish',auth.lane,auth.owner,row['id'],args['proposal_digest']])
        try:
            base,output,proposal,value=await prepare_publication(c,jobs,auth,row,folder,root,args,previous,operation_key)
        except BaseException as exc:
            state,stored=failure_receipt(exc,operation_key)
            # A premature/stale/mistyped new request must not reserve the job's
            # single publication row. Only definite conflicts may be recorded.
            if not previous and state!='conflict':raise
            failure=ExecutionError(exc.code,409,stored) if isinstance(exc,ExecutionError) else exc
        if not previous:
            await c.execute("INSERT INTO work_publications(lane,owner,job_id,request_key,proposal_digest,operation_key,state,authority) VALUES($1,$2,$3,$4,$5,$6,'intent',$7::jsonb)",auth.lane,auth.owner,row['id'],key,args['proposal_digest'],operation_key,canonical(auth.as_dict()))
        else:
            await c.execute('UPDATE work_publications SET authority=$4::jsonb WHERE lane=$1 AND owner=$2 AND job_id=$3',auth.lane,auth.owner,row['id'],canonical(auth.as_dict()))
        if failure:
            await c.execute("UPDATE work_publications SET state=$4,receipt=$5::jsonb WHERE lane=$1 AND owner=$2 AND job_id=$3 AND state<>'completed'",auth.lane,auth.owner,row['id'],state,canonical(stored))
    # Raise after commit to preserve terminal conflicts or retryable failures of
    # an existing intent. New caller/timing refusals leave no row behind.
    if failure:raise failure
    # Deterministic data edits execute inside the existing worker boundary.
    from core.worker.client import execute_async
    from .jobs import JobContext,job_context
    from .job_policy import JobPolicy
    payload={'operation':'workspace.edit','input_digest':base['digest'],'edits':value,
             'write_paths':['app'],'operation_key':operation_key,'allow_new_tests':True}
    context=JobContext(auth.owner,auth.actor,'publish:'+row['id'],auth.boundary,
                       policy=JobPolicy(900,'Owner-reviewed checked source publication',auto_escalate=False))
    try:
        with job_context(context):
            worker=await execute_async('checked',cwd=root,input=canonical(payload),timeout=900,output_limit=65536)
        from .job_store import JobStore
        attempt=await JobStore(admission.store.pool,auth.lane).get(worker['execution_id'])
        if worker_output(attempt,auth,row['id'])!=output['digest']:
            raise ExecutionError('publication_output_conflict',409)
        receipt={'job_id':row['id'],'run_id':row['run_id'],'state':'published',
                 'target':'bob-lane-workspace','attachment_id':row['attachment_id'],
                 'proposal_digest':proposal['digest'],'base_digest':base['digest'],
                 'output_digest':output['digest'],'tested_snapshot_digest':output['digest'],
                 'files':proposal['files'],'checks':proposal['check_runs'],
                 'operation_key':operation_key,'published_at':time.time(),
                 'owner_credential_epoch':auth.credential_epoch,
                 'worker':{k:v for k,v in worker.items() if k not in {'stdout','stderr','stdout_b64','stderr_b64'}}}
    except BaseException as exc:
        state,stored=failure_receipt(exc,operation_key)
        async with admission.store.transaction() as c:
            if getattr(exc,'code',None)=='edit_reconciliation_required':
                stored['detail']=await blocking_publication(c,auth,root,row['attachment_id'],operation_key)
            await c.execute("UPDATE work_publications SET state=$4,receipt=$5::jsonb WHERE lane=$1 AND owner=$2 AND job_id=$3 AND state<>'completed'",auth.lane,auth.owner,row['id'],state,canonical(stored))
        if isinstance(exc,ExecutionError):
            raise ExecutionError(stored['code'],409 if state=='conflict' else exc.status,stored) from exc
        raise
    async with admission.store.transaction() as c:
        current=await c.fetchrow('SELECT state,receipt FROM work_publications WHERE lane=$1 AND owner=$2 AND job_id=$3 FOR UPDATE',auth.lane,auth.owner,row['id'])
        if current['state']=='completed':return {**decoded(current['receipt']),'replayed':True}
        await c.execute("UPDATE work_publications SET state='completed',receipt=$4::jsonb WHERE lane=$1 AND owner=$2 AND job_id=$3 AND state<>'completed'",auth.lane,auth.owner,row['id'],canonical(receipt))
        await admission.store._event(c,(auth.lane,auth.owner,row['run_id']),'workspace_published',receipt)
    return receipt
