"""Day-one recipes and setup over existing admission, grants and checked jobs."""
import asyncio
import os
from pathlib import Path
import uuid

from .models import ExecutionError, canonical, digest, integer, only, request_key, slug, text, LOOP_TOPOLOGY
from .store import decoded

CHECKS = ('python.compile','python.unittest')
DEFAULT = {'id':'python-basic','revision':1,'checks':list(CHECKS),
           'steps':['edit','check','checked-proposal'],'target':'bob-lane-workspace'}
OPERATIONS = frozenset({'work.catalog','work.recipe.save','work.prepare','work.start',
                       'work.status','work.preparation','work.workspace.create','work.publish'})


def capabilities(ready, job_time):
    from .owner import unavailable_reason
    reason=unavailable_reason() or (None if ready else 'execution_storage_or_executor_unavailable')
    checked=not reason and job_time.get('available') and 'checked' in job_time.get('profile_revisions',{})
    return {op:{'version':2,'contract_versions':[2],'implemented':True,
        'available':not reason and (bool(checked) if op in {'work.prepare','work.start','work.publish'} else True),
        'reason':reason or (None if checked or op not in {'work.prepare','work.start','work.publish'} else 'installed_checked_profile_required'),
        'owner_session_required':op in {'work.workspace.create','work.publish'},'target':'bob-lane-workspace'} for op in OPERATIONS}


def recipe(value):
    only(value, {'name','checks','expected_revision','request_key'})
    slug(value.get('name'))
    if value['name'] == DEFAULT['id']: raise ExecutionError('builtin_recipe_readonly',409)
    checks = value.get('checks')
    if (not isinstance(checks,list) or not checks or len(set(checks)) != len(checks)
            or any(c not in CHECKS for c in checks)):
        raise ExecutionError('registered_checks_required')
    expected = value.get('expected_revision')
    if 'expected_revision' not in value: raise ExecutionError('expected_revision_required')
    if expected is not None: integer(expected)
    request_key(value.get('request_key'))
    return {'id':value['name'],'checks':checks,'steps':['edit','check','checked-proposal'],
            'target':'bob-lane-workspace'}


def configured_binding(models, lane, *, backend=None, model=None):
    backend = backend or lane.default_backend
    configured = getattr(lane, 'backends', {}).get(backend)
    model = model or (configured.model if configured else None)
    candidates = sorted(k for k, m in (models or {}).items()
                        if m.get('enabled') and m.get('exact_calls') and m.get('backend') == backend)
    matches = [k for k in candidates if model is None or models[k]['model'] == model]
    if len(matches) != 1:
        raise ExecutionError('configured_model_binding_required', 409,
                             {'candidate_binding_ids': candidates, 'backend': backend, 'model': model})
    return matches[0]


