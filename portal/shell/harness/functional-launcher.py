from pathlib import Path
import subprocess,os
repo=Path(__file__).resolve().parents[2]
p=repo/'scripts/linux/run-functional.sh'
assert p.is_file(),'functional main launcher missing'
r=subprocess.run(['bash',str(p),'--check'],capture_output=True,text=True);assert r.returncode==0,r.stderr
assert 'trusted SSH' in r.stdout
r=subprocess.run(['bash',str(p),'--no-build'],env={k:v for k,v in os.environ.items() if k not in ['DISPLAY','WAYLAND_DISPLAY']},capture_output=True,text=True)
assert r.returncode==6 and 'NO_DISPLAY' in r.stderr,r.stderr
print('functional launcher: 3 checks PASS')
