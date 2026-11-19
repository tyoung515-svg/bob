"""Durable conversation modes, exact approvals, session allowances and undo bytes."""
import base64
import time
import uuid
import zlib

from core.execution.models import ExecutionError, canonical, digest, integer, request_key
from core.execution.store import decoded
from .policy import MODES, YOLO_NOTICE


class BuilderStore:
    def __init__(self, pool):
        self.pool = pool

    async def binding(self, auth, conversation_id, *, connection=None):
        try:
            conversation = uuid.UUID(conversation_id)
        except (ValueError, TypeError, AttributeError):
            raise ExecutionError('conversation_not_found', 404) from None
        from core.execution.workflow_context import current
        workflow = current.get()
        run_bound = workflow and workflow.stage['conversation_id']==str(conversation) and workflow.record['initiator']==auth.as_dict()
        if auth.token_type == 'agent' and not run_bound:
            raise ExecutionError('not_found', 404)
        row = await (connection or self.pool).fetchrow('''SELECT c.project_id FROM conversations c
            LEFT JOIN projects p ON p.id=c.project_id WHERE c.id=$1 AND c.user_id=$2
            AND (c.project_id IS NULL OR (p.user_id=$2 AND p.is_archived=FALSE))
            AND COALESCE(to_jsonb(c)->>'is_archived','false')='false' ''', conversation, auth.owner)
        if not row:
            raise ExecutionError('conversation_project_required', 404)
        return conversation, row['project_id']

    async def mode(self, auth, conversation_id, *, connection=None):
        conversation, project = await self.binding(auth, conversation_id, connection=connection)
        c = connection or self.pool
        now = time.time()
        await c.execute('''INSERT INTO builder_modes(lane,owner,conversation_id,project_id,session_id,session_until)
            VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING''',
            auth.lane, auth.owner, conversation, project, uuid.uuid4().hex, now + 7200)
        row = await c.fetchrow('SELECT * FROM builder_modes WHERE lane=$1 AND owner=$2 AND conversation_id=$3',
                               auth.lane, auth.owner, conversation)
        if row is None:
            raise ExecutionError('builder_mode_unavailable', 409)
        if row['project_id'] != project:
            raise ExecutionError('builder_project_binding_changed', 409)
        if dict(row).get('workflow_run_id'):
            from core.execution.workflow_context import current
            workflow = current.get()
            if workflow and (workflow.row['id'] != row['workflow_run_id'] or
                             workflow.row['generation'] != row['workflow_generation']):
                raise ExecutionError('stale_controller',409)
            # No ordinary 7200-second rotation can renew or widen a run lease.
            if row['session_until']<=now:
                raise ExecutionError('builder_session_expired',409)
            return dict(row)
        # Owner calls carry a separate, deliberately empty scope. Only ordinary
        # authenticated calls establish/change the task's effective scope.
        scope = digest(auth.scope) if not auth.session_kind else row['authority_scope']
        scope_changed = row['authority_scope'] is not None and row['authority_scope'] != scope
        epoch_changed = row['credential_epoch'] is not None and row['credential_epoch'] != auth.credential_epoch
        if row['session_until'] <= now or scope_changed or epoch_changed:
            await c.execute('''UPDATE builder_modes SET session_id=$4,session_until=$5,
                mode=CASE WHEN mode='yolo' THEN 'auto' ELSE mode END,expires_at=NULL,revision=revision+1,
                authority_scope=$7,credential_epoch=$8
                WHERE lane=$1 AND owner=$2 AND conversation_id=$3 AND session_id=$6''',
                auth.lane, auth.owner, conversation, uuid.uuid4().hex, now+7200, row['session_id'],scope,auth.credential_epoch)
        else:
            await c.execute('''UPDATE builder_modes SET authority_scope=COALESCE(authority_scope,$4),
                credential_epoch=COALESCE(credential_epoch,$5)
                WHERE lane=$1 AND owner=$2 AND conversation_id=$3''',
                auth.lane,auth.owner,conversation,scope,auth.credential_epoch)
        await c.execute('''UPDATE builder_modes SET mode='auto',expires_at=NULL,revision=revision+1
            WHERE lane=$1 AND owner=$2 AND conversation_id=$3 AND mode='yolo' AND expires_at<=$4''',
            auth.lane, auth.owner, conversation, now)
        row = await c.fetchrow('SELECT * FROM builder_modes WHERE lane=$1 AND owner=$2 AND conversation_id=$3',
                              auth.lane, auth.owner, conversation)
        if row is None:
            raise ExecutionError('builder_mode_unavailable', 409)
        if row['project_id'] != project:
            raise ExecutionError('builder_project_binding_changed', 409)
        return dict(row)

    @staticmethod
    def mode_wire(row):
        return {k: str(row[k]) if k in {'conversation_id','project_id'} and row[k] is not None else row[k]
                for k in ('conversation_id','project_id','mode','expires_at','revision','session_until')}

    async def set_mode(self, auth, args):
        from core.execution.owner import require_owner
        await require_owner(auth, 'builder.mode.set')
        mode = args.get('mode')
        if mode not in MODES:
            raise ExecutionError('invalid_builder_mode')
        if mode == 'yolo' and args.get('yolo_acknowledged') is not True:
            raise ExecutionError('yolo_ack_required', 422, YOLO_NOTICE)
        key = request_key(args.get('request_key'))
        integer(args.get('expected_revision'))
        async with self.pool.acquire() as c, c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',int(digest([auth.lane,'grant-fence'])[:15],16))
            row = await self.mode(auth, args.get('conversation_id'), connection=c)
            await c.execute('SELECT pg_advisory_xact_lock($1::bigint)', int(digest([auth.lane,auth.owner,key])[:15],16))
            # Reuse the existing server mutation ledger, including payload hash.
            from core.execution.store import Store
            store = Store(self.pool, auth.lane)
            previous = await store._mutation(c, auth, 'builder.mode.set', key, args)
            if previous:
                return previous['result']
            expiry = min(time.time()+7200, row['session_until']) if mode == 'yolo' else None
            updated = await c.fetchrow('''UPDATE builder_modes SET mode=$4,expires_at=$5,revision=revision+1,
                session_id=CASE WHEN mode<>$4 THEN $7 ELSE session_id END,
                session_until=CASE WHEN mode<>$4 THEN $8 ELSE session_until END
                WHERE lane=$1 AND owner=$2 AND conversation_id=$3 AND revision=$6 RETURNING *''',
                auth.lane, auth.owner, row['conversation_id'], mode, expiry, args['expected_revision'],uuid.uuid4().hex,time.time()+7200)
            if not updated:
                raise ExecutionError('revision_conflict', 409)
            if row['mode']!=mode:
                if row.get('workflow_run_id'):
                    await store.wf_end_session(c,auth,row['workflow_run_id'],'The owner changed this workflow Builder session mode.')
                from core.execution.pc import end_conversation
                await end_conversation(c,auth.lane,auth.owner,row['conversation_id'])
            await self.reconcile(auth,dict(updated),connection=c)
            return await store._remember(c, auth, 'builder.mode.set', key, args,
                                         str(row['conversation_id']), self.mode_wire(updated))

    async def approval(self, auth, approval_id):
        row = await self.pool.fetchrow('SELECT * FROM builder_approvals WHERE id=$1 AND lane=$2 AND owner=$3',
                                       approval_id, auth.lane, auth.owner)
        if not row:
            raise ExecutionError('approval_not_found', 404)
        mode = await self.mode(auth, str(row['conversation_id']))
        await self.reconcile(auth,mode)
        row = await self.pool.fetchrow('SELECT * FROM builder_approvals WHERE id=$1 AND lane=$2 AND owner=$3',
                                       approval_id,auth.lane,auth.owner)
        if row is None:
            raise ExecutionError('approval_not_found', 404)
        return dict(row)

    async def reconcile(self, auth, mode, *, connection=None):
        """Read-side convergence never approves or dispatches an old proposal."""
        c = connection or self.pool
        await c.execute('''UPDATE builder_approvals SET state='expired',result=$6::jsonb
            WHERE lane=$1 AND owner=$2 AND conversation_id=$3 AND state='pending'
            AND (expires_at<=$4 OR session_id<>$5)''',auth.lane,auth.owner,mode['conversation_id'],
            time.time(),mode['session_id'],canonical({'status':'expired','error':{'code':'approval_expired_or_scope_ended'},
                'detail':'This proposal expired or its task scope ended. Request a fresh proposal; nothing was replayed.'}))
        await c.execute('''UPDATE builder_approvals SET execution_state='unknown',result=$5::jsonb
            WHERE lane=$1 AND owner=$2 AND conversation_id=$3 AND execution_state='running'
            AND COALESCE(execution_until,extract(epoch FROM created_at)+690)<=$4''',
            auth.lane,auth.owner,mode['conversation_id'],time.time(),canonical({'status':'unknown','end_turn':True,
                'error':{'code':'builder_outcome_unknown'},'detail':'The worker deadline passed without a final receipt. Inspect diagnostics; nothing was replayed.'}))

    @staticmethod
    def card(row):
        from .display import approval_display
        detail = decoded(row['detail'])
        return {'schema_version':1, 'approval_id':row['id'], 'conversation_id':str(row['conversation_id']),
                'agent':'builder',
                'kind':row['kind'], 'summary':row['summary'], 'detail':detail,
                'display':approval_display(row, detail),
                'mode':row['mode'], 'expires_at':row['expires_at'],
                'session_until':row.get('session_until')}

    async def allowances(self, auth, row):
        values = await self.pool.fetch('''SELECT allowance FROM builder_allowances WHERE lane=$1
            AND owner=$2 AND conversation_id=$3 AND session_id=$4''',
            auth.lane, auth.owner, row['conversation_id'], row['session_id'])
        return [v['allowance'] for v in values]

    async def checkpoint(self, auth, row, turn_id, root):
        existing = await self.pool.fetchval('''SELECT id FROM builder_checkpoints WHERE lane=$1
            AND owner=$2 AND project_id=$3 AND turn_id=$4''', auth.lane,auth.owner,row['project_id'],turn_id)
        if existing:
            return existing
        import asyncio
        from .files import checkpoint_snapshot
        frozen = await asyncio.to_thread(checkpoint_snapshot, root)
        frozen['blobs'] = {sha:base64.b64encode(zlib.compress(data)).decode() for sha,data in frozen['blobs'].items()}
        frozen['encoding'] = 'zlib-base64'
        encoded = canonical(frozen)
        # Restore must fit one immutable, bounded worker request. Fail before
        # the original mutation if an incompressible snapshot cannot be undone.
        if len(encoded.encode()) > 800000:
            raise ExecutionError('checkpoint_transport_limit',413)
        identifier = uuid.uuid4().hex
        return await self.pool.fetchval('''INSERT INTO builder_checkpoints
            (id,lane,owner,conversation_id,project_id,turn_id,snapshot) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)
            ON CONFLICT(lane,owner,project_id,turn_id) DO UPDATE SET turn_id=EXCLUDED.turn_id RETURNING id''',
            identifier,auth.lane,auth.owner,row['conversation_id'],row['project_id'],turn_id,encoded)
