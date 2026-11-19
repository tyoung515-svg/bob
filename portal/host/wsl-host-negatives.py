import hashlib,json,os,pathlib,subprocess,tempfile
package=pathlib.Path(os.environ.get('BOB_VALIDATION_ROOT', pathlib.Path(__file__).resolve().parents[1]));base=pathlib.Path('/root/bob-windows-ui-review-20261024/test-data');base.mkdir(parents=True,exist_ok=True)
protected=package/'sprint-evidence/ntfs-project/protected.txt';before=hashlib.sha256(protected.read_bytes()).hexdigest()
root=pathlib.Path(tempfile.mkdtemp(prefix='host-negatives-',dir=base))/'store'
p=subprocess.Popen([str(package/'release/portable/bob-wsl-host'),'--root',str(root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
def call(n,command,args):
 p.stdin.write(json.dumps({'id':n,'command':command,'args':args})+'\n');p.stdin.flush();reply=json.loads(p.stdout.readline());assert reply['id']==n;return reply
assert call(1,'app_boot',{})['ok'];rows=[]
paths=[str(package/'sprint-evidence/ntfs-project'),str(package/'sprint-evidence/ntfs-junction'),'/mnt/c',subprocess.check_output(['wslpath', '-w', str(package/'sprint-evidence/ntfs-project')], text=True).strip()]
for n,path in enumerate(paths,2):
 response=call(n,'app_attach',{'path':path});assert not response['ok'],response;rows.append({'path':path,'refused':True,'error':response['error']})
final=call(6,'app_boot',{});assert final['ok'] and final['value']['session']['project'] is None
p.stdin.close();assert p.wait(timeout=5)==0
actual=hashlib.sha256(protected.read_bytes()).hexdigest();assert actual==before
print(json.dumps({'ok':True,'tier':'actual WSL current-host boundary, no provider','live_model_dispatches':0,'store':str(root),'rows':rows,'protected_before_sha256':before,'protected_after_sha256':actual,'no_grant':True},indent=2))
