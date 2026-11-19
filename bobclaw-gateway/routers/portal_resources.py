"""Portal-only owner reads. Domain code runs in-process, without a core HTTP hop.

There is consequently no unsigned gateway-to-core owner field: the gateway
derives owner/lane from deployment configuration and passes verified scope to
the core library, which re-evaluates scope for each resource on every request.
"""
import asyncio
import json
import os
from pathlib import Path
import re

from aiohttp import web

from app_state import POSTGRES_POOL_KEY
from core.paths import family_root, memory_sqlite_path
from core.permissions import Scope
from core.portal_resources import (ResourceError, Snapshots, browse_file, integer,
                                   load_folders, owned_memory, scope_allows, safe_open,
                                   relative_parts, KINDS)

router = web.RouteTableDef()
SNAPSHOTS = web.AppKey('portal_read_snapshots', Snapshots)


def setup(app):
    app[SNAPSHOTS] = Snapshots()


def authority(request):
    claims = request.get('user', {})
    owner = os.environ.get('BOB_DEFAULT_OWNER', '')
    lane = os.environ.get('BOB_LANE_ROOT', '')
    if (claims.get('token_type') != 'portal' or claims.get('sub') != owner
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', owner) or owner == 'shared'):
        raise ResourceError('scope_denied', 403)
    if not lane or not Path(lane).is_absolute():
        raise ResourceError('lane_unavailable', 503)
    try:
        scope = Scope.model_validate(claims.get('scope')).model_dump()
    except Exception:
        raise ResourceError('scope_denied', 403) from None
    # Both resource selection and cursor binding use the configured lane.
    return owner, Path(lane), scope


async def arguments(request, fields):
    try:
        value = await request.json()
        if not isinstance(value, dict) or set(value) - set(fields):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ResourceError('invalid_args') from None


def binding(owner, lane, scope, operation, args):
    return json.dumps([owner, str(lane), scope, operation, args], sort_keys=True)


def endpoint(fn):
    async def handle(request):
        try:
            return web.json_response(await fn(request))
        except ResourceError as exc:
            return web.json_response({'error': {'code': exc.code, 'detail': exc.code}}, status=exc.status)
    return handle


@router.post('/agent-memory/list')
@endpoint
async def memory_list(request):
    owner, lane, scope = authority(request)
    if family_root() is None:
        raise web.HTTPServiceUnavailable(text='Personal memory is unavailable: set BOB_FAMILY_ROOT.')
    if not scope_allows(scope, family_root() / owner / 'MEMORY.md'):
        raise ResourceError('scope_denied', 403)
    args = await arguments(request, ('limit', 'cursor', 'kind'))
    limit = integer(args.get('limit'), 50, 50, 1)
    kind = args.get('kind')
    if kind is not None and (not isinstance(kind, str) or kind not in KINDS):
        raise ResourceError('invalid_args')
    key = binding(owner, lane, scope, 'memory.list', kind)
    rows = coverage = None
    if args.get('cursor') is None:
        rows, coverage = await asyncio.to_thread(owned_memory, family_root(), memory_sqlite_path(), owner, scope)
        if kind:
            rows = [r for r in rows if r['kind'] == kind]
        coverage = dict(coverage, filter_kind=kind, matched_records=len(rows))
        rows = [{k: v for k, v in row.items() if k != 'content'} |
                {'excerpt': row['content'][:600], 'truncated': len(row['content']) > 600} for row in rows]
    # Scope/policy is re-evaluated in the binding; a changed token cannot resume.
    return request.app[SNAPSHOTS].page(key, rows, coverage, args.get('cursor'), limit)


async def attachments(request, owner, lane):
    pool = request.app.get(POSTGRES_POOL_KEY)
    registered = await asyncio.to_thread(load_folders, lane)
    result = []
    for row in registered:
        if row.get('owner') != owner:
            continue
        if pool is None:
            raise ResourceError('projects_unavailable', 503)
        project = await pool.fetchrow(
            'SELECT id FROM projects WHERE id = $1 AND user_id = $2 AND is_archived = FALSE',
            row['project_id'], owner)
        if project is not None:
            result.append(row)
    return result


async def attachment(request, owner, lane, args):
    aid = args.get('attachment_id')
    if not isinstance(aid, str) or not aid or len(aid) > 100:
        raise ResourceError('invalid_args')
    rows = await asyncio.to_thread(load_folders, lane)
    row = next((r for r in rows if r['attachment_id'] == aid and r['owner'] == owner), None)
    if row is None:
        raise ResourceError('not_found', 404)
    pool = request.app.get(POSTGRES_POOL_KEY)
    if pool is None:
        raise ResourceError('projects_unavailable', 503)
    project = await pool.fetchrow(
        'SELECT id FROM projects WHERE id = $1 AND user_id = $2 AND is_archived = FALSE',
        row['project_id'], owner)
    if project is None:
        raise ResourceError('not_found', 404)
    return row


@router.get('/files/roots')
@endpoint
async def files_roots(request):
    owner, lane, scope = authority(request)
    rows = await attachments(request, owner, lane)
    visible = []
    for row in rows:
        if not scope_allows(scope, lane / row['path']):
            continue
        try:
            with safe_open(lane, relative_parts(row['path']), directory=True):
                visible.append({k: row[k] for k in ('attachment_id', 'project_id', 'label')} |
                               {'read_only': True})
        except ResourceError as exc:
            if exc.status != 404:
                raise
    return {'items': visible}


@router.post('/files/list')
@endpoint
async def files_list(request):
    owner, lane, scope = authority(request)
    args = await arguments(request, ('attachment_id', 'path', 'cursor'))
    row = await attachment(request, owner, lane, args)
    path = args.get('path', '')
    # Re-open/recheck the registered root even when continuing a directory snapshot.
    entries = await asyncio.to_thread(browse_file, lane, row, scope, path, listing=True)
    key = binding(owner, lane, scope, 'files.list', [row, path])
    page = request.app[SNAPSHOTS].page(key, entries, {}, args.get('cursor'), 100)
    return {k: page[k] for k in ('items', 'next_cursor')}


@router.post('/files/read')
@endpoint
async def files_read(request):
    owner, lane, scope = authority(request)
    args = await arguments(request, ('attachment_id', 'path', 'offset', 'max_bytes'))
    row = await attachment(request, owner, lane, args)
    offset = integer(args.get('offset'), 0, 16 * 1024 * 1024)
    size = integer(args.get('max_bytes'), 65536, 65536, 1)
    return await asyncio.to_thread(browse_file, lane, row, scope, args.get('path'), offset=offset, max_bytes=size)
