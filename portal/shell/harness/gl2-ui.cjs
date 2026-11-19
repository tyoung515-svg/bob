const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost'});
for (const k of ['window','document','HTMLElement','Event','CustomEvent','localStorage','navigator']) global[k] = dom.window[k];
global.IS_REACT_ACT_ENVIRONMENT = true;
const eventModule = require.resolve('@tauri-apps/api/event');
require.cache[eventModule] = {id:eventModule, filename:eventModule, loaded:true, exports:{listen:async()=>()=>{}}};
const React = require('react'), {act} = React, {createRoot} = require('react-dom/client');
const {ExperienceContext} = require('../src/portal/experience.ts');
const {PortalMarkdown} = require('../src/portal/PortalMarkdown.tsx');
const {BuilderPreview,hasPreviewScripts,STATIC_IN_APP_PREVIEW_CSP} = require('../src/portal/BuilderPreview.tsx');
const {BuilderApprovalCard,BuilderApprovalCards} = require('../src/portal/BuilderApprovalCard.tsx');
const {BuilderFinishStrip} = require('../src/portal/SaveToLibrary.tsx');
const {WorkspaceFileProvider,WorkspaceFilePreview} = require('../src/portal/WorkspaceFileLinks.tsx');
const {WorkspaceBrowser} = require('../src/portal/WorkspaceBrowser.tsx');
const {ModelSetup} = require('../src/portal/ModelSetup.tsx');
const {ProviderKeys} = require('../src/portal/SettingsControls.tsx');
const providers = require('./model-provider-fixture.cjs');
const root = createRoot(document.getElementById('root'));
const flush = () => new Promise(r=>setTimeout(r,0));
let key=0;
async function render(node,experience='simple') { await act(async()=>{root.render(React.createElement(ExperienceContext.Provider,{value:experience,key:++key},node));await flush();}); }
const el=(C,p)=>React.createElement(C,p);
const button=text=>[...document.querySelectorAll('button')].find(b=>b.textContent===text);
async function click(text) { assert.ok(button(text),text); await act(async()=>{button(text).click();await flush();}); }
const block=id=>'```bob-builder-approval\n'+JSON.stringify({schema_version:1,approval_id:id,conversation_id:'c'})+'\n```';
const base={schema_version:1,approval_id:'a',conversation_id:'c',kind:'command',summary:'command',detail:{command:'python -m pytest',cwd:'.'},display:{title:'Run: python -m pytest',lines:['Working directory: .','Network: bounded'],scope:'Only this command is covered.'},state:'allowed',mode:'auto',expires_at:Date.now()/1000+300,execution_state:'completed',result:{status:'completed',exit_code:0}};
const records=[base,{...base,approval_id:'b',kind:'write',detail:{path:'index.html'}},{...base,approval_id:'d',execution_state:'failed',result:{exit_code:1}}];
const mode={conversation_id:'c',project_id:'p',result_path:'index.html',mode:'auto',revision:1,expires_at:null,network:{allowlist:[],project_extra:[]},approval_records:records};
let browses=0,previews=0,apps=[];
const page='<button onclick="this.textContent=\'clicked\'">Try</button><script>document.body.dataset.ran=1</script>';
const client={builderModeGet:async()=>mode,builderApprovalGet:async id=>records.find(r=>r.approval_id===id),builderPreview:async(c,path)=>{previews++;return {path,mime:'text/html',size:Buffer.byteLength(page),bytes_b64:Buffer.from(page).toString('base64'),truncated:false};},
  workspaceBrowse:async()=>{browses++;return {project_id:'p',root_label:'Project',path:'',entries:[{name:'index.html',kind:'file',size:1,mtime:1},{name:'other.js',kind:'file',size:1,mtime:1},{name:'escape.html',kind:'symlink',size:1,mtime:1}],truncated:false};},
  builderProjectResult:async()=>({project_id:'p',conversation_id:'c',path:'index.html'}),projects:async()=>({items:[{id:'p',name:'Demo'}]})};
