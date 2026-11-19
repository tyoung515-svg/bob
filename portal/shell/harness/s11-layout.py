"""Offline Edge layout check of the real portal CSS and banner/header structure."""
import html
import json
from pathlib import Path
import re
import subprocess
import sys

shell=Path(__file__).resolve().parents[1]
evidence=shell/'evidence/s11-layout'
evidence.mkdir(parents=True,exist_ok=True)
edge=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
css=(shell/'src/portal/portal.css').read_text(encoding='utf-8')
fixture=evidence/'layout.html'
fixture.write_text('''<!doctype html><meta charset="utf-8"><style>body{margin:0}'''+css+'''</style>
<main class="portal"><nav class="portal-rail">BoB</nav><div class="portal-stage">
<aside class="owner-session">Confirmed as owner · 5 min left · <button>Lock</button><button>Log out</button></aside>
<div class="portal-body panels-resizable"><aside class="portal-sidebar">Sessions</aside><section class="portal-main">
<header class="conversation-header"><h1>Builder</h1><div class="row-actions"><button>Rename</button><button>Activity</button><button>Stop turn</button></div></header>
<div class="transcript">Workspace</div><form class="composer">Message</form></section><aside class="portal-panel">Activity</aside></div></div></main>
<script>
const results=[];const body=document.querySelector('.portal-body');
for(const surface of ['chat','workspace'])for(const left of [false,true])for(const right of [false,true]){
 body.className='portal-body '+(surface==='workspace'?'mode-workspace':'panels-resizable')+(left?' left-collapsed':'')+(right?' right-collapsed':'');
 const owner=document.querySelector('.owner-session').getBoundingClientRect();
 const controls=[...document.querySelectorAll('.row-actions button')].map(b=>b.getBoundingClientRect());
 results.push({surface,left,right,width:innerWidth,pass:controls.every(r=>r.top>=owner.bottom&&r.left>=0&&r.right<=innerWidth),ownerBottom:owner.bottom,controlTop:controls[0].top});
}
document.body.dataset.results=JSON.stringify(results);
</script>''',encoding='utf-8',newline='\n')
all_results=[]
for width in (1000,1180,1440):
    command=[str(edge),'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
             '--disable-background-networking','--disable-extensions',
             '--user-data-dir='+str(evidence/('profile-'+str(width))),
             '--window-size='+str(width)+',900','--dump-dom',fixture.as_uri()]
    try:
        response=subprocess.run(command,capture_output=True,timeout=25)
    except (OSError,subprocess.TimeoutExpired) as exc:
        print('Edge layout unavailable: '+str(exc));sys.exit(2)
    text=response.stdout.decode('utf-8',errors='replace')
    match=re.search(r'data-results="([^"]+)"',text)
    if not match:
        print('Edge did not return layout results: '+response.stderr.decode('utf-8',errors='replace')[-600:]);sys.exit(2)
    all_results+=json.loads(html.unescape(match[1]))
(evidence/'results.json').write_text(json.dumps(all_results,indent=2)+'\n',encoding='utf-8',newline='\n')
assert len(all_results)==24 and all(r['pass'] for r in all_results), all_results
print('S11 Edge CSS layout: 24 scenarios passed, widths 1000/1180/1440, chat/Workspace, all panel combinations')
