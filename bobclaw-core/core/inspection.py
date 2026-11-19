"""Read-only install/project inspection. No conversation binding or mutation API.

The core account already reads installed source and registered lane workspaces.
Only regex search and fixed git commands cross the existing isolated worker.
"""
import asyncio
import fnmatch
import json
import os
import re
from pathlib import Path
import uuid

from core.execution.models import Authority, ExecutionError, integer, only
from core.portal_resources import ResourceError, relative_parts, safe_open, scope_allows
from core.builder import files

OPERATIONS = frozenset({'projects_list', 'project_read', 'project_files_list',
    'project_files_read', 'project_files_search', 'project_git', 'bob_installation',
    'bob_source_list', 'bob_source_read', 'bob_source_search'})
PROGRAM_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = {
    'bobclaw-core': PROGRAM_ROOT / 'bobclaw-core',
    'bobclaw-gateway': PROGRAM_ROOT / 'bobclaw-gateway',
    'portal-broker': PROGRAM_ROOT / 'portal-broker',
    'deploy-runtime': PROGRAM_ROOT / 'deploy/runtime',
    'runtime-library': Path('/usr/local/lib/bob_runtime'),
}
SOURCE_SUFFIXES = {'.py', '.md', '.yaml', '.yml', '.toml', '.json', '.sh', '.ps1',
                   '.service', '.socket', '.timer', '.conf', '.txt', '.ini'}
SOURCE_NAMES = {'Containerfile', 'Makefile', 'bob-firstboot', 'bob-lane', 'bob-health',
    'bob-embedder', 'bob-broker', 'bob-model', 'bob-settings', 'bob-token-renew',
    'bob-project-folder', 'bob-runtime'}


def redact(value):
    """Defence in depth for accidental embedded credentials in allowed text."""
    value = re.sub(r'(?i)bearer\s+[A-Za-z0-9_./+\-=]+|(?:sk-|ghp_|eyJ)[A-Za-z0-9_./+\-=]{12,}', '[redacted]', str(value or ''))
    return re.sub(r'''(?im)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)["']?\s*[:=]\s*)["'][^"'\r\n]+["']''', r'\1"[redacted]"', value)


def read_parts(path, *, empty=False):
    parts = relative_parts(path, empty=empty)
    for part in parts:
        name = part.casefold()
        if (name in {'vault','vaults','auth.json','signin.json','sign-in.json','sign_in.json',
                'auth-store.json','auth_store.json','credentials.json','credential-journal.json'}
                or name.endswith(('.db','.sqlite','.sqlite3','.db-wal','.db-shm','.env.local'))
                or name.endswith(('.p12','.pfx','.jks'))):
            raise ResourceError('scope_denied',403)
    return parts


def source_parts(path, *, empty=False):
    parts = read_parts(path, empty=empty)
    for part in parts:
        name = part.casefold()
        if (any(word in name for word in ('credential', 'secret', 'vault', 'journal',
                'signin', 'sign-in', 'sign_in', 'auth-store', 'auth_store',
                'oauth', 'cookie', 'token', 'password', 'private', 'account'))
                and name not in {'credentials.py', 'credential_state.py', 'credential_auth.py',
                                 'journal.py', 'bob-token-renew', 'bob-credentials@.service'}) or name in {
                'data', 'logs', 'runs', 'test-db', 'auth.json', 'sessions', 'state',
                'env', 'environment', 'family', 'lanes', 'codex-home'}:
            raise ResourceError('scope_denied', 403)
    return parts


def source_listing(root, path='', scope=None):
    source_parts(path, empty=True)
    result = files.listing(root, path)
    items = []
    for name in result['items']:
        child = '/'.join(filter(None, (path, name)))
        if scope is not None and not scope_allows(scope, root/child):
            continue
        try:
            parts = source_parts(child)
            with safe_open(root, parts, directory=True):
                pass
        except ResourceError:
            try:
                source_parts(child)
                if Path(name).suffix not in SOURCE_SUFFIXES and name not in SOURCE_NAMES:
                    continue
                with safe_open(root, relative_parts(child)) as (_, _, info):
                    if info.st_nlink != 1:
                        continue
            except ResourceError:
                continue
        items.append(name)
    return {'path': path, 'items': items}


def source_read(root, path, offset=0, max_bytes=65536):
    source_parts(path)
    if Path(path).suffix not in SOURCE_SUFFIXES and Path(path).name not in SOURCE_NAMES:
        raise ResourceError('scope_denied', 403)
    integer(max_bytes, 1, files.READ_CAP)
    with safe_open(root, source_parts(path)) as (_, _, info):
        if os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022):
            raise ResourceError('installed_source_protection_required',403)
    data, size = files.read(root, path, offset, max_bytes)
    # Source is text. Reject binaries even when disguised by an allowed suffix.
    if b'\x00' in data:
        raise ResourceError('source_text_required', 403)
    text = data.decode('utf-8', 'replace')
    return {'path': path, 'content': redact(text), 'redacted':redact(text)!=text, 'size': size,
            'next_offset': offset + len(data), 'truncated': offset + len(data) < size}


