"""Owner read handlers with real files, no sockets, models, or provider calls."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import pytest

from app_state import POSTGRES_POOL_KEY
from core.portal_resources import register_project_folder
from routers import portal_resources as routes


@pytest.fixture(autouse=True)
def _isolated_gateway_db():
    yield


@pytest.fixture
def lane(tmp_path, monkeypatch):
    owner = 'orchid-7'
    root = tmp_path / owner
    data = root / 'data'
    folder = data / 'projects' / 'sample'
    folder.mkdir(parents=True)
    (root / 'MEMORY.md').write_text('你好\n\n!!!\n\n' + '\n\n'.join(str(i) for i in range(60)), encoding='utf-8')
    (folder / 'hello').write_bytes(b'abcdefghij')
    project = str(uuid.uuid4())
    registered = register_project_folder(data, owner, project, 'projects/sample', 'Sample')
    foreign = register_project_folder(data, 'foreign-1', str(uuid.uuid4()), 'projects/sample', 'Foreign')
    monkeypatch.setenv('BOB_DEFAULT_OWNER', owner)
    monkeypatch.setenv('BOB_LANE_ROOT', str(data))
    monkeypatch.setenv('BOB_FAMILY_ROOT', str(tmp_path))
    monkeypatch.setenv('MEMORY_SQLITE_PATH', str(data / 'missing.db'))
    app = web.Application()
    routes.setup(app)
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value={'id': project}))
    app[POSTGRES_POOL_KEY] = pool
    claims = {'sub': owner, 'token_type': 'portal', 'scope': {'may_touch': [str(root) + '/**']}}
    def request(args=None, user=None):
        req = make_mocked_request('POST', '/', app=app)
        req['user'] = claims if user is None else user
        req.json = AsyncMock(return_value=args or {})
        return req
    return SimpleNamespace(request=request, app=app, claims=claims, attachment=registered,
                           foreign=foreign, root=root, pool=pool)


def value(response):
    return json.loads(response.text)


async def test_memory_cursor_binding_and_filter(lane):
    first = value(await routes.memory_list(lane.request({'limit': 50})))
    assert len(first['items']) == 50
    assert first['coverage']['counts_by_kind']['family_memory'] == 62
    with (lane.root / 'MEMORY.md').open('a') as stream:
        stream.write('\n\nappend')
    second = value(await routes.memory_list(lane.request({'cursor': first['next_cursor']})))
    assert len(second['items']) == 12
    assert second['snapshot'] == first['snapshot'] and second['next_cursor'] is None
    response = await routes.memory_list(lane.request({'cursor': first['next_cursor'], 'kind': 'bob_event'}))
    assert response.status == 400 and value(response)['error']['code'] == 'invalid_cursor'
    for entry in lane.app[routes.SNAPSHOTS].entries.values():
        entry['expires'] = 0
    response = await routes.memory_list(lane.request({'cursor': first['next_cursor']}))
    assert response.status == 410 and value(response)['error']['code'] == 'snapshot_expired'


@pytest.mark.parametrize('identity', ['agent', 'admin', 'foreign', 'absent'])
async def test_handler_authority_matrix(lane, identity):
    claims = dict(lane.claims)
    if identity == 'foreign':
        claims['sub'] = 'foreign-1'
    elif identity == 'absent':
        claims = {}
    else:
        claims['token_type'] = identity
    for handler in (routes.memory_list, routes.files_roots, routes.files_list, routes.files_read):
        response = await handler(lane.request(user=claims))
        assert response.status == 403 and value(response)['error']['code'] == 'scope_denied'


async def test_files_project_ownership_bounds_and_scope(lane):
    roots = value(await routes.files_roots(lane.request()))['items']
    assert roots == [{k: lane.attachment[k] for k in ('attachment_id', 'project_id', 'label')} | {'read_only': True}]
    assert 'path' not in roots[0]
    aid = lane.attachment['attachment_id']
    files = value(await routes.files_list(lane.request({'attachment_id': aid})))
    assert files['items'][0]['path'] == 'hello'
    read = value(await routes.files_read(lane.request({'attachment_id': aid, 'path': 'hello', 'max_bytes': 3})))
    assert read['content'] == 'abc' and read['next_offset'] == 3 and read['truncated']
    for bad_id in ('absent', lane.foreign['attachment_id']):
        response = await routes.files_read(lane.request({'attachment_id': bad_id, 'path': 'hello'}))
        assert response.status == 404 and value(response)['error']['code'] == 'not_found'
    for args in ({'max_bytes': 65537}, {'max_bytes': True}, {'offset': -1}, {'path': '../MEMORY.md'}, {'path': 'C:/secret'}):
        response = await routes.files_read(lane.request({'attachment_id': aid, 'path': 'hello'} | args))
        assert response.status == 400
    lane.pool.fetchrow.return_value = None
    response = await routes.files_read(lane.request({'attachment_id': aid, 'path': 'hello'}))
    assert response.status == 404
    assert value(await routes.files_roots(lane.request()))['items'] == []


async def test_lane_unconfigured_scope_denied_and_strict_fields(lane, monkeypatch):
    for bad in ({'limit': 51}, {'limit': True}, {'kind': []}, {'root': 'C:/secret'}):
        assert (await routes.memory_list(lane.request(bad))).status == 400
    claims = dict(lane.claims, scope={'may_touch': ['/unrelated/**']})
    assert (await routes.memory_list(lane.request(user=claims))).status == 403
    monkeypatch.delenv('BOB_LANE_ROOT')
    assert (await routes.memory_list(lane.request())).status == 503


async def test_discovery_needs_no_core_registry_or_provider_calls(lane, monkeypatch):
    from routers import capabilities
    def forbidden(*args, **kwargs):
        pytest.fail('Operation discovery must not contact core or providers')
    monkeypatch.setattr(capabilities.aiohttp, 'ClientSession', forbidden)
    req = make_mocked_request('GET', '/capabilities?operations=1', app=lane.app)
    req['user'] = lane.claims
    operations = value(await capabilities.get_capabilities(req))['operations']
    assert set(operations) == {'memory.list', 'files.roots', 'files.list', 'files.read'}
    assert all(v['readable'] and not v['writable'] and not v['runnable'] for v in operations.values())


async def test_existing_portal_reads_include_owned_lane_database_records(lane, monkeypatch):
    import sqlite3
    from routers import agent_memory
    db = lane.root / 'owned.db'
    with sqlite3.connect(db) as conn:
        conn.executescript('CREATE TABLE memory_events(event_id TEXT, body_json TEXT, ts TEXT, insertion_order INTEGER); CREATE TABLE memory_facts(fact_id TEXT, body_json TEXT, source_event_id TEXT, ts TEXT);')
        conn.execute('INSERT INTO memory_events VALUES(?,?,?,?)', ('own', json.dumps({'user_id': lane.claims['sub'], 'user_message': '你好'}), 'now', 1))
        conn.execute('INSERT INTO memory_events VALUES(?,?,?,?)', ('foreign', json.dumps({'user_id': 'foreign-1'}), 'now', 2))
    agent_memory.setup(lane.app, root=lane.root.parent, db=db)
    rows = agent_memory.sources(lane.request())
    assert 'event:own' in {r['source_id'] for r in rows}
    assert 'event:foreign' not in {r['source_id'] for r in rows}
    read = await agent_memory.read(lane.request({'source_id': 'event:own'}))
    assert value(read)['source_id'] == 'event:own'
    assert value(await agent_memory.status(lane.request()))['counts_by_kind']['bob_event'] == 1
