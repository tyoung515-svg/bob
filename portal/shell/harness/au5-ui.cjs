// AU5: shared ChatGPT setup, Settings placement, workflow names/proposers.
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost',pretendToBeVisual:true});
for(const k of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage'])global[k]=dom.window[k];
global.IS_REACT_ACT_ENVIRONMENT=true;
const listeners=new Set(),events=require.resolve('@tauri-apps/api/event');
require.cache[events]={id:events,filename:events,loaded:true,exports:{listen:async(topic,fn)=>{const entry={topic,fn};listeners.add(entry);return()=>listeners.delete(entry);}}};
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const {ModelSetup}=require('../src/portal/ModelSetup.tsx');
const {SettingsView,DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const {WorkflowStore}=require('../src/portal/workflow-store.ts');
const {WorkflowContext,WorkflowProposalCard}=require('../src/portal/WorkflowViews.tsx');
const {WorkflowEditor}=require('../src/portal/WorkflowEditor.tsx');
const {faceLabel}=require('../src/portal/workflow-library.ts');
const {buildAgents}=require('../src/portal/agents.ts');
let root,calls,status,available,listError,resolveLogin,rejectLogin,selected,refreshed=0;
const clone=x=>JSON.parse(JSON.stringify(x));
const faces=['assistant','planner','orchestrator','builder','researcher','custom-agent'].map(id=>({id,name:id==='custom-agent'?'Custom Agent':id[0].toUpperCase()+id.slice(1)}));
const catalog={faces,models:[],backends:[{id:'openai_codex',label:'GPT (ChatGPT sign-in)',available:true}],capabilities:{operations:{},actions:[],warnings:[]}};
const state=()=>({signed_in:status.providers.openai_signin,selected_model:selected,last_refresh:'2026-11-17T12:00:00Z',model_selection_required:!selected});
const list=()=>({...state(),ok:!listError,lane:'owner',backend:'openai_codex',account_default:listError?null:available[0]??null,models:listError?[]:available.map((slug,i)=>({slug,account_default:i===0,selected:slug===selected})),...(listError?{error:{code:'chatgpt_models_unavailable',detail:'ChatGPT models could not be read.'}}:{})});
function reset(signed=false){localStorage.clear();calls=[];selected=signed?'visible-gpt':null;status={lane:'owner',providers:{openai_signin:signed},default_backend:'deepseek_api',ready:true};available=['visible-gpt','second-gpt'];listError=false;refreshed=0;}
async function invoke(cmd,args){calls.push({cmd,args});switch(cmd){
  case 'runtime_model_providers':return require('./model-provider-fixture.cjs');
  case 'runtime_model_status':return {...clone(status),chatgpt:state()};
  case 'runtime_model_openai_models':return list();
  case 'runtime_model_openai_set_model':assert.deepEqual(Object.keys(args),['model']);assert.ok(available.includes(args.model));selected=args.model;return list();
  case 'runtime_model_set_default':assert.deepEqual(args,{backend:'openai_codex'});status.default_backend=args.backend;return {ok:true};
  case 'runtime_model_openai_login':for(const e of listeners)if(e.topic==='runtime://model')e.fn({payload:{event:'device_code',url:'https://auth.openai.com/device',code:'TEST-CODE'}});return new Promise((resolve,reject)=>{resolveLogin=resolve;rejectLogin=reject;});
  case 'runtime_model_cancel':rejectLogin('Sign-in cancelled');return {ok:true};
  case 'runtime_model_open_url':return;
  case 'local_auth_status':return [{name:'Windows CLI',present:true,modified:null,how:'Use the CLI'}];
  default:throw Error('Unexpected '+cmd);
}}
const flush=async(fn=()=>{})=>act(async()=>{await fn();await Promise.resolve();});
const text=()=>document.body.textContent;
const button=(label,scope=document)=>[...scope.querySelectorAll('button')].find(b=>b.textContent===label);
const count=cmd=>calls.filter(c=>c.cmd===cmd).length;
async function click(label){assert.ok(button(label),label);await flush(()=>button(label).click());}
async function mount(element=React.createElement(ModelSetup,{invoke,catalog,onConfigured:()=>{refreshed++;}})){root=createRoot(document.getElementById('root'));await flush(()=>root.render(element));}
async function unmount(){await flush(()=>root.unmount());root=null;}
async function completeLogin(){status.providers.openai_signin=true;selected='visible-gpt';await flush(()=>resolveLogin({ok:true}));await flush();}
async function choose(model){await flush(()=>{const s=document.getElementById('chatgpt-model');s.value=model;s.dispatchEvent(new Event('change',{bubbles:true}));});}
(async()=>{
  reset();await mount();assert.match(text(),/Not signed in/);assert.equal(count('runtime_model_openai_models'),0);
  const sign=button('Sign in with OpenAI (ChatGPT)');assert.ok(sign.classList.contains('primary'));assert.ok(sign.compareDocumentPosition(button('Use an API key'))&window.Node.DOCUMENT_POSITION_FOLLOWING);
  await click('Sign in with OpenAI (ChatGPT)');assert.match(text(),/TEST-CODE/);await click('Open sign-in page');assert.deepEqual(calls.find(c=>c.cmd==='runtime_model_open_url').args,{url:'https://auth.openai.com/device'});
  await completeLogin();assert.match(text(),/Signed in · visible-gpt/);assert.ok(document.getElementById('chatgpt-model'));assert.ok(text().includes('visible-gpt (account default)'));
  assert.ok(calls.filter(c=>c.cmd==='runtime_model_openai_models').every(c=>c.args===undefined));assert.equal(count('runtime_model_set_default'),0,'sign-in never switches the existing default');assert.equal(status.default_backend,'deepseek_api');
  assert.ok(button('Keep current default'));await choose('second-gpt');assert.deepEqual(calls.find(c=>c.cmd==='runtime_model_openai_set_model').args,{model:'second-gpt'});assert.equal(status.default_backend,'deepseek_api');assert.equal(count('runtime_model_set_default'),0);
  await click('Keep current default');assert.equal(count('runtime_model_set_default'),0);await unmount();await mount();assert.equal(button('Keep current default'),undefined,'answered question stays answered');await unmount();
  reset();await mount();await click('Sign in with OpenAI (ChatGPT)');await completeLogin();await unmount();await mount();assert.ok(button('Keep current default'),'unanswered question survives navigation');
  const make=button('Make GPT the default for all agents');await flush(()=>{make.click();make.click();});assert.equal(count('runtime_model_set_default'),1,'one explicit click dispatch');assert.equal(status.default_backend,'openai_codex');assert.equal(button('Keep current default'),undefined);assert.ok(refreshed>=2);await unmount();await mount();assert.equal(button('Keep current default'),undefined);assert.ok(!text().includes('Sign out'));await unmount();
  reset(true);available=['second-gpt'];await mount();assert.match(document.getElementById('chatgpt-model').selectedOptions[0].textContent,/visible-gpt.*not in the current list/);assert.ok(document.getElementById('chatgpt-model').selectedOptions[0].disabled);assert.equal(button('Keep current default'),undefined,'already signed in does not ask again');await unmount();
  reset(true);selected=null;await mount();assert.match(text(),/Choose a model to finish ChatGPT setup/);await choose('second-gpt');assert.equal(selected,'second-gpt');assert.equal(count('runtime_model_set_default'),0);await unmount();
  // AU9: a list failure inside the minute after a model change is retried quietly; this case is a failure outside that window.
  {const realNow=Date.now;Date.now=()=>realNow()+70000;try{reset(true);listError=true;await mount();assert.match(text(),/ChatGPT models could not be read/);assert.ok(document.getElementById('chatgpt-model').disabled);listError=false;await click('Refresh ChatGPT models');assert.ok(!document.getElementById('chatgpt-model').disabled);await unmount();}finally{Date.now=realNow;}}
  reset();await mount();await click('Sign in with OpenAI (ChatGPT)');await click('Cancel sign-in');assert.equal(count('runtime_model_cancel'),1);assert.equal(button('Keep current default'),undefined);await unmount();
  reset(true);await mount(React.createElement(SettingsView,{invoke,client:{},catalog,hello:{identity:{sub:'owner',actor:'owner',owner:'owner',token_type:'human'}},profiles:null,onProfiles:()=>{},onReconnect:()=>{},appearance:DEFAULT_APPEARANCE,onAppearance:()=>{},pendingApprovals:0}));await click('Keys & sign-ins');
  const entry=document.querySelector('[aria-label="ChatGPT sign-in"]');assert.ok(entry);const provider=[...document.querySelectorAll('h3')].find(n=>n.textContent==='Provider keys');assert.ok(provider);assert.ok(entry.compareDocumentPosition(provider)&window.Node.DOCUMENT_POSITION_FOLLOWING);assert.match(text(),/Windows command-line tool sign-ins/);assert.match(text(),/runtime does not use these sign-ins/);assert.match(text(),/sign-in file found/);await unmount();
  assert.equal(faceLabel('orchestrator'),'Orchestrator');assert.equal(faceLabel('planner'),'Planner');
  const ordered=buildAgents([{id:'p',face_id:'planner',updated_at:'2026-11-17'},{id:'a',face_id:'assistant',updated_at:'2020-01-01'}],faces,{working:'p',asking:null,approvals:{}});
  assert.equal(ordered.findIndex(a=>a.face_id==='planner'),ordered.findIndex(a=>a.face_id==='assistant')+1);
  const definition={schema:'workflow/v1',name:'Plan first',summary:'Plan',agents:{plan:{face_id:'orchestrator',binding_id:null}},stages:[{id:'plan',kind:'agent',agent:'plan',brief:'Plan',depends_on:[],access:'read',output:'text'}],review_policy:'any',authority:{checks:[],workspace_ops:[]},budget:{calls:4,active_seconds:60,job_seconds:10,max_job_attempts:1}};
  const p={kind:'workflow_proposal',id:'p',revision:1,digest:'a'.repeat(64),state:'proposed',expires_at:'2099-01-01T00:00:00Z',origin_conversation_id:'planner-chat',proposed_by:{actor:'owner',via:'assistant',token_type:'portal'},runnable:true,reasons:[],run_id:null,coverage:{definition,inputs:{task:'Plan this'},bindings:{plan:{model:'visible-gpt'}},assistant_binding:{face_id:'planner'},assistant_summary_calls:1,workspace:null,review:{policy:'any',policy_met:true,same_model_choice_required:false},checks:[],budget:definition.budget,run_mode:'approve',run_modes:[{id:'approve',label:'Ask before changes'}]}};
  const store=new WorkflowStore({workflowProposals:async()=>({items:[p],next_cursor:null}),workflowCatalog:async()=>({available:true,bindings:[],reasons:[]})},'au5');store.proposalsAdvertised=true;await store.refreshProposals();assert.equal(store.proposals.get('p').source.name,'the Planner');
  store.catalog={available:true,bindings:[],reasons:[]};const context={store,catalog,onOpen:()=>{},onFresh:()=>{},pc:()=>null};
  await mount(React.createElement(WorkflowContext.Provider,{value:context},React.createElement(WorkflowProposalCard,{id:'p',source:store.proposals.get('p').source})));assert.match(text(),/Proposed by the Planner/);assert.match(text(),/Planner's summary/);assert.match(text(),/Orchestrator using visible-gpt/);await unmount();
  await mount(React.createElement(WorkflowContext.Provider,{value:context},React.createElement(WorkflowEditor,{definition,onSaved:()=>{},onCancel:()=>{}})));const faceSelect=[...document.querySelectorAll('select')].find(s=>[...s.options].some(o=>o.value==='custom-agent'));assert.ok(faceSelect);assert.match(faceSelect.textContent,/Custom Agent/);assert.equal([...faceSelect.options].find(o=>o.value==='orchestrator').textContent,'Orchestrator');await unmount();store.dispose();
  console.log('AU5 setup/workflows: PASS (shared device flow, signed-in states, exact list/select/default calls, pending/answered default choice, no automatic default, Settings placement, PC-only explanation, Planner proposer and summary, Orchestrator label, catalog agent slots)');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(root)await unmount();dom.window.close();});
