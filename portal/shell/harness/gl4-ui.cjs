// Offline React/client regressions for the owner-approved GL4 brief.
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost'});
for (const name of ['window','document','HTMLElement','Event','MouseEvent','CustomEvent','KeyboardEvent','localStorage']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const eventModule = require.resolve('@tauri-apps/api/event');
require.cache[eventModule] = {id:eventModule,filename:eventModule,loaded:true,exports:{listen:async()=>()=>{}}};
const React = require('react'), {act} = React, {createRoot} = require('react-dom/client');
const {createPortalClient} = require('../src/portal/portal-client.ts');
const {ProviderKeys} = require('../src/portal/SettingsControls.tsx');
const {DEFAULT_APPEARANCE} = require('../src/portal/SettingsView.tsx');
const PortalApp = require('../src/portal/PortalApp.tsx').default;
const {BuilderFinishStrip} = require('../src/portal/SaveToLibrary.tsx');
const {builderReadStore} = require('../src/portal/builder-read-store.ts');
const {WorkspaceFileProvider} = require('../src/portal/WorkspaceFileLinks.tsx');
const {PortalMarkdown} = require('../src/portal/PortalMarkdown.tsx');
const {ReadFaultText,expectProviderRestart,providerRestartMessage,observeRestartLink} = require('../src/portal/provider-restart.tsx');
let root;
const el = React.createElement;
const text = () => document.body.textContent;
async function flush(fn = ()=>{}) { await act(async()=>{await fn(); await new Promise(resolve=>setTimeout(resolve,0));}); }
async function mount(node) { root = createRoot(document.getElementById('root')); await flush(()=>root.render(node)); }
async function unmount() { await flush(()=>root.unmount()); }
const button = label => [...document.querySelectorAll('button')].find(node=>node.textContent===label);
async function click(label) { assert.ok(button(label),label); await flush(()=>button(label).click()); }
const transport = {code:'transport_unavailable',detail:'This PC WSL transport (distro test, lane test): check that the WSL distro is installed and the lane exists. URLError',hint:'normal hint'};
const operations = Object.fromEntries(['secrets.set','settings.status','owner.status','owner.begin','owner.confirm'].map(op=>[op,{implemented:true,available:true,contract_versions:[op==='settings.status'?1:2]}]));
function catalog(enrolls=true) { return {faces:[],backends:[],models:[],capabilities:{operations:{...operations,'secrets.set':{...operations['secrets.set'],storage_only:true,providers:['glm'],provider_display:[{id:'glm',label:'GLM (Z.ai)'}],...(enrolls?{enrolls_backend:true}:{})}}}}; }
const receipt = {provider:'glm',stored:true,fingerprint:'0123456789abcdef',operation_id:'gl4-receipt'};
const block = id => '```bob-builder-approval\n'+JSON.stringify({schema_version:1,conversation_id:'c',approval_id:id})+'\n```';
const passed = id => ({schema_version:1,conversation_id:'c',approval_id:id,kind:'command',summary:'check',detail:{command:'test'},mode:'auto',state:'allowed',expires_at:Date.now()/1000+300,execution_state:'completed',result:{status:'completed',exit_code:0}});
const listing = {project_id:'p',path:'',root_label:'Project',truncated:false,entries:[{name:'index.html',kind:'file',size:1,mtime:1},{name:'escape.html',kind:'symlink',size:1,mtime:1}]};
(async()=>{
  // P1: the real app loads and persists the default and an explicit stored Pro.
  assert.equal(DEFAULT_APPEARANCE.experience,'simple');
  const hello = {identity:{sub:'gl4',gateway:'fixture',faces:['builder'],may_touch:[],may_not_touch:[]},server:{boot:'test',version:'test'},gaps:[]};
  const appInvoke = async cmd => {
    if(cmd==='portal_connection_get') return {connection_configured:true};
    if(cmd==='portal_hello') return hello;
    if(cmd==='portal_catalog') return {faces:[],models:[],backends:[],capabilities:{actions:[],warnings:[]}};
    if(cmd==='portal_conversations') return {items:[],limit:50,offset:0};
    if(cmd==='store_load') return '[]';
    throw JSON.stringify({code:'bad_request',detail:'fixture '+cmd});
  };
  localStorage.clear();
  await mount(el(PortalApp,{invoke:appInvoke,profiles:null,onProfiles:()=>{}}));
  assert.ok(document.querySelector('.portal.simple'));
  assert.equal(JSON.parse(localStorage.getItem('bobportal.appearance')).experience,'simple');
  await unmount();
  localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience:'pro'}));
  await mount(el(PortalApp,{invoke:appInvoke,profiles:null,onProfiles:()=>{}}));
  assert.ok(document.querySelector('.portal')); assert.equal(document.querySelector('.portal.simple'),null);
  assert.equal(JSON.parse(localStorage.getItem('bobportal.appearance')).experience,'pro');
  await unmount(); localStorage.clear();

  // P2: all three messages, row disclosure, first-save snapshot and no resend.
  for (const [saved,enrolls,expected] of [
    [false,true,'Saved. GLM (Z.ai) is turned on.'],
    [true,true,'Saved. The new GLM (Z.ai) key is in use.'],
    [false,false,'Saved.'], [true,false,'Saved.'],
  ]) {
    let stores=0;
    const client=createPortalClient(async(cmd,args)=>{
      if(cmd==='portal_owner_status_v2') return {owner_session:{active:true,expires_at:Date.now()/1000+300,allowed_operations:Object.keys(operations)}};
      if(cmd==='portal_secret_set_v2') { stores++; assert.equal(args.key,'SYNTHETIC-GL4'); return receipt; }
      throw Error(cmd);
    });
    await mount(el(ProviderKeys,{client,catalog:catalog(enrolls),owner:true,scope:'gl4',invoke:async()=>({providers:{glm_key:saved}})}));
    document.querySelector('input').value='SYNTHETIC-GL4'; await click('Store key');
    assert.equal(document.querySelector('.key-row [role="status"]').textContent,expected);
    assert.equal(stores,1); assert.equal(document.querySelector('input').value,'');
    const details=document.querySelector('.key-row details');
    assert.equal(details.open,false); assert.equal(details.querySelector('summary').textContent,'Details');
    assert.match(details.textContent,/0123456789abcdef.*gl4-receipt/);
    await flush(()=>details.querySelector('summary').click()); assert.equal(details.open,true);
    assert.equal(providerRestartMessage(client,transport),enrolls?'Bob is restarting to load GLM (Z.ai)…':null);
    await unmount();
  }
  let stores=0;
  const lostClient=createPortalClient(async cmd=>{
    if(cmd==='portal_owner_status_v2') return {owner_session:{active:true,expires_at:Date.now()/1000+300,allowed_operations:Object.keys(operations)}};
    if(cmd==='portal_secret_set_v2') { stores++; throw JSON.stringify({code:'timeout_no_reply',request_key:'gl4-once'}); }
    if(cmd==='portal_settings_status_v1') return {phase:'ready',operation_id:receipt.operation_id,result:receipt};
    throw Error(cmd);
  });
  const keys=()=>el(ProviderKeys,{client:lostClient,catalog:catalog(),owner:true,scope:'lost'});
  await mount(keys()); document.querySelector('input').value='SYNTHETIC-GL4'; await click('Store key');
  assert.match(text(),/Bob did not confirm the save\. Check whether it was stored/);
  assert.ok(button('Store key').disabled); await unmount();
  await mount(keys()); assert.ok(button('Store key').disabled); await click('Check storage receipt');
  assert.match(text(),/Saved\./); assert.equal(stores,1); await unmount();
  assert.ok(!JSON.stringify({...localStorage}).includes('SYNTHETIC-GL4'));

  // The wording deadline updates an already-mounted read error, without any read.
  const originalNow=Date.now, originalTimeout=global.setTimeout, originalClear=global.clearTimeout;
  let now=originalNow(), deadline;
  Date.now=()=>now;
  global.setTimeout=(fn,ms,...args)=>ms>=100000?(deadline=fn,{gl4:true}):originalTimeout(fn,ms,...args);
  global.clearTimeout=id=>{if(!id?.gl4)originalClear(id);};
  try {
    const client={}; expectProviderRestart(client,'GLM');
    // A healthy read before the delayed disconnect must not retire the window.
    observeRestartLink(client,true);
    await mount(el(ReadFaultText,{client,error:transport,fallback:'normal transport fault'}));
    assert.equal(text(),'Bob is restarting to load GLM…');
    now+=119999; assert.ok(providerRestartMessage(client,transport));
    now++; await flush(()=>deadline()); assert.equal(text(),'normal transport fault');
    await unmount();
    expectProviderRestart(client,'GLM'); observeRestartLink(client,false,transport); observeRestartLink(client,true);
    assert.equal(providerRestartMessage(client,transport),null,'healthy after disconnect ends the window');
    expectProviderRestart(client,'GLM');
    const different={code:'protocol_invalid',detail:'bad frame'};
    assert.equal(providerRestartMessage(client,different),null);
    observeRestartLink(client,false,different); assert.equal(providerRestartMessage(client,transport),null);
    const observed=createPortalClient(async()=>{throw JSON.stringify(transport);});
    expectProviderRestart(observed,'GLM'); await assert.rejects(()=>observed.hello());
    assert.ok(providerRestartMessage(observed,transport));
  } finally {Date.now=originalNow;global.setTimeout=originalTimeout;global.clearTimeout=originalClear;}

  // P3: latest step wins, but pending/missing outcomes never claim done.
  const fail={...passed('bad'),execution_state:'failed',result:{exit_code:1}};
  const cases=[
    [[], 'Job done',null], [[passed('ok')],'Job done',null],
    [[fail,passed('ok')],'Job done','1 earlier check did not pass; the Builder carried on. See the steps above.'],
    [[fail,{...fail,approval_id:'bad2'},passed('ok')],'Job done','2 earlier checks did not pass; the Builder carried on. See the steps above.'],
    [[passed('ok'),fail],'A check or action did not pass',null],
    [[{...passed('wait'),state:'pending',execution_state:'pending',result:null}],'The Builder is waiting for you',null],
    [[{...passed('wait'),state:'pending',result:null},passed('ok')],'The Builder is waiting for you',null],
    ...['stopped','denied','expired'].map(state=>[[{...passed(state),state}], 'Some steps are not confirmed — nothing will be replayed',null]),
    [[{...passed('expired-pending'),state:'pending',execution_state:'pending',result:null,expires_at:1}], 'Some steps are not confirmed — nothing will be replayed',null],
    [[{...passed('unknown'),execution_state:'unknown',result:null}], 'Some steps are not confirmed — nothing will be replayed',null],
  ];
  for (const [records,label,note] of cases) {
    const client={builderModeGet:async()=>({conversation_id:'c',project_id:'p',mode:'auto',revision:1,expires_at:null,network:{allowlist:[],project_extra:[]},approval_records:records})};
    const store=builderReadStore(client,'c','finish'); records.forEach(record=>store.setRecord(record.approval_id,record));
    await mount(el(BuilderFinishStrip,{client,store:{apps:async()=>[]},projectId:'p',conversationId:'c',scope:'finish',content:records.map(record=>block(record.approval_id)).join('\n')}));
    assert.equal(document.querySelector('.builder-finish strong').textContent,label);
    assert.equal(document.querySelector('.builder-finish .hint')?.textContent??null,note);
    await unmount();
  }

  // P4: one bounded listing per view, refresh once after completion, silence/offline.
  let browses=0, failed=false;
  const fileClient={workspaceBrowse:async(project,path)=>{browses++; assert.equal(path,''); if(failed)throw Error('listing deliberately unavailable');return listing;}};
  const files=(props={},answer='Changed index.html. escape.html and index.html.bak stay plain.')=>el(React.StrictMode,null,el(WorkspaceFileProvider,{client:fileClient,projectId:'p',scope:'conversation:c',online:true,completedTurn:'old',...props},el(PortalMarkdown,{text:answer})));
  await mount(files()); assert.equal(browses,1); assert.equal(document.querySelectorAll('.workspace-file-link').length,1);
  await flush(()=>root.render(files({},'Changed index.html again.'))); assert.equal(browses,1,'no per-message listings');
  await flush(()=>root.render(files({completedTurn:''}))); assert.equal(browses,1);
  await flush(()=>root.render(files({completedTurn:'new'}))); assert.equal(browses,2);
  await flush(()=>root.render(files({completedTurn:'new'}))); assert.equal(browses,2);
  await flush(()=>root.render(files({online:false,completedTurn:'offline-turn'}))); assert.equal(browses,2);
  await flush(()=>root.render(files({online:true,completedTurn:'offline-turn'}))); assert.equal(browses,3);
  await unmount();
  browses=0; await mount(files({online:false})); assert.equal(browses,0); assert.equal(document.querySelector('.workspace-file-link'),null); await unmount();
  failed=true; await mount(files()); assert.equal(browses,1); assert.equal(document.querySelector('.workspace-file-link'),null);
  assert.ok(!text().includes('unavailable')); assert.equal(document.querySelector('[role="alert"]'),null);
  await flush(()=>root.render(files())); assert.equal(browses,1,'failed listing is not retried by rerenders'); await unmount();
  failed=false;
  for(const bad of [{...listing,project_id:'other'},{...listing,entries:[{name:'../escape',kind:'file',size:1,mtime:1}]},{...listing,entries:Array.from({length:1001},(_,i)=>({name:'file'+i,kind:'file',size:1,mtime:1}))}]) {
    const badClient={workspaceBrowse:async()=>bad};
    await mount(files({client:badClient})); assert.equal(document.querySelector('.workspace-file-link'),null);
    assert.equal(document.querySelector('[role="alert"]'),null); await unmount();
  }
  let release;
  const lateClient={workspaceBrowse:()=>new Promise(resolve=>{release=resolve;})};
  await mount(files({client:lateClient}));
  await flush(()=>root.render(files({client:lateClient,projectId:null,scope:'other-view'})));
  await flush(()=>release(listing)); assert.equal(document.querySelector('.workspace-file-link'),null); await unmount();
  console.log('GL4 UI: PASS (P1 Simple/default persistence + stored Pro; P2 messages/details/lost-reply/restart cap and recovery; P3 final outcomes and earlier failures; P4 one listing/refresh/links/StrictMode/silent failure/offline)');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(root)await flush(()=>root.unmount());});
