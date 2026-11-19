'use strict';
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
for (const name of ['window','document','HTMLElement','Event','MouseEvent','localStorage']) global[name]=dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const {SaveToLibrary,LibraryResult}=require('../src/portal/SaveToLibrary.tsx');
const {PcCard,PcAccess,PcModeRow}=require('../src/portal/PcAccess.tsx');
const {SelfRepair}=require('../src/portal/SelfRepair.tsx');
const {BuilderModeBar}=require('../src/portal/BuilderModeBar.tsx');
let root;
async function flush(fn){await act(async()=>{if(fn)await fn();await Promise.resolve();});}
async function mount(component,props){root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(component,props)));}
async function unmount(){await flush(()=>root.unmount());}
async function click(text){const button=[...document.querySelectorAll('button')].find(b=>b.textContent===text);assert.ok(button,text);await flush(()=>button.dispatchEvent(new MouseEvent('click',{bubbles:true})));}
(async()=>{
  let apps=[],saved=0,latest='index.html',previews=[];
  const store={apps:async()=>apps,saveApps:async next=>{apps=next;saved++;}};
  const client={builderProjectResult:async project=>({project_id:project,conversation_id:'conversation',path:latest}),projects:async()=>({items:[{id:'project',name:'My app'}]}),
    builderPreview:async(conversation,path)=>{previews.push({conversation,path});return {path,mime:'text/html',size:Buffer.byteLength('<h1>latest</h1>'),truncated:false,bytes_b64:Buffer.from('<h1>latest</h1>').toString('base64')};}};
  await mount(SaveToLibrary,{client,store,projectId:'project'});assert.equal(saved,0,'rendering and agent proposals cannot save');
  await click('Save to Library');assert.equal(saved,1);assert.equal(apps.length,1);assert.equal(apps[0].project_id,'project');assert.equal(apps[0].status,'ready');assert.equal(apps[0].launch,'open_result');assert.equal(apps[0].command,'');assert.equal(apps[0].folder,'');
  assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='Saved to Library'&&b.disabled));await unmount();
  apps[0].status='retired';await mount(SaveToLibrary,{client,store,projectId:'project'});await click('Save to Library');assert.equal(apps.length,1,'same project updates its entry');await unmount();
  latest='app/latest.html';await mount(LibraryResult,{client,projectId:'project'});await click('Open result');
  assert.equal(previews.at(-1).path,latest,'Open result resolves the latest successful project HTML');
  const iframe=document.querySelector('iframe');assert.ok(iframe);assert.equal(iframe.getAttribute('sandbox'),'');assert.match(iframe.getAttribute('srcdoc'),/latest/);await unmount();
  const yoloClient={...client,builderModeGet:async()=>({conversation_id:'conversation',project_id:'project',result_path:latest,mode:'yolo',revision:1,
    expires_at:Date.now()/1000+600,session_until:Date.now()/1000+600,network:{allowlist:[],project_extra:[]},approvals:[],approval_records:[]})};
  await mount(BuilderModeBar,{client:yoloClient,catalog:null,conversationId:'conversation',scope:'c7-yolo-library',libraryStore:store});
  assert.ok([...document.querySelectorAll('button')].some(b=>b.textContent==='Saved to Library'),'a successful YOLO result keeps its Library status without an approval card');
  assert.equal(apps[0].status,'ready');await unmount();

  const calls=[];const invoke=async(command,args)=>{calls.push({command,args});return {};};
  const card={request:{id:'request',task_id:'task',agent:'assistant',conversation_id:'conversation',operation:'pc.write',args:{path:'C:\\task\\hello.txt',content:'hello'},expires_at:Date.now()/1000+600},mode:'approve',root:'C:\\task',until:null,protected_reason:null,running:false};
  await mount(PcCard,{card:{...card,root:null},invoke,refresh:async()=>{}});
  assert.equal(document.querySelector('input[aria-label="PC task folder"]').value,'C:\\task','a new request suggests its parent folder');await unmount();
  await mount(PcCard,{card:{...card,root:'\\\\?\\C:\\task'},invoke,refresh:async()=>{}});
  assert.equal(document.querySelector('input[aria-label="PC task folder"]').value,'C:\\task','a native canonical scope round-trips as an ordinary drive path');
  await click('Set PC mode for this agent and task');assert.equal(calls.at(-1).args.taskFolder,'C:\\task');await unmount();calls.length=0;
  await mount(PcCard,{card,invoke,refresh:async()=>{}});assert.equal(calls.length,0,'a queued proposal cannot approve itself');
  const select=document.querySelector('select');await flush(()=>{select.value='yolo';select.dispatchEvent(new Event('change',{bubbles:true}));});
  await click('Set PC mode for this agent and task');assert.equal(calls.length,0,'PC yolo requires its separate confirmation');
  assert.match(document.body.textContent,/anything your Windows account can/);const confirm=[...document.querySelectorAll('button')].find(b=>b.textContent==='Enable PC yolo for this agent and task');assert.ok(confirm.disabled);
  await flush(()=>document.querySelector('input[type=checkbox]').click());await click('Enable PC yolo for this agent and task');
  assert.equal(calls[0].command,'pc_set_mode');assert.equal(calls[0].args.yoloAcknowledged,true);assert.equal(calls[0].args.requestId,'request');await unmount();
  await mount(PcCard,{card:{...card,mode:'yolo',protected_reason:'This SSH key needs a fresh native approval.'},invoke,refresh:async()=>{}});
  assert.match(document.body.textContent,/Always ask/);const before=calls.length;await click('Allow once on this PC');assert.equal(calls.length,before+1);assert.equal(calls.at(-1).command,'pc_decide');await unmount();
  const idleCalls=[];const idleInvoke=async(command,args)=>{idleCalls.push({command,args});return command==='pc_poll'?{requests:[],outcomes:{one:{status:'done',agent:'assistant'}}}:{revoked:true};};
  await mount(PcModeRow,{invoke:idleInvoke,queue:{requests:[],outcomes:{}},refresh:async()=>{},conversationId:'conversation'});await click('Revoke PC access in this conversation');
  assert.ok(idleCalls.some(c=>c.command==='pc_revoke'&&c.args.conversationId==='conversation'),'an idle task grant stays revocable without another agent request');await unmount();

  const repairCalls=[];
  const repairInvoke=async(command,args)=>{repairCalls.push({command,args});
    if(command==='runtime_repair_status')return {repairable:true,observation:{state:'wedged',observed:'A new WSL session failed.'}};
    if(command==='runtime_repair_start')return {repairable:true,observation:{state:'degraded',observed:'Still unhealthy'},needs:args.shutdownConfirmed?'restore_confirmation':'shutdown_confirmation',backup:{id:'backup-1',time:1,path:'C:\\backup.tar'},diagnostics:'C:\\diagnostics.json'};
    if(command==='runtime_repair_restore')return {verification:{state:'healthy',observed:'Restored'}};
    throw Error(command);
  };
  await mount(SelfRepair,{invoke:repairInvoke});assert.match(document.body.textContent,/A new WSL session failed/);await click('Repair');
  assert.equal(repairCalls.filter(c=>c.command==='runtime_repair_start').length,1);await click('Review WSL shutdown');assert.match(document.body.textContent,/Docker and Ubuntu/);
  await click('Stop all WSL and continue');assert.equal(repairCalls.at(-1).args.shutdownConfirmed,true);
  await click('Review backup restore');assert.match(document.body.textContent,/backup-1/);assert.match(document.body.textContent,/will be lost/);
  assert.equal(repairCalls.filter(c=>c.command==='runtime_repair_restore').length,0);await click('Restore this named backup and discard later changes');assert.equal(repairCalls.at(-1).args.backupId,'backup-1');await unmount();
  console.log('C7 native UI: Library link/latest sandbox preview, native PC yolo/protected reviews, repair shutdown and named restore confirmations passed');
})().catch(async error=>{console.error(error);if(root)await flush(()=>root.unmount());process.exitCode=1;});