class Workbench:
    def __init__(self, admission):
        self.admission,self.store = admission,admission.store

    async def command(self,auth,op,args,*,face=None,conversation_id=None):
        if op not in OPERATIONS: raise ExecutionError('unsupported_operation',404)
        if op == 'work.catalog': return await self.catalog(auth,args)
        if op == 'work.recipe.save': return await self.save_recipe(auth,args)
        if op == 'work.prepare':
            if face in {'builder','fixer'} and args.get('mode') != face:
                raise ExecutionError('face_work_mode_denied',403)
            if conversation_id is not None:
                if args.get('conversation_id') not in (None,conversation_id):
                    raise ExecutionError('work_conversation_mismatch',403)
                args={**args,'conversation_id':conversation_id}
            return await self.prepare(auth,args)
        if op == 'work.preparation':
            only(args,{'preparation_id'})
            return await self.preparation(auth,args['preparation_id'])
        if op == 'work.start': return await self.start(auth,args)
        if op == 'work.workspace.create': return await self.create_workspace(auth,args)
        if op == 'work.publish':
            from .publication import publish
            return await publish(self.admission,auth,args)
        if op == 'work.status':
            from core.tool_jobs.service import Jobs
            only(args,{'job_id'})
            jobs=Jobs(self.admission)
            row=await jobs.row(auth,args.get('job_id'))
            result=await jobs.audit(auth,row,{'job_id':row['id']})
            from .publication import blocking_publication
            from core.tool_jobs.service import lane_root
            folder=await jobs.attachment(auth,row['attachment_id'])
            async with self.store.transaction() as c:
                publication=await c.fetchrow('SELECT state,receipt FROM work_publications WHERE lane=$1 AND owner=$2 AND job_id=$3',auth.lane,auth.owner,row['id'])
                baseline=await c.fetchval("SELECT payload FROM checked_step_receipts WHERE lane=$1 AND owner=$2 AND run_id=$3 AND payload ? 'baseline' ORDER BY ordinal LIMIT 1",auth.lane,auth.owner,row['run_id'])
                blocker=await blocking_publication(c,auth,lane_root()/folder['path'],row['attachment_id'])
            proposal=None
            if result.get('proposal'):
                proposal=await jobs.proposal(auth,row)
                if len(proposal['diff'])>12000:
                    proposal={**proposal,'diff':proposal['diff'][:12000],'diff_truncated':True}
            return {**result,'checked_proposal':proposal,'baseline':decoded(baseline) if baseline else None,
                    'blocking_publication':blocker,
                    'publication':{'state':publication['state'],'receipt':decoded(publication['receipt'])} if publication else None}

    async def catalog(self,auth,args):
        only(args,set())
        from core.tool_jobs.service import Jobs
        from .owner import job_time_capabilities
        jobs=Jobs(self.admission)
        attachments=await jobs.command(auth,'jobs.attachments',{})
        async with self.store.transaction() as c:
            projects=await c.fetch('SELECT id,name FROM projects WHERE user_id=$1 AND is_archived=FALSE ORDER BY updated_at DESC LIMIT 100',auth.owner)
            recipes=await c.fetch('SELECT DISTINCT ON(id) id,revision,definition FROM work_recipes WHERE lane=$1 AND owner=$2 ORDER BY id,revision DESC LIMIT 100',auth.lane,auth.owner)
            preparations=await c.fetch('SELECT w.id,w.request FROM work_preparations w JOIN projects p ON p.id=(w.request->>\'project_id\')::uuid AND p.user_id=w.owner AND p.is_archived=FALSE WHERE w.lane=$1 AND w.owner=$2 AND w.boundary=$3 ORDER BY w.created_at DESC LIMIT 20',auth.lane,auth.owner,auth.boundary)
        visible={r['project_id'] for r in attachments['items']}
        return {'target':'bob-lane-workspace','windows_folder_access':False,
                'projects':[{'id':str(p['id']),'name':p['name']} for p in projects
                            if auth.token_type!='agent' or str(p['id']) in visible],
                'models':await self.admission.models(),'recent_jobs':await jobs.command(auth,'jobs.list',{'limit':20}),
                'recent_preparations':[{'preparation_id':p['id'],**decoded(p['request'])} for p in preparations],
                'teams':await self.admission.teams(auth,{}),
                'recipes':[DEFAULT,*[{**decoded(r['definition']),'revision':r['revision']} for r in recipes]],
                'attachments':attachments,'publication':'owner-reviewed checked output',
                'capabilities':await job_time_capabilities(execution_ready=self.admission.enabled)}

    async def save_recipe(self,auth,args):
        definition=recipe(args)
        async with self.store.transaction() as c:
            prior=await self.store._mutation(c,auth,'work.recipe.save',args['request_key'],args)
            if prior:return prior['result']
            await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',int(digest([auth.lane,auth.owner,'recipe',definition['id']])[:15],16))
            previous=await c.fetchval('SELECT max(revision) FROM work_recipes WHERE lane=$1 AND owner=$2 AND id=$3',auth.lane,auth.owner,definition['id'])
            if previous!=args['expected_revision']:raise ExecutionError('revision_conflict',409)
            revision=(previous or 0)+1
            await c.execute('INSERT INTO work_recipes VALUES($1,$2,$3,$4,$5::jsonb)',auth.lane,auth.owner,definition['id'],revision,canonical(definition))
            return await self.store._remember(c,auth,'work.recipe.save',args['request_key'],args,definition['id'],
                {'recipe':{**definition,'revision':revision},'started':False})

    async def prepare(self,auth,args):
        only(args,{'project_id','task','mode','recipe','request_key','start_calls','conversation_id'})
        from .api import check_resources
        from core.tool_jobs.service import Jobs
        from core.worker.client import configuration
        from core.worker.contract import profile_revision
        from .checked import CONTRACT
        await check_resources(self.store.pool,auth,{k:args[k] for k in ('project_id','conversation_id') if k in args},allow_agent_project=True)
        uuid.UUID(args['project_id'])
        text(args.get('task'),12000)
        if args.get('mode') not in {'builder','fixer'}:raise ExecutionError('invalid_work_mode')
        key=request_key(args.get('request_key'))
        calls=integer(args.get('start_calls',10),2,200)
        identifier=digest([auth.lane,auth.owner,auth.boundary,'work.prepare',key])
        async with self.store.transaction() as c:
            previous=await c.fetchrow('SELECT request FROM work_preparations WHERE lane=$1 AND owner=$2 AND id=$3',auth.lane,auth.owner,identifier)
        if previous:
            if decoded(previous['request'])!=args:raise ExecutionError('idempotency_conflict',409)
            return await self.preparation(auth,identifier)
        selected=DEFAULT if args.get('recipe')==DEFAULT['id'] else None
        if selected is None:
            async with self.store.transaction() as c:
                row=await c.fetchrow('SELECT revision,definition FROM work_recipes WHERE lane=$1 AND owner=$2 AND id=$3 ORDER BY revision DESC LIMIT 1',auth.lane,auth.owner,args.get('recipe'))
            if not row:raise ExecutionError('recipe_not_found',404)
            selected={**decoded(row['definition']),'revision':row['revision']}
        if args['mode']=='fixer' and 'python.unittest' not in selected['checks']:
            raise ExecutionError('fixer_unittest_required',409)
        jobs=Jobs(self.admission)
        attachments=await jobs.command(auth,'jobs.attachments',{})
        target=next((r for r in attachments['items'] if r['project_id']==args['project_id'] and r['coverage_target']['cwd']==args['project_id']),None)
        if not target:
            return {'status':'needs_workspace','target':'bob-lane-workspace',
                    'action':{'schema_version':1,'operation':'work.workspace.create',
                              'resource':{'project_id':args['project_id']},'request_key':'workspace:'+identifier},
                    'detail':'Create the owned lane workspace in the review card, then prepare again with this same key.'}
        cfg=configuration()
        models=await self.admission.models()
        # Match the configured default exactly; never silently choose a paid fallback.
        from core.lane_config import get_lane
        preferences={}
        if args.get('conversation_id'):
            async with self.store.transaction() as c:
                conversation=await c.fetchrow('SELECT backend_preference,model_preference FROM conversations WHERE id=$1 AND user_id=$2',uuid.UUID(args['conversation_id']),auth.owner)
            if not conversation:raise ExecutionError('not_found',404)
            preferences={'backend':conversation['backend_preference'],'model':conversation['model_preference']}
        binding=configured_binding(models,get_lane(),**preferences)
        team_id='direct-'+digest([args['project_id'],binding])[:24]
        definition={'schema_version':1,'id':team_id,'state':'saved','topology':LOOP_TOPOLOGY,
                    'roles':{r:binding for r in ('manager','worker','critic')},'review_independence':'same_model'}
        try:
            saved=await self.admission.save(auth,{'definition':definition,'expected_revision':None,
                'request_key':'work-team:'+digest(definition)})
        except ExecutionError as exc:
            if exc.code!='revision_conflict':raise
            raise ExecutionError('work_team_conflict',409) from None
        coverage={'profiles':{'checked':profile_revision(cfg['profiles']['checked'])},
                  'workspaces':{'projects':[target['coverage_target']['cwd']]},
                  'attachments':[target['id']],'max_job_attempts':16,'job_seconds_budget':3600,'allow_cli':False}
        request={'contract_version':2,'team':team_id,'team_revision':saved['team']['revision'],
                 'task':args['task'],'project_id':args['project_id'],'request_key':'job:'+identifier,
                 'budget':{'start_calls':calls,'wall_mode':'fixed','wall_seconds':3600,
                           'max_job_attempts':16,'job_seconds_budget':3600},
                 'job_coverage':coverage,'worker_contract':CONTRACT,'work_mode':args['mode'],
                 'work_recipe':{'id':selected['id'],'revision':selected['revision'],
                                'definition':selected,'digest':digest(selected)},
                 **({'conversation_id':args['conversation_id']} if args.get('conversation_id') else {}),
                 'acceptance':{'criteria':[{'id':'checked-output','required':True,'output_kind':'proposal',
                     'checks':{'required_check_ids':selected['checks']}}]}}
        proposal=await self.admission.prepare(auth,{'request':request,'request_key':'grant:'+identifier})
        job_request={**request,'role':'coder','attachment':target['id'],
                     'intent_id':proposal['intent_id'],
                     'job_policy':{'proposed_seconds':900,'rationale':'Bounded direct Python build/fix',
                                   'auto_escalate':False,'custom_limit':False}}
        async with self.store.transaction() as c:
            await c.execute('INSERT INTO work_preparations(lane,owner,id,boundary,request,job_request,grant_proposal) VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb) ON CONFLICT DO NOTHING',
                auth.lane,auth.owner,identifier,auth.boundary,canonical(args),canonical(job_request),canonical(proposal))
            stored=await c.fetchval('SELECT request FROM work_preparations WHERE lane=$1 AND owner=$2 AND id=$3',auth.lane,auth.owner,identifier)
            if decoded(stored)!=args:raise ExecutionError('idempotency_conflict',409)
        return await self.preparation(auth,identifier)

    async def preparation(self,auth,identifier):
        if not isinstance(identifier,str) or len(identifier)!=64:raise ExecutionError('not_found',404)
        async with self.store.transaction() as c:
            row=await c.fetchrow('SELECT * FROM work_preparations WHERE lane=$1 AND owner=$2 AND id=$3',auth.lane,auth.owner,identifier)
        if not row or row['boundary']!=auth.boundary:raise ExecutionError('not_found',404)
        from .api import check_resources
        request=decoded(row['job_request'])
        await check_resources(self.store.pool,auth,{k:request[k] for k in ('project_id','conversation_id') if k in request},allow_agent_project=True)
        proposal=decoded(row['grant_proposal'])
        return {'preparation_id':identifier,'status':'prepared','target':'bob-lane-workspace',
                'project_id':request['project_id'],'task':request['task'],'recipe':request['work_recipe']['id'],
                'recipe_revision':request['work_recipe']['revision'],
                'proposal':proposal,'action':{'schema_version':1,'operation':'grants.issue',
                    'preparation_id':identifier,'resource':{'proposal_id':proposal['proposal_id']}}}

    async def start(self,auth,args):
        only(args,{'preparation_id','request_key'})
        request_key(args.get('request_key'))
        await self.preparation(auth,args.get('preparation_id'))
        async with self.store.transaction() as c:
            row=await c.fetchrow('SELECT job_request FROM work_preparations WHERE lane=$1 AND owner=$2 AND id=$3',auth.lane,auth.owner,args['preparation_id'])
        from core.tool_jobs.service import Jobs
        result=await Jobs(self.admission).start(auth,{**decoded(row['job_request']),'request_key':args['request_key']})
        return {**result,'target':'bob-lane-workspace','files_published':False}

    async def create_workspace(self,auth,args):
        from .owner import require_owner
        await require_owner(auth,'work.workspace.create')
        only(args,{'project_id','request_key'})
        from .api import check_resources
        await check_resources(self.store.pool,auth,args)
        project=str(uuid.UUID(args['project_id']))
        key=request_key(args.get('request_key'))
        async with self.store.transaction() as c:
            previous=await self.store._mutation(c,auth,'work.workspace.create',key,args)
            if previous:return previous['result']
            folder, created = await ensure_workspace(c, auth, project)
            if not created:
                return await self.store._remember(c,auth,'work.workspace.create',key,args,folder['attachment_id'],
                    {'workspace':folder,'created':False})
            return await self.store._remember(c,auth,'work.workspace.create',key,args,folder['attachment_id'],
                {'workspace':folder,'created':created,'target':'bob-lane-workspace','windows_folder_access':False})


