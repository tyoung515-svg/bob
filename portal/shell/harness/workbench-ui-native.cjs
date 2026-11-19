// Real WebKit/Tauri smoke. All input and capture are on private Xvfb + D-Bus.
// --live deliberately adds bounded approved model chat/edit/resume checks.
const fs=require('node:fs'),path=require('node:path'),{spawnSync}=require('node:child_process');
const root=path.resolve(process.argv[2]||path.join(process.env.HOME,'.cache','workbench-ui-native-'+Date.now()));fs.mkdirSync(root,{recursive:true});
const script=String.raw`import os,sys,json,subprocess,time,signal,uuid
from pathlib import Path
root=Path(sys.argv[1]);repo=Path(sys.argv[2]);live='--live' in sys.argv;resume='--resume-only' in sys.argv
if '--inside' not in sys.argv:
 env=os.environ.copy()
 for key in ['DISPLAY','WAYLAND_DISPLAY','DBUS_SESSION_BUS_ADDRESS','AT_SPI_BUS_ADDRESS','DBUS_STARTER_ADDRESS','DBUS_STARTER_BUS_TYPE','BOB_DEVELOPER_FIXTURE']:env.pop(key,None)
 for key,sub in [('XDG_RUNTIME_DIR','runtime'),('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),('XDG_DATA_HOME','data'),('TMPDIR','tmp')]:
  directory=root/sub;directory.mkdir(exist_ok=True);directory.chmod(0o700);env[key]=str(directory)
 env.update(GDK_BACKEND='x11',GTK_A11Y='always',GTK_MODULES='atk-bridge',LIBGL_ALWAYS_SOFTWARE='1',GALLIUM_DRIVER='llvmpipe',WEBKIT_DISABLE_DMABUF_RENDERER='1',WEBKIT_DISABLE_COMPOSITING_MODE='1',GSETTINGS_BACKEND='memory',BOB_FUNCTIONAL_DATA_ROOT=str(root/'store'),P1_UI_PRIVATE_DISPLAY='1')
 sys.exit(subprocess.run(['xvfb-run','-a','-f',str(root/'Xauthority'),'-s','-screen 0 1280x1000x24 -nolisten tcp','dbus-run-session','--',sys.executable,__file__,str(root),str(repo),'--inside']+(['--live'] if live else [])+(['--resume-only'] if resume else []),env=env,timeout=450).returncode)
assert os.environ['P1_UI_PRIVATE_DISPLAY']=='1'
import pyatspi
app=None;log=open(root/('resume-app.log' if resume else 'app.log'),'w');result={'native':True,'resume_only':resume,'private_display':os.environ['DISPLAY'],'live_requested':live,'screenreader':'not accepted; AT-SPI measured only','checks':[]}
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
   if n.name==name and (name not in ['Message','Project directory','Search conversations'] or n.getRoleName() in ['entry','text']):return n
  except Exception:pass
def text():
 out=[]
 for n in nodes():
  try:out.append(n.queryText().getText(0,-1))
  except Exception:pass
 return '\n'.join(out)
def wait(fn,timeout=100):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  value=fn()
  if value:return value
  if app and app.poll() is not None:raise RuntimeError('app exited '+str(app.returncode))
  time.sleep(.2)
 raise AssertionError('condition timed out: '+text()[-5000:])
def click(name):
 n=wait(lambda:find(name));assert n.queryAction().doAction(0),name
 time.sleep(.2)
def enter(name,value):
 n=wait(lambda:find(name));assert n.queryComponent().grabFocus(),name
 subprocess.run(['xdotool','key','--clearmodifiers','ctrl+a','BackSpace'],check=True)
 subprocess.run(['xdotool','type','--clearmodifiers','--',value],check=True)
def screenshot(name):
 (root/(name+'.txt')).write_text(text())
 visible=subprocess.check_output(['xdotool','search','--onlyvisible','--name','BoB'],text=True).split()
 subprocess.run(['import','-window',visible[-1] if visible else 'root',str(root/(name+'.png'))],check=True)
def start():return subprocess.Popen(['bash',str(repo/'scripts/linux/run-functional.sh'),'--no-build'],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
def store():
 paths=list((root/'store').glob('*.json'))
 return json.loads(max(paths,key=lambda p:p.stat().st_mtime_ns).read_text())['snapshot'] if paths else None
def send(value,pending=False):
 old=store();count=len(old['receipts']) if old else 0
 enter('Message',value);click('Send message')
 wait(lambda:(s:=store()) and len(s['receipts'])>count and (s['pending'] is not None if pending else s['messages'][-1]['role']=='assistant'),timeout=140)
 wait(lambda:'Working: real model' not in text(),timeout=140)
 return store()
try:
 app=start();wait(lambda:find('Message'));wait(lambda:store())
 if resume:
  assert root.resolve().is_relative_to(Path.home()/'.cache/bob-p1-ui-evidence'),'resume-only is restricted to private UI evidence stores'
  observations=[]
  for n in nodes():
   try:observations.append({'role':n.getRoleName(),'name':n.name,'description':n.description})
   except Exception:pass
  (root/'resume-atspi.json').write_text(json.dumps(observations,indent=2))
  saved={p.stem:json.loads(p.read_text())['snapshot'] for p in (root/'store').glob('*.json') if p.stem[0]!='.'}
  target=next(id for id,s in saved.items() if s['messages'] and s['messages'][0]['content'].startswith('Our imaginary mascot'))
  assert saved[target]['pending'] is None,'never reopen an unknown pending effect in this resume probe'
  saved_name=next(n.name for n in nodes() if n.getRoleName() in ['button','push button'] and n.name.startswith('Our imaginary mascot'))
  click(saved_name);wait(lambda:store()['id']==target)
  assert 'Luma' in text();result['checks'].append('native persisted session reopened; actual prior messages and project restored')
  if live:
   resumed=send('Recall our imaginary mascot name. Do not read or write files.');assert 'Luma' in resumed['messages'][-1]['content'];result['checks'].append('new real model turn recalled persisted context');result['model_pins']=[r.get('bob',{}).get('model_pin') for r in resumed['receipts']];assert all(p=='deepseek-v4-flash' for p in result['model_pins'])
  result['session_id']=target;result['pass']=True;screenshot('native-resumed-final');print(json.dumps(result),flush=True);sys.exit(0)
 ids=subprocess.check_output(['xdotool','search','--onlyvisible','--name','BoB'],text=True).split()
 window=ids[-1];subprocess.run(['xdotool','windowsize',window,'1200','800'],check=True);time.sleep(.6)
 screenshot('native-1200-initial');assert find('New session');assert find('Settings');assert find('Agents');result['checks'].append('normal launcher and live native bridge, no fixture')
 click('Agents');wait(lambda:'observed agents' in text().lower());screenshot('native-1200-agents');click('Overview')
 subprocess.run(['xdotool','windowsize',window,'900','800'],check=True);time.sleep(.5);screenshot('native-900-initial')
 click('Hide inspector');wait(lambda:find('Show inspector'));screenshot('native-900-inspector-collapsed');click('Show inspector')
 enter('Search conversations','no-such-conversation');wait(lambda:'No matching conversations' in text());enter('Search conversations','');result['checks'].append('native search and inspector collapse at 900px')
 click('Settings');wait(lambda:find('Close settings'));screenshot('native-900-settings');click('Close settings')
 project=root/'disposable-project';project.mkdir(exist_ok=True);marker='ORCHARD-'+uuid.uuid4().hex[:10];original='Marker: '+marker+'\nStatus: draft\n';(project/'note.txt').write_text(original)
 enter('Project directory',str(project));click('Attach project');wait(lambda:store()['project'] is not None);result['checks'].append('native attached disposable project')
 if live:
  first=send('Our imaginary mascot is an ochre fox named Luma. Acknowledge in one sentence.');assert 'Luma' in first['messages'][-1]['content']
  second=send('What is our imaginary mascot name? Answer briefly.');assert 'Luma' in second['messages'][-1]['content'];result['checks'].append('two real model chat turns')
  proposal=send('Read note.txt with the read tool, then propose a write action changing only Status: draft to Status: reviewed. Preserve the Marker line exactly. Use the actual structured action and wait for my decision; do not merely describe it.',pending=True)
  p=proposal['pending'];assert p['path']=='note.txt' and marker in p['content'] and 'Status: reviewed' in p['content'];assert (project/'note.txt').read_text()==original
  click('Review pending write');screenshot('native-900-exact-diff');assert p['diff'] in text();click('Allow this write once');wait(lambda:store()['pending'] is None);wait(lambda:(project/'note.txt').read_text()==p['content']);result['checks'].append('real model diff allowed; exact native bytes match')
  wait(lambda:'Working: real model' not in text());before=(project/'note.txt').read_bytes()
  denied=send('Propose a write action changing Status: reviewed to Status: denied in note.txt, preserving the Marker line. Wait for my approval.',pending=True);assert denied['pending']['path']=='note.txt';click('Deny this write');wait(lambda:store()['pending'] is None);wait(lambda:'Working: real model' not in text());assert (project/'note.txt').read_bytes()==before;result['checks'].append('real model diff denied; bytes unchanged')
  session_id=store()['id'];screenshot('native-900-conversation')
  os.killpg(app.pid,signal.SIGTERM);app.wait(timeout=10);time.sleep(.6);app=start();wait(lambda:find('Message'));wait(lambda:any(n.getRoleName() in ['button','push button'] and n.name.startswith('Our imaginary mascot') for n in nodes()))
  saved_name=next(n.name for n in nodes() if n.getRoleName() in ['button','push button'] and n.name.startswith('Our imaginary mascot'));click(saved_name);wait(lambda:store()['id']==session_id);resumed=send('Recall our imaginary mascot name. Do not read or write files.');assert 'Luma' in resumed['messages'][-1]['content'];result['checks'].append('native persisted session opened and real model recalled prior context')
  result['session_id']=session_id;result['receipts']=len(resumed['receipts']);result['model_pins']=[r.get('bob',{}).get('model_pin') for r in resumed['receipts']];assert all(p=='deepseek-v4-flash' for p in result['model_pins'])
  screenshot('native-resumed')
 result['pass']=True;print(json.dumps(result),flush=True)
except Exception as error:
 result['pass']=False;result['error']=str(error);screenshot('resume-failure' if resume else 'native-failure');raise
finally:
 (root/('resume-result.json' if resume else 'result.json')).write_text(json.dumps(result,indent=2))
 if app and app.poll() is None:os.killpg(app.pid,signal.SIGTERM);app.wait(timeout=10)
 log.close()
`;
const file=path.join(root,'private-probe.py');fs.writeFileSync(file,script);const r=spawnSync('python3',[file,root,path.resolve('..'),...(process.argv.includes('--live')?['--live']:[]),...(process.argv.includes('--resume-only')?['--resume-only']:[])],{stdio:'inherit',timeout:480000});process.exitCode=r.status??1;
