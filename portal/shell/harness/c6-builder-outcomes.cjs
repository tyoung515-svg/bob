const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
for(const key of ['window','document','HTMLElement','Event','CustomEvent','localStorage'])global[key]=dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const {BuilderApprovalCard,builderOutcome}=require('../src/portal/BuilderApprovalCard.tsx');
const {BuilderReadStore}=require('../src/portal/builder-read-store.ts');
const {CheckReceipts}=require('../src/portal/WorkActionCard.tsx');
const {BuilderModeBar}=require('../src/portal/BuilderModeBar.tsx');
const root=createRoot(document.getElementById('root'));
const base={schema_version:1,approval_id:'a',conversation_id:'c',kind:'write',summary:'fs_write',
  detail:{path:'app/index.html'},display:{title:'Write app/index.html',lines:['Workspace: this workspace'],scope:'Writes, edits and bounded checks for this task.'},
  mode:'approve',state:'pending',expires_at:Date.now()/1000+600,session_until:Date.now()/1000+7200};
let record=structuredClone(base);
const mode=()=>({conversation_id:'c',project_id:'p',mode:'approve',revision:1,expires_at:null,network:{allowlist:[],project_extra:[]},approval_records:[structuredClone(record)]});
const client={builderModeGet:async()=>mode(),builderApprovalGet:async()=>structuredClone(record)};
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
(async()=>{
  for(const [changes,label] of [
    [{},'Waiting for you'],[{state:'denied'},'Denied'],[{state:'expired'},'Expired'],
    [{state:'stopped',execution_state:'stopped'},'Stopped'],
    [{state:'allowed',execution_state:'unknown'},'Outcome unknown'],
    [{state:'allowed',execution_state:'completed',result:{exit_code:0}},'Done'],
    [{state:'allowed',execution_state:'failed',result:{exit_code:1,error:{code:'permission_denied'}}},'Write failed: app/index.html'],
    [{state:'allowed',execution_state:'completed',result:{exit_code:1,error:{code:'not_found'}}},'Write failed'],
  ])assert.ok(builderOutcome({...base,...changes}).startsWith(label),label);
  record={...base,state:'allowed',execution_state:'running'};
  const store=new BuilderReadStore(client,'c');
  const unsubscribe=store.subscribe(()=>{},'a');await store.read();
  assert.equal(store.records.get('a').execution_state,'running');
  record={...record,execution_state:'failed',result:{status:'failed',exit_code:1,error:{code:'permission_denied'},diagnostics:{stage:'temp-open',errno:13,full:'x'.repeat(400)+'DIAGNOSTICS-END'}}};
  await store.refresh();assert.equal(store.records.get('a').execution_state,'failed');unsubscribe();
  await act(async()=>{root.render(React.createElement(BuilderApprovalCard,{client,catalog:null,conversationId:'c',approvalId:'a',scope:'c6'}));await flush();});
  assert.ok(document.body.textContent.includes('Write failed: app/index.html'));
  assert.ok(document.body.textContent.includes('Authorization: allowed'));
  assert.ok(document.body.textContent.includes('ask it to continue'));
  assert.ok(document.querySelector('details').textContent.includes('DIAGNOSTICS-END'));
  assert.ok(!document.querySelector('details').open);
  const checks=[{check_id:'python.unittest',passed:true,exit_status:0,snapshot_digest:'a'.repeat(64),stdout:'x'.repeat(400)+'CHECK-END'}];
  await act(async()=>root.render(React.createElement(CheckReceipts,{rows:checks})));
  const cells=[...document.querySelectorAll('tbody td')].map(e=>e.textContent);
  assert.deepEqual(cells,['python.unittest','Passed','0','a'.repeat(12)]);
  assert.ok(document.querySelector('pre').textContent.includes('CHECK-END'));
  const page='<h1>C6 result</h1>';
  const previewClient={...client,builderModeGet:async()=>({...mode(),result_path:'app/index.html'}),
    workspaceBrowse:async()=>({project_id:'p',root_label:'C6 app',path:'',entries:[],truncated:false}),
    builderPreview:async(c,path)=>({path,mime:'text/html',size:page.length,truncated:false,bytes_b64:Buffer.from(page).toString('base64')})};
  await act(async()=>{root.render(React.createElement(BuilderModeBar,{client:previewClient,catalog:null,conversationId:'c',scope:'preview'}));await flush();});
  const open=[...document.querySelectorAll('button')].find(button=>button.textContent==='Open result');assert.ok(open);
  await act(async()=>{open.click();await flush();});
  assert.ok(document.querySelector('iframe[title="Workspace HTML preview"]').getAttribute('srcdoc').includes('C6 result'));
  console.log('C6 UI passed: all outcomes, legacy failed receipts, allowed/running polling to failure, plain task expiry, complete diagnostics and readable revision-bound checks.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await act(async()=>root.unmount());});
