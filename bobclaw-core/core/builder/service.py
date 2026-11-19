"""Builder policy, durable approval suspension and isolated worker dispatch."""
import asyncio
import difflib
import hashlib
import json
import time
import uuid
from contextvars import ContextVar

from core.execution.models import Authority, ExecutionError, canonical, digest, only, request_key
from core.execution.store import decoded
from core.portal_resources import load_folders, relative_parts, safe_open, ResourceError
from .store import BuilderStore
from .policy import decide, allowance_keys
from . import files

turn_id = ContextVar('builder_turn_id', default=None)
execution = ContextVar('builder_execution', default=None)
APPROVAL_WAIT = 600
COMMAND_DEFAULT_TIMEOUT = 120
COMMAND_MAX_TIMEOUT = 600
READS = {'fs_list','fs_read','fs_search','preview_file','checkpoint_list'}
FIELDS = {'fs_list':{'path'}, 'fs_read':{'path','offset','max_bytes'}, 'fs_search':{'pattern','glob'},
          'fs_write':{'path','content'}, 'fs_edit':{'path','old','new'}, 'fs_delete':{'path'},
          'shell_run':{'command','cwd','timeout_s','network'}, 'preview_file':{'path'},
          'workspace_create':{'name','purpose'}, 'workspace_mount':{'project_id'},
          'checkpoint_list':set(), 'checkpoint_restore':{'checkpoint_id'}, 'task_finish':{'report'}}


