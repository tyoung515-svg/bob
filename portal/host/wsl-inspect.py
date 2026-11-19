import os
import hashlib,json,pathlib,sys
package=pathlib.Path(os.environ.get('BOB_VALIDATION_ROOT', pathlib.Path(__file__).resolve().parents[1]))
root='/root/bob-windows-ui-review-20261024/validation-v2/data'
if sys.argv[1]=='processes':
 found=[]
 for entry in pathlib.Path('/proc').iterdir():
  if not entry.name.isdigit():continue
  try:args=(entry/'cmdline').read_bytes().split(b'\0')
  except OSError:continue
  if args and args[0]==str(package/'release/portable/bob-wsl-host').encode() and root.encode() in args:found.append({'pid':int(entry.name),'argv':[a.decode() for a in args if a]})
 print(json.dumps(found))
elif sys.argv[1]=='test-hashes':
 expected=json.loads((package/'sprint-evidence/wsl-security-test-manifest.json').read_text());rows=[]
 for item in expected:
  path=package/'sprint-evidence/wsl-test-bins'/(item['suite']+'.test');actual=hashlib.sha256(path.read_bytes()).hexdigest();assert actual==item['sha256'];rows.append({'suite':item['suite'],'sha256':actual,'matched':True})
 assert len(rows)==9;print(json.dumps({'ok':True,'verified_count':len(rows),'rows':rows}))
else:raise SystemExit('unsupported read-only inspection')