def source_search(root, pattern, glob='*', scope=None):
    """Bounded literal search; no attacker-controlled regex in the core process."""
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 512 or not isinstance(glob, str) or len(glob)>512:
        raise ExecutionError('invalid_search')
    matches, pending, visited, remaining = [], [''], 0, 4*1024*1024
    truncated = False
    while pending:
        path = pending.pop()
        for name in source_listing(root, path, scope)['items']:
            visited += 1
            if visited > 2048 or remaining <= 0:
                return {'matches': matches, 'truncated': True, 'search_kind': 'literal'}
            child = '/'.join(filter(None, (path, name)))
            if scope is not None and not scope_allows(scope, root/child):
                continue
            try:
                with safe_open(root, source_parts(child), directory=True):
                    pass
            except ResourceError:
                if not fnmatch.fnmatchcase(child, glob):
                    continue
                try:
                    result = source_read(root, child, max_bytes=min(65536, remaining))
                except ResourceError:
                    continue
                remaining -= min(result['size'], 65536)
                truncated |= result['truncated']
                for number, line in enumerate(result['content'].splitlines(), 1):
                    if pattern in line:
                        matches.append({'path': child, 'line': number, 'text': line[:500]})
                        if len(matches) == 100:
                            return {'matches': matches, 'truncated': True, 'search_kind': 'literal'}
            else:
                pending.append(child)
    return {'matches': matches, 'truncated': truncated, 'search_kind': 'literal'}


def build_version():
    from core.guide import public_id
    try:
        data = json.loads(Path('/etc/bob/runtime-version').read_text(encoding='utf-8'))
        return {k: public_id(data.get(k)) for k in ('build_id', 'git_rev')}
    except (OSError, ValueError, AttributeError):
        return {'build_id': 'unknown', 'git_rev': 'unknown'}


async def authorize(auth):
    from core.execution.api import lane_id, policy_epoch
    from core.execution.owner import owner_mapping, check_epoch
    from core.config import config
    if (auth.lane != lane_id() or not auth.owner or not auth.actor
            or not isinstance(auth.scope, dict) or auth.token_type not in {'human', 'portal', 'agent'}
            or auth.session_kind or auth.audience or not owner_mapping(auth.owner)
            or not auth.policy_epoch or auth.policy_epoch != policy_epoch(config.BOBCLAW_SECRET)):
        raise ExecutionError('inspection_not_authorized', 403)
    await check_epoch(auth)


def scoped(auth, root):
    # Native owner sessions retain this install's read entitlement. Delegated
    # credentials must explicitly cover each resource with their existing scope.
    if auth.token_type == 'agent' and not scope_allows(auth.scope, root):
        raise ExecutionError('not_found', 404)


async def git_summary(auth, root):
    from core.worker.client import execute_async
    from core.execution.jobs import JobContext, job_context
    # A repository belongs to this exact workspace, never an ancestor repository
    # or a gitdir indirection to another project. No .git content is returned.
    try:
        with safe_open(root, ('.git',), directory=True):
            pass
    except ResourceError as exc:
        if exc.status == 404:
            return {'is_repository': False}
        raise ExecutionError('git_directory_refused', 403) from None

    async def run(*args, unborn=False):
        fixed = ['--no-optional-locks', '-c', 'core.fsmonitor=false', '-c',
                 'core.hooksPath=/dev/null', '-c', 'log.showSignature=false',
                 '-c', 'core.untrackedCache=false', *args]
        with job_context(JobContext(auth.owner, auth.actor, 'inspection:'+auth.owner, auth.boundary)):
            receipt = await execute_async('git', cwd=root, args=fixed, timeout=15, output_limit=65536)
        if (receipt.get('cleanup') != 'idle_proved' or receipt.get('reason') in {'timeout','output_limit','cancelled'}):
            raise ExecutionError('git_observation_incomplete', 503)
        if receipt.get('returncode') != 0:
            if unborn and receipt.get('returncode') in {1,128}:
                return ''
            raise ExecutionError('git_observation_failed', 409)
        data = receipt.get('stdout', '')
        return data.decode('utf-8', 'replace') if isinstance(data, bytes) else data

    branch = (await run('symbolic-ref', '--quiet', '--short', 'HEAD', unborn=True)).strip()
    # An unborn branch still has a symbolic name; a detached HEAD has none.
    branches = (await run('for-each-ref', '--count=101', '--format=%(refname:short)', 'refs/heads', 'refs/remotes')).splitlines()
    log = await run('log', '-10', '--format=%h%x09%cI%x09%s', unborn=True)
    changes = await run('status', '--porcelain=v1', '--untracked-files=normal')
    return {'is_repository': True, 'branch': branch or 'detached', 'branches': branches[:100],
        'branches_truncated': len(branches)>100,
        'commits': [dict(zip(('commit','date','subject'), [redact(v) for v in line.split('\t', 2)])) for line in log.splitlines()],
        'has_uncommitted_changes': bool(changes.strip()), 'layout': 'This registered project workspace has its own repository.'}


