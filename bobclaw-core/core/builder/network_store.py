"""Scoped network decisions, owner project edits and durable per-job receipts."""
import time
import uuid

from core.execution.models import ExecutionError, canonical, digest, integer, request_key, only
from core.execution.store import decoded, Store
from .network import DEFAULTS, domain, network_decide, network_key


class NetworkStore:
    def __init__(self, builder):
        self.builder, self.pool = builder, builder.pool

    async def project(self, auth, project_id, *, connection=None):
        if project_id is None and auth.token_type != 'agent':
            return {'allowlist':list(DEFAULTS['allowlist']), 'project_extra':[], 'revision':0}
        c = connection or self.pool
        try:
            project = uuid.UUID(str(project_id))
        except (ValueError, TypeError):
            raise ExecutionError('project_not_found', 404) from None
        if auth.token_type == 'agent' or not await c.fetchval(
                'SELECT id FROM projects WHERE id=$1 AND user_id=$2 AND is_archived=FALSE', project, auth.owner):
            raise ExecutionError('project_not_found', 404)
        row = await c.fetchrow('''SELECT domains,revision FROM builder_network_projects
            WHERE lane=$1 AND owner=$2 AND project_id=$3''', auth.lane, auth.owner, project)
        return {'allowlist': list(DEFAULTS['allowlist']), 'project_extra': decoded(row['domains']) if row else [],
                'revision': row['revision'] if row else 0}

    async def set_project(self, auth, args):
        from core.execution.owner import require_owner
        await require_owner(auth, 'builder.network.set')
        only(args, {'project_id','add','remove','expected_revision','request_key'})
        key = request_key(args.get('request_key'))
        integer(args.get('expected_revision'),0)
        changes = {}
        for field in ('add', 'remove'):
            values = args.get(field, [])
            if not isinstance(values, list) or len(values) > 256:
                raise ExecutionError('invalid_network_domains')
            try:
                changes[field] = {domain(v) for v in values}
            except (ValueError, UnicodeError):
                raise ExecutionError('invalid_network_domains') from None
        if changes['add'] & changes['remove']:
            raise ExecutionError('invalid_network_domains')
        async with self.pool.acquire() as c, c.transaction():
            current = await self.project(auth, args.get('project_id'), connection=c)
            project = uuid.UUID(str(args['project_id']))
            await c.execute('SELECT pg_advisory_xact_lock($1::bigint)', int(digest([auth.lane,auth.owner,key])[:15],16))
            ledger = Store(self.pool, auth.lane)
            previous = await ledger._mutation(c, auth, 'builder.network.set', key, args)
            if previous:
                return previous['result']
            result = await self.update_project(c, auth, project, changes['add'], changes['remove'], args['expected_revision'])
            return await ledger._remember(c,auth,'builder.network.set',key,args,str(project),result)

    async def update_project(self, c, auth, project, add, remove=(), expected=None):
        await c.execute('''INSERT INTO builder_network_projects(lane,owner,project_id)
            VALUES($1,$2,$3) ON CONFLICT DO NOTHING''', auth.lane,auth.owner,project)
        row = await c.fetchrow('''SELECT domains,revision FROM builder_network_projects
            WHERE lane=$1 AND owner=$2 AND project_id=$3 FOR UPDATE''',auth.lane,auth.owner,project)
        if expected is not None and row['revision'] != expected:
            raise ExecutionError('revision_conflict',409)
        values = sorted((set(decoded(row['domains'])) | set(add)) - set(remove))
        if len(values) > 256:
            raise ExecutionError('network_allowlist_limit',413)
        await c.execute('''UPDATE builder_network_projects SET domains=$4::jsonb,revision=revision+1
            WHERE lane=$1 AND owner=$2 AND project_id=$3''',auth.lane,auth.owner,project,canonical(values))
        return {'project_id':str(project),'allowlist':list(DEFAULTS['allowlist']),
                'project_extra':values,'revision':row['revision']+1}

    async def policy(self, auth, conversation, command, job_id, host, method):
        from core.execution.workflow_context import current
        workflow=current.get()
        row = await self.builder.store.mode(auth, str(conversation))
        host = domain(host)
        detail = {'domain':host,'method':method,'command':command}
        key = network_key(host, method)
        allowances = await self.builder.store.allowances(auth,row)
        config = await self.project(auth,row['project_id'])
        # Reuse pending/denied decisions for this command and session. allow_once
        # is consumed atomically on retry, never becomes a blanket command grant.
        async with self.pool.acquire() as c, c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',int(digest([
                auth.lane,auth.owner,str(conversation),detail])[:15],16))
            existing = await c.fetchrow('''SELECT * FROM builder_approvals WHERE lane=$1 AND owner=$2
                AND conversation_id=$3 AND project_id=$4 AND session_id=$5 AND kind='network'
                AND detail=$6::jsonb AND expires_at>$7 AND state!='expired'
                AND (state!='allowed' OR execution_state='pending') ORDER BY created_at DESC LIMIT 1 FOR UPDATE''',
                auth.lane,auth.owner,row['conversation_id'],row['project_id'],row['session_id'],canonical(detail),time.time())
            if existing:
                if existing['state'] == 'allowed':
                    await c.execute("UPDATE builder_approvals SET execution_state='completed' WHERE id=$1",existing['id'])
                    return {'decision':'allow','approval_id':existing['id']}
                if existing['state'] == 'denied':
                    return {'decision':'deny','approval_id':existing['id']}
                # A later owner project/session grant can satisfy an old card.
            decision = network_decide(row['mode'],host,method,config['project_extra'],key in allowances)
            if decision != 'ask':
                return {'decision':decision}
            if existing:
                return {'decision':'ask','approval_id':existing['id'], 'approval':self.builder.store.card(existing)}
            identifier = uuid.uuid4().hex
            approval = await c.fetchrow('''INSERT INTO builder_approvals
                (id,lane,owner,conversation_id,project_id,session_id,turn_id,kind,mode,summary,detail,payload,expires_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,'network',$8,$9,$10::jsonb,$11::jsonb,$12) RETURNING *''',
                identifier,auth.lane,auth.owner,row['conversation_id'],row['project_id'],row['session_id'],job_id,
                row['mode'],'Network: '+method+' '+host,canonical(detail),canonical({'operation':'network_request','job_id':job_id}),
                min(workflow.expires if workflow else time.time()+600,row['session_until']))
            return {'decision':'ask','approval_id':identifier,'approval':self.builder.store.card(approval)}

    async def log(self, auth, row, job_id, event):
        await self.pool.execute('''INSERT INTO builder_network_log
            (lane,owner,conversation_id,project_id,job_id,approval_id,domain,method,bytes_in,bytes_out,decision)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)''',auth.lane,auth.owner,row['conversation_id'],row['project_id'],
            job_id,event.get('approval_id'),event['domain'],event['method'],event['bytes_in'],event['bytes_out'],event['decision'])

    async def logs(self, auth, args):
        only(args, {'conversation_id','job_id'})
        if bool(args.get('conversation_id')) == bool(args.get('job_id')) or auth.token_type == 'agent':
            raise ExecutionError('invalid_network_log_scope')
        if args.get('conversation_id'):
            row = await self.builder.store.mode(auth,args['conversation_id'])
            rows = await self.pool.fetch('''SELECT * FROM builder_network_log WHERE lane=$1 AND owner=$2
                AND conversation_id=$3 AND project_id=$4 ORDER BY id DESC LIMIT 501''',
                auth.lane,auth.owner,row['conversation_id'],row['project_id'])
        else:
            job = args['job_id']
            if not isinstance(job,str) or len(job)>128:
                raise ExecutionError('invalid_network_job')
            rows = await self.pool.fetch('''SELECT n.* FROM builder_network_log n JOIN projects p ON p.id=n.project_id
                JOIN conversations c ON c.id=n.conversation_id AND c.project_id=n.project_id
                WHERE n.lane=$1 AND n.owner=$2 AND n.job_id=$3 AND p.user_id=$2 AND c.user_id=$2
                AND p.is_archived=FALSE ORDER BY n.id DESC LIMIT 501''',auth.lane,auth.owner,job)
        return {'items':[{k:(str(v) if k in {'conversation_id','project_id','created_at'} else v)
                          for k,v in dict(r).items() if k not in {'lane','owner'}} for r in rows[:500]],
                'truncated':len(rows)>500}
