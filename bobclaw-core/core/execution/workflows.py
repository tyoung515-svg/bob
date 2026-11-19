"""Workflow contract v1 admission, owner decisions and durable report projections."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .models import Authority, ExecutionError, canonical, digest, integer, only, request_key, wire_integers
from . import workflow_schema as schema

UNAVAILABLE = 'workflow runner not installed in this build'
# Owner cancellation currently finishes as stopped; also honor explicit cancelled records.
TERMINAL = {'completed', 'stopped', 'cancelled', 'expired', 'failed', 'blocked', 'unknown'}
WORKSPACE_REUSE_NOTICE = 'Files from the earlier run are still in this workspace'
OWNER_CLICKS = {'grant_start', 'resume', 'gate'}
OWNER_WRITES = {'save', 'copy', 'delete', 'pause'}
FIELDS = {
    'catalog': set(), 'list': {'cursor','limit'}, 'get': {'id','revision'},
    'save': {'id','definition','expected_revision','request_key'},
    'copy': {'id','revision','name','request_key'}, 'delete': {'id','expected_revision','request_key'},
    'propose': {'definition_ref','definition','inputs','bindings','project_id','workspace','origin_conversation_id',
                'expires_at','run_mode','request_key'},
    'proposal': {'proposal_id'}, 'propose_again': {'proposal_id'},
    'grant_start': {'proposal_id','proposal_revision','proposal_digest','allow_same_model_review','request_key'},
    'runs': {'cursor','limit','project_id','status'}, 'run': {'run_id','stage_cursor'},
    'events': {'run_id','after_seq','limit'}, 'pause': {'run_id','expected_revision','request_key'},
    'resume': {'run_id','expected_revision','wait_id','request_key'}, 'stop': {'run_id','request_key'},
    'message': {'run_id','stage_key','text','request_key'},
    'gate': {'run_id','wait_id','decision','expected_revision','request_key'},
    'artifact': {'run_id','artifact_id','offset','max_bytes'},
}
MUTATIONS = {'save','copy','delete','propose','grant_start','pause','resume','stop','message','gate'}
LIMITS = {'run_controller_limit':2, 'workflow_parallelism':2, 'workflow_turn_seconds':1800, 'workflow_tool_rounds':100,
          'effective_worker_slots':1, 'snapshot_max_files':2048, 'snapshot_max_bytes':33554432,
          'snapshot_max_file_bytes':4194304,
          'max_calls':100000, 'max_active_seconds':604800, 'max_job_seconds':604800,
          'max_job_attempts':10000, 'max_grant_seconds':604800, 'max_expanded_leaves':4096}
PREMADES = json.loads(Path(__file__).with_name('workflow_premades.json').read_text(encoding='utf-8'))


def capabilities(storage_ready, runner_ready=False):
    return {'workflows.'+op:{'version':1,'contract_versions':[1],'implemented':True,
            'available':bool(storage_ready) and (runner_ready or op not in {'grant_start','resume'}),
            'reason':'execution_storage_unavailable' if not storage_ready else UNAVAILABLE if op in {'grant_start','resume'} and not runner_ready else None,
            'owner_session_required':op in OWNER_CLICKS,'request_key_required':op in MUTATIONS}
            for op in FIELDS}


def utc(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT.*(?:Z|\+00:00)', value):
        raise ExecutionError('invalid_timestamp')
    try:
        return datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        raise ExecutionError('invalid_timestamp') from None


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise ExecutionError('invalid_id')
    return value


def number(value, minimum=1, maximum=9223372036854775807):
    if isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]{0,18}', value):
        value = int(value)
    return integer(value, minimum, maximum)


class WorkflowRuntime:
    """Read-only resolution from the lane's real catalogs; no provider requests.

    WB2 may supply a richer evidence resolver through this interface. Missing
    catalog metadata is a blocker, never inferred from display model names.
    """
    def __init__(self, admission):
        self.admission = admission

    async def limits(self, auth):
        # Lane service environment is server-owned configuration, never an RPC
        # argument. Installed ceilings may narrow the schema's hard maxima.
        result = dict(LIMITS)
        for key, maximum in LIMITS.items():
            if key in {'effective_worker_slots','snapshot_max_files','snapshot_max_bytes','snapshot_max_file_bytes'}:
                continue  # Installed snapshot/worker limits are fixed in 1.0.
            value = os.environ.get('BOB_'+(key if key.startswith('workflow_') else 'workflow_'+key).upper())
            if value is not None:
                result[key] = number(value,1,16 if key in {'workflow_parallelism','run_controller_limit'} else maximum)
        return result

    async def bindings(self, auth):
        models = await self.admission.models()
        from core.faces.registry import get_registry
        try:
            from core.nodes.execute import _is_tool_enabled
        except ImportError:
            # A definitions-only installation may lack the interactive SDK.
            # Never invent a tool-capable binding in that environment.
            _is_tool_enabled = None
        faces = get_registry().all_faces()
        result = []
        for key, model in sorted((models or {}).items()):
            # The Codex CLI owns its coding loop; it cannot run Bob workflow tools.
            if model['backend'] == 'codex_code':
                continue
            tools = _is_tool_enabled('builder', model['backend']) if _is_tool_enabled else False
            reasons = [] if model.get('enabled') else [model.get('disabled_reason') or 'binding unavailable']
            controller=getattr(getattr(self.admission,'store',None),'workflow_controller',None)
            adapter=True
            if controller and controller.running:
                try:
                    from core.backends._lc_openai import tool_backend
                    adapter=tool_backend(model['backend'])
                except ImportError: adapter=False
                if not adapter: reasons.append('workflow_turn_adapter_unavailable')
            if not model.get('model_identity'):
                reasons.append('canonical_model_identity_unavailable')
            result.append({'binding_id':key, 'backend':model['backend'], 'model':model['model'],
                'model_identity':model.get('model_identity') or 'unknown',
                'family':model.get('family'), 'face_ids':model.get('face_ids', [f.id for f in faces]),
                'tool_capable':bool(tools), 'runnable':bool(model.get('enabled')) and adapter,
                'reasons':reasons,
                **({'reported_model_names':list(model['reported_model_names'])}
                   if model.get('reported_model_names') else {})})
        return result

    async def resolve(self, c, auth, args, d, planned_project):
        from .api import check_resources
        from core.tool_jobs.service import lane_root
        from core.portal_resources import load_folders
        project = args.get('project_id')
        workspace = args.get('workspace')
        origin = args.get('origin_conversation_id')
        await check_resources(self.admission.store.pool, auth, {'project_id':project,'conversation_id':origin})
        origin_row = None
        if origin:
            origin_row = await c.fetchrow('SELECT face_id,backend_preference,model_preference FROM conversations WHERE id=$1 AND user_id=$2 AND NOT is_archived', uuid.UUID(origin), auth.owner)
            if not origin_row or origin_row['face_id'] not in (None, 'assistant', 'planner'):
                raise ExecutionError('conversation_not_found',404)
        evidence = {}; evidence_bytes = 0
        for key, spec in d['inputs'].items():
            if spec['type'] != 'evidence_ids':
                continue
            for eid in args['inputs'].get(key, []):
                try:
                    mid = uuid.UUID(eid.removeprefix('message:'))
                except ValueError:
                    raise ExecutionError('evidence_not_found',404, eid) from None
                if auth.token_type == 'agent':
                    raise ExecutionError('evidence_not_found',404)
                row = await c.fetchrow('''SELECT m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id
                    WHERE m.id=$1 AND c.user_id=$2''', mid, auth.owner)
                if not row:
                    raise ExecutionError('evidence_not_found',404, eid)
                if len(row['content']) > 30000:
                    raise ExecutionError('evidence_too_large',422,'inputs.'+key+': expected evidence messages of at most 30000 characters each')
                evidence_bytes += len(row['content'].encode('utf-8'))
                if evidence_bytes>LIMITS['snapshot_max_bytes']:
                    raise ExecutionError('evidence_too_large',422,'inputs.'+key+': expected total evidence within '+str(LIMITS['snapshot_max_bytes'])+' bytes')
                evidence[eid] = {'digest':digest(row['content']), 'content':row['content']}
        registration = None; folder_record = None
        root = None
        if workspace:
            from .workbench import CHECKS
            expected = {'attachment_id':str(uuid.uuid5(uuid.NAMESPACE_URL,auth.lane+':'+planned_project)),
                        'project_id':planned_project,'owner':auth.owner,'path':'projects/'+planned_project,
                        'label':'BoB lane workspace '+planned_project[:8],
                        'checked_edits':{'checks':list(CHECKS),'write_paths':['app']}}
            registration = {'create':workspace['create'], 'project_id':planned_project,
                            'registration_digest':digest(expected),
                            'path_label':workspace['create']}
            folder_record = expected
        elif project:
            folder = next((f for f in load_folders(lane_root()) if f['owner']==auth.owner
                           and f['project_id']==project and f['path']=='projects/'+project), None)
            if not folder:
                raise ExecutionError('workspace_not_found',404)
            from core.builder.service import Builder
            root = Builder(self.admission.store.pool).workspace(auth, {'project_id':project})
            registration = {'registration_digest':digest(folder), 'path_label':folder['label']}
            folder_record = folder
        elif d['workspace']['required']:
            raise ExecutionError('workspace_required',422,'workspace: expected a new workspace object with create, or an existing project_id')
        from core.builder.policy import command_scope_reason
        for check in d['authority']['checks']:
            if root is None:
                # A new workspace is empty; still validate command/path syntax.
                root = lane_root()/'projects'/planned_project if workspace else lane_root()
            reason = command_scope_reason(root, {**check,'network':False})
            if reason:
                raise ExecutionError('unsafe_check',422,reason)
        from core.faces.registry import get_registry
        registry = get_registry(); skills = {}; faces = {}
        for _, stage in schema.stages(d):
            steps = [stage] if stage['kind']=='agent' else [stage[k] for k in ('build','review','fix')] if stage['kind']=='repair' else []
            for step in steps:
                face = d['agents'][step['agent']]['face_id']
                try:
                    face_obj = registry.get_face(face)
                    face_data = face_obj.model_dump(mode='json')
                    faces[face] = {'digest':digest(face_data),'definition':face_data}
                    for skill in step['skills']:
                        loaded = registry.load_skill(face, skill['skill_id'], skill['version'])
                        sha = loaded.get('sha256') or digest(loaded)
                        if skill['sha256'] is not None and skill['sha256'] != sha:
                            raise ExecutionError('skill_digest_mismatch',409,skill['skill_id'])
                        skills[face+'/'+skill['skill_id']] = {'version':1, 'sha256':sha,
                            'body':loaded['body'],'operations':loaded.get('operations',[])}
                except (KeyError, ValueError, PermissionError) as exc:
                    raise ExecutionError('face_or_skill_unavailable',409,face) from exc
        assistant_binding = None
        if origin_row:
            from core.lane_config import configured_mode,get_lane
            backend=origin_row['backend_preference'] or (get_lane().default_backend if configured_mode() else None)
            model=origin_row['model_preference']
            if not model and configured_mode():
                selected = get_lane().backends.get(backend)
                model = selected.model if selected else None
            origin_face = origin_row['face_id'] or 'assistant'
            candidates=[b for b in await self.bindings(auth) if b['runnable'] and origin_face in b['face_ids']
                        and b['backend']==backend and (not model or b['model']==model)]
            if len(candidates)!=1:
                raise ExecutionError('assistant_binding_unavailable',409,
                    'Select an enrolled model for this conversation agent before proposing a workflow.')
            # Keep the legacy wire key; freeze the actual proposing face for its summary.
            assistant_binding={**candidates[0], 'face_id':origin_face}
            face_data=registry.get_face(origin_face).model_dump(mode='json')
            faces[origin_face]={'digest':digest(face_data),'definition':face_data}
        return {'workspace':registration, 'registration':folder_record, 'evidence':evidence, 'skills':skills, 'faces':faces,
                **({'assistant_binding':assistant_binding} if assistant_binding else {})}

    async def create_workspace(self, c, auth, record):
        workspace = record['args'].get('workspace')
        if not workspace:
            return
        project = record['planned_project']
        await c.execute("INSERT INTO projects(id,user_id,name,description,default_face_id) VALUES($1,$2,$3,'Workflow workspace','builder')",
                        uuid.UUID(project), auth.owner, workspace['create'])
        from .workbench import ensure_workspace
        folder, _ = await ensure_workspace(c, auth, project)
        if digest(folder) != record['proposal']['coverage']['workspace']['registration_digest']:
            raise ExecutionError('proposal_stale',409)


def review_facts(d, bindings):
    pairs = []
    for _, s in schema.stages(d):
        if s['kind'] == 'repair':
            pairs.extend((s[k]['agent'], s['review']['agent']) for k in ('build','fix'))
        if s['kind'] == 'agent' and s.get('review_of'):
            # Review edges are sibling-scoped, including foreach bodies.
            siblings = next((x['body'] for _, x in schema.stages(d) if x['kind']=='foreach' and s in x['body']), d['stages'])
            pairs.append((next(x for x in siblings if x['id']==s['review_of'])['agent'], s['agent']))
    violations = []
    for author, reviewer in sorted(set(pairs)):
        a, b = bindings.get(author), bindings.get(reviewer)
        ok = bool(a and b)
        if ok and d['review_policy'] == 'different_model':
            ok = (bool(a['model_identity'] and b['model_identity'])
                  and all('canonical_model_identity_unavailable' not in x['reasons'] for x in (a,b))
                  and a['model_identity'] != b['model_identity'])
        if ok and d['review_policy'] == 'different_family':
            ok = bool(a['family'] and b['family'] and a['family'] != b['family'])
        if not ok:
            violations.append({'author':author,'reviewer':reviewer})
    return {'policy':d['review_policy'], 'policy_met':not violations, 'violations':violations,
            'same_model_choice_required':bool(violations)}


class WorkflowService:
    def __init__(self, admission, *, runtime=None, test_only_runner_available=False, clock=None, assistant_origin=None):
        self.store = admission.store
        self.runtime = runtime or WorkflowRuntime(admission)
        self.test_only_runner_available = test_only_runner_available
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # In-process WB2 tool callers set this only after authenticating their
        # Assistant conversation context. It is never accepted in RPC args.
        self.assistant_origin = assistant_origin

    @property
    def runner_available(self):
        controller = getattr(self.store,'workflow_controller',None)
        return bool(self.test_only_runner_available or (controller and controller.running))

    def now(self):
        return self.clock().isoformat()

    async def limits(self, auth):
        return await self.runtime.limits(auth) if hasattr(self.runtime,'limits') else dict(LIMITS)

    async def command(self, auth, operation, args):
        op = operation.removeprefix('workflows.')
        if op not in FIELDS:
            raise ExecutionError('unsupported_operation',404)
        try:
            only(args, FIELDS[op] | {'contract_version'})
        except ExecutionError as exc:
            if op=='propose':
                fields=', '.join(sorted(set(args)-FIELDS[op]-{'contract_version'})) if isinstance(args,dict) else 'args'
                raise ExecutionError(exc.code,422,fields+': expected only the documented proposal fields') from None
            raise
        if type(args.get('contract_version')) is not int or args['contract_version'] != 1:
            raise ExecutionError('workflow_contract_required',422,'contract_version: expected integer 1')
        args = deepcopy(args)
        for k in ('revision','expected_revision','proposal_revision','after_seq','limit','offset','max_bytes'):
            if args.get(k) is not None:
                args[k] = number(args[k], 0 if k in {'after_seq','offset'} else 1)
        for k in ('id','proposal_id','run_id','wait_id','artifact_id'):
            if k in args:
                identifier(args[k])
        if (not auth.owner or not auth.actor or auth.lane != self.store.lane
                or auth.token_type not in {'human','portal','agent'}):
            raise ExecutionError('not_found',404)
        if op in OWNER_CLICKS:
            from .owner import require_owner
            await require_owner(auth, 'workflows.'+op)
        elif auth.session_kind or auth.audience:
            raise ExecutionError('owner_audience_denied',403)
        if op in OWNER_WRITES and (auth.token_type not in {'human','portal'} or auth.actor != auth.owner):
            raise ExecutionError('not_found',404)
        if op == 'propose_again' and (auth.token_type not in {'human','portal'}
                or auth.actor != auth.owner or self.assistant_origin is not None):
            raise ExecutionError('proposal_again_not_permitted',403,
                'Only the signed-in owner can ask for this proposal again.')
        if op == 'propose':
            schema.proposal_args(args)
        if op in MUTATIONS:
            request_key(args.get('request_key'))
        receipt_payload = deepcopy(args)
        try:
            async with self.store.transaction(exclusive_grants=op in MUTATIONS or op == 'propose_again') as c:
                if op in MUTATIONS:
                    old = await self.store._mutation(c, auth, 'workflows.'+op, args['request_key'], receipt_payload)
                    if old:
                        return wire_integers({k:v for k,v in old['result'].items() if k!='operation_id'})
                result = await getattr(self, '_'+op)(c, auth, args)
                if op in MUTATIONS:
                    # Existing receipt storage adds operation_id internally; v1 wire is exact.
                    await self.store._remember(c, auth, 'workflows.'+op, args['request_key'], receipt_payload,
                                               args.get('run_id') or args.get('id') or args.get('proposal_id') or '', result)
                return wire_integers(result)
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise ExecutionError('invalid_args',422) from exc

    def eligibility(self, record):
        return {k:v for k,v in {**record, 'runnable':False,
            'reasons':['Select enrolled bindings and review a proposal before starting.'] if self.runner_available else [UNAVAILABLE]}.items() if k!='boundary'}

    async def definition(self, c, auth, id, revision=None):
        identifier(id)
        r = await self.store.wf_get(c, auth, 'definition', id, revision)
        if not r:
            raise ExecutionError('not_found',404)
        if r['deleted'] and revision is None:
            raise ExecutionError('not_found',404)
        return r

    async def seed(self, c, auth):
        for key, d in PREMADES.items():
            await self.store.wf_lock(c, auth, 'definition', key)
            if not await self.store.wf_get(c, auth, 'definition', key):
                await self.store.wf_definition(c, auth, self.new_definition(auth,key,d,origin='premade'))

    def new_definition(self, auth, id, d, origin='user', revision=1, created=None):
        return {'id':id, 'revision':revision, 'digest':digest(d), 'schema':schema.SCHEMA, 'definition':d,
                'origin':origin,'read_only':origin=='premade','deleted':False,
                'created_at':created or self.now(),'updated_at':self.now(),'boundary':auth.boundary}

    async def _catalog(self, c, auth, args):
        await self.seed(c, auth)
        return {'templates':[self.eligibility(await self.definition(c,auth,k,1)) for k in PREMADES],
                'bindings':await self.runtime.bindings(auth), 'limits':await self.limits(auth),
                'review_policy': {'explanation': "The platform checks the review policy when a proposal is made. An unmet policy needs the owner's explicit same-model review choice, which is recorded."},
                'available':self.runner_available, 'reasons':[] if self.runner_available else [UNAVAILABLE]}

    async def _list(self, c, auth, a):
        limit = integer(a.get('limit',20),1,50)
        rows = await self.store.wf_list(c,auth,'definition',a.get('cursor',''),limit+4)
        rows = [r for r in rows if r['origin']=='user']
        return {'items':[self.eligibility(r) for r in rows[:limit]], 'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None}

    async def _get(self, c, auth, a):
        if a['id'] in PREMADES:
            await self.seed(c,auth)
        return {'definition':self.eligibility(await self.definition(c,auth,a['id'],a.get('revision')))}

    async def _save(self, c, auth, a):
        d = schema.validate(a['definition']); key = a.get('id') or str(uuid.uuid4())
        identifier(key)
        if key in PREMADES:
            raise ExecutionError('premade_read_only',409)
        await self.store.wf_lock(c,auth,'definition',key)
        old = await self.store.wf_get(c,auth,'definition',key)
        if 'expected_revision' not in a or (old['revision'] if old else None) != a['expected_revision']:
            raise ExecutionError('revision_conflict',409)
        if old and old['deleted']:
            raise ExecutionError('definition_deleted',409)
        r = self.new_definition(auth,key,d,revision=old['revision']+1 if old else 1,
                                created=old['created_at'] if old else None)
        await self.store.wf_definition(c,auth,r)
        return {'definition':self.eligibility(r)}

    async def _copy(self, c, auth, a):
        if a['id'] in PREMADES:
            await self.seed(c,auth)
        old = await self.definition(c,auth,a['id'],number(a['revision']))
        d = deepcopy(old['definition']); d['name'] = schema.string(a['name'],1,80)
        r = self.new_definition(auth,str(uuid.uuid4()),schema.validate(d))
        await self.store.wf_definition(c,auth,r)
        return {'definition':self.eligibility(r)}

    async def _delete(self, c, auth, a):
        if a['id'] in PREMADES:
            raise ExecutionError('premade_read_only',409)
        r = await self.definition(c,auth,a['id'])
        if r['revision'] != a.get('expected_revision'):
            raise ExecutionError('revision_conflict',409)
        r.update(revision=r['revision']+1, deleted=True, updated_at=self.now())
        await self.store.wf_definition(c,auth,r)
        return {'id':r['id'], 'revision':r['revision'], 'deleted':True}

    async def coverage(self, c, auth, a, d, planned_project):
        resolved = await self.runtime.resolve(c,auth,a,d,planned_project)
        catalog = {b['binding_id']:b for b in await self.runtime.bindings(auth)}
        used = set()
        for _, s in schema.stages(d):
            if s['kind']=='agent': used.add(s['agent'])
            if s['kind']=='repair': used.update(s[k]['agent'] for k in ('build','review','fix'))
        for group in d.get('distinct_models',[]): used.update(group)
        only(a['bindings'], d['agents'])
        bindings = {}; reasons = []
        for alias in sorted(used):
            slot = d['agents'][alias]; selected = a['bindings'].get(alias,slot['binding_id'])
            if slot['binding_id'] and selected != slot['binding_id']:
                raise ExecutionError('binding_override_denied',422,'bindings.'+alias+': expected the binding_id pinned by the workflow definition')
            b = catalog.get(selected)
            if not b:
                raise ExecutionError('binding_required',409,alias)
            if slot['face_id'] not in b['face_ids'] or not b['runnable']:
                raise ExecutionError('binding_unavailable',409,alias)
            bindings[alias] = b
        for _, s in schema.stages(d):
            writers = [s['agent']] if s['kind']=='agent' and s['access']=='write' else [s[k]['agent'] for k in ('build','fix')] if s['kind']=='repair' else []
            for alias in writers:
                if not bindings[alias]['tool_capable']:
                    raise ExecutionError('tool_capable_binding_required',409,alias)
        for group in d.get('distinct_models',[]):
            identities = [bindings[x]['model_identity'] for x in group if x in bindings]
            if (len(identities)!=len(group) or len(set(identities))!=len(group)
                    or any('canonical_model_identity_unavailable' in bindings[x]['reasons'] for x in group)):
                raise ExecutionError('distinct_models_required',409,group)
        review = review_facts(d,bindings)
        if not review['policy_met']: reasons.append('review_policy_requires_explicit_same_model_choice')
        if not self.runner_available: reasons.append(UNAVAILABLE)
        limits = await self.limits(auth)
        if schema.bounds(d)>limits['max_expanded_leaves']:
            raise ExecutionError('expansion_limit',409)
        for k in d['budget']:
            if d['budget'][k] > limits[k if k=='max_job_attempts' else 'max_'+k]:
                raise ExecutionError('budget_limit',409,k)
        mode = a.get('run_mode','auto')
        if mode not in {'auto','approve'}:
            raise ExecutionError('invalid_run_mode')
        coverage = {'definition':d, 'inputs':a['inputs'],
                    'evidence_digests':{k:v['digest'] for k,v in resolved['evidence'].items()},
                    'bindings':bindings,'project_id':a.get('project_id') or planned_project,
                    'workspace':resolved['workspace'],'authority':d['authority'],'budget':d['budget'],
                    'grant_expires_at':a['expires_at'], 'run_mode':mode,
                    'run_modes':[{'id':'auto','label':'Work without asking in this workspace','default':True},
                                 {'id':'approve','label':'Ask before each change','default':False}],
                    'checks':d['authority']['checks'], 'checks_notice':'Checks listed will run.' if d['authority']['checks'] else 'No deterministic checks will run.',
                    'review':review, 'expanded_max':schema.bounds(d),
                    'skills':{k:{'version':v['version'],'sha256':v['sha256']} for k,v in resolved['skills'].items()},
                    'faces':{k:v['digest'] if isinstance(v,dict) else v for k,v in resolved['faces'].items()},
                    'assistant_summary_calls':1 if a.get('origin_conversation_id') else 0, 'limits':limits}
        if resolved.get('assistant_binding'): coverage['assistant_binding']=resolved['assistant_binding']
        return coverage, resolved, reasons

    async def _propose(self, c, auth, a):
        if ('definition' in a)==('definition_ref' in a):
            raise ExecutionError('definition_choice_required')
        ref = a.get('definition_ref')
        if ref:
            schema.obj(ref, {'id','revision'}); ref['revision'] = number(ref['revision'])
            if ref['id'] in PREMADES: await self.seed(c,auth)
            r = await self.definition(c,auth,ref['id'],ref['revision']); d = schema.validate(r['definition'])
            if r['deleted']: raise ExecutionError('definition_deleted',409)
        else:
            try:
                d = schema.validate(a['definition'])
            except ExecutionError as exc:
                raise ExecutionError(exc.code,exc.status,'definition: expected workflow/v1; '+str(exc.detail or exc.code)) from None
        schema.validate_inputs(d,a['inputs'])
        for alias,selected in a['bindings'].items():
            if alias not in d['agents'] or not isinstance(selected,str) or not selected:
                raise ExecutionError('invalid_args',422,'bindings.'+str(alias)+': expected a declared agent alias mapped to an enrolled binding_id from workflows_catalog')
        try:
            expires = utc(a['expires_at'])
        except ExecutionError as exc:
            raise ExecutionError(exc.code,422,'expires_at: expected a valid UTC ISO-8601 timestamp ending in Z or +00:00') from None
        limits = await self.limits(auth)
        if not self.clock() < expires <= self.clock()+timedelta(seconds=limits['max_grant_seconds']):
            raise ExecutionError('invalid_grant_expiry',422,'expires_at: expected a future UTC timestamp within '+str(limits['max_grant_seconds'])+' seconds of server time')
        if a.get('project_id'):
            try:
                a['project_id'] = str(uuid.UUID(a['project_id']))
            except (ValueError,TypeError,AttributeError):
                raise ExecutionError('invalid_args',422,'project_id: expected an existing project UUID') from None
        workspace = a.get('workspace')
        if workspace is not None:
            schema.obj(workspace, {'create'}); schema.string(workspace['create'],1,200)
            if not workspace['create'].strip() or any(ord(ch)<32 for ch in workspace['create']):
                raise ExecutionError('invalid_workspace_name')
            workspace['create']=workspace['create'].strip()
            if a.get('project_id'): raise ExecutionError('workspace_choice_required')
        project = str(uuid.uuid4()) if workspace else None
        coverage, resolved, reasons = await self.coverage(c,auth,a,d,project)
        p = {'id':str(uuid.uuid4()),'revision':1,'state':'proposed',
             'created_at':self.now(), 'origin_conversation_id':a.get('origin_conversation_id'),
             'expires_at':(self.clock()+timedelta(minutes=15)).isoformat(),'planned_run_id':str(uuid.uuid4()),
             'definition_ref':ref,'definition_digest':digest(d),'coverage':coverage,'runnable':not reasons,
             'reasons':reasons,'run_id':None}
        # Freeze all authority/evidence/face/skill inputs, not only the visible card.
        frozen = {'initiator':auth.as_dict(), 'resolved':resolved, 'coverage':coverage,
                  'planned_run_id':p['planned_run_id'], 'proposal_expires_at':p['expires_at'],
                  'definition_ref':ref, 'origin':a.get('origin_conversation_id')}
        p['digest'] = digest(frozen)
        record = {'proposal':p,'args':a,'frozen':frozen,'planned_project':project}
        await self.store.wf_proposal(c,auth,record,new=True)
        return {'proposal':p}

    async def proposal_record(self, c, auth, id):
        r = await self.store.wf_get(c,auth,'proposal',id)
        if not r: raise ExecutionError('not_found',404)
        if r['proposal']['state']=='proposed' and utc(r['proposal']['expires_at']) <= self.clock():
            r['proposal']['state']='expired'
            await self.store.wf_proposal(c,auth,r)
        return r

    async def _proposal(self, c, auth, a):
        return {'proposal':(await self.proposal_record(c,auth,a['proposal_id']))['proposal']}

    async def _propose_again(self, c, auth, a):
        from core.portal_resources import ResourceError
        old = await self.store.wf_get(c,auth,'proposal',a['proposal_id'])
        if not old:
            raise ExecutionError('proposal_not_found',404,
                'This proposal was not found for the signed-in owner.')
        p = old['proposal']
        expired = p['state'] == 'expired' or (p['state'] == 'proposed' and utc(p['expires_at']) <= self.clock())
        run = None
        if p.get('run_id'):
            run = await self.store.wf_get(c,auth,'run',p['run_id'])
            if not run:
                raise ExecutionError('run_not_found',404,
                    'The earlier run was not found for the signed-in owner.')
            if run['run']['status'] not in TERMINAL:
                raise ExecutionError('run_still_active',409,
                    'This run is still going. Wait for it to end before asking to run it again.')
            # The proposal row is locked by wf_get inside the exclusive admission
            # transaction, so simultaneous clicks cannot create two open copies.
            for id in reversed(p.get('proposed_again_ids',[])):
                candidate = await self.store.wf_get(c,auth,'proposal',id)
                if candidate:
                    fresh = candidate['proposal']
                    if (fresh['state']=='proposed' and not fresh.get('run_id')
                            and utc(fresh['expires_at'])>self.clock()):
                        return {'proposal':fresh}
        elif not expired:
            raise ExecutionError('proposal_not_expired',409,
                'Only an expired proposal that has not been started or cancelled can be asked for again.')
        args = deepcopy(old['args'])
        if run and args.get('workspace'):
            # Start creates the workspace before persisting a run, atomically.
            # Legacy runs therefore prove creation when they have a project id.
            # An explicit false receipt (or no registered project) means creation
            # did not happen; renew the original creation request in that case.
            if run.get('workspace_created',bool(run['run'].get('project_id'))):
                args.pop('workspace')
                args['project_id'] = run['run']['project_id']
        reused_workspace = bool(run and args.get('project_id'))
        args.pop('request_key',None)
        ref = p.get('definition_ref')
        if ref:
            current = await self.store.wf_get(c,auth,'definition',ref['id'])
            if not current or current['deleted']:
                raise ExecutionError('workflow_deleted',409,
                    'This workflow has been deleted. It cannot be proposed again.')
            args['definition_ref'] = {'id':current['id'],'revision':current['revision']}
        # Preserve the selected models, including choices originally supplied by
        # definition defaults. The normal admission path checks them again now.
        args['bindings'] = {alias:b['binding_id'] for alias,b in p['coverage']['bindings'].items()}
        created = utc(p['created_at']) if p.get('created_at') else utc(p['expires_at'])-timedelta(minutes=15)
        # Renew the requested grant horizon, not a grant. Start still displays and
        # checks this new absolute deadline and issues authority separately.
        args['expires_at'] = (self.clock()+(utc(args['expires_at'])-created)).isoformat()
        try:
            result = await self._propose(c,auth,args)
            # A fresh proposal is returned exactly as a first-time one would be,
            # caveats included (for example an unmet review policy, which the owner
            # may still accept explicitly at Start). Only a refused check stops it.
            fresh = result['proposal']
        except ExecutionError as exc:
            if reused_workspace and exc.code in {'project_not_found','workspace_not_found'}:
                raise ExecutionError('workspace_not_found',409,
                    'The workspace from the earlier run is no longer available. Choose another workspace before trying again.') from None
            checks = {
                'binding_unavailable':'the selected model is unavailable',
                'binding_required':'a selected model is no longer registered',
                'binding_override_denied':'the current workflow requires a different model',
                'tool_capable_binding_required':'the selected model cannot use the required tools',
                'budget_limit':'the workflow exceeds the current budget limit',
                'fresh_proposal_unavailable':'the proposal is currently unavailable',
            }
            reason = checks.get(exc.code,'the '+exc.code.replace('_',' ')+' check did not pass')
            detail = ' The check reported '+str(exc.detail).replace('_',' ').rstrip('.')+'.' if exc.detail is not None else ''
            raise ExecutionError('proposal_recheck_failed',409,
                'The fresh workflow check failed because '+reason+'.'+detail) from None
        except ResourceError:
            # A removed directory is distinct from a missing project/registration.
            # Keep filesystem diagnostics private and return owner-readable prose.
            if reused_workspace:
                raise ExecutionError('workspace_not_found',409,
                    'The workspace from the earlier run is no longer available. Choose another workspace before trying again.') from None
            raise
        if reused_workspace:
            # Informational: preserve runnable/review reasons and Start's checks.
            fresh['reasons'].append(WORKSPACE_REUSE_NOTICE)
        fresh['previous_proposal_id'] = p['id']
        fresh['definition_changed'] = fresh['definition_digest'] != p['definition_digest']
        fresh['definition_change_notice'] = (
            'The workflow definition has changed. This proposal uses the current definition.'
            if fresh['definition_changed'] else None)
        record = await self.store.wf_get(c,auth,'proposal',fresh['id'])
        record['proposal'] = fresh
        await self.store.wf_proposal(c,auth,record)
        if not run:
            p['state'] = 'expired'
        p.setdefault('proposed_again_ids',[]).append(fresh['id'])
        await self.store.wf_proposal(c,auth,old)
        return {'proposal':fresh}

    async def _grant_start(self, c, auth, a):
        r = await self.proposal_record(c,auth,a['proposal_id']); p = r['proposal']
        if p['revision'] != a['proposal_revision'] or p['digest'] != a['proposal_digest']:
            raise ExecutionError('proposal_stale',409)
        choice = a.get('allow_same_model_review',False)
        if type(choice) is not bool: raise ExecutionError('invalid_args')
        if p['state']=='granted':
            if r['same_model_choice'] != choice: raise ExecutionError('idempotency_conflict',409)
            w = (await self.run_record(c,auth,p['run_id']))['run']
            return {'run':self.run_page(w), 'grant_id':w['grant']['id']}
        if p['state'] != 'proposed': raise ExecutionError('proposal_expired',409)
        original = Authority(**r['frozen']['initiator'])
        if original.owner != auth.owner or original.lane != auth.lane:
            raise ExecutionError('not_found',404)
        # The epoch, including proposals created by scoped agents, cannot drift.
        if original.credential_epoch != auth.credential_epoch:
            raise ExecutionError('proposal_stale',409)
        ref = p['definition_ref']
        if ref:
            pinned = await self.definition(c,auth,ref['id'],ref['revision'])
            if pinned['deleted'] or pinned['digest'] != p['definition_digest']:
                raise ExecutionError('proposal_stale',409)
        coverage, resolved, reasons = await self.coverage(c,original,r['args'],p['coverage']['definition'],r['planned_project'])
        if coverage != p['coverage'] or resolved != r['frozen']['resolved']:
            raise ExecutionError('proposal_stale',409)
        if not coverage['review']['policy_met'] and not choice:
            raise ExecutionError('same_model_review_choice_required',409)
        if utc(coverage['grant_expires_at']) <= self.clock(): raise ExecutionError('proposal_expired',409)
        await self.runtime.create_workspace(c,auth,r)
        stages = []
        for key, s in schema.stages(coverage['definition']):
            # Body templates expand only after a real units output in WB2.
            if '/*/' in key: continue
            stages.append({'key':key,'ordinal':len(stages),'kind':s['kind'],'state':'pending','unit_id':None,
                'agent_alias':s.get('agent'),'conversation_id':None,'request_id':None,'actual_models':[],
                'input_digest':digest({'inputs':coverage['inputs'],'stage':key}),'output_artifact_id':None,'snapshot':None,'reason':None})
        w = {'id':p['planned_run_id'],'status':'accepted','reason':None if self.runner_available else UNAVAILABLE,'name':coverage['definition']['name'],
             'project_id':coverage['project_id'],'proposal_id':p['id'],'origin_conversation_id':r['args'].get('origin_conversation_id'),
             'revision':1,'event_seq':0,'created_at':self.now(),'updated_at':self.now(),
             'definition_ref':p['definition_ref'],'definition_digest':p['definition_digest'],
             'grant':{'id':str(uuid.uuid4()),'expires_at':coverage['grant_expires_at'],'active':True},
             'budget':{'limit':coverage['budget'],'consumed_calls':0,'reserved_calls':0,'unknown_calls':0,
                       'active_seconds':0,'job_seconds':0,'job_attempts':0},
             'stages':stages,'next_stage_cursor':None,'needs_you':[],'report':None}
        grant_coverage = {**coverage,'allow_same_model_review':choice,'resolved_digest':digest(resolved),
                          'same_model_review_downgrade':not coverage['review']['policy_met']}
        w['review'] = {**coverage['review'],'allow_same_model_review':choice,
                       'downgraded':not coverage['review']['policy_met']}
        record = {'run':w,'initiator':original.as_dict(),'boundary':original.boundary,
                  'coverage':grant_coverage,'allow_same_model_review':choice,
                  'resolved':resolved,'proposal_args':r['args'],'planned_project':r['planned_project'],
                  'workspace_created':bool(r['args'].get('workspace')),
                  'same_model_review_downgrade':not coverage['review']['policy_met']}
        await self.store.wf_start(c,auth,record,r)
        p.update(state='granted',run_id=w['id']); r['same_model_choice']=choice; r['grant_id']=w['grant']['id']
        await self.store.wf_proposal(c,auth,r)
        await self.event(c,auth,record,'run_state',{'status':'accepted','reason':w['reason']})
        return {'run':self.run_page(w), 'grant_id':w['grant']['id']}

    async def run_record(self, c, auth, id):
        r = await self.store.wf_get(c,auth,'run',id)
        if not r: raise ExecutionError('not_found',404)
        r['run']['grant']['active'] = bool(r['run']['grant']['active'] and
            await self.store.wf_grant_active(c,auth,r['run']['grant']['id']))
        return r

    def run_page(self, w, cursor=None):
        w = deepcopy(w)
        stages = w['stages']; start = 0
        if cursor is not None:
            keys = [s['key'] for s in stages]
            if cursor not in keys: raise ExecutionError('invalid_cursor')
            start = keys.index(cursor)+1
        w['stages'] = stages[start:start+50]
        w['next_stage_cursor'] = w['stages'][-1]['key'] if len(stages)>start+50 else None
        return w

    async def _run(self, c, auth, a):
        return {'run':self.run_page((await self.run_record(c,auth,a['run_id']))['run'],a.get('stage_cursor'))}

    async def _runs(self, c, auth, a):
        limit = integer(a.get('limit',20),1,50)
        if 'status' in a and a['status'] not in TERMINAL | {'accepted','queued','running','recovering','pausing','paused','needs_you','stopping'}:
            raise ExecutionError('invalid_status')
        # Scan through filtered rows so pagination never drops a matching run.
        cursor = a.get('cursor',''); found = []
        while len(found)<=limit:
            rows = await self.store.wf_list(c,auth,'run',cursor,51)
            if not rows: break
            for r in rows:
                w=r['run']; cursor=w['id']
                if ('project_id' not in a or w['project_id']==a['project_id']) and ('status' not in a or w['status']==a['status']):
                    found.append(w)
                    if len(found)>limit: break
            if len(rows)<51 or len(found)>limit: break
        fields = {'id','status','reason','name','project_id','proposal_id','origin_conversation_id','revision','event_seq','created_at','updated_at'}
        return {'items':[{k:v for k,v in w.items() if k in fields} for w in found[:limit]],
                'next_cursor':found[limit-1]['id'] if len(found)>limit else None}

    async def _events(self, c, auth, a):
        await self.run_record(c,auth,a['run_id'])
        after = number(a['after_seq'],0); limit=integer(a.get('limit',100),1,100)
        rows = await self.store.wf_events(c,auth,a['run_id'],after,limit+1)
        return {'events':rows[:limit], 'next_seq':rows[min(limit,len(rows))-1]['seq'] if rows else after, 'has_more':len(rows)>limit}

    async def event(self, c, auth, record, kind, payload, stage_key=None):
        w=record['run']; w['event_seq']+=1; w['revision']+=1; w['updated_at']=self.now()
        stage=next((s for s in w['stages'] if s['key']==stage_key),{})
        await self.store.wf_event(c,auth,{'run_id':w['id'],'seq':w['event_seq'],'revision':w['revision'],'at':self.now(),
            'kind':kind,'stage_key':stage_key,'conversation_id':stage.get('conversation_id'),'request_id':stage.get('request_id'),'payload':payload})
        await self.store.wf_run(c,auth,record)

    async def transition(self, c, auth, a, op):
        r=await self.run_record(c,auth,a['run_id']); w=r['run']
        if op in {'stop','message'} and self.assistant_origin is not None and w['origin_conversation_id']!=self.assistant_origin:
            raise ExecutionError('not_found',404)
        if op!='stop' and w['status'] in TERMINAL: raise ExecutionError('run_terminal',409)
        if op in {'pause','resume','gate'} and w['revision']!=a.get('expected_revision'):
            raise ExecutionError('revision_conflict',409)
        if op=='pause':
            if w['status'] not in {'accepted','queued','running','needs_you','recovering'}:
                raise ExecutionError('invalid_state',409)
            w['status']='paused' if w['status'] in {'accepted','queued'} else 'pausing'
        elif op=='resume':
            if a.get('wait_id'):
                wait=next((x for x in w['needs_you'] if x['wait_id']==a['wait_id'] and x['kind']=='recovery' and x['state']=='open'),None)
                if not wait or w['status']!='needs_you': raise ExecutionError('invalid_state',409)
                if hasattr(self.store,'wf_recovery_ready') and not await self.store.wf_recovery_ready(c,auth,r):
                    raise ExecutionError('worker_cleanup_pending',409)
                wait['state']='answered'
                await self.store.wf_wait(c,auth,w['id'],wait,{'decision':'Resume this stage','actor':auth.owner})
                stage=next(x for x in w['stages'] if x['key']==wait['stage_key'])
                stage.update(state='pending',reason=None,interrupted_attempt=True)
                await self.store.wf_stage(c,auth,w['id'],stage)
                w['needs_you'].remove(wait)
                await self.event(c,auth,r,'wait',{'wait':wait},stage['key'])
                await self.event(c,auth,r,'stage_state',{'stage':deepcopy(stage)},stage['key'])
            elif w['status']!='paused': raise ExecutionError('invalid_state',409)
            if utc(w['grant']['expires_at'])<=self.clock() or not w['grant']['active']:
                raise ExecutionError('grant_expired',409)
            w['status']='needs_you' if w['needs_you'] else 'queued'
        elif op=='stop':
            if w['status'] in TERMINAL: return {'run':self.run_page(w)}
            if w['status']=='stopping': return {'run':self.run_page(w)}
            # WB1 can prove an inert accepted run stopped. A dispatched row
            # stays stopping until WB2 establishes cleanup, never false success.
            if w['status'] not in {'accepted','queued','paused'} or (self.runner_available and not self.test_only_runner_available):
                w.update(status='stopping',reason='owner_stopped'); w['grant']['active']=False
                for wait in w['needs_you']:
                    wait['state']='expired'
                    await self.store.wf_wait(c,auth,w['id'],wait,{'reason':'The run was stopped.'})
                    await self.event(c,auth,r,'wait',{'wait':deepcopy(wait)},wait['stage_key'])
                w['needs_you']=[]
                await self.store.wf_request_stop(c,auth,r)
                await self.event(c,auth,r,'run_state',{'status':'stopping','reason':w['reason']})
                return {'run':self.run_page(w)}
            w.update(status='stopped',reason='owner_stopped'); w['grant']['active']=False
            for s in w['stages']: s['state']='stopped'
            for wait in w['needs_you']:
                wait['state']='expired'
                await self.store.wf_wait(c,auth,w['id'],wait,{'reason':'The run was stopped.'})
                await self.event(c,auth,r,'wait',{'wait':deepcopy(wait)},wait['stage_key'])
            w['needs_you']=[]
            await self.store.wf_stop(c,auth,r)
            await self.make_report(c,auth,r)
        elif op=='gate':
            if a.get('decision') not in {'approve','reject'}: raise ExecutionError('invalid_decision')
            wait=next((x for x in w['needs_you'] if x['wait_id']==a['wait_id'] and x['kind']=='gate' and x['state']=='open'),None)
            if not wait: raise ExecutionError('not_found',404)
            if wait['expires_at'] and utc(wait['expires_at'])<=self.clock(): raise ExecutionError('wait_expired',409)
            if not w['grant']['active'] or utc(w['grant']['expires_at'])<=self.clock(): raise ExecutionError('grant_expired',409)
            wait['state']='approved' if a['decision']=='approve' else 'rejected'
            task_approval=wait.get('record_id')=='task-approval'
            gate = next((stage for stage in w['stages'] if stage['key']==wait['stage_key'] and (stage['kind']=='gate' or task_approval)),None)
            if not gate: raise ExecutionError('not_found',404)
            gate.update(state='succeeded' if a['decision']=='approve' else 'blocked',
                        reason=None if a['decision']=='approve' else 'owner_rejected')
            if task_approval and a['decision']=='approve': gate.update(state='pending',task_approved=True)
            await self.store.wf_stage(c,auth,w['id'],gate)
            await self.store.wf_wait(c,auth,w['id'],wait,{'decision':a['decision'],'actor':auth.owner})
            await self.event(c,auth,r,'wait',{'wait':deepcopy(wait)},wait['stage_key'])
            await self.event(c,auth,r,'stage_state',{'stage':deepcopy(gate)},gate['key'])
            w['needs_you']=[x for x in w['needs_you'] if x['wait_id']!=wait['wait_id']]
            if a['decision']=='reject':
                w.update(status='blocked',reason='owner_rejected'); w['grant']['active']=False
                for other in w['needs_you']:
                    other['state']='expired'
                    await self.store.wf_wait(c,auth,w['id'],other,{'reason':'The owner rejected a required gate.'})
                    await self.event(c,auth,r,'wait',{'wait':deepcopy(other)},other['stage_key'])
                w['needs_you']=[]
                for stage in w['stages']:
                    if stage['state']=='pending': stage.update(state='skipped',reason='owner_rejected')
                await self.store.wf_stop(c,auth,r); await self.make_report(c,auth,r)
            elif w['status']=='needs_you' and not w['needs_you']: w['status']='queued'
        await self.event(c,auth,r,'run_state',{'status':w['status'],'reason':w['reason']})
        return {'run':self.run_page(w)}

    async def _pause(self,c,auth,a): return await self.transition(c,auth,a,'pause')
    async def _resume(self,c,auth,a): return await self.transition(c,auth,a,'resume')
    async def _stop(self,c,auth,a): return await self.transition(c,auth,a,'stop')
    async def _gate(self,c,auth,a): return await self.transition(c,auth,a,'gate')

    async def _message(self,c,auth,a):
        r=await self.run_record(c,auth,a['run_id']); w=r['run']
        if self.assistant_origin is not None and w['origin_conversation_id']!=self.assistant_origin:
            raise ExecutionError('not_found',404)
        if w['status'] in TERMINAL: raise ExecutionError('run_terminal',409)
        schema.string(a['text'],1,12000)
        stage=next((s for s in w['stages'] if s['key']==a['stage_key']),None)
        if not stage: raise ExecutionError('not_found',404)
        mid=str(uuid.uuid4())
        await self.store.wf_message(c,auth,w['id'],stage,mid,a['text'])
        await self.event(c,auth,r,'message',{'message_id':mid,'state':'queued'},a['stage_key'])
        return {'message_id':mid,'state':'queued','run_id':w['id'],'stage_key':a['stage_key']}

    async def make_report(self,c,auth,r,*,publish=True):
        w=r['run']
        executed=[s for s in w['stages'] if s.get('output') or s.get('calls') or s.get('receipts') or s.get('changed_files')]
        checks=[]
        for stage in w['stages']:
            checks.extend(stage.get('checks',[]))
            # Model shell activity stays in stage receipts; only the controller
            # writes deterministic check receipts to stage['checks'].
        caveats=[]
        caveats.extend(item['note'] for item in w.get('model_coverage',{}).values() if item.get('note'))
        if not checks: caveats.append('No deterministic check ran.')
        if r['same_model_review_downgrade']: caveats.append('Owner explicitly chose same-model review.')
        if any(s['state']=='interrupted' or s.get('interrupted_attempt') for s in w['stages']):
            caveats.append('An earlier attempt was interrupted. Its success is unverified and calls spent remain charged.')
        if w['status']!='completed': caveats.append('Required work remains unfinished. See the stage states and reason.')
        if not executed: caveats.append('No work was executed.')
        if not self.runner_available: caveats.append(UNAVAILABLE)
        summary_state=r.get('assistant_summary','pending' if w['origin_conversation_id'] else 'none')
        if w['origin_conversation_id'] and not r['coverage'].get('assistant_binding'):
            summary_state='skipped'
            caveats.append('Assistant summary skipped because no frozen Assistant binding was available.')
        if w['origin_conversation_id'] and w['budget']['consumed_calls']>=w['budget']['limit']['calls'] and summary_state!='delivered':
            summary_state='skipped'
            caveats.append('Assistant summary skipped because the call budget was spent.')
        if r.get('assistant_summary_note'): caveats.append(r['assistant_summary_note'])
        for stage in w['stages']:
            for omitted in stage.get('review_omitted',[]):
                caveats.append('Reviewer did not see '+omitted['path']+': '+omitted['reason'])
        files=[{'stage_key':s['key'],**f} for s in w['stages'] for f in s.get('changed_files',[])]
        fields={'key','kind','state','reason','reason_code','actual_models','binding','changed_files','checks','receipts','calls','snapshot','output','output_artifact_id','snapshot_note','interrupted_attempt','review_omitted','format_repairs','answer_artifacts'}
        stages=[{k:deepcopy(v) for k,v in s.items() if k in fields} for s in w['stages']]
        body={'outcome':w['status'],'reason':w['reason'],'reason_code':w.get('reason_code'),
              'summary':'Workflow '+w['status']+'. '+str(len(executed))+' stages performed work. '+
                        (str(len(checks))+' deterministic check receipts were recorded.' if checks else 'No deterministic check ran.')+
                        ' Calls used: '+str(w['budget']['consumed_calls'])+'.'+(' No work was executed.' if not executed else ''),
              'stages':stages,'files':files,'units':[s['output']['items'] for s in w['stages'] if s.get('output',{}).get('items')],
              'model_coverage':deepcopy(w.get('model_coverage',{})),
              'checks':checks,'caveats':caveats,'accounting':'per_model_request','calls_used':w['budget']['consumed_calls'],
              'budget':deepcopy(w['budget']),'review_downgraded':r['same_model_review_downgrade'],
              'assistant_summary':summary_state,'assistant_delivery':'delivered' if w['origin_conversation_id'] else 'none'}
        report_digest=digest(body); artifact='report-'+report_digest
        w['report']={'digest':report_digest,'artifact_id':artifact,**body}
        await self.store.wf_artifact(c,auth,w['id'],artifact,canonical(body).encode())
        if publish and r.get('published_report_digest')!=report_digest:
            await self.store.wf_report(c,auth,r)
            await self.event(c,auth,r,'report',{'report':w['report']})
            r['published_report_digest']=report_digest

    async def _artifact(self,c,auth,a):
        await self.run_record(c,auth,a['run_id'])
        row=await self.store.wf_artifact(c,auth,a['run_id'],a['artifact_id'])
        if not row: raise ExecutionError('not_found',404)
        raw=bytes(row['bytes']); offset=integer(a.get('offset',0),0,len(raw)); size=integer(a.get('max_bytes',65536),1,65536)
        end=min(len(raw),offset+size)
        try:
            raw.decode('utf8')
            if offset<len(raw) and raw[offset]&0xC0==0x80: raise ExecutionError('invalid_offset')
            while end<len(raw) and raw[end]&0xC0==0x80: end-=1
            if end==offset and end<len(raw): raise ExecutionError('max_bytes_too_small')
            content=raw[offset:end].decode('utf8'); encoding='utf8'
        except UnicodeDecodeError:
            content=base64.b64encode(raw[offset:end]).decode(); encoding='base64'
        return {'artifact_id':a['artifact_id'],'digest':row['digest'],'encoding':encoding,'content':content,
                'offset':offset,'next_offset':end if end<len(raw) else None,'total_bytes':len(raw)}


# Each public function always goes through the same strict boundary, including
# direct callers and future MCP/Assistant tools.
def _operation(name):
    async def call(self, auth, args):
        return await self.command(auth,'workflows.'+name,args)
    call.__name__=name
    return call


for _name in FIELDS:
    setattr(WorkflowService,_name,_operation(_name))
