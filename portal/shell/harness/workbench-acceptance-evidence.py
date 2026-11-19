#!/usr/bin/env python3
"""P1 evidence invariants. Validates artifacts; does NOT authenticate their origin.
UNIT mutations prove the checker catches lies, not that a runtime is implemented.
Only the integrator's actual native/provider/Bob replay can close live gates.
"""
import argparse, copy, hashlib, json, sys, unittest
from pathlib import Path
LIFECYCLE={'idle','running','waiting_approval','blocked','completed','failed','cancel_requested','cancelled','unknown'}
CAPS={'chat','project_tools','writes','multi_agent','workflows','cancel','steer','reconnect'}

def require(ok,message):
 if not ok:raise AssertionError(message)

def sha(data):return 'sha256:'+hashlib.sha256(data).hexdigest()

def canonical(v):
 # RFC8785 subset used by these events: exact safe integers; no float rounding.
 if isinstance(v,dict):return {k:canonical(v[k]) for k in sorted(v,key=lambda s:s.encode('utf-16-be'))}
 if isinstance(v,list):return [canonical(x) for x in v]
 if isinstance(v,int) and not isinstance(v,bool):require(abs(v)<2**53,'unsafe JCS integer')
 require(not isinstance(v,float),'unsupported floating JCS number; use authoritative RFC8785 checker')
 return v

def digest(v):return sha(json.dumps(canonical(v),ensure_ascii=False,separators=(',',':')).encode())

def event_chain(events):
 previous=None;ids=set()
 for ordinal,e in enumerate(events,1):
  require(e['schema_version']=='functional-app-v2','event schema drift')
  require(e['event_id'] not in ids,'duplicate event id');ids.add(e['event_id'])
  require(e['ordinal']==str(ordinal),'event ordinal gap')
  require(e['previous_event_id']==(previous['event_id'] if previous else None),'event predecessor id mismatch')
  require(e['previous_digest']==(previous['digest'] if previous else None),'event predecessor digest mismatch')
  v=copy.deepcopy(e);v['digest']='';require(digest(v)==e['digest'],'event digest mismatch')
  previous=e
 return ids

def unique(rows,label):
 ids=[x['id'] for x in rows];require(len(ids)==len(set(ids)),label+' duplicate identity');return set(ids)

