#!/usr/bin/env python3
"""Server-side stdio bridge. No listener; no tools, code teams, fallback or secrets returned.
Install a reviewed copy in a private project directory on the credential host.
Dispatch journals coordinate idempotency only; Bob owns execution/history/receipts.
"""
import hashlib,json,os,re,sys,urllib.error,urllib.request
from pathlib import Path
class RelayError(Exception):pass
def valid_id(value):return isinstance(value,str) and re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',value) is not None
def private_write(path,value,exclusive=False):
 flags=os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW|(os.O_EXCL if exclusive else os.O_TRUNC)
 fd=os.open(path,flags,0o600)
 with os.fdopen(fd,'w') as f:json.dump(value,f);f.flush();os.fsync(f.fileno())
 fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
def handle(request,root,http):
 if not isinstance(request,dict):raise RelayError('invalid_request')
 op=request.get('op')
 if op in ('get','cancel'):
  if set(request)!={'op','run_id'} or not valid_id(request['run_id']):raise RelayError('invalid_run_id_or_fields')
  return http('GET' if op=='get' else 'POST','/agent-services/runs/'+request['run_id']+('/cancel' if op=='cancel' else ''),None if op=='get' else {})
 if op!='start' or set(request)!={'op','request_id','task'} or not valid_id(request.get('request_id')):raise RelayError('invalid_start')
 task=request['task']
 if not isinstance(task,str) or not task.strip() or len(task.encode())>16000:raise RelayError('invalid_task')
 root.mkdir(mode=0o700,parents=True,exist_ok=True)
 if root.is_symlink() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:raise RelayError('unsafe_dispatch_directory')
 path=root/(request['request_id']+'.json');digest=hashlib.sha256(task.encode()).hexdigest()
 try:private_write(path,{'task_digest':digest,'state':'unknown_dispatch'},exclusive=True)
 except FileExistsError:
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
  with os.fdopen(fd) as f:old=json.load(f)
  if old.get('task_digest')!=digest:raise RelayError('request_id_conflict')
  if not valid_id(old.get('run_id')):raise RelayError('unknown_dispatch_no_automatic_retry')
  return http('GET','/agent-services/runs/'+old['run_id'])
 try:result=http('POST','/agent-services/runs',{'team':'balance-budget','task':task,'worker':'bob-ds-flash','max_model_calls':4,'max_seconds':180,'require_bulk':False})
 except Exception:raise RelayError('unknown_dispatch_no_automatic_retry') from None
 if not valid_id(result.get('id')):raise RelayError('unknown_dispatch_invalid_receipt')
 tmp=path.with_suffix('.accepted')
 private_write(tmp,{'task_digest':digest,'state':'attached','run_id':result['id']},exclusive=True);os.replace(tmp,path)
 fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
 return result
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args):return None
def http(method,path,payload=None):
 # Existing credential stays on this server. No environment/key import to PC.
 token=(Path.home()/'.hermes/.bob-agent-token').read_text().strip()
 data=None if payload is None else json.dumps(payload).encode()
 request=urllib.request.Request('http://127.0.0.1:7836'+path,data=data,method=method,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
 try:
  with opener.open(request,timeout=20) as response:raw=response.read(1048577)
 except urllib.error.HTTPError as e:raise RelayError('bob_http_'+str(e.code)) from None
 except (OSError,TimeoutError):raise RelayError('bob_transport_unavailable') from None
 if len(raw)>1048576:raise RelayError('bob_response_oversize')
 try:result=json.loads(raw)
 except ValueError:raise RelayError('bob_invalid_json') from None
 if not isinstance(result,dict):raise RelayError('bob_invalid_response')
 return result
def main():
 try:
  raw=sys.stdin.buffer.read(20001)
  if len(raw)>20000:raise RelayError('request_oversize')
  result=handle(json.loads(raw),Path(__file__).resolve().parent/'runtime-dispatch',http)
  print(json.dumps({'ok':True,'run':result},ensure_ascii=False));return 0
 except RelayError as e:print(json.dumps({'ok':False,'error':str(e)}));return 1
 except Exception:print(json.dumps({'ok':False,'error':'runtime_bridge_unavailable'}));return 1
if __name__=='__main__':sys.exit(main())
