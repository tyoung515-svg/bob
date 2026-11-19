#!/usr/bin/env python3
"""Sprint-only dispatch-boundary guard. Existing relay files remain read-only."""
import os,datetime,hashlib,importlib.util,json,pathlib,subprocess,sys,uuid
D=pathlib.Path(__file__).resolve().parent
ROUTES={'ds':os.environ.get('BOB_FUNCTIONAL_RELAY'),'models':os.environ.get('BOB_MODEL_RELAY')}
def main():
 if len(sys.argv)!=2 or sys.argv[1] not in ROUTES: raise ValueError('unsupported relay route')
 if not ROUTES[sys.argv[1]]: raise ValueError('Legacy relay unavailable: configure BOB_FUNCTIONAL_RELAY and BOB_MODEL_RELAY')
 data=sys.stdin.buffer.read(80001)
 if len(data)>80000: raise ValueError('request too large; no dispatch')
 request=json.loads(data)
 if not isinstance(request,dict): raise ValueError('invalid request; no dispatch')
 catalog=sys.argv[1]=='models' and request.get('op')=='catalog'
 if catalog:data=json.dumps({'op':'catalog'},separators=(',',':')).encode()
 if (D/'STOP').exists(): raise ValueError('sprint stopped; no dispatch')
 spec=importlib.util.spec_from_file_location('sprint_ledger',D/'call_ledger.py');ledger=importlib.util.module_from_spec(spec);spec.loader.exec_module(ledger)
 ident='win-'+uuid.uuid4().hex
 if not catalog:
  allocation_path=D/'UI-ALLOCATION.json'
  if not allocation_path.is_file():raise ValueError('UI phase allocation missing; no dispatch')
  cap=json.loads(allocation_path.read_text()).get('effective_ui_cap')
  if type(cap) is not int or not 0<=cap<=12:raise ValueError('invalid UI phase allocation; no dispatch')
  ledger.LANES['ui']=('UI-CALLS.json',cap)
 receipts=D/'ui-receipts'/ident;receipts.mkdir(parents=True,exist_ok=False)
 (receipts/'request.json').write_bytes(data)
 if not catalog:ledger.reserve(D,'ui',ident,'Windows native shell / WSL core / existing pinned Windows SSH / '+sys.argv[1])
 started=datetime.datetime.now(datetime.timezone.utc).isoformat()
 try:
  # Check again immediately at the real relay process boundary, after reservation.
  if (D/'STOP').exists() or (not catalog and __import__('time').time()>=ledger.CUTOFF): raise ValueError('stopped after reservation; no relay spawned')
  result=subprocess.run([sys.executable,ROUTES[sys.argv[1]]],input=data,capture_output=True,timeout=130)
  (receipts/'stdout.json').write_bytes(result.stdout);(receipts/'stderr.log').write_bytes(result.stderr)
  try:value=json.loads(result.stdout)
  except Exception:value={}
  ok=result.returncode==0 and value.get('ok') is True
  record={'id':ident,'catalog':catalog,'started_at':started,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'exit_code':result.returncode,'stdout_sha256':hashlib.sha256(result.stdout).hexdigest(),'bytes':len(result.stdout),'ok':ok}
  (receipts/'receipt.json').write_text(json.dumps(record,indent=2))
  if len(result.stdout)>256000:raise ValueError('relay response too large; outcome unknown; no retry')
  if not catalog:ledger.resolve(D,'ui',ident,'achieved' if ok else 'failed',str(receipts/'receipt.json'))
  sys.stdout.buffer.write(result.stdout);return result.returncode
 except Exception as e:
  (receipts/'exception.txt').write_text(str(e))
  if not catalog:ledger.resolve(D,'ui',ident,'unknown',str(receipts/'exception.txt'))
  raise
if __name__=='__main__':
 try:sys.exit(main())
 except Exception as e:
  print(json.dumps({'ok':False,'error':str(e),'no_automatic_retry':True}));sys.exit(2)