def snapshot(v,bob_event_ids=()):
 require(v.get('schema_version')=='workbench-view-v1','missing workbench-view-v1 (legacy compatibility is not P1)')
 for k in ('app','profiles','active_profile_id','connection_state','agents','workflows','outputs','notices'):require(k in v,'missing '+k)
 require(v['connection_state'] in {'ready','connecting','disconnected','unknown'},'invented connection state')
 profiles={p['id']:p for p in v['profiles']}
 require(len(profiles)==len(v['profiles']),'duplicate profile')
 require(set(profiles)>={'local_bob_model','standalone_http','bob_team'},'required runtime profiles absent')
 require(v['active_profile_id'] in profiles,'unknown selected profile')
 for p in profiles.values():
  for k in ('label','adapter_kind','execution_host','model_label','memory_owner','effect_owner','configured','capabilities','unavailable_reason'):require(k in p,'profile missing '+k)
  require(type(p['configured']) is bool,'configured not boolean')
  require(set(p['capabilities'])>=CAPS,'capability omitted')
  require(all(type(p['capabilities'][k]) is bool for k in CAPS),'nonboolean capability')
 for id in ('local_bob_model','standalone_http'):
  p=profiles[id];require(p['adapter_kind']=='model_only' and p['effect_owner']=='cleanroom_local','model pipe falsely claims runtime ownership')
  require(not p['capabilities']['multi_agent'],'local model fabricates subagents')
 p=profiles['bob_team'];require(p['adapter_kind']=='agent_runtime' and p['effect_owner']=='bob_runtime','Bob ownership absent')
 require(not p['capabilities']['writes'] and not p['capabilities']['project_tools'],'read-only Bob viewer grants local project authority')
 app=v['app'];session=app['session'] if app else None
 local_ids=event_chain(session['events']) if session else set()
 known=local_ids|set(bob_event_ids)
 agent_ids=unique(v['agents'],'agent');output_ids=unique(v['outputs'],'output');unique(v['workflows'],'workflow')
 agents={x['id']:x for x in v['agents']}
 local=[]
 for x in v['agents']:
  for k in ('name','role','parent_agent_id','runtime_profile_id','execution_host','model_label','run_id','task_id','step_id','state','source_event_ids','summary'):require(k in x,'agent missing '+k)
  require(x['runtime_profile_id'] in profiles,'unknown agent owner');require(x['state'] in LIFECYCLE,'invented lifecycle')
  require(x['parent_agent_id'] is None or x['parent_agent_id'] in agent_ids,'dangling parent agent')
  if x['runtime_profile_id']!='bob_team':local.append(x)
  require(set(x['source_event_ids'])<=known,'fabricated agent source event')
 require(len(local)<=1,'local model-only session has fake teammates')
 for w in v['workflows']:
  require(w['state'] in LIFECYCLE,'invented workflow lifecycle');require('session_id' in w and 'title' in w and 'run_id' in w,'workflow attribution missing')
  steps={s['id']:s for s in w['steps']};require(len(steps)==len(w['steps']),'duplicate step')
  for s in steps.values():
   for k in ('parent_step_id','depends_on','agent_id','label','state','approval_id','output_ids','source_event_ids'):require(k in s,'step missing '+k)
   require(s['state'] in LIFECYCLE,'invented step lifecycle')
   require(set(s['depends_on'])<=set(steps),'dangling dependency')
   require(s['parent_step_id'] is None or s['parent_step_id'] in steps,'dangling parent step')
   require(s['agent_id'] is None or s['agent_id'] in agents,'dangling step agent')
   if s['agent_id']:require(agents[s['agent_id']]['run_id']==w['run_id'],'step crosses run authority')
   require(set(s['output_ids'])<=output_ids,'dangling output')
   require(set(s['source_event_ids'])<=known,'fabricated step source event')
   if s['state'] in {'completed','failed','waiting_approval','cancelled'}:require(bool(s['source_event_ids']),'observed step without source evidence')
   if s['state']=='waiting_approval':require(session is not None and session['pending'] is not None and s['approval_id']==session['pending']['id'],'approval rebound to wrong pending action')
   for e in (session['events'] if session else []):
    if e['event_id'] in s['source_event_ids'] and e['kind']=='tool.result' and e['data'].get('status') in {'denied','failed','aborted','indeterminate'}:
     require(s['state']!='completed','failed/denied tool painted green')
  def visit(id,chain):
   require(id not in chain,'cyclic workflow dependencies')
   for dep in steps[id]['depends_on']:visit(dep,chain|{id})
  for id in steps:visit(id,set())
 for o in v['outputs']:
  for k in ('run_id','step_id','agent_id','kind','title','path','text','source_event_ids'):require(k in o,'output missing '+k)
  require(o['kind'] in {'file','diff','text','tool_result'},'invented output type')
  require(set(o['source_event_ids'])<=known and bool(o['source_event_ids']),'output lacks real source')
  require(o['agent_id'] is None or (o['agent_id'] in agents and agents[o['agent_id']]['run_id']==o['run_id']),'output crosses owner/run')
 return {'profiles':len(profiles),'agents':len(agents),'workflows':len(v['workflows']),'validated_local_event_count':len(local_ids),'bob_origin_verified':False}

