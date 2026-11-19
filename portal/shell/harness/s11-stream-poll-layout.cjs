const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url:'http://localhost', pretendToBeVisual:true });
for (const key of ['window','document','HTMLElement','Event','CustomEvent','localStorage']) global[key]=dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React = require('react'), { act } = React;
const { createRoot } = require('react-dom/client');
const { PortalMarkdown, readableTranscript } = require('../src/portal/PortalMarkdown.tsx');
const { BuilderReadStore } = require('../src/portal/builder-read-store.ts');
const { BuilderModeBar } = require('../src/portal/BuilderModeBar.tsx');
const { BuilderApprovalCard } = require('../src/portal/BuilderApprovalCard.tsx');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const { toPortalError } = require('../src/portal/portal-protocol.ts');
const root=createRoot(document.getElementById('root'));
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
const record=(id,state='pending',conversation_id='c')=>({ schema_version:1, approval_id:id, conversation_id, kind:'command',
  summary:'SERVER check',detail:{command:'python -m unittest'},mode:'approve',expires_at:Date.now()/1000+600,state });
const mode=(records,conversation_id='c')=>({conversation_id,project_id:'p',mode:'approve',expires_at:null,revision:1,
  network:{allowlist:[],project_extra:[]},approval_records:records,approvals:records.filter(r=>r.state==='pending')});
let checks=0;
(async()=>{
  const diff='--- a/app/hello_page.py\n+++ b/app/hello_page.py\n'+Array.from({length:166},(_,n)=>'+line '+n).join('\n');
  const envelope='Working\n```bob-builder-approval\n'+JSON.stringify({schema_version:1,approval_id:'a',detail:{diff}})+'\n```\nReady';
  for(let n=1;n<=envelope.length;n++) {
    const text=readableTranscript(envelope.slice(0,n));
    assert.ok(!text.includes('schema_version') && !text.includes('approval_id') && !text.includes('+line'));
  }
  assert.equal(readableTranscript(envelope),'Working\n\nReady'); checks++;
  assert.equal(readableTranscript('Before\n```bob-work-action\n{"operation":"work.publish"}'),'Before\n'); checks++;
  await act(async()=>root.render(React.createElement(PortalMarkdown,{text:'Done\n```json\n'+JSON.stringify({status:'completed',path:'app/hello_page.py',diff,sha256:'a'.repeat(64)})+'\n```'})));
  assert.ok(document.body.textContent.includes('Wrote app/hello_page.py') && document.body.textContent.includes('+166 lines'), document.body.textContent.slice(0,300));
  assert.ok([...document.querySelectorAll('details')].every(d=>!d.open));
  assert.ok(document.querySelector('details pre').textContent.includes('+line 165')); checks++;
  const error=toPortalError({code:'proposal_stale',detail:{path:'app/calc.py',reason:'source changed'}});
  assert.ok(error.detail.includes('app/calc.py') && !error.detail.includes('[object Object]')); checks++;
  const css=fs.readFileSync(path.join(__dirname,'../src/portal/portal.css'),'utf8');
  const app=fs.readFileSync(path.join(__dirname,'../src/portal/PortalApp.tsx'),'utf8');
  assert.match(css,/\.owner-session \{ display: flex;/);
  assert.ok(!/\.owner-session[^}]*position:\s*fixed/.test(css));
  assert.match(app,/<div className="portal-stage">\s*<OwnerSessionIndicator/);checks++;

  // Counter uses the real store and simulated server records. 40 pending + 80
  // historical cards, duplicated surfaces, 60 seconds and session switching.
  const actualNow=Date.now; let now=actualNow(), hits=0, single=0, fail=false;
  Date.now=()=>now;
  const records=[...Array.from({length:40},(_,n)=>record('p'+n)),...Array.from({length:80},(_,n)=>record('h'+n,'allowed'))];
  const client={builderModeGet:async(conversation)=>{hits++;if(fail)throw JSON.stringify({code:'gateway_http_429',detail:'rate limit exceeded',retry_after:20});return mode(records.map(r=>({...r,conversation_id:conversation})),conversation);},
    builderApprovalGet:async()=>{single++;throw Error('unexpected per-card read');}};
  const store=new BuilderReadStore(client,'c');
  const stops=records.flatMap(r=>[store.subscribe(()=>{},r.approval_id),store.subscribe(()=>{},r.approval_id)]);
  await Promise.resolve();await store.read();
  assert.equal(hits,1);assert.equal(single,0);checks++;
  for(let n=0;n<12;n++){now+=5001;await store.read();}
  assert.equal(hits,5);assert.equal(single,0);checks++;
  fail=true;now+=15001;await store.read();const after429=hits;
  assert.ok(store.error.includes('Reconnecting'));assert.equal(store.records.get('p0').state,'pending');
  for(let n=0;n<10;n++)await store.refresh();
  assert.equal(hits,after429); checks++;
  const switched=new BuilderReadStore(client,'another-session');
  const stopSwitched=switched.subscribe(()=>{},'h0');await Promise.resolve();await switched.read();
  assert.equal(hits,after429,'switching cannot bypass a connection-wide Retry-After');stopSwitched();checks++;
  fail=false;now+=22000;await store.read();assert.equal(store.error,'');checks++;
  for(const r of records){r.state='allowed';r.execution_state='completed';}now+=5001;await store.read();const terminalHits=hits;
  now+=5001;await store.read();assert.equal(hits,terminalHits);assert.equal(single,0);checks++;
  Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
  now+=30001;await store.read();assert.equal(hits,terminalHits);checks++;
  Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
  for(const stop of stops)stop();
  // Returning to a session reuses terminal approval records, never individual reads.
  const stop=store.subscribe(()=>{},'h0');await Promise.resolve();await store.read();stop();
  assert.equal(single,0);assert.ok(hits<20,'request budget stays independent of card count');checks++;
  now+=30001;const resume=switched.subscribe(()=>{},'h0');await Promise.resolve();await switched.read();resume();
  assert.equal(switched.records.get('h0').conversation_id,'another-session');assert.equal(single,0);checks++;
  Date.now=actualNow;

  let uiFail=false;
  const uiClient=createPortalClient(async(command)=>{
    if(command==='portal_builder_mode_get_v2'){if(uiFail)throw JSON.stringify({code:'gateway_http_429',retry_after:0});return mode([record('ui')]);}
    throw Error('Unexpected IPC '+command);
  });
  await act(async()=>{root.render(React.createElement(React.Fragment,null,
    React.createElement(BuilderModeBar,{client:uiClient,catalog:null,conversationId:'c',scope:'s11'}),
    React.createElement(BuilderApprovalCard,{client:uiClient,catalog:null,conversationId:'c',approvalId:'ui',scope:'s11'})));await flush();});
  assert.ok(!document.querySelector('select').disabled);
  const allow=()=>[...document.querySelectorAll('button')].find(b=>b.textContent==='Allow once');
  assert.ok(!allow().disabled);uiFail=true;
  await act(async()=>{[...document.querySelectorAll('button')].find(b=>b.textContent==='Read server state').click();await new Promise(resolve=>setTimeout(resolve,600));});
  assert.ok(!allow().disabled && !document.querySelector('select').disabled);
  assert.ok(document.body.textContent.includes("wasn't applied"));checks++;
  console.log(`S11 stream/poll/layout: ${checks} checks passed (split fences, 166-line result, typed errors, 120 cards, dedup, 429, retention, terminal/hidden/switching, controls)`);
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await act(async()=>root.unmount());dom.window.close();});
