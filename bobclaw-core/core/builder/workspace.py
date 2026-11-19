"""Workspace proposals and transactional conversation rebinding.

Only registered UUID roots in this lane are mountable. The approval receipt,
conversation binding and permission/session reset commit together. Preparation
may leave an empty, unbound registry entry after a database failure; it grants
no access and can be reused by the same approved retry.
"""
import asyncio
import time
import uuid

from core.execution.models import ExecutionError, canonical, digest, only


async def project(builder, auth, project_id, *, connection=None):
    try:
        identifier = uuid.UUID(project_id)
    except (ValueError, TypeError, AttributeError):
        raise ExecutionError('project_not_found', 404) from None
    if auth.token_type == 'agent':
        raise ExecutionError('project_not_found', 404)
    row = await (connection or builder.pool).fetchrow(
        'SELECT id,name FROM projects WHERE id=$1 AND user_id=$2 AND is_archived=FALSE', identifier, auth.owner)
    if not row:
        raise ExecutionError('project_not_found', 404)
    root = builder.workspace(auth, {'project_id':identifier})
    if root is None:
        raise ExecutionError('workspace_unavailable', 409)
    return dict(row), root


async def browse(builder, auth, args):
    only(args, {'project_id','path'})
    record, root = await project(builder, auth, args.get('project_id'))
    from .files import browse as listing
    result = await asyncio.to_thread(listing, root, args.get('path',''))
    return {'project_id':str(record['id']), 'root_label':record['name'], **result}


async def proposal(builder, auth, row, operation, args):
    if operation == 'workspace_create':
        name, purpose = args.get('name'), args.get('purpose','')
        if (not isinstance(name,str) or not name.strip() or len(name)>200 or
                any(ord(c)<32 for c in name) or not isinstance(purpose,str) or len(purpose)>4000):
            raise ExecutionError('invalid_workspace_name')
        # Allocate once, and persist this ID in the exact approval payload.
        args = {'name':name.strip(),'purpose':purpose,'project_id':str(uuid.uuid4())}
        detail = {'action':'create','name':args['name'],'root_label':args['name']}
    else:
        record, _ = await project(builder, auth, args.get('project_id'))
        args = {'project_id':str(record['id'])}
        detail = {'action':'mount','project_id':args['project_id'],'name':record['name'],'root_label':record['name']}
    detail['from_project_id'] = str(row['project_id']) if row['project_id'] else None
    return args, detail


def lock_id(auth, conversation):
    return int(digest([auth.lane,auth.owner,str(conversation),'builder-binding'])[:15],16)


async def execute(builder, auth, approval):
    from core.execution.workbench import ensure_workspace
    from core.execution.store import decoded
    payload = decoded(approval['payload'])
    args = payload['args']
    # No claim outside this transaction: a failure leaves both binding and
    # approval pending, so a retry is safe. Row locks serialize duplicate clicks.
    async with builder.pool.acquire() as c, c.transaction():
        await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',lock_id(auth,approval['conversation_id']))
        current = dict(await c.fetchrow('SELECT * FROM builder_approvals WHERE id=$1 AND lane=$2 AND owner=$3 FOR UPDATE',
                                       approval['id'],auth.lane,auth.owner))
        if current['execution_state'] == 'completed':
            return decoded(current['result'])
        if current['state'] != 'allowed' or current['expires_at'] <= time.time():
            raise ExecutionError('approval_expired',409)
        # Lock both records against the ordinary project-assignment/mode routes.
        await c.fetchrow('SELECT id FROM conversations WHERE id=$1 AND user_id=$2 FOR UPDATE',
                         approval['conversation_id'], auth.owner)
        await c.fetchrow('SELECT conversation_id FROM builder_modes WHERE lane=$1 AND owner=$2 AND conversation_id=$3 FOR UPDATE',
                         auth.lane,auth.owner,approval['conversation_id'])
        row = await builder.store.mode(auth,str(approval['conversation_id']),connection=c)
        if row['project_id'] != current['project_id'] or row['session_id'] != current['session_id']:
            raise ExecutionError('builder_session_expired',409)
        if payload['operation'] == 'workspace_create':
            identifier = uuid.UUID(args['project_id'])
            await c.execute('''INSERT INTO projects(id,user_id,name,description,default_face_id)
                VALUES($1,$2,$3,$4,'builder')''',identifier,auth.owner,args['name'],args.get('purpose',''))
            await ensure_workspace(c,auth,str(identifier))
            name = args['name']
        else:
            target, _ = await project(builder,auth,args['project_id'],connection=c)
            identifier, name = target['id'], target['name']
        await c.execute('UPDATE conversations SET project_id=$3,updated_at=now() WHERE id=$1 AND user_id=$2',
                        row['conversation_id'],auth.owner,identifier)
        session_id, session_until = uuid.uuid4().hex,time.time()+7200
        await c.execute('''UPDATE builder_modes SET project_id=$4,mode='approve',expires_at=NULL,
            session_id=$5,session_until=$6,revision=revision+1 WHERE lane=$1 AND owner=$2 AND conversation_id=$3''',
            auth.lane,auth.owner,row['conversation_id'],identifier,session_id,session_until)
        from core.execution.pc import end_conversation
        await end_conversation(c,auth.lane,auth.owner,row['conversation_id'])
        if current.get('task_authorized'):
            from .policy import task_allowance
            from core.tool_jobs.service import lane_root
            key = task_allowance(str(lane_root()/'projects'/str(identifier)))
            await c.execute('''INSERT INTO builder_allowances VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING''',
                auth.lane,auth.owner,row['conversation_id'],session_id,key)
        # Old approvals cannot migrate across a workspace, including network grants.
        await c.execute('''UPDATE builder_approvals SET state='expired' WHERE lane=$1 AND owner=$2
            AND conversation_id=$3 AND id<>$4 AND state='pending' ''',auth.lane,auth.owner,row['conversation_id'],approval['id'])
        result = {'status':'completed','approval_id':approval['id'],'conversation_id':str(row['conversation_id']),
                  'project_id':str(identifier),'name':name,'root_label':name,'mode':'approve','session_until':session_until,
                  'task_authorized':bool(current.get('task_authorized')),
                  'receipt':{'kind':'workspace','text':'Now working in '+name}}
        await c.execute("UPDATE builder_approvals SET execution_state='completed',result=$2::jsonb WHERE id=$1",
                        approval['id'],canonical(result))
        # A server history divider survives reconnect without trusting model text.
        await c.execute('''INSERT INTO messages(id,conversation_id,role,content,metadata)
            VALUES($1,$2,'assistant',$3,$4::jsonb) ON CONFLICT(id) DO NOTHING''',
            uuid.uuid5(uuid.NAMESPACE_URL,'builder-workspace:'+approval['id']),row['conversation_id'],
            result['receipt']['text'],canonical({'kind':'builder_workspace','project_id':str(identifier)}))
        return result