class Inspection:
    def __init__(self, admission):
        self.admission = admission
        self.pool = admission.store.pool

    async def project(self, auth, value, *, workspace=False):
        try:
            identifier = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError):
            raise ExecutionError('project_not_found', 404) from None
        from core.tool_jobs.service import lane_root
        scoped(auth, lane_root()/'projects'/str(identifier))
        row = await self.pool.fetchrow('''SELECT id,name,description,instructions,updated_at FROM projects
            WHERE id=$1 AND user_id=$2 AND is_archived=FALSE''', identifier, auth.owner)
        if not row:
            raise ExecutionError('project_not_found', 404)
        from core.builder.service import Builder
        root = Builder(self.pool).workspace(auth, {'project_id': identifier})
        if workspace and root is None:
            raise ExecutionError('workspace_unavailable', 409)
        return row, root

    async def command(self, auth, operation, args, *, visible_faces):
        await authorize(auth)
        if operation not in OPERATIONS:
            raise ExecutionError('unsupported_inspection', 404)
        fields = {
            'projects_list': {'offset','limit'}, 'project_read': {'project_id'},
            'project_files_list': {'project_id','path'},
            'project_files_read': {'project_id','path','offset','max_bytes'},
            'project_files_search': {'project_id','pattern','glob'}, 'project_git': {'project_id'},
            'bob_installation': set(), 'bob_source_list': {'component','path'},
            'bob_source_read': {'component','path','offset','max_bytes'},
            'bob_source_search': {'component','pattern','glob'},
        }
        only(args, fields[operation])
        if operation == 'projects_list':
            offset, limit = integer(args.get('offset', 0), 0, 1000000), integer(args.get('limit', 20), 1, 50)
            rows = await self.pool.fetch('''SELECT id FROM projects WHERE user_id=$1 AND is_archived=FALSE
                ORDER BY id OFFSET $2 LIMIT $3''', auth.owner, offset, limit+1)
            items = []
            for row in rows[:limit]:
                try:
                    items.append(await self.project_record(auth, str(row['id'])))
                except ExecutionError as exc:
                    if exc.status != 404:
                        raise
            return {'items': items, 'next_offset': offset+limit if len(rows)>limit else None}
        if operation == 'project_read':
            return await self.project_record(auth, args.get('project_id'), brief=True)
        if operation.startswith('project_'):
            _, root = await self.project(auth, args.get('project_id'), workspace=True)
            if operation == 'project_git':
                scoped(auth, root/'.git')
                # Git itself walks metadata and the worktree. A partial file
                # grant cannot safely authorize that traversal, so refuse it.
                if auth.token_type=='agent' and (auth.scope.get('may_not_touch')
                        or not scope_allows(auth.scope,root/'*')):
                    raise ExecutionError('not_found',404)
                return await git_summary(auth, root)
            path = args.get('path', '')
            if operation == 'project_files_list':
                read_parts(path,empty=True)
                scoped(auth,root/path)
                result = await asyncio.to_thread(files.listing, root, path)
                permitted = []
                for name in result['items']:
                    try:
                        read_parts('/'.join(filter(None,(path,name))))
                        scoped(auth,root/path/name)
                    except (ResourceError,ExecutionError):
                        continue
                    permitted.append(name)
                return {**result,'items':permitted}
            if operation == 'project_files_read':
                read_parts(path)
                scoped(auth,root/path)
                integer(args.get('max_bytes', 65536), 1, files.READ_CAP)
                data, size = await asyncio.to_thread(files.read, root, path, args.get('offset', 0), args.get('max_bytes', 65536))
                end = args.get('offset', 0)+len(data)
                text = data.decode('utf-8','replace')
                return {'path': path, 'content': redact(text), 'redacted':redact(text)!=text, 'size': size, 'next_offset': end, 'truncated': end<size}
            from core.builder.service import Builder
            # Only this fixed read operation reaches Builder.worker. No mode(),
            # tool(), approval, mount or conversation binding is involved.
            result = await Builder(self.pool).worker(auth, root, 'fs_search', args={
                **{k:v for k,v in args.items() if k!='project_id'}, '_inspection':True,
                '_read_scope':auth.scope if auth.token_type=='agent' else None})
            if result.get('error') or result.get('exit_code')!=0:
                raise ExecutionError('project_search_incomplete',503)
            return {'matches':[{**m,'text':redact(m['text'])} for m in result.get('matches',[])],
                    'truncated':result.get('truncated',False)}
        if operation.startswith('bob_source_'):
            component = args.get('component')
            if component not in SOURCE_ROOTS:
                raise ExecutionError('unknown_source_component', 404)
            root = SOURCE_ROOTS[component]
            scoped(auth, root)
            scope = auth.scope if auth.token_type=='agent' else None
            if operation == 'bob_source_list':
                source_parts(args.get('path',''),empty=True)
                scoped(auth,root/args.get('path',''))
                result = await asyncio.to_thread(source_listing, root, args.get('path',''),scope)
            elif operation == 'bob_source_read':
                source_parts(args.get('path'))
                scoped(auth,root/args['path'])
                result = await asyncio.to_thread(source_read, root, args.get('path'), args.get('offset',0), args.get('max_bytes',65536))
            else:
                result = await asyncio.to_thread(source_search, root, args.get('pattern'), args.get('glob','*'),scope)
            return {'component': component, 'version': build_version(), **result}
        return await self.installation(auth, visible_faces)

    async def project_record(self, auth, identifier, brief=False):
        row, root = await self.project(auth, identifier)
        conversations = await self.pool.fetch('''SELECT id,title,updated_at FROM conversations
            WHERE project_id=$1 AND user_id=$2 ORDER BY updated_at DESC,id LIMIT 101''', row['id'], auth.owner)
        result = {'id': str(row['id']), 'name': redact(row['name']), 'description': redact(row['description']),
            'has_managed_workspace': root is not None,
            'conversations': [{'id':str(c['id']), 'title':redact(c['title']), 'updated_at':c['updated_at'].isoformat()} for c in conversations[:100]],
            'conversations_truncated':len(conversations)>100,
            'last_activity':max([row['updated_at'], *(c['updated_at'] for c in conversations)]).isoformat()}
        if brief:
            result['instructions'] = redact(row['instructions'])
        return result

    async def installation(self, auth, visible_faces):
        from core.faces.registry import get_default_registry
        from core.guide import render, public_id, public_text
        from core.execution.workflows import PREMADES
        registry = get_default_registry()
        faces = [f for f in registry.all_faces() if f.id in visible_faces]
        from core.lane_config import configured_mode, catalog
        from core.backends.local_router import LocalModelRouter
        if configured_mode():
            backends = await catalog(LocalModelRouter())
        else:
            from api.server import backends_catalog, ROUTER_KEY
            from types import SimpleNamespace
            backends = json.loads((await backends_catalog(SimpleNamespace(app={ROUTER_KEY:LocalModelRouter()}))).text)
        models = {b['backend']:b.get('model') for b in backends}
        topics = {}
        for topic in ('models','permissions','workflows','tasks-library-runs','extending-bob'):
            topics[topic] = (await render(topic, registry=registry, visible_faces=[f.id for f in faces],backend_rows=backends))['content']
        # No connection heartbeat exists in the core PC transport. Enrollment or
        # a historical request is not evidence that the native app is connected.
        saved = await self.admission_workflows(auth)
        return {'version':build_version(), 'agents':[{'id':public_id(f.id), 'default_backend':public_id(f.preferred_backend or 'automatic'),
                'default_model':public_id(models.get(f.preferred_backend) or 'configured-default'),
                'pinned_models':{p:public_id(getattr(f,p,{}).get('model')) for p in ('cc_posture','agy_posture','codex_posture','pi_posture') if getattr(f,p,{}).get('model')}} for f in faces],
            'skills':{public_id(f.id):[{**s,'summary':public_text(redact(s['summary']),1000)} for s in registry.list_skills(f.id)] for f in faces},
            'premade_workflows':[{'id':k,'name':v['name']} for k,v in PREMADES.items()],
            'saved_workflows':{'items':[{'id':r['id'],'revision':r['revision'],
                'name':public_text(redact(r.get('definition',{}).get('name',r.get('name','')))),
                'available':r.get('available')} for r in saved['items']], 'next_cursor':saved.get('next_cursor')},
            'pc':{'connected':None, 'connection_evidence':'This build has no server-visible native connection heartbeat.',
                'access':'Native Windows approval is required per agent and task. Grants last at most two hours, or thirty minutes in PC Yolo. Stop, task end, mode change, logout or revoke ends access sooner. Reconnecting does not renew it. Pending requests expire after 600 seconds; commands are limited to 600 seconds.'},
            'guide':topics}

    async def admission_workflows(self, auth):
        from core.workflow_surface import WorkflowSurface
        return await WorkflowSurface(self.admission).command(auth, 'workflows.list', {'contract_version':1,'limit':50})