const store={apps:async()=>apps,saveApps:async rows=>{apps=rows;}};
const cardProps={client,catalog:null,conversationId:'c',scope:'gl2',refreshKey:'ready'};
(async()=>{
  // A2: parse HTML, not script-like text in comments; static sandbox stays empty.
  for(const html of ['<SCRIPT>1</SCRIPT>','<button ONCLICK="run()">Go</button>','<svg onload="run()"></svg>','<template><script>1</script></template>']) assert.equal(hasPreviewScripts(html),true,html);
  for(const html of ['<p>Static</p>','<!-- <script>not code</script> -->','<p data-onclick="plain">Text</p>']) assert.equal(hasPreviewScripts(html),false,html);
  await render(el(BuilderPreview,{client,conversationId:'c',initialPath:'index.html',autoLoad:true}));
  assert.match(document.querySelector('[role="note"]').textContent,/Open on this PC/);
  assert.equal(document.querySelector('iframe').getAttribute('sandbox'),'');
  assert.ok(STATIC_IN_APP_PREVIEW_CSP.includes("script-src 'none'"));
  assert.ok(!document.querySelector('iframe').getAttribute('srcdoc').includes('<script>'));
  const staticClient={...client,builderPreview:async()=>({path:'index.html',mime:'text/html',size:9,bytes_b64:Buffer.from('<p>Hi</p>').toString('base64'),truncated:false})};
  await render(el(BuilderPreview,{client:staticClient,conversationId:'c',initialPath:'index.html',autoLoad:true}));
  assert.equal(document.querySelector('[role="note"]'),null);
  // A4: decided cards are compact and contain zero decision or save controls.
  await render(el(BuilderApprovalCard,{...cardProps,approvalId:'a'}));
  assert.equal(document.querySelector('.builder-step-line > span').textContent,'Ran a check · passed');
  assert.equal(document.querySelector('.builder-step-line > details').open,false);
  assert.equal(button('Allow once'),undefined);assert.equal(button('Deny'),undefined);assert.equal(button('Save to Library'),undefined);
  assert.ok(button('Read server state').closest('details'));
  await render(el(BuilderApprovalCards,{...cardProps,content:records.map(r=>block(r.approval_id)).join('\n')}));
  assert.equal(document.querySelector('.builder-steps > summary').textContent,'3 steps · 1 did not pass');
  assert.equal(document.querySelectorAll('.builder-step').length,3);
  // Pending Pro retains expanded command, safety text and all decision buttons.
  const pending={...base,state:'pending',execution_state:undefined,result:undefined};
  const pendingClient={builderModeGet:async()=>({...mode,approval_records:[pending]}),builderApprovalGet:async()=>pending};
  await render(el(BuilderApprovalCard,{...cardProps,client:pendingClient,approvalId:'a'}),'pro');
  assert.ok(button('Allow once')&&!button('Allow once').disabled);assert.ok(button('Allow for session'));assert.ok(button('Deny'));
  assert.equal([...document.querySelectorAll('summary')].find(s=>s.textContent==='Show command'),undefined);
  assert.ok([...document.querySelectorAll('p')].find(p=>p.textContent==='Run: python -m pytest'&&!p.closest('details')));
  await render(el(BuilderApprovalCard,{...cardProps,client:pendingClient,approvalId:'a'}));
  assert.equal([...document.querySelectorAll('summary')].find(s=>s.textContent==='Show command').parentElement.open,false);
  assert.ok([...document.querySelectorAll('p')].find(p=>p.textContent==='Only this command is covered.'&&!p.closest('details')));
  // One finish strip has the state, Open result, Save in that order; saves stay user clicks.
  await render(el(BuilderFinishStrip,{...cardProps,store,projectId:'p',content:records.map(r=>block(r.approval_id)).join('\n')}));
  assert.equal(document.querySelectorAll('.builder-finish').length,1);
  assert.match(document.querySelector('.builder-finish strong').textContent,/did not pass/);
  assert.deepEqual([...document.querySelectorAll('.builder-finish button')].map(b=>b.textContent),['Open result','Save to Library']);
  assert.equal(apps.length,0);await click('Save to Library');assert.ok(button('Saved to Library').disabled);assert.equal(apps.length,1);
  // A5: same message, same original copy payload, Simple folded / Pro unchanged.
  const answer='Changed `index.html`. missing.html and escape.html are not linked.\n\n```css\na { color: red; }\nb { color: blue; }\n```';
  await render(el(PortalMarkdown,{text:answer}));
  assert.equal(document.querySelector('.md-code-fold').open,false);assert.match(document.querySelector('.md-code-fold summary').textContent,/Code · css · 2 lines · Show/);
  await render(el(PortalMarkdown,{text:answer}),'pro');assert.equal(document.querySelector('.md-code-fold'),null);assert.ok(document.querySelector('.md-code pre'));
  await render(el(WorkspaceFileProvider,{projectId:'p',children:React.createElement(React.Fragment,null,el(WorkspaceBrowser,{client,projectId:'p'}),el(PortalMarkdown,{text:answer}),el(WorkspaceFilePreview,{client,conversationId:'c'}))}));
  assert.equal(document.querySelectorAll('.workspace-file-link').length,1);assert.equal(document.querySelector('.workspace-file-link').textContent,'index.html');
  const reads=browses,opens=previews;await act(async()=>{document.querySelector('.workspace-file-link').click();await flush();});
  assert.equal(browses,reads,'linking must never probe');assert.equal(previews,opens+1);assert.ok(document.querySelector('iframe'));
  // A1: server labels + conditional copy, shared status re-read after either save route.
  const operations=Object.fromEntries(['secrets.set','settings.status','owner.status','owner.begin','owner.confirm'].map(op=>[op,{contract_versions:[op==='settings.status'?1:2],implemented:true,available:true}]));
  const cap={storage_only:true,enrolls_backend:true,providers:['glm','kimi','qwen'],provider_display:[{id:'glm',label:'GLM (Z.ai)'},{id:'kimi',label:'Kimi (Moonshot)'},{id:'qwen',label:'Qwen (Alibaba Model Studio)'}]};
  const catalog={backends:[],models:[],faces:[],capabilities:{operations:{...operations,'secrets.set':{...operations['secrets.set'],...cap}}}};
  let saved={},statusReads=0,stores=0,changes=0;
  const invoke=async(cmd,args)=>{if(cmd==='runtime_model_providers')return providers;if(cmd==='runtime_model_status'){statusReads++;return {lane:'lane',providers:{...saved},default_backend:null,ready:!!Object.keys(saved).length};}if(cmd==='runtime_model_set_key'){stores++;saved[args.provider+'_key']=true;return {ok:true};}throw Error(cmd);};
  const keyClient={modelsChanged:async()=>{window.dispatchEvent(new Event('bob-model-changed'));},ownerStatus:async()=>({owner_session:{active:true,expires_at:Date.now()/1000+300,allowed_operations:Object.keys(operations)}}),secretSet:async(provider,key)=>{assert.equal(key,'SYNTHETIC-GL2');stores++;saved[provider+'_key']=true;return {provider,stored:true,fingerprint:'0123456789abcdef',operation_id:'op-1'};}};
  const changed=()=>changes++;window.addEventListener('bob-model-changed',changed);
  const cards=()=>React.createElement(React.Fragment,null,el(ProviderKeys,{client:keyClient,catalog,owner:true,scope:'gl2',invoke}),el(ModelSetup,{invoke,catalog}));
  await render(cards());assert.match(document.body.textContent,/Saving a key turns that provider’s model on/);assert.ok(document.querySelector('.key-row strong').textContent==='GLM (Z.ai)');
  const input=document.querySelector('[aria-label="New key for glm"]');input.value='SYNTHETIC-GL2';
  const before=statusReads;await act(async()=>{input.closest('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();});
  assert.equal(input.value,'');assert.equal(stores,1);assert.ok(statusReads>=before+1);/* AU9: one shared, deduplicated status read may serve both cards */assert.match(document.querySelector('.model-setup').textContent,/GLM \(Z.ai\): Saved/);assert.match(document.querySelector('.key-row').textContent,/Saved/);
  await click('Use an API key');
  const select=document.querySelector('#model-provider');select.value='kimi';await act(async()=>{select.dispatchEvent(new Event('change',{bubbles:true}));await flush();});
  document.querySelector('#model-key').value='SYNTHETIC-GL2';await act(async()=>{document.querySelector('#model-key').closest('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();});
  assert.equal(stores,2);assert.match(document.querySelector('.model-setup').textContent,/Kimi \(Moonshot\): Saved/);assert.match(document.querySelectorAll('.key-row')[1].textContent,/Saved/);assert.equal(changes,2);
  saved.qwen_key=true;await act(async()=>{window.dispatchEvent(new Event('bob-model-changed'));await flush();});
  assert.match(document.querySelector('.model-setup').textContent,/Qwen \(Alibaba Model Studio\): Saved/);assert.match(document.querySelectorAll('.key-row')[2].textContent,/Saved/);
  window.removeEventListener('bob-model-changed',changed);
  const oldCatalog=structuredClone(catalog);delete oldCatalog.capabilities.operations['secrets.set'].enrolls_backend;delete oldCatalog.capabilities.operations['secrets.set'].provider_display;
  await render(el(ProviderKeys,{client:keyClient,catalog:oldCatalog,owner:true,scope:'old'}));assert.match(document.body.textContent,/Storage does not enroll/);assert.equal(document.querySelector('.key-row strong').textContent,'glm');
  console.log('GL2 UI: PASS (A1 labels, both save routes/status refresh, capability copy; A2 notices/static sandbox; A4 compact/grouped/pending Pro/finish/save; A5 folded code and confirmed file preview without probes)');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await act(async()=>root.unmount());});
