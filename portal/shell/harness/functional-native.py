#!/usr/bin/env python3
"""REAL native app interaction in private Xvfb/D-Bus, CPU only. No fake model."""
import os, sys, json, subprocess, time, signal
from pathlib import Path
repo=Path(__file__).resolve().parents[2]
root=Path(sys.argv[1]).resolve(); root.mkdir(parents=True,exist_ok=True)
if '--inside' not in sys.argv:
 env=os.environ.copy()
 for k in ['DISPLAY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS','AT_SPI_BUS_ADDRESS','DBUS_STARTER_ADDRESS','DBUS_STARTER_BUS_TYPE']:env.pop(k,None)
 for k,sub in [('XDG_RUNTIME_DIR','runtime'),('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),('XDG_DATA_HOME','data'),('TMPDIR','tmp')]:
  p=root/sub;p.mkdir(exist_ok=True);p.chmod(0o700);env[k]=str(p)
 env.update(GDK_BACKEND='x11',GTK_A11Y='always',GTK_MODULES='atk-bridge',LIBGL_ALWAYS_SOFTWARE='1',GALLIUM_DRIVER='llvmpipe',WEBKIT_DISABLE_DMABUF_RENDERER='1',WEBKIT_DISABLE_COMPOSITING_MODE='1',GSETTINGS_BACKEND='memory',BOB_FUNCTIONAL_PRIVATE_DISPLAY='1')
 sys.exit(subprocess.run(['xvfb-run','-a','-f',str(root/'Xauthority'),'-s','-screen 0 1280x1100x24 -nolisten tcp','dbus-run-session','--',sys.executable,__file__,str(root),'--inside']+(['--disconnect-only'] if '--disconnect-only' in sys.argv else []),env=env,timeout=420).returncode)
assert os.environ['BOB_FUNCTIONAL_PRIVATE_DISPLAY']=='1'
import pyatspi
log=open(root/'app.log','w'); app=None

def start(disconnected=False):
 env=os.environ.copy()
 if disconnected:
  bindir=root/'transport-failure'/'bin';bindir.mkdir(parents=True,exist_ok=True)
  wrapper=bindir/'ssh';wrapper.write_text('#!/bin/sh\nexec /usr/bin/ssh -o ControlMaster=no -o ControlPath=none -o ProxyCommand=/usr/bin/false "$@"\n');wrapper.chmod(0o700)
  env['PATH']=str(bindir)+':'+env['PATH']
 return subprocess.Popen(['bash',str(repo/'scripts/linux/run-functional.sh'),'--no-build'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=env)
def nodes():
 out=[]
 def walk(n,d=0):
  if d>50:return
  try:
   out.append(n)
   for c in n:walk(c,d+1)
  except Exception:pass
 walk(pyatspi.Registry.getDesktop(0));return out

def find(name):
 for n in nodes():
  try:
   if n.name==name and (name not in ["Message","Project directory"] or n.getRoleName() in ["entry","text"]):return n
  except Exception:pass

def text():
 out=[]
 for n in nodes():
  try:out.append(n.queryText().getText(0,-1))
  except Exception:pass
 return '\n'.join(out)
def wait(fn,timeout=140):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  v=fn()
  if v:return v
  if app.poll() is not None:raise RuntimeError('app exited '+str(app.returncode))
  time.sleep(.2)
 raise AssertionError('condition timed out; UI: '+text()[-6000:])
def click(name):
 n=wait(lambda:find(name));assert n.queryAction().doAction(0),name

def enter(name,value):
 n=wait(lambda:find(name));assert n.queryComponent().grabFocus(),name
 subprocess.run(['xdotool','key','--clearmodifiers','ctrl+a'],check=True)
 subprocess.run(['xdotool','type','--clearmodifiers','--',value],check=True)

def store():return root/'data'/'bob-functional-v2'
def snapshot():
 files=list(store().glob('*.json'))
 if not files:return None
 return json.loads(max(files,key=lambda f:f.stat().st_mtime_ns).read_text())['snapshot']
def snap_save(name):
 s=snapshot();(root/(name+'.json')).write_text(json.dumps(s,indent=2));(root/(name+'.txt')).write_text(text());return s

def send(value,expected=None,pending=False):
 # A text model can occasionally narrate instead of issuing the requested tool.
 # Behave like a real user: one explicit corrective follow-up, never synthesize
 # an action or count narrative as a file proposal. Both replies remain recorded.
 for attempt in range(2):
  old=snapshot();old_count=len(old['receipts']) if old else 0
  request=value if attempt==0 else ('Please issue the actual structured write ACTION now for my preceding request, not a message describing a hypothetical proposal. Preserve the actual current file evidence; the app will ask me for explicit approval. Do not execute or claim a write yourself.')
  enter('Message',request);click('Send message')
  wait(lambda:(s:=snapshot()) and len(s['receipts'])>old_count and (s['pending'] is not None or s['messages'][-1]['role']=='assistant') and 'Working: real Bob' not in text())
  s=snapshot()
  if pending and s['pending'] is None:
   snap_save('model-narrative-instead-of-action-'+str(len(list(root.glob('model-narrative-*')))))
   continue
  if not pending:assert s['pending'] is None,'unexpected write proposal instead of read/chat answer'
  if expected:assert expected.lower() in s['messages'][-1]['content'].lower(),s['messages'][-1]
  return s
 raise AssertionError('Actual model did not issue requested write action after explicit follow-up; no proposal was fabricated')

def choose_saved(id):
 ids=sorted(f.stem for f in store().glob('*.json'))
 index=ids.index(id)+1
 combo=wait(lambda:find('Saved session'));assert combo.queryComponent().grabFocus()
 subprocess.run(['xdotool','key','--clearmodifiers','Home'],check=True)
 for _ in range(index):subprocess.run(['xdotool','key','Down'],check=True)
 subprocess.run(['xdotool','key','Return'],check=True);click('Open saved session')
 wait(lambda:snapshot()['id']==id);wait(lambda:id in text())
try:
 if '--disconnect-only' in sys.argv:
  app=start(disconnected=True);wait(lambda:find('Message'))
  enter('Message','Hello disconnected transport acceptance probe.');click('Send message')
  wait(lambda:'relay unavailable' in text().lower(),timeout=30)
  failed=snap_save('C-disconnected-transport');assert len(failed['receipts'])==0
  assert not any(m['role']=='assistant' for m in failed['messages'])
  subprocess.run(['import','-window','root',str(root/'native-disconnected.png')],check=True)
  (root/'DISCONNECTED-ACCEPTANCE.json').write_text(json.dumps({'native_error':True,'no_fake_fallback':True,'fault':'real SSH process: ControlPath=none, ProxyCommand=false; existing master untouched'},indent=2))
  print('Native real SSH disconnection: visible error, zero receipts, no assistant fallback',flush=True)
  sys.exit(0)
 app=start();wait(lambda:find('Message'))
 subprocess.run(['import','-window','root',str(root/'native-initial.png')],check=True)
 send('Our imaginary story mascot is a cobalt otter named Nori. Please acknowledge.','Nori')
 send('What is the name of the imaginary story mascot I mentioned?','Nori')
 a=snap_save('A-chat');session_id=a['id']
 # Fresh project evidence generated only on disk, never put into the question.
 import uuid
 code='ORCHID-'+uuid.uuid4().hex[:12];project=root/'disposable-project';project.mkdir(exist_ok=True)
 original=f'Observatory code: {code}\nStatus: draft\n'
 (project/'mission.txt').write_text(original)
 enter('Project directory',str(project));click('Attach project');wait(lambda:snapshot()['project'] is not None)
 send('Use the list action to list the attached project files.','mission.txt')
 send('Use the search action to search for the literal text Observatory and tell me the full matching line.',code)
 send('Use the read action to read mission.txt and tell me its code and status.',code)
 b=snap_save('B-project-evidence')
 tools=[e['data']['action']['tool'] for e in b['events'] if e['kind']=='tool.call']
 assert {'list','search','read'}<=set(tools),tools
 # Approved update: actual model authors complete replacement using tool evidence.
 send('Update mission.txt: preserve the Observatory code line exactly and change Status: draft to Status: reviewed. Read it if necessary, then propose the write action; do not claim it is written until the app confirms.',pending=True)
 pending=snapshot()['pending'];assert pending['path']=='mission.txt';assert code in pending['content'];assert 'Status: reviewed' in pending['content'];assert (project/'mission.txt').read_text()==original
 wait(lambda:find('Allow this write once'));assert pending['diff'] in text(),text()[-4000:]
 snap_save('C-approved-preview');find('Allow this write once').queryComponent().grabFocus();subprocess.run(['import','-window','root',str(root/'native-write-preview.png')],check=True);click('Allow this write once')
 wait(lambda:snapshot()['pending'] is None and any(e['kind']=='effect' for e in snapshot()['events']))
 wait(lambda:'Working: real Bob' not in text())
 changed=(project/'mission.txt').read_text();assert code in changed and 'Status: reviewed' in changed
 snap_save('C-approved-result')
 # Live stale-file negative: change disk bytes only AFTER the real model proposal.
 send('Propose adding a final line Audit: checked to mission.txt, preserving its current lines. Use write and wait for my decision.',pending=True)
 external=changed+'Manual check: preserved\n';(project/'mission.txt').write_text(external)
 click('Allow this write once');wait(lambda:'stale file' in text().lower())
 assert (project/'mission.txt').read_text()==external
 snap_save('C-stale-rejected');click('Cancel current turn');wait(lambda:snapshot()['pending'] is None)
 changed=external
 # Denied update: user decision must leave every byte unchanged.
 send('Propose changing Status: reviewed to Status: blocked in mission.txt. Use write, and wait for my approval.',pending=True)
 wait(lambda:find('Deny this write'));snap_save('C-denied-preview');click('Deny this write')
 wait(lambda:snapshot()['pending'] is None);wait(lambda:'Working: real Bob' not in text())
 assert (project/'mission.txt').read_text()==changed
 assert any(e['kind']=='tool.result' and e['data']['status']=='denied' for e in snapshot()['events'])
 snap_save('C-denied-result')
 # Approved creation is useful work, not a fixed fixture.
 send('Create recap.txt containing the Observatory code and current reviewed status from mission.txt. Use the write action and wait for my approval.',pending=True)
 assert snapshot()['pending']['path']=='recap.txt';assert not (project/'recap.txt').exists();click('Allow this write once')
 wait(lambda:(project/'recap.txt').exists());wait(lambda:'Working: real Bob' not in text())
 assert code in (project/'recap.txt').read_text();snap_save('C-created-file')
 # Restart with an unresolved model proposal: must cancel, never execute on reopen.
 send('Propose changing mission.txt to Status: archived, preserving its Observatory code. Use write and wait for my decision.',pending=True)
 before_restart=(project/'mission.txt').read_bytes();snap_save('D-pending-before-restart')
 os.killpg(app.pid,signal.SIGTERM);app.wait(timeout=10);time.sleep(1)
 app=start();wait(lambda:find('Message'));choose_saved(session_id)
 assert snapshot()['pending'] is None;assert (project/'mission.txt').read_bytes()==before_restart
 assert snapshot()['project']['root']==str(project)
 send('What was our imaginary story mascot name before we worked on the files?','Nori')
 send('Read mission.txt again and report its current code and status. Do not write anything.',code)
 assert 'reviewed' in snapshot()['messages'][-1]['content'].lower()
 d=snap_save('D-resumed')
 click('Real model receipts');time.sleep(.3)
 (root/'native-final.txt').write_text(text());subprocess.run(['import','-window','root',str(root/'native-final.png')],check=True)
 completions=[{'completion_id':r.get('completion_id'),'model':r.get('model'),'bob':r.get('bob')} for r in d['receipts']]
 assert all(r['completion_id'] and r['bob']['model_pin']=='deepseek-v4-flash' for r in completions)
 # Disconnection is a real SSH transport failure, injected only into this
 # child process via ProxyCommand=false and ControlPath=none (a reused
 # SSH master would otherwise bypass the injected disconnect). No fake response, server outage or
 # credentials/config changes. Assert actual native alert + no assistant fallback.
 os.killpg(app.pid,signal.SIGTERM);app.wait(timeout=10);time.sleep(.5)
 app=start(disconnected=True);wait(lambda:find('Message'))
 enter('Message','Hello, this is a disconnected transport acceptance probe.');click('Send message')
 wait(lambda:'relay unavailable' in text().lower());failed=snap_save('C-disconnected-transport')
 assert len(failed['receipts'])==0
 assert not any(m['role']=='assistant' for m in failed['messages'])
 subprocess.run(['import','-window','root',str(root/'native-disconnected.png')],check=True)
 summary={'A':True,'B':True,'C':True,'D':True,'E':True,'F':'separate full battery','stale_native':True,'disconnected_native':'actual SSH process failure via test-only ProxyCommand=false; no fake model output','session_id':session_id,'project':str(project),'fresh_code':code,'completions':completions,'display':os.environ['DISPLAY'],'screenreader':'NOT_ACCEPTED: native AT-SPI interactions measured, no human speech acceptance','launcher':'scripts/linux/run-functional.sh --no-build'}
 (root/'LIVE-ACCEPTANCE.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)

finally:
 if app and app.poll() is None:
  os.killpg(app.pid,signal.SIGTERM);app.wait(timeout=10)
 log.close()