def functional(root):
 def read(name):return json.loads((root/(name+'.json')).read_text())
 require(not list(root.glob('model-narrative-*')),'clarification used: preserve failure and report retry-assisted, not first-attempt PASS')
 names=['A-chat','B-project-evidence','C-approved-preview','C-approved-result','C-stale-rejected','C-denied-preview','C-denied-result','C-created-file','D-pending-before-restart','D-resumed','C-disconnected-transport']
 data={n:read(n) for n in names}
 # This driver's known action sequence has exactly one user request per write.
 # Internal APP evidence/rejection/cancellation messages are not user retries.
 counts={'A-chat':2,'B-project-evidence':5,'C-approved-preview':6,'C-approved-result':6,'C-stale-rejected':7,'C-denied-preview':8,'C-denied-result':8,'C-created-file':9,'D-pending-before-restart':10,'D-resumed':12}
 for name,count in counts.items():
  observed=sum(m['role']=='user' and not m['content'].startswith('APP') for m in data[name]['messages'])
  require(observed==count,f'{name}: {observed} user requests, expected {count}; no retry-assisted first-attempt claim')
 for s in data.values():event_chain(s['events'])
 a=data['A-chat'];require(sum(m['role']=='user' for m in a['messages'])>=2,'no arbitrary multi-turn chat')
 require(len({r['completion_id'] for r in a['receipts']})>=2,'no distinct model completions')
 b=data['B-project-evidence'];tools={e['data']['action']['tool'] for e in b['events'] if e['kind']=='tool.call'}
 require({'read','search','list'}<=tools,'missing actual list/read/search execution')
 p=data['C-approved-preview']['pending'];require(p is not None,'narrative is not a write proposal')
 require(p['diff'] in (root/'C-approved-preview.txt').read_text(),'exact native diff not visible')
 result=data['C-approved-result'];effects=[e['data'] for e in result['events'] if e['kind']=='effect' and e['data']['call_id']==p['call_id']]
 require(len(effects)==1 and effects[0]['verified'] is True,'missing verified actual effect')
 require(effects[0]['after_digest']==sha(p['content'].encode()),'Allow bytes differ from approved model-authored bytes')
 require(effects[0]['before_digest']==sha(p['before'].encode()),'preimage mismatch')
 denied=data['C-denied-preview']['pending'];after=data['C-denied-result']
 require(any(e['kind']=='tool.result' and e['data'].get('call_id')==denied['call_id'] and e['data']['status']=='denied' for e in after['events']),'no actual denial receipt')
 require(not any(e['kind']=='effect' and e['data']['call_id']==denied['call_id'] for e in after['events']),'denial caused an effect')
 require('stale file' in (root/'C-stale-rejected.txt').read_text().lower(),'stale rejection not visible')
 before=data['D-pending-before-restart'];resumed=data['D-resumed'];require(before['pending'] is not None and resumed['pending'] is None,'pending approval not cancelled on resume')
 require(before['id']==resumed['id'] and before['project']==resumed['project'],'resume changed session or project authority')
 pending_call=before['pending']['call_id'];require(not any(e['kind']=='effect' and e['data']['call_id']==pending_call for e in resumed['events']),'resume auto-executed pending write')
 # Read only copied disposable artifacts, never follow recorded absolute project paths.
 disk=root/'disposable-project/mission.txt';require(disk.read_bytes()==denied['before'].encode(),'denied/restart byte preservation does not match final disposable file')
 d=data['C-disconnected-transport'];require(not d['receipts'] and not any(m['role']=='assistant' for m in d['messages']),'disconnect invented model fallback')
 return {'tier':'ARTIFACT_RECHECK_NOT_FRESH_LIVE','event_chain_valid':True,'approved_exact_bytes':True,'denial_resume_final_bytes':True,'first_attempt_only':True,'session_id':resumed['id']}

def unit_view():
 # Explicit UNIT fixture only; not a product projection or live run.
 def profile(id,bob=False):return {'id':id,'label':'UNIT','adapter_kind':'agent_runtime' if bob else 'model_only','execution_host':'UNIT','model_label':None,'memory_owner':'UNIT','effect_owner':'bob_runtime' if bob else 'cleanroom_local','configured':False,'capabilities':{k:False for k in CAPS},'unavailable_reason':'UNIT'}
 return {'schema_version':'workbench-view-v1','app':None,'profiles':[profile('local_bob_model'),profile('standalone_http'),profile('bob_team',True)],'active_profile_id':'local_bob_model','connection_state':'unknown','agents':[],'workflows':[],'outputs':[],'notices':['UNIT ONLY']}
