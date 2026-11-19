#!/usr/bin/env python3
"""Real host boundary acceptance, never calls a model. Run with host executable."""
import json, os, pathlib, subprocess, sys, tempfile
host=pathlib.Path(sys.argv[1])
assert host.is_file(), 'RED: current workbench headless host executable is missing'
root=pathlib.Path(tempfile.mkdtemp(prefix='bob-wsl-host-test-', dir=os.environ.get('TMPDIR')))
store=root/'store'
p=subprocess.Popen([str(host),'--root',str(store)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
n=0
def call(op,**args):
 global n
 n+=1;p.stdin.write(json.dumps({'id':n,'command':op,'args':args})+'\n');p.stdin.flush()
 while True:
  line=p.stdout.readline(); assert line, 'host exited: '+p.stderr.read()
  response=json.loads(line)
  assert response['id']==n,response
  return response
boot=call('app_boot');assert boot.get('ok'),boot
session=boot['value']['session']['id']
unknown=call('not_a_command');assert not unknown['ok'],unknown
snap=call('workbench_snapshot');assert snap['ok'],snap
rename=call('workbench_rename_session',sessionId=session,title='Windows portable synthetic session');assert rename['ok'],rename
organize=call('workbench_organize_session',sessionId=session,pinned=True,archived=False);assert organize['ok'],organize
p.stdin.close();assert p.wait(timeout=15)==0
p=subprocess.Popen([str(host),'--root',str(store)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
opened=call('app_open',id=session);assert opened['ok'],opened
saved=next(x for x in opened['value']['saved'] if x['id']==session)
assert saved['title']=='Windows portable synthetic session',opened
assert saved['pinned'],opened
p.stdin.close();assert p.wait(timeout=15)==0
print(json.dumps({'ok':True,'store':str(store),'session':session,'calls':n,'live_model_dispatches':0}))