class Builder:
    def __init__(self, pool):
        self.pool, self.store = pool, BuilderStore(pool)
        from .network_store import NetworkStore
        self.network = NetworkStore(self)

    def workspace(self, auth, row):
        from core.tool_jobs.service import lane_root
        root = lane_root()
        folder = next((f for f in load_folders(root) if f['owner']==auth.owner
                       and f['project_id']==str(row['project_id'])
                       and f['path']=='projects/'+str(row['project_id'])), None)
        if not folder:
            return None
        with safe_open(root, relative_parts(folder['path']), directory=True) as (_, path, _):
            return path

    async def command(self, auth, op, args):
        if op == 'builder.project.result':
            only(args,{'project_id'})
            try: project=uuid.UUID(args.get('project_id'))
            except (ValueError,TypeError,AttributeError): raise ExecutionError('project_not_found',404) from None
            if not await self.pool.fetchval('SELECT id FROM projects WHERE id=$1 AND user_id=$2 AND is_archived=FALSE',project,auth.owner):
                raise ExecutionError('project_not_found',404)
            row=await self.pool.fetchrow('''SELECT a.conversation_id,a.payload->'args'->>'path' AS path
                FROM builder_approvals a JOIN conversations c ON c.id=a.conversation_id AND c.user_id=a.owner AND c.project_id=a.project_id
                WHERE a.lane=$1 AND a.owner=$2 AND a.project_id=$3 AND a.summary IN('fs_write','fs_edit')
                AND a.execution_state='completed' AND lower(a.payload->'args'->>'path') LIKE '%.html'
                ORDER BY a.created_at DESC,a.id DESC LIMIT 1''',auth.lane,auth.owner,project)
            if not row: raise ExecutionError('project_result_unavailable',404)
            return {'project_id':str(project),'conversation_id':str(row['conversation_id']),'path':row['path']}
        if op == 'workspace.browse':
            from .workspace import browse
            return await browse(self,auth,args)
        if op == 'builder.network.set':
            return await self.network.set_project(auth,args)
        if op == 'builder.network_log':
            return await self.network.logs(auth,args)
        if op == 'builder.mode.set':
            only(args, {'conversation_id','mode','expected_revision','request_key','yolo_acknowledged'})
            result = await self.store.set_mode(auth, args)
            return {**result,'network':await self.network.project(auth,result['project_id'])}
        if op == 'builder.approve':
            only(args, {'approval_id','decision','request_key'})
            return await self.approve(auth, args)
        if op == 'builder.approval.get':
            only(args, {'approval_id'})
            row = await self.store.approval(auth, args.get('approval_id'))
            return self.approval_record(row)
        if op == 'builder.task.end':
            only(args, {'conversation_id'})
            row = await self.store.mode(auth,args.get('conversation_id'))
            if row.get('workflow_run_id'):
                from core.execution.store import Store
                store=Store(self.pool,auth.lane)
                async with store.transaction(exclusive_grants=True) as c:
                    await store.wf_end_session(c,auth,row['workflow_run_id'],'The owner ended this workflow Builder task.')
            from .lifecycle import end_sessions
            await end_sessions(self.pool,auth.lane,auth.owner,[row['conversation_id']],reason='task_ended_by_you')
            return {'status':'stopped','conversation_id':str(row['conversation_id'])}
        if op == 'builder.approvals.get':
            only(args, {'conversation_id', 'ids'})
            ids = args.get('ids')
            if (not isinstance(ids, list) or len(ids) > 100 or
                    any(not isinstance(i, str) or not i or len(i) > 128 for i in ids)):
                raise ExecutionError('invalid_args', 422)
            mode = await self.command(auth, 'builder.mode.get', {'conversation_id':args.get('conversation_id')})
            records = []
            rows = await self.pool.fetch('''SELECT * FROM builder_approvals WHERE lane=$1 AND owner=$2
                AND conversation_id=$3 AND id=ANY($4::text[])''',
                auth.lane, auth.owner, uuid.UUID(mode['conversation_id']), list(dict.fromkeys(ids)))
            if {r['id'] for r in rows} != set(ids):
                raise ExecutionError('approval_not_found', 404)
            for row in rows:
                records.append(self.approval_record(row))
            if len(canonical(records).encode()) > 2*1024*1024:
                raise ExecutionError('approval_batch_too_large', 413)
            return {'conversation_id':mode['conversation_id'], 'records':records, 'mode':mode}
        if op == 'builder.mode.get':
            only(args, {'conversation_id'})
            row = await self.store.mode(auth, args.get('conversation_id'))
            await self.store.reconcile(auth,row)
            pending = await self.pool.fetch('''SELECT * FROM builder_approvals WHERE lane=$1 AND owner=$2
                AND conversation_id=$3 AND state='pending' AND expires_at>$4 ORDER BY created_at LIMIT 100''',
                auth.lane, auth.owner, row['conversation_id'], time.time())
            cards, records, total = [], [], 0
            for item in pending:
                card=self.store.card(item)
                total += len(canonical(card).encode())
                if total > 512*1024:break
                cards.append(card)
                records.append(self.approval_record(item))
            # Compatibility batch for the shipping broker's mode.get opcode.
            # Include recent resolved records so a pending card converges after
            # another client decides it, without per-card polling.
            recent = await self.pool.fetch('''SELECT * FROM builder_approvals WHERE lane=$1 AND owner=$2
                AND conversation_id=$3 AND (state<>'pending' OR expires_at<=$4)
                ORDER BY created_at DESC LIMIT 100''', auth.lane, auth.owner, row['conversation_id'], time.time())
            for item in recent:
                record = self.approval_record(item)
                total += len(canonical(record).encode())
                if total > 1024*1024: break
                records.append(record)
            return {**self.store.mode_wire(row), 'network':await self.network.project(auth,row['project_id']),
                    'result_path':await self.pool.fetchval('''SELECT payload->'args'->>'path' FROM builder_approvals
                        WHERE lane=$1 AND owner=$2 AND conversation_id=$3 AND project_id=$4
                        AND summary IN ('fs_write','fs_edit') AND execution_state='completed'
                        AND lower(payload->'args'->>'path') LIKE '%.html' ORDER BY created_at DESC LIMIT 1''',
                        auth.lane,auth.owner,row['conversation_id'],row['project_id']),
                    'workspace_receipt':decoded(await self.pool.fetchval('''SELECT result FROM builder_approvals
                        WHERE lane=$1 AND owner=$2 AND conversation_id=$3 AND kind='workspace'
                        AND execution_state='completed' ORDER BY created_at DESC LIMIT 1''',auth.lane,auth.owner,row['conversation_id'])) or None,
                    'approvals':cards,'approval_records':records,'approvals_truncated':len(cards)<len(pending)}
        if op == 'builder.preview':
            only(args, {'conversation_id','path'})
            return await self.tool(auth, args.get('conversation_id'), 'preview_file', {'path':args.get('path')})
        raise ExecutionError('unsupported_operation',404)

    def approval_record(self, row):
        state = 'expired' if row['state'] == 'pending' and row['expires_at'] <= time.time() else row['state']
        result = decoded(row['result'])
        execution_state = row['execution_state']
        if execution_state == 'completed' and isinstance(result,dict) and (result.get('error') or result.get('exit_code',0)!=0):
            execution_state = 'failed'
        outcome = (state if state in {'denied','expired','stopped'} else
                   'waiting_for_you' if state=='pending' else
                   'done' if execution_state=='completed' else execution_state)
        return {**self.store.card(row), 'state':state, 'execution_state':execution_state,
                'outcome':outcome,'result':result}

    async def tool(self, auth, conversation, operation, args, *, writer=None):
        from core.execution.workflow_context import current as workflow_current
        workflow = workflow_current.get()
        if workflow:
            await workflow.controller.validate(workflow,tools=True)
        if operation not in FIELDS:
            raise ExecutionError('unsupported_builder_operation')
        only(args, FIELDS[operation])
        row = await self.store.mode(auth, conversation)
        if operation == 'task_finish':
            report = args.get('report')
            if report is not None and (not isinstance(report,str) or not report.strip() or len(report)>6000):
                raise ExecutionError('invalid_task_report')
            from .lifecycle import end_sessions
            await end_sessions(self.pool,auth.lane,auth.owner,[row['conversation_id']],reason='task_completed')
            return {'status':'completed','end_turn':True,'report':report,
                    'detail':'Task ended. Its write, edit and check allowance is closed.'}
        root = self.workspace(auth, row)
        is_workspace = operation in {'workspace_create','workspace_mount'}
        if root is None and not is_workspace:
            if row['project_id'] is None:
                return {'status':'needs_workspace','error':{'code':'workspace_missing'},
                        'tools':['workspace_create','workspace_mount'],
                        'detail':'Request a workspace through a workspace tool; approval is required.'}
            return {'status':'needs_workspace','error':{'code':'workspace_missing'},
                    'tools':['workspace_create','workspace_mount'],
                    'detail':'This project has no workspace yet; call workspace_create with the app name (owner approval required). Do not retry workspace_mount or ask the owner to create folders.',
                    'action':{'schema_version':1,'operation':'work.workspace.create',
                    'resource':{'project_id':str(row['project_id'])},'request_key':'builder-workspace:'+str(row['project_id'])}}
        if 'path' in args:
            relative_parts(args['path'], empty=operation=='fs_list')
        if operation == 'fs_list':
            return await asyncio.to_thread(files.listing, root, args.get('path',''))
        if operation == 'fs_read':
            data, size = await asyncio.to_thread(files.read, root, args['path'], args.get('offset',0), args.get('max_bytes',65536))
            return {'path':args['path'],'content':data.decode('utf-8','replace'),'size':size,
                    'next_offset':args.get('offset',0)+len(data),'truncated':args.get('offset',0)+len(data)<size}
        if operation == 'preview_file':
            return await asyncio.to_thread(files.preview, root, args['path'])
        if operation == 'checkpoint_list':
            rows = await self.pool.fetch('''SELECT id,turn_id,created_at FROM builder_checkpoints
                WHERE lane=$1 AND owner=$2 AND conversation_id=$3 AND project_id=$4 ORDER BY created_at DESC LIMIT 100''',
                auth.lane,auth.owner,row['conversation_id'],row['project_id'])
            return {'items':[{'checkpoint_id':r['id'],'turn_id':r['turn_id'],'created_at':r['created_at'].isoformat()} for r in rows]}
        if operation == 'fs_search':
            return await self.worker(auth, root, operation, args)
        if is_workspace:
            from .workspace import proposal
            try:
                args, detail = await proposal(self,auth,row,operation,args)
            except ExecutionError as exc:
                if operation != 'workspace_mount' or exc.code != 'workspace_unavailable':
                    raise
                return {'status':'needs_workspace','error':{'code':'workspace_unavailable'},
                        'tools':['workspace_create'],
                        'detail':'This project has no registered workspace in this lane yet. Call workspace_create with the app name next (owner approval required). Do not retry workspace_mount or stop to ask the owner to create folders.'}
            kind = 'workspace'
        elif operation == 'shell_run':
            relative_parts(args.get('cwd',''), empty=True)
            if not isinstance(args.get('command'),str) or not args['command'].strip() or len(args['command'].encode())>65536:
                raise ExecutionError('invalid_command')
            timeout = args.get('timeout_s',COMMAND_DEFAULT_TIMEOUT)
            if type(timeout) is not int or not 1 <= timeout <= COMMAND_MAX_TIMEOUT or type(args.get('network',False)) is not bool:
                raise ExecutionError('invalid_command_options')
            kind = 'command'
            detail = {'command':args['command'],'cwd':args.get('cwd',''),'network':args.get('network',False)}
        else:
            kind = 'write' if operation == 'fs_write' else 'edit'
            detail = {'path':args.get('path',''), 'operation':operation, 'diff':canonical({'operation':operation,**args})}
            preimage = None
            if operation in {'fs_write','fs_edit','fs_delete'}:
                try:
                    before, size = await asyncio.to_thread(files.read,root,args['path'])
                    if size > len(before):raise ExecutionError('change_too_large',413)
                    preimage = hashlib.sha256(before).hexdigest()
                except ResourceError as exc:
                    if exc.status != 404 or operation != 'fs_write':raise
                    before = b''
                old = before.decode('utf-8','replace')
                if operation == 'fs_edit':
                    if not args['old'] or old.count(args['old'])!=1:
                        raise ExecutionError('edit_match_not_unique',409)
                    new = old.replace(args['old'],args['new'],1)
                else:
                    new = args['content'] if operation=='fs_write' else ''
                detail['diff']=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),
                    fromfile='a/'+args['path'],tofile='b/'+args['path']))
            if len(canonical(detail).encode()) > 350000:
                raise ExecutionError('change_too_large',413)
        if not is_workspace:
            detail['workspace'] = str(root)
        if kind == 'command':
            from .policy import command_scope_reason
            reason = await asyncio.to_thread(command_scope_reason, root, detail)
            if reason:
                detail['exact_only_reason'] = reason
        action = decide(row, kind, detail, await self.store.allowances(auth,row))
        if action == 'refuse':
            raise ExecutionError('builder_policy_refused',403)
        identifier = uuid.uuid4().hex
        tid = turn_id.get() or uuid.uuid4().hex
        state = 'allowed' if action == 'run' else 'pending'
        expiry = min(workflow.expires if workflow else time.time()+APPROVAL_WAIT,row['session_until'])
        await self.pool.execute('''INSERT INTO builder_approvals
            (id,lane,owner,conversation_id,project_id,session_id,turn_id,kind,mode,summary,detail,payload,expires_at,state,session_until)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12::jsonb,$13,$14,$15)''',
            identifier,auth.lane,auth.owner,row['conversation_id'],row['project_id'],row['session_id'],tid,
            kind,row['mode'],operation,canonical(detail),canonical({'operation':operation,'args':args,
                **({'preimage':preimage} if operation in {'fs_write','fs_edit','fs_delete'} else {})}),expiry,state,row['session_until'])
        approval = await self.store.approval(auth, identifier)
        card = self.store.card(approval)
        def emit_wait(waiting, **extra):
            if writer:
                writer({'type':'builder_wait','approval_id':identifier,'conversation_id':conversation,
                        'agent':'builder','waiting':waiting,**extra})

        if action in {'ask','propose'}:
            workflow_wait = await workflow.open_wait('approval',identifier,
                'Approve this Builder operation using its existing approval card.') if workflow else None
            block = '```bob-builder-approval\n'+canonical(card)+'\n```'
            if writer:
                writer({'type':'token','content':block+'\n'})
            if action == 'propose':
                return {'status':'proposed','approval':card,'block':block}
            last_wait = 0
            while time.time() < expiry:
                current = await self.store.approval(auth,identifier)
                if writer and time.monotonic()-last_wait >= 5:
                    emit_wait(current['state']=='pending',expires_at=expiry)
                    last_wait = time.monotonic()
                if current['state'] == 'denied':
                    if workflow_wait: await workflow.close_wait(workflow_wait,{'decision':'denied'})
                    emit_wait(False)
                    return {'status':'denied','approval_id':identifier,'end_turn':True}
                if current['execution_state'] in {'completed','failed','stopped'}:
                    if workflow_wait: await workflow.close_wait(workflow_wait,{'decision':current['state']})
                    emit_wait(False)
                    return decoded(current['result'])
                if current['execution_state'] == 'unknown':
                    emit_wait(False)
                    return {'status':'unknown','approval_id':identifier,'error':{'code':'builder_outcome_unknown'},'end_turn':True}
                if current['state'] == 'expired':
                    break
                if workflow and current['state']=='allowed' and current['execution_state']=='pending':
                    await workflow.close_wait(workflow_wait,{'decision':'allowed'})
                    return await self.execute(auth,current)
                await asyncio.sleep(0.5)
            emit_wait(False)
            current = await self.store.approval(auth,identifier)
            if current['state'] == 'allowed':
                return {'status':current['execution_state'],'result_ref':identifier,'end_turn':True,
                        'detail':'Owner approved; execution has not supplied its final receipt. Read approval.get.'}
            return {'status':'expired','error':{'code':'approval_expired'},'approval_id':identifier,'end_turn':True}
        return await self.execute(auth, approval)

    async def approve(self, auth, args):
        from core.execution.owner import require_owner
        await require_owner(auth, 'builder.approve')
        key = request_key(args.get('request_key'))
        decision = args.get('decision')
        if decision not in {'allow_once','allow_session','allow_project','deny'}:
            raise ExecutionError('invalid_approval_decision')
        row = await self.store.approval(auth,args.get('approval_id'))
        mode = await self.store.mode(auth,str(row['conversation_id']))
        is_network = decoded(row['payload']).get('operation') == 'network_request'
        if row['kind'] == 'workspace' and decision not in {'allow_once','allow_session','deny'}:
            raise ExecutionError('workspace_requires_allow_once',422)
        if decoded(row['payload']).get('operation') in {'fs_delete','checkpoint_restore'} and decision=='allow_session':
            raise ExecutionError('destructive_action_requires_allow_once',422)
        if decision == 'allow_project' and not is_network:
            raise ExecutionError('invalid_approval_decision')
        if is_network and (row['project_id'] != mode['project_id'] or row['session_id'] != mode['session_id']):
            raise ExecutionError('builder_session_expired',409)
        async with self.pool.acquire() as c, c.transaction():
            from .workspace import lock_id
            await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',lock_id(auth,row['conversation_id']))
            mode = await self.store.mode(auth,str(row['conversation_id']),connection=c)
            row = dict(await c.fetchrow('SELECT * FROM builder_approvals WHERE id=$1 FOR UPDATE',row['id']))
            # A workspace switch always invalidates pending approvals. A session end
            # keeps allow_once/deny usable (preimage checks guard stale diffs), but a
            # session allowance may only be granted inside the live session.
            if row['state'] == 'pending' and (row['project_id'] != mode['project_id'] or
                    (decision == 'allow_session' and row['session_id'] != mode['session_id'])):
                raise ExecutionError('builder_session_expired',409)
            if row['decision_key']:
                if row['decision_key'] != key or row['decision_hash'] != digest(args):
                    raise ExecutionError('idempotency_conflict',409)
            elif row['state'] == 'pending':
                state = 'denied' if decision=='deny' else 'allowed'
                if row['expires_at'] <= time.time():
                    state = 'expired'
                await c.execute('''UPDATE builder_approvals SET state=$2,decision_key=$3,decision_hash=$4
                    WHERE id=$1''',row['id'],state,key,digest(args))
                row['state'] = state
                if state == 'allowed' and decision == 'allow_session' and row['kind']=='workspace':
                    await c.execute('UPDATE builder_approvals SET task_authorized=TRUE WHERE id=$1',row['id'])
                    row['task_authorized']=True
                elif state == 'allowed' and decision == 'allow_session':
                    from .network import network_key
                    detail = decoded(row['detail'])
                    keys = [network_key(detail['domain'],detail['method'])] if is_network else allowance_keys(row['kind'],detail)
                    for key_value in set(keys):
                        await c.execute('''INSERT INTO builder_allowances VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING''',
                            auth.lane,auth.owner,row['conversation_id'],mode['session_id'],key_value)
                if state == 'allowed' and decision == 'allow_project':
                    await self.network.update_project(c,auth,row['project_id'],[decoded(row['detail'])['domain']])
        if is_network:
            return {'approval_id':row['id'],'state':row['state'],'result_ref':row['id']}
        if row['state'] == 'allowed' and mode.get('workflow_run_id'):
            # Persist the owner's decision here. Only the live controller may
            # dispatch, after revalidating its run, stage, mode and generation.
            return {'approval_id':row['id'],'state':'allowed','result_ref':row['id']}
        if row['state'] == 'allowed':
            result = await self.execute(auth,row)
            return {'approval_id':row['id'],'state':'allowed','result_ref':row['id'],'result':result,
                    'execution_state':'failed' if result.get('status') in {'failed','refused'} else
                    'completed' if result.get('status') in {'completed','succeeded'} else result.get('status','unknown')}
        return {'approval_id':row['id'],'state':row['state']}

    async def execute(self, auth, approval):
        if approval['execution_state'] in {'completed','failed','stopped','unknown'}:
            return decoded(approval['result']) or {'status':'unknown','approval_id':approval['id'],'end_turn':True}
        if approval.get('kind') == 'workspace':
            from .workspace import execute
            return await execute(self,auth,approval)
        row = await self.store.mode(auth,str(approval['conversation_id']))
        if row['project_id'] != approval['project_id'] or row['session_id'] != approval['session_id']:
            raise ExecutionError('builder_session_expired',409)
        if approval['expires_at'] <= time.time() and approval['execution_state'] == 'pending':
            raise ExecutionError('approval_expired',409)
        root = self.workspace(auth,row)
        if root is None:
            raise ExecutionError('workspace_unavailable',409)
        # Serialize all Builder mutations of the same project, across sessions.
        # Claim commits before dispatch; a crash leaves running/unknown and is
        # never automatically replayed as a second command.
        payload = decoded(approval['payload'])
        bound = payload['args'].get('timeout_s',COMMAND_DEFAULT_TIMEOUT) if payload['operation']=='shell_run' else 30
        claimed = await self.pool.fetchval("UPDATE builder_approvals SET execution_state='running',execution_until=$2 WHERE id=$1 AND state='allowed' AND execution_state='pending' RETURNING id",approval['id'],time.time()+bound+90)
        if not claimed:
            current = await self.store.approval(auth,approval['id'])
            return decoded(current['result']) or {'status':'unknown','approval_id':approval['id']}
        context = {'row':row,'approval_id':approval['id'],'dispatched':False}
        try:
            async with self.pool.acquire() as c, c.transaction():
                from .workspace import lock_id
                await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',lock_id(auth,approval['conversation_id']))
                # Do not hold a mode-row UPDATE lock while a worker is running:
                # Stop/logout must revoke the lease immediately. The binding
                # advisory lock still serializes workspace switches/mutations.
                row = dict(await c.fetchrow('''SELECT * FROM builder_modes WHERE lane=$1 AND owner=$2
                    AND conversation_id=$3''',auth.lane,auth.owner,approval['conversation_id']))
                if row['project_id'] != approval['project_id'] or row['session_id'] != approval['session_id']:
                    raise ExecutionError('builder_session_expired',409)
                root = self.workspace(auth,row)
                if root is None:raise ExecutionError('workspace_unavailable',409)
                await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',int(digest([auth.lane,str(row['project_id']),'builder-mutation'])[:15],16))
                from core.execution.workflow_context import current as workflow_current, writer_guard
                workflow = workflow_current.get()
                await writer_guard(c,auth,row['project_id'],run_id=workflow.row['id'] if workflow else None,
                                   generation=workflow.row['generation'] if workflow else None)
                if workflow: await workflow.controller.validate(workflow,tools=True)
                await self.store.checkpoint(auth,row,approval['turn_id'],root)
                payload = decoded(approval['payload'])
                if 'preimage' in payload:
                    try:
                        current, size = await asyncio.to_thread(files.read,root,payload['args']['path'])
                        actual=hashlib.sha256(current).hexdigest()
                        if size>len(current):raise ExecutionError('builder_preimage_changed',409)
                    except ResourceError as exc:
                        if exc.status!=404:raise
                        actual=None
                    if actual != payload['preimage']:
                        raise ExecutionError('builder_preimage_changed',409)
                    payload['args'] = {**payload['args'],'_preimage':payload['preimage']}
                if payload['operation']=='checkpoint_restore':
                    frozen = await c.fetchval('''SELECT snapshot FROM builder_checkpoints WHERE id=$1
                        AND lane=$2 AND owner=$3 AND conversation_id=$4 AND project_id=$5''',
                        payload['args']['checkpoint_id'],auth.lane,auth.owner,row['conversation_id'],row['project_id'])
                    if frozen is None:
                        raise ExecutionError('checkpoint_not_found',404)
                    payload['args'] = {'snapshot':decoded(frozen)}
                token = execution.set(context)
                try:
                    result = await self.worker_for_task(auth,root,payload['operation'],payload['args'],row)
                finally:
                    execution.reset(token)
                failed = result.get('exit_code',0)!=0 or bool(result.get('error'))
                state = 'stopped' if result.get('status')=='stopped' else 'failed' if failed else 'completed'
                result = {'approval_id':approval['id'],**result,'status':state}
            await self.pool.execute("UPDATE builder_approvals SET execution_state=$3,result=$2::jsonb WHERE id=$1",approval['id'],canonical(result),state)
            return result
        except BaseException as exc:
            if not context['dispatched'] and isinstance(exc,(ExecutionError,ResourceError)):
                result={'approval_id':approval['id'],'status':'refused','error':{'code':exc.code}}
                if getattr(exc,'diagnostics',None):result['diagnostics']=exc.diagnostics
                await self.pool.execute("UPDATE builder_approvals SET execution_state='failed',result=$2::jsonb WHERE id=$1",approval['id'],canonical(result))
                return result
            unknown = {'status':'unknown','approval_id':approval['id'],'error':{'code':'builder_outcome_unknown'},
                    'end_turn':True,'detail':'Execution receipt unavailable. Inspect this approval; do not repeat the action.'}
            await self.pool.execute("UPDATE builder_approvals SET execution_state='unknown',result=$2::jsonb WHERE id=$1",approval['id'],canonical(unknown))
            if not isinstance(exc,Exception):
                raise
            return unknown

    async def worker_for_task(self, auth, root, operation, args, row):
        task = asyncio.create_task(self.worker(auth,root,operation,args))
        try:
            while True:
                done, _ = await asyncio.wait({task},timeout=.5)
                if done:
                    return await task
                lease = await self.pool.fetchrow('''SELECT session_id,session_until FROM builder_modes
                    WHERE lane=$1 AND owner=$2 AND conversation_id=$3''',auth.lane,auth.owner,row['conversation_id'])
                live = lease and lease['session_id']==row['session_id'] and lease['session_until']>time.time()
                from core.execution.owner import owner_mapping, check_epoch
                if live and owner_mapping(auth.owner):
                    try:await check_epoch(auth)
                    except ExecutionError:live=False
                if not live:
                    task.cancel()
                    try:
                        # If completion won the race, retain its actual receipt.
                        return await task
                    except asyncio.CancelledError as exc:
                        if getattr(exc,'worker_cleanup',None)!='idle_proved':
                            raise ExecutionError('builder_stop_outcome_unknown',503) from None
                        return {'status':'stopped','end_turn':True,'cleanup':'idle_proved',
                                'execution_ids':exc.execution_ids,'error':{'code':'task_stopped'}}
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def worker(self, auth, root, operation, args):
        from core.execution.workflow_context import current as workflow_current
        workflow = workflow_current.get()
        from core.worker.client import execute_async
        from core.execution.jobs import JobContext, job_context
        from contextlib import AsyncExitStack
        network = None
        async with AsyncExitStack() as stack:
            if operation == 'shell_run' and not (workflow and workflow.spec.get('deterministic_check')):
                from core.worker.client import configuration
                from core.worker.contract import WorkerUnavailable
                from .network_client import JobNetwork
                context = execution.get()
                if context is None:
                    raise ExecutionError('builder_context_unavailable',503)
                try:
                    network = await stack.enter_async_context(JobNetwork(self,auth,context['row'],args['command'],
                        context['approval_id'],configuration(),args.get('timeout_s',COMMAND_DEFAULT_TIMEOUT)))
                except (OSError,ValueError,KeyError,asyncio.TimeoutError,WorkerUnavailable):
                    raise ExecutionError('builder_proxy_unavailable',503) from None
            payload = canonical({'adapter':'builder-command/1','operation':operation,'args':args,
                'network':bool(network),**({'proxy_url':network.url} if network else {})})
            if len(payload.encode()) > 900000:
                raise ExecutionError('builder_request_too_large',413)
            from core.execution.workflow_context import current as workflow_current
            workflow = workflow_current.get()
            with job_context(JobContext(auth.owner,auth.actor,'builder:'+auth.owner,auth.boundary,
                    workflow.row['id'] if workflow else None,workflow.row['generation'] if workflow else None)):
                # Proxy setup and payload validation cannot have run the command.
                # Once submission begins, failures still require an unknown receipt.
                context = execution.get()
                if context is not None:
                    context['dispatched'] = True
                receipt = await execute_async('builder',cwd=root,input=payload,
                            timeout=args.get('timeout_s',COMMAND_DEFAULT_TIMEOUT) if operation=='shell_run' else 30, output_limit=128*1024)
        if not isinstance(receipt, dict) or receipt.get('cleanup') != 'idle_proved':
            raise ExecutionError('builder_cleanup_unproved',503)
        out = receipt.get('stdout',b'')
        err = receipt.get('stderr',b'')
        if isinstance(out,bytes): out=out.decode('utf-8','replace')
        if isinstance(err,bytes): err=err.decode('utf-8','replace')
        if network:
            out, err = out.replace(network.token,'[job-token]'), err.replace(network.token,'[job-token]')
        result = {'exit_code':receipt['returncode'],'stdout':out,'stderr':err,
                  'duration':receipt.get('execution_seconds',0),'truncated':receipt.get('reason')=='output_limit',
                  'network':bool(network),'execution_id':receipt.get('execution_id'),'cleanup':receipt['cleanup']}
        if receipt.get('reason') in {'timeout','cancelled','output_limit'}:
            result['termination']=receipt['reason']
            if receipt['reason']=='timeout':result['error']={'code':'execution_timed_out'}
        if network:
            result.update(job_id=network.job_id,network_log=network.events,approvals=network.approvals)
        if operation != 'shell_run':
            try:
                value = json.loads(out)
                if not isinstance(value, dict) or value.get('adapter') != 'builder-command/1': raise ValueError()
                data = value.get('result', {})
                if not isinstance(data, dict): raise ValueError()
                result.update(data)
                if value.get('error'):result['error']={'code':value['error']}
                if isinstance(value.get('diagnostics'),dict):result['diagnostics']=value['diagnostics']
            except (ValueError,TypeError):
                result['error']={'code':'invalid_builder_receipt'}
        return result


async def invoke(operation, args):
    from core.execution.chat_context import current
    from core.execution.chat_records import POOL
    from core.tools.ask import _writer
    from core.execution.workflow_context import current as workflow_current
    workflow = workflow_current.get()
    if workflow:
        return canonical(await workflow.builder_tool(operation,args,_writer.get()))
    context = current.get()
    if not context or context['face_id']!='builder' or context['exp'] <= time.time() or POOL is None:
        return canonical({'status':'refused','error':{'code':'builder_context_unavailable'}})
    auth = Authority(**context['authority'])
    from core.execution.owner import check_epoch
    try:
        await check_epoch(auth)
        result = await Builder(POOL).tool(auth,context['conversation_id'],operation,args,writer=_writer.get())
        return canonical(result)
    except (ExecutionError,ResourceError) as exc:
        return canonical({'status':'refused','error':{'code':exc.code}})
