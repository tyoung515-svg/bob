"""Direct central family memory. Scope enforced here, not by client tool selection.
Historical Bob DB records without owner attribution are deliberately excluded.
"""
import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from aiohttp import web
from core.permissions import Scope,evaluate_path
from core.paths import family_root
router=web.RouteTableDef()
ROOT=web.AppKey('agent_memory_root',Path)
LOCK=web.AppKey('agent_memory_lock',asyncio.Lock)
DB=web.AppKey('agent_memory_db',Path)
def primary_owner():
 return os.environ.get('BOB_DEFAULT_OWNER','')
def configured_owners():
 raw=os.environ.get('BOB_MEMORY_OWNERS',primary_owner())
 return tuple(dict.fromkeys([*(v.strip() for v in raw.split(',') if v.strip()),'shared']))
def owners(request):
 # Runtime lanes can have any validated lane name. Legacy agent coverage stays
 # unchanged; portal owners still need the same evaluated filesystem scope.
 claims=request['user'];name=claims.get('sub','')
 if claims.get('token_type')=='portal':
  import re
  if re.fullmatch(r'[a-z][a-z0-9-]{0,31}',name) and name!='shared':return (name,'shared')
 return configured_owners()
def setup(app,root=None,db=None):
 app[ROOT]=family_root() if root is None else Path(root);app[LOCK]=asyncio.Lock()
 if db:app[DB]=Path(db)
def allowed_path(request,path):
 claims=request['user'];scope=claims.get('scope')
 if claims.get('token_type') not in ('agent','portal') or not isinstance(scope,dict):return False
 try:return evaluate_path(path,Scope.model_validate(scope))=='auto'
 except Exception:return False
def allowed(request,owner):
 if request.app.get(ROOT) is None or not owner:return False
 names=('SHARED_MEMORY.md','PINBOARD.md','MEMORY.md') if owner=='shared' else ('MEMORY.md',)
 return any(allowed_path(request,str(request.app[ROOT]/owner/name)) for name in names)
def sources(request):
 if request['user'].get('token_type') == 'portal':
  from core.portal_resources import owned_memory
  return owned_memory(request.app[ROOT], request.app.get(DB), request['user']['sub'], request['user'].get('scope'))[0]
 result=[];root=request.app[ROOT]
 for owner in owners(request):
  if not allowed(request,owner):continue
  names=['MEMORY.md'] if owner!='shared' else ['SHARED_MEMORY.md','PINBOARD.md','MEMORY.md']
  for name in names:
   if not allowed_path(request,str(root/owner/name)):continue
   p=root/owner/name
   if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):continue
   text=p.read_text(encoding='utf-8')
   # Each paragraph is a source record, not an artificial global memory total.
   for i,part in enumerate(text.split('\n\n')):
    if part.strip():result.append({'source_id':f'family:{owner}:{name}:{i}','owner':owner,'kind':'family_memory','content':part,'provenance':{'path':str(p),'paragraph':i,'sha256':hashlib.sha256(part.encode()).hexdigest()}})
 if DB in request.app and allowed(request,primary_owner()) and request['user'].get('sub')=='admin':
  p=request.app[DB]
  if p.is_file():
   with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as c:
    events={}
    for eid,body,ts in c.execute('select event_id,body_json,ts from memory_events order by insertion_order'):
     b=json.loads(body)
     if b.get('user_id')!='admin':continue
     events[eid]=True
     result.append({'source_id':'event:'+eid,'owner':primary_owner(),'kind':'bob_event','content':json.dumps(b,ensure_ascii=False),'provenance':{'event_id':eid,'timestamp':ts,'ownership':'explicit_user_id'}})
    for fid,body,eid,ts in c.execute('select fact_id,body_json,source_event_id,ts from memory_facts'):
     if eid in events:result.append({'source_id':'fact:'+fid,'owner':primary_owner(),'kind':'bob_fact','content':body,'provenance':{'fact_id':fid,'source_event_id':eid,'timestamp':ts}})
 return result
def coverage(request,rows):
 if request['user'].get('token_type') == 'portal':
  from core.portal_resources import owned_memory
  return owned_memory(request.app[ROOT], request.app.get(DB), request['user']['sub'], request['user'].get('scope'))[1]
 result={'authority':'server','sources':['family_memory','explicitly_owned_bob_events_and_facts'],'counts_by_kind':{k:sum(r['kind']==k for r in rows) for k in ('family_memory','bob_event','bob_fact')},'complete_corpus':False,'excluded':['historical Bob records without explicit owner attribution','project/corpus files outside this memory endpoint'],'search_mode':'literal_case_insensitive','cache':False}
 if DB in request.app and allowed(request,primary_owner()) and request['user'].get('sub')=='admin' and request.app[DB].is_file():
  with sqlite3.connect('file:'+str(request.app[DB])+'?mode=ro',uri=True) as c:result['unattributed_events_excluded']=c.execute('select count(*) from memory_events').fetchone()[0]-result['counts_by_kind']['bob_event']
 return result