class CheckerTests(unittest.TestCase):
 def test_unit_minimal_view(self):snapshot(unit_view())
 def mutation(self,fn,message):
  v=unit_view();fn(v)
  with self.assertRaisesRegex(AssertionError,message):snapshot(v)
 def test_legacy_app_is_not_P1(self):
  with self.assertRaisesRegex(AssertionError,'missing workbench'):snapshot({'session':{}})
 def test_model_pipe_not_bob_runtime(self):self.mutation(lambda v:v['profiles'][0].update(effect_owner='bob_runtime'),'falsely claims')
 def test_standalone_not_bob_execution(self):self.mutation(lambda v:v['profiles'][1].update(adapter_kind='agent_runtime'),'falsely claims')
 def test_no_local_fake_multiagent(self):self.mutation(lambda v:v['profiles'][0]['capabilities'].update(multi_agent=True),'fabricates')
 def test_bob_readonly_no_project_grant(self):self.mutation(lambda v:v['profiles'][2]['capabilities'].update(writes=True),'grants local')
 def test_disconnected_not_success(self):self.mutation(lambda v:v.update(connection_state='completed'),'invented connection')
 def test_selected_profile_must_exist(self):self.mutation(lambda v:v.update(active_profile_id='from-model-text'),'unknown selected')
 def test_missing_capability_is_not_truth(self):self.mutation(lambda v:v['profiles'][0]['capabilities'].pop('cancel'),'capability omitted')
 def test_duplicate_profile_rejected(self):self.mutation(lambda v:v['profiles'].append(v['profiles'][0]),'duplicate profile')
 def test_fabricated_output_rejected(self):self.mutation(lambda v:v['outputs'].append({'id':'unit-o','run_id':'r','step_id':None,'agent_id':None,'kind':'text','title':'fake','path':None,'text':'claimed output','source_event_ids':['invented']}),'lacks real source')
 def event_view(self,status='denied'):
  v=unit_view();e={'schema_version':'functional-app-v2','event_id':'UNIT-event','ordinal':'1','kind':'tool.result','data':{'status':status},'previous_event_id':None,'previous_digest':None,'digest':''};e['digest']=digest(e)
  v['app']={'session':{'id':'UNIT-session','events':[e],'pending':None}}
  v['agents']=[{'id':'UNIT-agent','name':'UNIT','role':'root','parent_agent_id':None,'runtime_profile_id':'local_bob_model','execution_host':'UNIT','model_label':None,'run_id':'UNIT-run','task_id':'UNIT-task','step_id':None,'state':'completed','source_event_ids':['UNIT-event'],'summary':'UNIT'}]
  v['workflows']=[{'id':'UNIT-workflow','run_id':'UNIT-run','session_id':'UNIT-session','title':'UNIT','state':'completed','steps':[{'id':'UNIT-step','parent_step_id':None,'depends_on':[],'agent_id':'UNIT-agent','label':'UNIT denied tool','state':'blocked','approval_id':None,'output_ids':[],'source_event_ids':['UNIT-event']}]}]
  return v
 def test_completed_root_may_contain_denied_step(self):snapshot(self.event_view())
 def test_denied_step_cannot_turn_green(self):
  v=self.event_view();v['workflows'][0]['steps'][0]['state']='completed'
  with self.assertRaisesRegex(AssertionError,'painted green'):snapshot(v)
 def test_pending_approval_cannot_rebind(self):
  v=self.event_view();v['app']['session']['pending']={'id':'real-pending'};s=v['workflows'][0]['steps'][0];s.update(state='waiting_approval',approval_id='different-owner')
  with self.assertRaisesRegex(AssertionError,'rebound'):snapshot(v)
 def test_fake_teammate_not_in_local_runtime(self):
  v=self.event_view();v['agents'].append({**v['agents'][0],'id':'invented-worker'})
  with self.assertRaisesRegex(AssertionError,'fake teammates'):snapshot(v)
 def test_unknown_source_is_not_measured_progress(self):
  v=self.event_view();v['agents'][0]['source_event_ids']=['nonexistent']
  with self.assertRaisesRegex(AssertionError,'fabricated agent'):snapshot(v)
 def test_cyclic_step_dependencies(self):
  v=self.event_view();v['workflows'][0]['steps'][0]['depends_on']=['UNIT-step']
  with self.assertRaisesRegex(AssertionError,'cyclic'):snapshot(v)
 def test_step_cannot_move_between_runtime_runs(self):
  v=self.event_view();v['workflows'][0]['run_id']='other-run'
  with self.assertRaisesRegex(AssertionError,'crosses run'):snapshot(v)
 def test_tampered_actual_source_event_fails_digest(self):
  v=self.event_view();v['app']['session']['events'][0]['data']['status']='success'
  with self.assertRaisesRegex(AssertionError,'digest mismatch'):snapshot(v)
 def test_fabricated_event_chain_rejected(self):
  e={'schema_version':'functional-app-v2','event_id':'UNIT','ordinal':'1','kind':'tool.result','data':{},'previous_event_id':None,'previous_digest':None,'digest':'invented'}
  with self.assertRaisesRegex(AssertionError,'digest mismatch'):event_chain([e])

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--self-test',action='store_true');p.add_argument('--snapshot',type=Path);p.add_argument('--bob-event-ids',type=Path);p.add_argument('--functional-dir',type=Path);a=p.parse_args()
 if a.self_test:sys.exit(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CheckerTests)).wasSuccessful())
 try:
  if a.snapshot:result=snapshot(json.loads(a.snapshot.read_text()),json.loads(a.bob_event_ids.read_text()) if a.bob_event_ids else [])
  elif a.functional_dir:result=functional(a.functional_dir)
  else:p.error('choose --self-test, --snapshot or --functional-dir')
  print(json.dumps({'status':'PASS','result':result},indent=2))
 except (AssertionError,KeyError,ValueError,OSError) as e:print(json.dumps({'status':'FAIL','error':str(e)},indent=2));sys.exit(1)