async def ensure_workspace(c, auth, project):
    """Register a lane UUID workspace under the existing lane-wide lock.

    Caller must check project ownership and authorize the mutation first.
    Filesystem preparation precedes binding; failed DB commits may leave an
    unbound empty registration, never a partially switched conversation.
    """
    from core.tool_jobs.service import lane_root
    from core.portal_resources import load_folders, safe_open
    root = lane_root()
    await c.execute('SELECT pg_advisory_xact_lock($1::bigint)',int(digest([auth.lane,'project-folders'])[:15],16))
    folders=load_folders(root)
    relative='projects/'+project
    existing=next((f for f in folders if f['path']==relative),None)
    if existing:
        if existing['owner']!=auth.owner or existing['project_id']!=project:raise ExecutionError('workspace_conflict',409)
        return existing, False
    if len(folders)>=1000:raise ExecutionError('workspace_registry_full',409)
    # No caller filesystem path. Only the lane's registered projects root.
    with safe_open(root,('projects',),directory=True) as (fd,parent,_):
        try:
            if fd is None:(parent/project).mkdir(mode=0o2770)
            else:os.mkdir(project,mode=0o2770,dir_fd=fd)
        except FileExistsError:
            with safe_open(root,('projects',project),directory=True) as (child,directory,_):
                if os.listdir(child if child is not None else directory):
                    raise ExecutionError('unregistered_workspace_not_empty',409)
    folder={'attachment_id':str(uuid.uuid5(uuid.NAMESPACE_URL,auth.lane+':'+project)),
            'project_id':project,'owner':auth.owner,'path':relative,
            'label':'BoB lane workspace '+project[:8],
            'checked_edits':{'checks':list(CHECKS),'write_paths':['app']}}
    folders.append(folder)
    # Atomic registry replacement under the lane-wide registration lock.
    from core.worker.workspace import write
    await asyncio.to_thread(write,root/'project-folders.json',canonical(folders))
    return folder, True