async def body(request):
 try:
  b=await request.json()
  if not isinstance(b,dict):raise ValueError()
  return b
 except Exception:raise web.HTTPBadRequest(text='invalid_json')
def number(b,key,default,maximum):
 v=b.get(key,default)
 if type(v)!=int or v<0 or v>maximum:raise web.HTTPBadRequest(text='invalid_'+key)
 return v
def coded(fn):
 # Portal lanes read through owned_memory, which reports store faults as coded statuses.
 # Without this they surfaced as a bare 500 (and a save looked failed after it was written).
 async def handle(request):
  from core.portal_resources import ResourceError
  try:return await fn(request)
  except ResourceError as exc:return web.json_response({'error':{'code':exc.code,'detail':exc.code}},status=exc.status)
  # A store this service account may not read or write (runtime lanes run the gateway under its own
  # account) is an unavailable store, not a server crash. Nothing was written when this is raised.
  except PermissionError:return web.json_response({'error':{'code':'memory_store_denied','detail':'memory_store_denied'}},status=503)
 return handle
def check(request):
 if request.app.get(ROOT) is None:raise web.HTTPServiceUnavailable(text='Personal memory is unavailable: set BOB_FAMILY_ROOT.')
 if not any(allowed(request,o) for o in owners(request)):raise web.HTTPForbidden(text='memory_scope_required')
@router.get('/agent-memory/status')
@coded
async def status(request):
 check(request);rows=await asyncio.to_thread(sources,request);return web.json_response(coverage(request,rows))
@router.post('/agent-memory/search')
@coded
async def search(request):
 check(request);b=await body(request);q=b.get('query','')
 if not isinstance(q,str) or len(q)>1000:raise web.HTTPBadRequest(text='invalid_query')
 limit=number(b,'limit',10,50);offset=number(b,'offset',0,100000)
 if limit==0:raise web.HTTPBadRequest(text='invalid_limit')
 rows=await asyncio.to_thread(sources,request);hits=[r for r in rows if q.casefold() in r['content'].casefold()];page=hits[offset:offset+limit]
 return web.json_response({'items':[{k:v for k,v in r.items() if k!='content'}|{'excerpt':r['content'][:600],'truncated':len(r['content'])>600} for r in page],'matched_records':len(hits),'has_more':offset+limit<len(hits),'next_offset':offset+limit if offset+limit<len(hits) else None,'coverage':coverage(request,rows)})
@router.post('/agent-memory/read')
@coded
async def read(request):
 check(request);b=await body(request);offset=number(b,'offset',0,10000000);size=number(b,'max_chars',6000,12000)
 if size==0:raise web.HTTPBadRequest(text='invalid_max_chars')
 rows=await asyncio.to_thread(sources,request);r=next((r for r in rows if r['source_id']==b.get('source_id')),None)
 if not r:raise web.HTTPNotFound(text='source_not_found')
 text=r['content'];return web.json_response(r|{'content':text[offset:offset+size],'total_chars':len(text),'has_more':offset+size<len(text),'next_offset':offset+size if offset+size<len(text) else None})
@router.post('/agent-memory/save')
@coded
async def save(request):
 check(request);b=await body(request);text=b.get('content');category=b.get('category','fact')
 if not isinstance(text,str) or not text.strip() or len(text)>12000 or category not in ('fact','preference','project','note'):raise web.HTTPBadRequest(text='invalid_memory')
 # Private owner inferred ONLY from verified filesystem scope, never client labels.
 private_owners=[o for o in owners(request) if o!='shared' and allowed(request,o)]
 if len(private_owners)!=1:raise web.HTTPForbidden(text='single_private_owner_required')
 owner=private_owners[0];p=request.app[ROOT]/owner/'MEMORY.md'
 if p.is_symlink() or not p.resolve().is_relative_to(request.app[ROOT].resolve()):raise web.HTTPForbidden()
 import datetime,fcntl
 async with request.app[LOCK]:
  with p.open('a+',encoding='utf-8') as f:
   fcntl.flock(f.fileno(),fcntl.LOCK_EX);f.seek(0);before=f.read()
   entry='- ['+category+'; Bob central; '+datetime.datetime.now(datetime.timezone.utc).isoformat()+'] '+text.strip().replace('\n',' ')
   f.seek(0,2);f.write('\n\n'+entry+'\n');f.flush();os.fsync(f.fileno());fcntl.flock(f.fileno(),fcntl.LOCK_UN)
  rows=sources(request);saved=next(r for r in reversed(rows) if r['owner']==owner and entry in r['content'])
 return web.json_response({'source_id':saved['source_id'],'owner':owner,'authority':'server','provenance':saved['provenance']},status=201)
