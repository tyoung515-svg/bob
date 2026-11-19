// AU9: offline restart recovery through the real client and React screens.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost'});
for (const k of ['window','document','HTMLElement','Event','CustomEvent','localStorage']) global[k] = dom.window[k];
Object.defineProperty(global,'navigator',{configurable:true,value:{userAgent:'node'}});
global.IS_REACT_ACT_ENVIRONMENT = true;
const listeners = new Set(), events = require.resolve('@tauri-apps/api/event');
require.cache[events] = {id:events,filename:events,loaded:true,exports:{listen:async(topic,fn)=>{listeners.add({topic,fn});return()=>{for(const l of listeners)if(l.fn===fn)listeners.delete(l);};}}};
const React = require('react'), {act} = React, {createRoot} = require('react-dom/client');
const PortalApp = require('../src/portal/PortalApp.tsx').default;
const {ModelSetup} = require('../src/portal/ModelSetup.tsx');
const {ProviderKeys} = require('../src/portal/SettingsControls.tsx');
const {usePcQueue} = require('../src/portal/PcAccess.tsx');
const {createPortalClient} = require('../src/portal/portal-client.ts');
const {CatalogReload,notifyModelChanged,beginModelRestart,MODEL_APPLYING,retryModelRead} = require('../src/portal/model-reload.ts');
const {WorkflowContext,WorkflowProposalCard,WorkflowRunView} = require('../src/portal/WorkflowViews.tsx');
const {WorkflowStore} = require('../src/portal/workflow-store.ts');
const {NeedsYouPage} = require('../src/portal/NeedsYouPage.tsx');
const clone=x=>JSON.parse(JSON.stringify(x));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const flush=async(fn=()=>{})=>act(async()=>{await fn();await Promise.resolve();});
const text=()=>document.body.textContent;
const button=(label,scope=document)=>[...scope.querySelectorAll('button')].find(n=>n.textContent===label);
const click=async(label,scope=document)=>{const b=button(label,scope);assert.ok(b,label);assert.ok(!b.disabled,label+' enabled');await flush(()=>b.click());};
const select=async(node,value)=>{assert.ok(node);assert.ok(!node.disabled);await flush(()=>{node.value=value;node.dispatchEvent(new Event('change',{bubbles:true}));});};
const emit=async(topic,payload)=>flush(()=>{for(const l of listeners)if(l.topic===topic)l.fn({payload});});
const originalNow=Date.now, originalTimeout=global.setTimeout, originalClear=global.clearTimeout;
let now=originalNow(), timers=new Map(), timerId=0, root;
function clock(){now=originalNow();timers=new Map();Date.now=()=>now;global.setTimeout=(fn,ms=0)=>{const id=++timerId;timers.set(id,{at:now+Number(ms),fn});return id;};global.clearTimeout=id=>timers.delete(id);}
async function advance(ms){const until=now+ms;for(let i=0;i<2000;i++){const next=[...timers].filter(([,t])=>t.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];if(!next){now=until;await flush();return;}now=next[1].at;timers.delete(next[0]);await flush(()=>next[1].fn());}throw Error('timer loop');}
async function mount(node){root=createRoot(document.getElementById('root'));await flush(()=>root.render(node));await flush();}
async function unmount(){if(root){await flush(()=>root.unmount());root=null;}Date.now=originalNow;global.setTimeout=originalTimeout;global.clearTimeout=originalClear;timers.clear();}
const catalog=(model='sol')=>({faces:[{id:'assistant',name:'Assistant',allowed:true}],models:[{id:'gpt',backend:'gpt',model,label:model,available:true,reason:null},{id:'ds',backend:'ds',model:'flash',label:'DeepSeek',available:true,reason:null,choices:[{model:'flash',label:'flash',default:true},{model:'flash-alt',label:'flash-alt',default:false}]}],backends:[{id:'gpt',model,kind:'http',available:true},{id:'ds',model:'flash',kind:'http',available:true}],capabilities:{faces:1,backends:2,actions:[],warnings:[]}});
const hello={identity:{sub:'au9',gateway:'http://fixture',faces:[],may_touch:[],may_not_touch:[],expires_at:'2027-01-01'},server:{},gaps:[],operations:{}};
const chat={id:'c1',title:'AU9 chat',face_id:null,model:null,backend:null,project_id:null};
function fixture(){let current=catalog(),plan=[],calls=[],status={lane:'fixture',providers:{},ready:true,default_backend:'gpt'};const invoke=async(cmd,args)=>{calls.push({cmd,args});switch(cmd){case 'portal_connection_get':return{connection_configured:true,mode:{value:'this_pc'}};case 'portal_hello':return clone(hello);case 'portal_catalog':{const next=plan.shift();if(next instanceof Error||next?.code)throw next;return next?.promise?next.promise:clone(next??current);}case 'portal_conversations':return{items:[chat],limit:30,offset:0,excluded:0};case 'portal_conversation_open':return{conversation:chat,messages:[],has_more:false};case 'portal_pins':return{face_id:null,backend:null,model:null};case 'runtime_model_status':return clone(status);case 'runtime_model_providers':return require('./model-provider-fixture.cjs');case 'portal_reconnect':return{target:'fixture',hello:clone(hello)};case 'pc_poll':return{requests:[],outcomes:{}};default:throw Error('Unexpected AU9 '+cmd);}};return{invoke,calls,status,plan,setCurrent:c=>{current=c;}};}
async function catalogChecks(){
  const f=fixture();await mount(React.createElement(PortalApp,{invoke:f.invoke,profiles:null,onProfiles:()=>{}}));await flush(()=>document.querySelector('.conversation-list button')?.click());clock();
  assert.ok(document.getElementById('portal-chat-model'),text());
  const picker=()=>document.getElementById('portal-chat-model'), populated=()=>{assert.ok([...picker().options].some(o=>o.textContent.includes('DeepSeek')));const model=document.getElementById('portal-chat-model-choice');assert.ok(model,'model companion survives retry');if(picker().value==='ds'){assert.equal(model.options.length,2);assert.equal(model.value,'flash-alt');}};
  populated();await select(picker(),[...picker().options].find(o=>o.textContent.includes('DeepSeek')).value);await select(document.getElementById('portal-chat-model-choice'),'flash-alt');const selected=picker().value;assert.ok(selected);
  f.plan.push({code:'transport_unavailable',detail:'restart transport'},{code:'gateway_http_503',detail:'restart 503'},catalog('fresh'));
  let change;await flush(()=>{change=notifyModelChanged(f.invoke);});populated();assert.equal(document.querySelector('.window-fault-notice'),null);
  await advance(500);populated();assert.equal(document.querySelector('.window-fault-notice'),null);
  await advance(1000);await flush(()=>change);populated();assert.equal(document.getElementById('portal-chat-model-choice').value,'flash-alt');assert.equal(picker().value,selected);assert.match(picker().textContent,/fresh/);
  // Component failures can be returned as a partial catalog, not an IPC rejection.
  const degraded=catalog();degraded.models=[];degraded.capabilities.warnings=['models: gateway_http_500','capabilities: gateway_http_502','execution_v2: gateway_http_503'];
  f.plan.push(degraded,catalog('components'));await click('Refresh models');populated();assert.match(picker().textContent,/fresh/);await advance(500);assert.match(picker().textContent,/components/);
  // Default label changes while the explicit pin survives.
  f.status.default_backend='ds';await flush(()=>{change=notifyModelChanged(f.invoke);});await flush(()=>change);assert.match(picker().options[0].textContent,/flash/);assert.equal(picker().value,selected);
  // Two model changes have independent flights; the older response arrives last.
  const older=deferred();f.plan.push(older);let firstChange,secondChange;await flush(()=>{firstChange=notifyModelChanged(f.invoke);});
  f.plan.push({code:'gateway_http_503',detail:'second restart'},catalog('second-change'));await flush(()=>{secondChange=notifyModelChanged(f.invoke);});populated();await advance(500);await flush(()=>secondChange);await flush(()=>older.resolve(catalog('obsolete')));await flush(()=>firstChange);assert.match(picker().textContent,/second-change/);assert.ok(!picker().textContent.includes('obsolete'));
  // A known restart's link drop quietly schedules read recovery.
  f.setCurrent(catalog('known-return'));f.plan.push({code:'gateway_http_503',detail:'known link drop'},catalog('known-return'));await emit('portal://event',JSON.stringify({event:'transport_fault',data:{code:'transport_unavailable',detail:'known drop'}}));assert.equal(document.querySelector('.window-fault-notice'),null);populated();await advance(500);assert.match(picker().textContent,/known-return/);
  // Exhaustion keeps the list; a manual retry clears its error.
  const bad=Array.from({length:30},()=>({code:'gateway_http_500',detail:'restart exhausted'}));f.plan.push(...bad);await click('Refresh models');await advance(60000);populated();assert.match(document.querySelector('.window-fault-notice').textContent,/restart exhausted/);
  f.plan.length=0;f.setCurrent(catalog('manual'));await click('Try loading models again');populated();assert.equal(document.querySelector('.window-fault-notice'),null);assert.match(picker().textContent,/manual/);
  // A drop followed by explicit reconnect reloads even without a model event.
  const before=f.calls.filter(c=>c.cmd==='portal_catalog').length;await advance(65001);await emit('portal://event',JSON.stringify({event:'transport_fault',data:{code:'transport_unavailable',detail:'fixture drop'}}));
  const reconnect=button('Reconnect and inspect history')||button('Reconnect');assert.ok(reconnect,text());f.setCurrent(catalog('returned'));await flush(()=>reconnect.click());await flush();assert.ok(f.calls.filter(c=>c.cmd==='portal_catalog').length>before);assert.match(picker().textContent,/returned/);
  await unmount();
  // Superseded responses and errors may not overwrite the latest request.
  const invoke=async()=>{},a=deferred(),b=deferred();let reads=[a.promise,b.promise],accepted=[],faults=[];const loader=new CatalogReload(()=>reads.shift(),v=>accepted.push(v),e=>{if(e)faults.push(e);});const old=loader.reload(),fresh=loader.reload();b.resolve('new');await fresh;a.resolve('old');await old;assert.deepEqual(accepted,['new']);assert.deepEqual(faults,[]);
  const c=deferred(),d=deferred();reads=[c.promise,d.promise];const stale=loader.reload(),latest=loader.reload();d.resolve('newest');await latest;c.reject('stale failure');await stale;assert.deepEqual(accepted,['new','newest']);assert.deepEqual(faults,[]);
  // In-flight deduplication is scoped to a model-change generation.
  const p=deferred(),q=deferred();let hits=0;const native=()=>++hits===1?p.promise:q.promise,client=createPortalClient(native);const first=client.catalog();beginModelRestart(native);const second=client.catalog();assert.equal(hits,2);q.resolve(catalog('second'));await second;p.resolve(catalog('first'));await first;
  // No permission, protocol, rate-limit or save retries are introduced.
  clock();for(const code of ['gateway_http_403','protocol_invalid','rate_limited']){let calls=0;beginModelRestart(invoke);await assert.rejects(retryModelRead(invoke,async()=>{calls++;throw{code,detail:code};},true));assert.equal(calls,1);}await unmount();
}
async function applyingChecks(){
  let status={lane:'fixture',providers:{openai_signin:true},ready:true,default_backend:'openai_codex',chatgpt:{signed_in:true,selected_model:'sol',model_selection_required:false},claude:{signed_in:true,selected_model:'opus',model_selection_required:false}},login,gate,calls=[];
  const models=()=>({...status.chatgpt,ok:true,account_default:'sol',models:['sol','next'].map(slug=>({slug,selected:slug===status.chatgpt.selected_model,account_default:slug==='sol'}))});
  const invoke=async(cmd,args)=>{calls.push(cmd);switch(cmd){case 'runtime_model_providers':return require('./model-provider-fixture.cjs');case 'runtime_model_status':return clone(status);case 'runtime_model_openai_models':return models();case 'runtime_model_claude_models':return{models:[{slug:'opus',label:'Opus'},{slug:'sonnet',label:'Sonnet'}]};case 'runtime_model_openai_login':case 'runtime_model_claude_login':login=deferred();return login.promise;case 'runtime_model_openai_set_model':status.chatgpt.selected_model=args.model;return models();case 'runtime_model_claude_set_model':status.claude.selected_model=args.model;return{ok:true};case 'runtime_model_claude_logout':status.claude.signed_in=false;return{ok:true};case 'runtime_model_set_default':status.default_backend=args.backend;return{ok:true};case 'runtime_model_set_key':return{ok:true};default:throw Error('Unexpected applying '+cmd);}};
  const reload=()=>{gate=deferred();return gate.promise;};const c=catalog();c.backends.push({id:'openai_codex',model:'sol',kind:'http',available:true});
  await mount(React.createElement(ModelSetup,{invoke,catalog:c,onConfigured:reload}));
  async function finish(action,confirmation){await action();assert.ok(text().includes(MODEL_APPLYING));assert.ok(!text().includes(confirmation));assert.equal(document.querySelector('[role="alert"]'),null);await flush(()=>gate.resolve());await flush();assert.ok(!text().includes(MODEL_APPLYING));assert.ok(text().includes(confirmation));}
  await finish(()=>select(document.getElementById('chatgpt-model'),'next'),'ChatGPT model saved.');
  await finish(()=>select(document.getElementById('model-default'),'ds'),'Default saved.');
  await click('Use an API key');document.getElementById('model-key').value='SYNTHETIC-AU9-KEY';await finish(()=>flush(()=>document.getElementById('model-key').form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))),'OpenAI saved.');assert.equal(document.getElementById('model-key').value,'');
  await click('Sign in with OpenAI (ChatGPT)');await finish(()=>flush(()=>login.resolve({ok:true})),'OpenAI sign-in saved.');
  const claude=()=>document.querySelector('[aria-label="Claude sign-in"]');
  await finish(()=>select(claude().querySelector('select'),'sonnet'),'Claude model saved.');
  await click('Sign in with Claude (subscription)',claude());await emit('runtime://model',{event:'done',ok:true});await finish(()=>flush(()=>login.resolve({ok:true})),'Claude sign-in saved. Choose a Claude model.');
  await click('Sign out',claude());await finish(()=>click('Confirm sign out',claude()),'Claude signed out.');
  for(const cmd of ['runtime_model_openai_set_model','runtime_model_set_default','runtime_model_set_key','runtime_model_openai_login','runtime_model_claude_set_model','runtime_model_claude_login','runtime_model_claude_logout'])assert.equal(calls.filter(c=>c===cmd).length,1,cmd+' is never replayed');
  await unmount();
  // Server enrollment keys use the same fresh-catalog acknowledgement.
  const keyCatalog=catalog();keyCatalog.capabilities.operations=Object.fromEntries(['secrets.set','owner.begin','owner.confirm','owner.status'].map(op=>[op,{contract_versions:[2],available:true,implemented:true,...(op==='secrets.set'?{enrolls_backend:true,providers:['openai']}:{})}]));
  const keyClient={ownerStatus:async()=>({owner_session:{active:true,expires_at:Date.now()/1000+300,allowed_operations:['secrets.set']}}),secretSet:async()=>({stored:true,provider:'openai',fingerprint:'0123456789abcdef',operation_id:'receipt-au9'}),modelsChanged:reload};
  await mount(React.createElement(ProviderKeys,{client:keyClient,catalog:keyCatalog,owner:true,scope:'au9'}));document.querySelector('input[type="password"]').value='SYNTHETIC-AU9-KEY';await finish(()=>click('Store key'),'Saved. openai is turned on.');assert.equal(document.querySelector('input[type="password"]').value,'');await unmount();
}
async function otherReads(){
  let failures=0,modelFailures=0;const state={lane:'fixture',providers:{openai_signin:true},ready:true,default_backend:'openai_codex',chatgpt:{signed_in:true,selected_model:'sol',model_selection_required:false}};
  const readInvoke=async cmd=>{if(cmd==='runtime_model_providers')return require('./model-provider-fixture.cjs');if(cmd==='runtime_model_status'){if(failures-->0)throw 'Model status failed';return clone(state);}if(cmd==='runtime_model_openai_models'){if(modelFailures-->0)return{...state.chatgpt,ok:false,error:{code:'chatgpt_models_unavailable',detail:'temporary model list'},models:[]};return{...state.chatgpt,ok:true,models:[{slug:state.chatgpt.selected_model,selected:true}]};}throw Error(cmd);};
  await mount(React.createElement(ModelSetup,{invoke:readInvoke,catalog:catalog()}));clock();failures=2;modelFailures=2;state.chatgpt.selected_model='fresh-status';await flush(()=>notifyModelChanged(readInvoke));assert.equal(document.querySelector('[role="alert"]'),null);await advance(500);assert.equal(document.querySelector('[role="alert"]'),null);await advance(1000);await advance(500);assert.equal(document.querySelector('[role="alert"]'),null);await advance(1000);assert.equal(document.getElementById('chatgpt-model').value,'fresh-status');assert.equal(document.querySelector('[role="alert"]'),null);await unmount();
  let attempt=0;const invoke=async()=>{attempt++;if(attempt<3)throw{code:attempt===1?'transport_unavailable':'gateway_http_503',detail:'restart'};return{requests:[],outcomes:{}};};function Queue(){const q=usePcQueue(invoke,'au9');return React.createElement('p',null,q.error||'queue ready');}clock();beginModelRestart(invoke);await mount(React.createElement(Queue));assert.equal(text(),'queue ready');await advance(500);assert.equal(text(),'queue ready');await advance(1000);assert.equal(attempt,3);await unmount();
}
function fixtureFile(name){return JSON.parse(fs.readFileSync(path.resolve(__dirname,'../../../../runs/golive/workflow-fixtures',name+'.result.json'),'utf8'));}
async function workflowChecks(){
  const p=fixtureFile('propose').proposal;const retained='Files from the earlier run are still in this workspace';let starts=0,refusal='';
  const client={workflowCatalog:async()=>fixtureFile('catalog'),workflowProposal:async()=>({proposal:p}),workflowWatch:async()=>({}),workflowStart:async()=>{starts++;throw Error('Start not authorized');},workflowProposeAgain:async()=>{throw{code:refusal,detail:refusal};}};
  const store=new WorkflowStore(client,'au9');store.catalog={...fixtureFile('catalog'),available:true,reasons:[]};store.proposeAgainAdvertised=true;
  p.expires_at=new Date(Date.now()+600000).toISOString();p.runnable=false;p.coverage.review.same_model_choice_required=true;p.coverage.review.policy_met=false;p.reasons=['review_policy_requires_explicit_same_model_choice',retained];const source={conversationId:'c1',name:'Assistant',revision:p.revision,digest:p.digest};store.proposals.set(p.id,{proposal:p,source});const context={store,catalog:null,onOpen:()=>{},onFresh:()=>{},pc:()=>null};
  await mount(React.createElement(WorkflowContext.Provider,{value:context},React.createElement(WorkflowProposalCard,{id:p.id,source})));assert.ok(!button('Start anyway with same-model review').disabled);assert.ok(text().includes(retained+'.'));
  for(const reason of ['execution_storage_unavailable',retained+'.']){await flush(()=>{p.reasons.push(reason);store.changed();});assert.ok(button('Start anyway with same-model review').disabled);p.reasons.pop();}assert.equal(starts,0);await unmount();
  const run={...fixtureFile('run-stopped').run,id:'ended-au9',status:'failed',updated_at:new Date().toISOString()};store.putRun(run);p.state='granted';p.run_id=run.id;store.proposals.set(p.id,{proposal:p,source});
  const sentences={run_still_active:'This run is still going. Wait for it to end before asking to run it again.',run_not_found:'The earlier run was not found for the signed-in owner.',workspace_not_found:'The workspace from the earlier run is no longer available. Choose another workspace before trying again.'};
  for(const location of ['earlier','run'])for(const [code,sentence]of Object.entries(sentences)){client.workflowProposeAgain=async()=>{throw{code,detail:sentence};};await mount(React.createElement(WorkflowContext.Provider,{value:context},location==='earlier'?React.createElement(NeedsYouPage,{items:[],failures:[],checkedAt:Date.now(),checking:false,onRefresh:()=>{},store}):React.createElement(WorkflowRunView,{id:run.id})));await click('Run it again');assert.ok(text().includes(sentence),location+' '+code);assert.equal(starts,0);await unmount();}store.dispose();
}
module.exports=(async()=>{await catalogChecks();await applyingChecks();await otherReads();await workflowChecks();console.log('AU9 UI: PASS (catalog retry/retention/exhaustion/manual retry, partial catalogs, selection/default, reconnect, supersession/generation, every applying path, PC reads, exact notice/review blockers and all BU14 sentences)');})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await unmount();dom.window.close();});
