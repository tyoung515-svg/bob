#!/usr/bin/env python3
"""Independent P1 native smoke/RED and scripted transport replay, PRIVATE display only.
No live model or Bob calls: child-only SSH refusal is mandatory in this mode.
Use existing functional-native.py separately for authorized real-model journeys.
"""
import argparse, datetime, hashlib, json, os, signal, subprocess, sys, time
from pathlib import Path

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('root',type=Path);p.add_argument('--binary',type=Path)
p.add_argument('--inside',action='store_true');p.add_argument('--plan',type=Path)
p.add_argument('--transport',choices=['ok','error','redirect']);p.add_argument('--timeout',type=int,default=35)
a=p.parse_args()
if a.transport and not a.plan:p.error('--transport requires an explicit native UI --plan; zero requests cannot pass')
root=a.root.expanduser().resolve();repo=Path(__file__).resolve().parents[2]
allowed=(Path.home()/'.cache/bob-workbench-qa').resolve()
if allowed not in root.parents: p.error('root must be a fresh child of ~/.cache/bob-workbench-qa')
if not a.inside:
 if root.exists():p.error('refuse reusing existing evidence/session root')
 root.mkdir(parents=True,mode=0o700)
 env=os.environ.copy()
 for k in ['DISPLAY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS','AT_SPI_BUS_ADDRESS','DBUS_STARTER_ADDRESS','DBUS_STARTER_BUS_TYPE','BOB_FUNCTIONAL_DATA_ROOT','BOB_DEVELOPER_FIXTURE']:
  env.pop(k,None)
 for k,sub in [('XDG_RUNTIME_DIR','runtime'),('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),('XDG_DATA_HOME','data'),('TMPDIR','tmp')]:
  f=root/sub;f.mkdir(mode=0o700);env[k]=str(f)
 env.update(BOB_FUNCTIONAL_DATA_ROOT=str(root/'data/store'),GDK_BACKEND='x11',GTK_A11Y='always',GTK_MODULES='atk-bridge',LIBGL_ALWAYS_SOFTWARE='1',GALLIUM_DRIVER='llvmpipe',WEBKIT_DISABLE_DMABUF_RENDERER='1',WEBKIT_DISABLE_COMPOSITING_MODE='1',GSETTINGS_BACKEND='memory',P1_PRIVATE_DISPLAY='1')
 cmd=['xvfb-run','-a','-f',str(root/'Xauthority'),'-s','-screen 0 1280x1100x24 -nolisten tcp','dbus-run-session','--',sys.executable,str(Path(__file__).resolve()),str(root),'--inside','--timeout',str(a.timeout)]
 for key,value in [('--binary',a.binary),('--plan',a.plan),('--transport',a.transport)]:
  if value:cmd += [key,str(value)]
 sys.exit(subprocess.run(cmd,env=env,timeout=240).returncode)
assert os.environ.get('P1_PRIVATE_DISPLAY')=='1'
import pyatspi
checks=[];app=None;server=None;redirect_server=None;requests=[];redirect_hits=[]
(root/'bin').mkdir();ssh=root/'bin/ssh'
ssh.write_text('#!/bin/sh\nprintf "blocked SSH invocation\\n" >> "$P1_SSH_ATTEMPTS"\nprintf "P1 QA: SSH blocked; no live Bob or provider dispatch\\n" >&2\nexit 97\n');ssh.chmod(0o700)
os.environ['PATH']=str(root/'bin')+':'+os.environ['PATH'];os.environ['P1_SSH_ATTEMPTS']=str(root/'ssh-attempts.txt')
# Loopback HTTP is a TRANSPORT_SIMULATED fixture, NEVER a real provider.
if a.transport:
 from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
 import threading,uuid
 marker='SIMULATED-'+uuid.uuid4().hex
 class RedirectTrap(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def do_GET(self):
   redirect_hits.append(self.path);self.send_response(403);self.end_headers()
  do_POST=do_GET
 if a.transport=='redirect':
  redirect_server=ThreadingHTTPServer(('127.0.0.1',0),RedirectTrap)
  threading.Thread(target=redirect_server.serve_forever,daemon=True).start()
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def do_POST(self):
   data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
   requests.append({'path':self.path,'body':data}) # synthetic QA messages only; no headers/secrets
   if a.transport=='error':self.send_response(503);self.end_headers();self.wfile.write(b'QA synthetic unavailable');return
   if a.transport=='redirect':self.send_response(307);self.send_header('Location','http://localhost:'+str(redirect_server.server_port)+'/forbidden');self.end_headers();return
   body=json.dumps({'id':'transport-fixture','object':'chat.completion','model':'qa-transport-only','choices':[{'index':0,'message':{'role':'assistant','content':json.dumps({'type':'message','content':marker})},'finish_reason':'stop'}]}).encode()
   self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
 endpoint='http://127.0.0.1:'+str(server.server_port)+'/v1/chat/completions'
else: marker=endpoint=''

def nodes():
 out=[]
 def walk(n,d=0):
  if d>60:return
  try:
   out.append(n)
   for c in n:walk(c,d+1)
  except Exception:pass
 walk(pyatspi.Registry.getDesktop(0));return out

def inventory():
 out=[]
 for n in nodes():
  try:out.append({'name':n.name,'role':n.getRoleName()})
  except Exception:pass
 return out

def text():
 out=[]
 for n in nodes():
  try:out.append(n.queryText().getText(0,-1))
  except Exception:pass
 return '\n'.join(out)

def find(name):
 for n in nodes():
  try:
   if n.name==name:return n
  except Exception:pass

def wait(fn):
 end=time.monotonic()+a.timeout
 while time.monotonic()<end:
  result=fn()
  if result:return result
  if app.poll() is not None:raise RuntimeError('native app exited '+str(app.returncode))
  time.sleep(.2)
 raise AssertionError('native condition timed out')

def check(id,condition,detail):
 checks.append({'id':id,'status':'PASS' if condition else 'FAIL','detail':detail})

def save(label):
 (root/(label+'-atspi.json')).write_text(json.dumps(inventory(),indent=2))
 (root/(label+'.txt')).write_text(text())
 subprocess.run(['import','-window','root',str(root/(label+'.png'))],check=True)

try:
 log=open(root/'app.log','w')
 binary=a.binary.resolve() if a.binary else repo/'shell/src-tauri/target/debug/bob-slice-shell'
 assert binary.is_file(),'missing built native binary; setup failure is NOT expected product RED'
 app=subprocess.Popen([str(binary)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 wait(lambda:any(n.get('role') in ('entry','text') and n['name']=='Message' for n in inventory()))
 save('initial');inv=inventory();names='\n'.join(n['name'] for n in inv);t=text()
 check('CONTROL-native-real-composer',find('Message') is not None,'actual compiled app and AT-SPI ready; no build/import failure')
 check('CONTROL-native-project-attach',find('Attach project') is not None,'functional attach control remains usable')
 check('CONTROL-no-startup-generation',not (root/'ssh-attempts.txt').exists(),'fresh app launch never attempts remote dispatch')
 check('J1-navigation',any(n['role'] in ('navigation','navigation bar') for n in inv) or 'navigation' in names.lower(),'workbench navigation landmark required, not just legacy session select')
 check('J4-agent-inspector','agent' in names.lower() and any(w in names.lower() for w in ('workflow','timeline','steps')),'named agent and workflow inspection controls derived from real native view')
 check('J6-standalone-selection','standalone' in (names+'\n'+t).lower(),'explicit independent endpoint profile, not temporary Bob model label')
 check('J5-bob-runtime-distinction',any(s in (names+'\n'+t).lower() for s in ('bob runtime','bob team','bob-owned')),'separate Bob-owned execution from model-only route')
 check('J3-native-title-controls',any(s in names.lower() for s in ('rename','session title','task title')),'native persisted title editing required')
 # Exercise native project authority/persistence without spending a model call.
 project=root/'synthetic-project';project.mkdir();(project/'public.txt').write_text('Synthetic QA project only\n')
 n=wait(lambda:find('Project directory'));assert n.queryComponent().grabFocus()
 subprocess.run(['xdotool','key','--clearmodifiers','ctrl+a'],check=True)
 subprocess.run(['xdotool','type','--clearmodifiers','--',str(project)],check=True)
 assert wait(lambda:find('Attach project')).queryAction().doAction(0)
 def attached():
  files=list((root/'data/store').glob('*.json'))
  for file in files:
   v=json.loads(file.read_text()).get('snapshot',{})
   if (v.get('project') or {}).get('root')==str(project):return v
 s=wait(attached);(root/'attached-session.json').write_text(json.dumps(s,indent=2))
 check('CONTROL-real-native-attach',s['project']['root']==str(project),'actual native persistence grants only disposable selected project')
 check('CONTROL-attach-does-not-call-model',not (root/'ssh-attempts.txt').exists(),'local project grant is not model/provider execution')
 save('attached')
 # An integration plan is an explicit AT-SPI action list using actual UI names,
 # not an alternate implementation. Adapt names only, never bypass native authority.
 if a.plan:
  plan=json.loads(a.plan.read_text())
  for i,step in enumerate(plan):
   action=step['action'];value=step.get('value','').replace('$ENDPOINT',endpoint).replace('$MARKER',marker)
   if action=='click':assert wait(lambda:find(step['name'])).queryAction().doAction(0)
   elif action=='enter':
    n=wait(lambda:find(step['name']));assert n.queryComponent().grabFocus()
    subprocess.run(['xdotool','key','--clearmodifiers','ctrl+a'],check=True)
    subprocess.run(['xdotool','type','--clearmodifiers','--',value],check=True)
   elif action=='select_index':
    n=wait(lambda:find(step['name']));assert n.queryComponent().grabFocus()
    index=step['index'];assert type(index) is int and 0<=index<100
    subprocess.run(['xdotool','key','--clearmodifiers','Home'],check=True)
    for _ in range(index):subprocess.run(['xdotool','key','Down'],check=True)
    subprocess.run(['xdotool','key','Return'],check=True)
   elif action=='wait_text':wait(lambda:value in text())
   elif action=='absent_text':assert value not in text()
   elif action=='assert_no_receipts':
    sessions=[json.loads(f.read_text())['snapshot'] for f in (root/'data/store').glob('*.json')]
    assert sessions and all(not v.get('receipts') and not any(m['role']=='assistant' for m in v.get('messages',[])) for v in sessions),'native transport failure fabricated receipt or answer'
    check('CONTROL-native-disconnect-zero-receipts',True,'real child SSH refusal left no model receipts or assistant fallback')
   elif action=='assert_ssh_attempts':
    attempts=(root/'ssh-attempts.txt').read_text().splitlines() if (root/'ssh-attempts.txt').exists() else []
    assert len(attempts)==step['count'],'unexpected SSH dispatch count'
    check('CONTROL-native-SSH-dispatch-count',True,'observed child SSH invocations: '+str(len(attempts)))
   elif action=='capture':save('plan-'+str(i))
   else:raise AssertionError('unknown plan action '+action)
  if a.transport:
   check('J6-transport-request',len(requests)>0,'native app must actually POST to disposable server')
   check('J6-no-SSH-fallback',not (root/'ssh-attempts.txt').exists(),'standalone success/failure must never attempt Bob SSH')
   if a.transport=='ok':check('J6-native-transport-response',marker in text(),'random marker came through native adapter, not a fixture UI')
   else:
    final_sessions=[json.loads(f.read_text())['snapshot'] for f in (root/'data/store').glob('*.json')]
    check('J6-error-no-fake-success',marker not in text() and all(not v.get('receipts') and not any(m['role']=='assistant' for m in v.get('messages',[])) for v in final_sessions),'error/redirect produces zero completion receipts and no assistant fallback in any private session')
    check('J6-visible-native-error',any(n['role']=='alert' for n in inventory()),'endpoint failure must be visible, not swallowed')
   if a.transport=='redirect':check('J6-no-cross-host-redirect',not redirect_hits,'second localhost trap must receive zero redirected requests')
  save('after-plan')
except Exception as e:
 checks.append({'id':'HARNESS-OR-PLAN','status':'ERROR','detail':repr(e)})
 try:save('failure')
 except Exception:pass
finally:
 if app and app.poll() is None:os.killpg(app.pid,signal.SIGTERM);app.wait(timeout=10)
 if server:server.shutdown();(root/'transport-requests.json').write_text(json.dumps(requests,indent=2))
 if redirect_server:redirect_server.shutdown();(root/'redirect-hits.json').write_text(json.dumps(redirect_hits))
 if 'log' in locals():log.close()
 report={'schema':'p1-qa-evidence-v1','tier':'TRANSPORT_SIMULATED' if a.transport else 'NATIVE_NO_PROVIDER','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_head':subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),'binary':str(binary),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest() if binary.is_file() else None,'checks':checks,'human_screenreader':'NOT_ACCEPTED','wayland_desktop':'NOT_TESTED','live_model':False,'live_bob':False}
 (root/'result.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
sys.exit(2 if any(c['status']=='ERROR' for c in checks) else 1 if any(c['status']=='FAIL' for c in checks) else 0)
