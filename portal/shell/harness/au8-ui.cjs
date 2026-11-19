// AU8: synthetic Claude protocol and ended workflow retries. No provider or browser calls.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost'});
for (const k of ['window','document','HTMLElement','Event','CustomEvent','localStorage']) global[k] = dom.window[k];
global.IS_REACT_ACT_ENVIRONMENT = true;
let clipboard = '', copyFails = false;
Object.defineProperty(global, 'navigator', {configurable:true,value:{userAgent:'node',clipboard:{writeText:async value => { if(copyFails) throw Error('copy failed'); clipboard=value; }}}});
const listeners = new Set(), events = require.resolve('@tauri-apps/api/event');
require.cache[events] = {id:events,filename:events,loaded:true,exports:{listen:async (_,fn)=>{listeners.add(fn);return()=>listeners.delete(fn);}}};
const React = require('react'), {act} = React, {createRoot} = require('react-dom/client');
const {ModelSetup} = require('../src/portal/ModelSetup.tsx');
const {ModelPicker} = require('../src/portal/ModelPicker.tsx');
const {WorkflowStore} = require('../src/portal/workflow-store.ts');
const {NeedsYouPage} = require('../src/portal/NeedsYouPage.tsx');
const {WorkflowContext,WorkflowRunView,WorkflowProposalCard} = require('../src/portal/WorkflowViews.tsx');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const clone=x=>JSON.parse(JSON.stringify(x));
const flush=async(fn=()=>{})=>act(async()=>{await fn();await Promise.resolve();});
let root, calls=[], status, login, opening, statusPending=null, launchPending=null;
const catalog={faces:[],models:[{id:'claude',backend:'claude_code',model:'opus',label:'Claude (subscription)',available:false,reason:'Claude subscription sign-in is ready; running turns requires a later runtime.'}],backends:[{id:'claude_code',model:'opus',kind:'cli',available:false,unavailable_reason:'Claude subscription sign-in is ready; running turns requires a later runtime.'}],capabilities:{}};
const choices=['default','fable','opus','sonnet'].map(slug=>({slug,label:slug==='default'?'Account default':slug}));
function reset(){calls=[];status={lane:'owner',providers:{openai_signin:false},ready:false,default_backend:null,signin_in_progress:false,claude:{signed_in:false,selected_model:null,last_checked:null,model_selection_required:false}};statusPending=null;launchPending=null;localStorage.clear();}
async function invoke(cmd,args){
  // The fake code is recorded only by the synthetic sensitive-channel assertion.
  calls.push({cmd,args:cmd==='runtime_model_claude_submit_code'?{sensitive:true}:args});
  switch(cmd){
    case 'runtime_model_providers':return require('./model-provider-fixture.cjs');
    case 'runtime_model_status':return statusPending ? statusPending.promise : clone(status);
    case 'runtime_model_claude_login':login=deferred();return login.promise;
    case 'runtime_model_openai_login':login=deferred();return login.promise;
    case 'runtime_model_claude_submit_code':assert.equal(args.code,'OBVIOUS_FAKE_CODE1');return{ok:true};
    case 'runtime_model_open_url':opening=deferred();return opening.promise;
    case 'runtime_model_cancel':if(login)login.reject('The sign-in was cancelled.');return{ok:true};
    case 'runtime_model_claude_models':return{ok:true,models:clone(choices)};
    case 'runtime_model_claude_set_model':status.claude.selected_model=args.model;status.claude.model_selection_required=false;return{ok:true};
    case 'runtime_model_claude_logout':status.claude.signed_in=false;status.claude.selected_model=null;return{ok:true};
    default:throw Error('Unexpected AU8 '+cmd);
  }
}
const body=()=>document.body.textContent, card=()=>document.querySelector('[aria-label="Claude sign-in"]');
const button=(label,scope=document)=>[...scope.querySelectorAll('button')].find(n=>n.textContent===label);
const click=async(label,scope=document)=>{const b=button(label,scope);assert.ok(b,label);assert.ok(!b.disabled,label+' enabled');await flush(()=>b.click());};
const field=()=>card().querySelector('input');
const emit=async payload=>{await flush(()=>{for(const fn of listeners)fn({payload});});};
const url='https://claude.com/cai/oauth/authorize?x=1&redirect=https%3A%2F%2Fclaude.ai&state=A_B';
async function mount(signinOnly=false){root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(ModelSetup,{invoke,signinOnly,catalog})));}
async function unmount(){if(root){await flush(()=>root.unmount());root=null;}assert.equal(listeners.size,0);}
async function start(){await click(button('Start again',card())?'Start again':'Sign in with Claude (subscription)',card());}
async function link(){await emit({event:'signin_link',url});assert.equal(card().querySelector('[aria-label="Claude sign-in address"]').textContent,url);}
async function submit(value='OBVIOUS_FAKE_CODE1'){const input=field();input.value=value;await click('Submit code',card());assert.equal(input.value,'');return input;}
const reasons={signin_unreachable:'Could not reach the sign-in service through the lane proxy.',signin_refused:'The sign-in service refused the request.',signin_timeout:'The sign-in timed out.',signin_cancelled:'The sign-in was cancelled.',signin_not_stored:'The sign-in finished but no usable sign-in was stored.',signin_unavailable:'The sign-in helper or lane proxy is unavailable.',signin_failed:'The sign-in failed; try again.',signin_code_rejected:'The sign-in code was wrong or expired. Start sign-in again.',signin_in_progress:'A sign-in is already in progress for this lane.'};
async function claudeChecks(){
  for(const signinOnly of [false,true]){
    reset();await mount(signinOnly);assert.match(card().textContent,/not signed in/);assert.match(card().textContent,/running turns requires a later runtime/);
    await start();assert.ok(button('Cancel sign-in',card()));assert.ok(button('Sign in with OpenAI (ChatGPT)').disabled);
    await click('Cancel sign-in',card());assert.match(card().textContent,/The sign-in was cancelled/);
    await start();await link();field().value='OBVIOUS_FAKE_CODE1';const cancelledField=field();await click('Cancel sign-in',card());assert.equal(cancelledField.value,'');
    await start();await link();
    const range=document.createRange();range.selectNodeContents(card().querySelector('.model-device-url'));window.getSelection().removeAllRanges();window.getSelection().addRange(range);assert.equal(window.getSelection().toString(),url);
    copyFails=true;await click('Copy link',card());assert.match(card().textContent,/Select the sign-in address and copy it/);copyFails=false;
    await click('Copy link',card());assert.equal(clipboard,url);
    await click('Open sign-in page',card());assert.deepEqual(calls.find(c=>c.cmd==='runtime_model_open_url').args,{url});await flush(()=>opening.reject('OS detail'));assert.match(card().textContent,/could not be opened from here/);
    await click('Open sign-in page',card());await flush(()=>opening.resolve());assert.match(card().textContent,/was opened in your default browser/);
    for(const invalid of ['', 'bad code', 'é', 'a'.repeat(4097), 'a&b']){const before=calls.filter(c=>c.cmd==='runtime_model_claude_submit_code').length;await submit(invalid);assert.match(card().textContent,/at most 4096 ASCII/);assert.equal(calls.filter(c=>c.cmd==='runtime_model_claude_submit_code').length,before);}
    const paste=new Event('paste',{bubbles:true,cancelable:true});Object.defineProperty(paste,'clipboardData',{value:{getData:()=> 'a\n'}});await flush(()=>field().dispatchEvent(paste));assert.ok(paste.defaultPrevented);assert.equal(field().value,'');
    await submit();assert.match(card().textContent,/Checking the code with Claude…/);assert.equal(field(),null);assert.ok(!body().includes('OBVIOUS_FAKE_CODE1'));assert.ok(!localStorage.length);assert.notEqual(clipboard,'OBVIOUS_FAKE_CODE1');
    assert.equal(calls.filter(c=>c.cmd==='runtime_model_claude_submit_code').length,1);
    status.signin_in_progress=true;await click('Refresh model status');assert.match(card().textContent,/A sign-in is in progress/);status.signin_in_progress=false;
    await click('Cancel sign-in',card());assert.match(card().textContent,/The sign-in was cancelled/);assert.ok(!button('Sign in with Claude (subscription)',card()).disabled);
    for(const [code,detail] of Object.entries(reasons)){await start();await link();field().value='OBVIOUS_FAKE_CODE1';const old=field();await emit({event:'error',code,detail});assert.equal(old.value,'');await flush(()=>login.reject('late error'));assert.equal(card().querySelector('[data-signin-code]').dataset.signinCode,code);assert.ok(card().textContent.includes(detail));assert.ok(button('Start again',card()));}
    await start();await link();await flush(()=>login.resolve({ok:true}));assert.match(card().textContent,/interrupted/);assert.ok(!card().textContent.includes('sign-in saved'));
    await start();await link();await submit();status.claude={signed_in:true,selected_model:null,last_checked:null,model_selection_required:true};await emit({event:'done',ok:true});await flush(()=>login.resolve({ok:true}));assert.ok(card().textContent.includes('Claude (subscription): signed in'));assert.match(card().textContent,/Choose a model to finish Claude setup/);
    const select=card().querySelector('select');assert.deepEqual([...select.options].slice(1).map(o=>o.textContent),['Account default','fable','opus','sonnet']);assert.match(card().textContent,/list the Claude program accepts, not a list read from the account/);
    await flush(()=>{select.value='opus';select.dispatchEvent(new Event('change',{bubbles:true}));});assert.match(card().textContent,/Chosen model: opus/);
    await click('Sign out',card());assert.equal(calls.filter(c=>c.cmd==='runtime_model_claude_logout').length,0);await click('Keep signed in',card());await click('Sign out',card());await click('Confirm sign out',card());assert.equal(calls.filter(c=>c.cmd==='runtime_model_claude_logout').length,1);assert.match(card().textContent,/not signed in/);
    status.signin_in_progress=true;await click('Refresh model status');assert.match(card().textContent,/A sign-in is in progress/);assert.ok(button('Sign in with Claude (subscription)',card()).disabled);assert.ok(button('Sign in with OpenAI (ChatGPT)').disabled);
    status.signin_in_progress=false;await click('Refresh model status');
    statusPending=deferred();const before=calls.filter(c=>c.cmd==='runtime_model_status').length;await click('Refresh model status');await flush(()=>window.dispatchEvent(new Event('bob-model-changed')));assert.equal(calls.filter(c=>c.cmd==='runtime_model_status').length,before+1);await flush(()=>statusPending.resolve(clone(status)));statusPending=null;
    await start();await link();field().value='OBVIOUS_FAKE_CODE1';const old=field(),beforeCancel=calls.filter(c=>c.cmd==='runtime_model_cancel').length;await unmount();assert.equal(old.value,'');assert.equal(calls.filter(c=>c.cmd==='runtime_model_cancel').length,beforeCancel+1);
    reset();await mount(signinOnly);await start();await link();await submit();const submittedCancel=calls.filter(c=>c.cmd==='runtime_model_cancel').length;await unmount();assert.equal(calls.filter(c=>c.cmd==='runtime_model_cancel').length,submittedCancel+1);
    // Cancel while waiting for listen() to finish must never launch a helper.
    reset();await mount(signinOnly);const eventModule=require.cache[events].exports,original=eventModule.listen;const wait=deferred();eventModule.listen=()=>wait.promise;
    const beforeLogin=calls.filter(c=>c.cmd==='runtime_model_claude_login').length;await start();await click('Cancel sign-in',card());await flush(()=>wait.resolve(()=>{}));assert.equal(calls.filter(c=>c.cmd==='runtime_model_claude_login').length,beforeLogin);eventModule.listen=original;await unmount();
  }
  // The original ChatGPT card preserves BU11's exact refusal sentence.
  reset();await mount();await click('Sign in with OpenAI (ChatGPT)');const detail='A ChatGPT sign-in is already in progress for this lane.';await emit({event:'error',code:'signin_in_progress',detail});await flush(()=>login.reject('later'));assert.match(body(),/A ChatGPT sign-in is already in progress for this lane/);await unmount();
  root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(ModelPicker,{id:'model',catalog,pin:null,defaultName:null,disabled:false,onChange:()=>{throw Error('disabled model selected');}})));const option=[...document.querySelectorAll('option')].find(o=>o.textContent.includes('Claude'));assert.ok(option.disabled);assert.ok(option.textContent.includes(catalog.models[0].reason));assert.equal(document.querySelector('[role="alert"]'),null);await unmount();
}
function fixture(name){return JSON.parse(fs.readFileSync(path.resolve(__dirname,'../../../../runs/golive/workflow-fixtures',name+'.result.json'),'utf8'));}
async function runChecks(){
  const proposal=fixture('propose').proposal, ended=fixture('run-stopped').run;
  let refusal='',callsAgain=[],starts=0;
  const client={workflowProposeAgain:async id=>{callsAgain.push(id);if(refusal)throw JSON.stringify({code:refusal,detail:refusal});const p=clone(proposal);p.id='fresh';p.state='proposed';p.run_id=null;p.expires_at=new Date(Date.now()+600000).toISOString();p.coverage.workspace={path_label:'garden',registration_digest:'registered'};return{proposal:p};},workflowCatalog:async()=>fixture('catalog'),workflowProposals:async()=>({items:[],next_cursor:null}),workflowWatch:async()=>({}),workflowStart:async()=>{starts++;throw Error('must require Start');}};
  for(const location of ['earlier','run']){
    const store=new WorkflowStore(client,'au8-'+location);store.proposeAgainAdvertised=true;store.catalog={...fixture('catalog'),available:true,reasons:[]};
    const run={...clone(ended),id:'ended',status:'failed',updated_at:new Date().toISOString()};store.putRun(run);
    const old={...clone(proposal),id:'consumed',state:'granted',run_id:run.id};store.proposals.set(old.id,{proposal:old,source:{conversationId:'chat',name:'the Assistant',revision:old.revision,digest:old.digest}});
    const context={store,catalog:null,onOpen:()=>{},onFresh:()=>{},pc:()=>null};
    async function render(){root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(WorkflowContext.Provider,{value:context},location==='earlier'?React.createElement(NeedsYouPage,{items:[],failures:[],checkedAt:Date.now(),checking:false,onRefresh:()=>{},store}):React.createElement(WorkflowRunView,{id:run.id}))));}
    for(const state of ['failed','stopped','cancelled','completed']){store.putRun({...run,status:state});await render();assert.ok(button('Run it again'));await unmount();}store.putRun(run);
    for(const code of ['proposal_not_expired','workspace_exists']){refusal=code;await render();await click('Run it again');assert.match(body(),code==='proposal_not_expired'?/only allows expired proposals/:/must reuse the workspace/);assert.equal(starts,0);await unmount();}
    refusal='';await render();await flush(()=>{const b=button('Run it again');b.click();b.click();});assert.equal(callsAgain.at(-1),'consumed');assert.ok(button('Start'));assert.match(body(),/existing workspace: garden/);assert.equal(starts,0);assert.equal(store.proposals.get('fresh').proposal.coverage.inputs.task,proposal.coverage.inputs.task);await unmount();
    store.proposals.clear();await render();await click('Run it again');assert.match(body(),/not supplied the original proposal/);await unmount();store.dispose();
  }
}
async function reviewNoticeChecks(){
  const p=clone(fixture('propose').proposal),retained='Files from the earlier run are still in this workspace';
  p.runnable=false;p.expires_at=new Date(Date.now()+600000).toISOString();p.reasons=['review_policy_requires_explicit_same_model_choice',retained];
  p.coverage.review.same_model_choice_required=true;p.coverage.review.policy_met=false;
  const client={workflowCatalog:async()=>fixture('catalog'),workflowProposal:async()=>({proposal:p})};
  const store=new WorkflowStore(client,'au8-retained');store.catalog={...fixture('catalog'),available:true,reasons:[]};
  const source={conversationId:'chat',name:'Assistant',revision:p.revision,digest:p.digest};store.proposals.set(p.id,{proposal:p,source});
  root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(WorkflowContext.Provider,{value:{store,catalog:null,onOpen:()=>{},onFresh:()=>{},pc:()=>null}},React.createElement(WorkflowProposalCard,{id:p.id,source}))));
  assert.ok(body().includes(retained+'.'));assert.ok(!button('Start anyway with same-model review').disabled);
  await flush(()=>{p.reasons.push('execution_storage_unavailable');store.changed();});assert.ok(button('Start anyway with same-model review').disabled);
  await unmount();store.dispose();
}
module.exports=(async()=>{await claudeChecks();await runChecks();await reviewNoticeChecks();console.log('AU8 UI: PASS (both setup surfaces, link/copy/open, code refusal and clearing, checking/done/errors/cancel/unmount, in-progress/status deduplication, model choice/logout, disabled catalog, ended-run retry/refusals/workspace/Start boundary)');})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await unmount();dom.window.close();});
