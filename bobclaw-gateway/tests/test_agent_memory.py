import pytest
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer

@pytest.mark.asyncio
async def test_central_roundtrip_and_scope(tmp_path):
 from routers import agent_memory as m
 for owner in ('dad','luna','shared'):
  (tmp_path/owner).mkdir();(tmp_path/owner/'MEMORY.md').write_text('# Memory\n- '+owner+' original fact\n')
 claims={'sub':'admin','token_type':'agent','scope':{'may_touch':[str(tmp_path/'dad'/'**'),str(tmp_path/'shared'/'**')],'may_not_touch':[str(tmp_path/'luna'/'**')]}}
 @web.middleware
 async def identity(request,handler):request['user']=claims;return await handler(request)
 app=web.Application(middlewares=[identity]);m.setup(app,root=tmp_path);app.router.add_routes(m.router)
 async with TestClient(TestServer(app)) as c:
  r=await c.post('/agent-memory/search',json={'query':'original'});assert r.status==200
  data=await r.json();assert {x['owner'] for x in data['items']}=={'dad','shared'}
  r=await c.post('/agent-memory/save',json={'content':'cross-client marker zebra','category':'fact'});assert r.status==201
  saved=await r.json();sid=saved['source_id']
  r=await c.post('/agent-memory/read',json={'source_id':sid});assert 'cross-client marker zebra' in (await r.json())['content']
  claims['scope']['may_touch']=[str(tmp_path/'luna'/'**')];claims['scope']['may_not_touch']=[]
  r=await c.post('/agent-memory/read',json={'source_id':sid});assert r.status==404
  r=await c.post('/agent-memory/search',json={'query':'zebra'});assert (await r.json())['items']==[]

@pytest.mark.asyncio
async def test_fail_closed_pagination_and_bounds(tmp_path):
 from routers import agent_memory as m
 (tmp_path/'dad').mkdir();(tmp_path/'dad'/'MEMORY.md').write_text('paragraph one\n\nparagraph two')
 claims={'sub':'admin','token_type':'agent','scope':{'may_touch':[str(tmp_path/'dad'/'**')]}}
 @web.middleware
 async def identity(request,handler):request['user']=claims;return await handler(request)
 app=web.Application(middlewares=[identity]);m.setup(app,root=tmp_path);app.router.add_routes(m.router)
 async with TestClient(TestServer(app)) as c:
  r=await c.post('/agent-memory/search',json={'query':'paragraph','limit':1});d=await r.json();assert d['has_more'] and d['next_offset']==1 and d['matched_records']==2
  r=await c.post('/agent-memory/read',json={'source_id':d['items'][0]['source_id'],'max_chars':3});d=await r.json();assert d['has_more'] and d['next_offset']==3
  for b in ({'limit':-1},{'limit':True},{'limit':51}):
   r=await c.post('/agent-memory/search',json=b);assert r.status==400
  claims['scope']['may_not_touch']=[str(tmp_path/'dad'/'**')]
  r=await c.get('/agent-memory/status');assert r.status==403
  claims.pop('scope');r=await c.get('/agent-memory/status');assert r.status==403

@pytest.mark.asyncio
async def test_portal_lane_store_fault_is_coded_and_unreadable_db_degrades(tmp_path):
 import os
 from routers import agent_memory as m
 for owner in ('orchid','shared'):(tmp_path/owner).mkdir()
 (tmp_path/'orchid'/'MEMORY.md').write_text('lane note one')
 db=tmp_path/'memory.db';db.write_bytes(b'not a database')
 claims={'sub':'orchid','token_type':'portal','scope':{'may_touch':[str(tmp_path/'orchid'/'**'),str(tmp_path/'shared'/'**')]}}
 @web.middleware
 async def identity(request,handler):request['user']=claims;return await handler(request)
 app=web.Application(middlewares=[identity]);m.setup(app,root=tmp_path,db=db);app.router.add_routes(m.router)
 async with TestClient(TestServer(app)) as c:
  # A broken lane store is a coded 503 on every route, never a bare 500.
  for call in (c.get('/agent-memory/status'),c.post('/agent-memory/search',json={'query':'note'}),c.post('/agent-memory/read',json={'source_id':'x'})):
   r=await call;assert r.status==503;assert (await r.json())['error']['code']=='memory_unavailable'
  if os.geteuid()!=0:
   # The runtime gateway account cannot read the core-owned database: memory files still work.
   db.chmod(0o000)
   try:
    r=await c.get('/agent-memory/status');assert r.status==200
    data=await r.json();assert data['lane_database']=='unavailable_or_out_of_scope' and data['counts_by_kind']['family_memory']==1
    r=await c.post('/agent-memory/save',json={'content':'saved while db unreadable','category':'fact'});assert r.status==201
    r=await c.post('/agent-memory/search',json={'query':'saved while'});assert len((await r.json())['items'])==1
   finally:db.chmod(0o600)
   # A lane directory the gateway account may not write: coded 503, never a bare 500, nothing saved.
   (tmp_path/'orchid'/'MEMORY.md').unlink();(tmp_path/'orchid').chmod(0o500)
   try:
    r=await c.post('/agent-memory/save',json={'content':'must not be saved','category':'fact'});assert r.status==503
    assert (await r.json())['error']['code']=='memory_store_denied'
   finally:(tmp_path/'orchid').chmod(0o700)
   assert not (tmp_path/'orchid'/'MEMORY.md').exists()


@pytest.mark.asyncio
async def test_unconfigured_personal_memory_is_unavailable(monkeypatch):
 from routers import agent_memory as m
 monkeypatch.delenv('BOB_FAMILY_ROOT',raising=False)
 claims={'sub':'operator','token_type':'agent','scope':{'may_touch':['**']}}
 @web.middleware
 async def identity(request,handler):request['user']=claims;return await handler(request)
 app=web.Application(middlewares=[identity]);m.setup(app);app.router.add_routes(m.router)
 async with TestClient(TestServer(app)) as c:
  r=await c.get('/agent-memory/status')
  assert r.status==503 and 'set BOB_FAMILY_ROOT' in await r.text()


def test_legacy_owner_coverage_comes_only_from_configuration(monkeypatch):
 from routers import agent_memory as m
 monkeypatch.setenv('BOB_DEFAULT_OWNER','operator')
 monkeypatch.delenv('BOB_MEMORY_OWNERS',raising=False)
 assert m.configured_owners()==('operator','shared')
 monkeypatch.setenv('BOB_MEMORY_OWNERS','operator, reviewer,operator')
 assert m.configured_owners()==('operator','reviewer','shared')
