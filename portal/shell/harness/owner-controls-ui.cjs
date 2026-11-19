// S3c: offline native seam, real React components, client and mutation journals.
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
for (const key of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent', 'localStorage']) global[key] = dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const { useOwnerAction, OwnerSessionIndicator } = require('../src/portal/OwnerControls.tsx');
const { RunResume } = require('../src/portal/RunResume.tsx');
const { RunsPanel } = require('../src/portal/DurableRunsPanel.tsx');
const { ProjectBudget } = require('../src/portal/ProjectBudget.tsx');
const { ExecutionApprovals } = require('../src/portal/ExecutionApprovals.tsx');
const { operationReason, ownerEligible, validInteger } = require('../src/portal/durable-runs.ts');
const ops = ['owner.begin','owner.confirm','owner.status','owner.lock','owner.logout','grants.prepare','grants.issue','grants.list','grants.get','grants.revoke','budgets.get','budgets.list','budgets.set','budgets.remove','runs.resume'];
const catalog = { capabilities: { operations: Object.fromEntries(ops.map(op => [op, {contract_versions:[2], available:true, implemented:true}])), execution_v2: { contract_versions:[2], execution_enabled:true, topologies:['four-step-readonly/v1'] } } };
const identity = { sub:'fixture-owner', token_type:'portal', owner_controls_eligible:true };
const budget = {start_calls:1000, wall_mode:'none', wall_seconds:null};
const baseRun = {id:'run-1', revision:5, status:'paused', pause_reason:'call_budget_exhausted', effective_call_limit:200, calls_used:200, project_id:'project-1', budget};
const team = {id:'fixture-team', revision:2, schema_version:1, topology:'four-step-readonly/v1', runnable:true, blocked_reasons:[], roles:{worker:'configured-face'}};
let state, root, hits = [];
const text = () => document.body.textContent;
const button = label => [...document.querySelectorAll('button')].find(b => b.textContent === label);
const field = label => document.querySelector(`[aria-label="${label}"]`);
async function flush(fn = () => {}) { await act(async () => { await fn(); await new Promise(r => setTimeout(r,0)); }); }
async function click(label) { const b = button(label); assert.ok(b, label); assert.ok(!b.disabled,label+' enabled'); await flush(() => b.click()); }
async function input(label,value) { const el = field(label); assert.ok(el,label); const proto = el.tagName==='SELECT' ? dom.window.HTMLSelectElement.prototype : el.tagName==='TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
  await flush(() => { Object.getOwnPropertyDescriptor(proto,'value').set.call(el,value); el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input',{bubbles:true})); }); }
function noFactors() { for (const label of ['Owner password','Owner authenticator code']) if (field(label)) assert.equal(field(label).value,'');
  const snapshot = document.body.innerHTML + JSON.stringify({...localStorage}); assert.ok(!snapshot.includes('synthetic-password')); assert.ok(!snapshot.includes('456789')); }
const fail = (code,detail=code,extra={}) => { throw JSON.stringify({code,detail,...extra}); };
const detail = () => ({run:{...baseRun,revision:state.revision},events:[],receipts:[],next_cursor:null});
const client = createPortalClient(async (cmd,args) => {
  hits.push({cmd,args});
  switch(cmd) {
    case 'portal_owner_status_v2': return {owner_session:{active:state.active,expires_at:Date.now()/1000+240,allowed_operations:ops}};
    case 'portal_owner_begin_v2': return {challenge_id:'challenge-'+hits.length,expires_at:Date.now()/1000+120,...(state.signedIn?{confirmation_mode:'signed_in_user'}:{}),summary:{operation:args.operation,resource:args.resource,canonical:'Server reviewed task, exact team/model pins'}};
    case 'portal_owner_confirm_v2':
      noFactors();
      if(state.signedIn){assert.equal(args.confirmation_mode,'signed_in_user');assert.ok(!('password' in args));assert.ok(!('totp' in args));}
      else {assert.equal(args.password,'synthetic-password');assert.equal(args.totp,'456789');}
      if (state.confirmError) fail(state.confirmError,'safe refusal',state.confirmError==='gateway_http_429'?{retry_after:2}:{});
      state.active=true; return {owner_session:{active:true}};
    case 'portal_owner_lock_v2': case 'portal_owner_logout_v2': state.active=false; return {owner_session:{active:false}};
    case 'portal_budgets_get_v2': return state.pool;
    case 'portal_budget_set_v2': state.pool={...state.pool,configured:true,call_limit:args.call_limit,revision:2}; return state.pool;
    case 'portal_budgets_remove_v2': if(state.budgetInUse) fail('gateway_http_409','budget_in_use'); state.pool={...state.pool,configured:false,call_limit:null,revision:3};return state.pool;
    case 'portal_run_resume_v2': if(state.expired){state.active=false;fail('gateway_http_401','owner_session_invalid');} state.resumed=args; return { ...detail(),run:{...baseRun,status:'recovering'} };
    case 'portal_grants_prepare_v2': {
      const request=args.request || {contract_version:2,request_key:'original-run-key',team:team.id,team_revision:2,task:'original task',project_id:'project-1',budget:{...budget,start_calls:200}};
      assert.ok(request.request_key,'full nested admission request');
      return {proposal_id:'proposal-1',intent_id:'proposal-1',revision:1,digest:'server-digest',coverage:{request,snapshot:{team,models:{'configured-face':{model:'configured-model'}}},initiator:{actor:'fixture-owner',scope:'original scope'},planned_run_id:baseRun.id}};
    }
    case 'portal_grants_issue_v2': state.issued=args;state.revision++;return {grant:{id:'grant-1',revision:1}};
    case 'portal_run_get_v2':if(state.loseRead){state.loseRead=false;fail('timeout_no_reply');}return detail();
    case 'portal_teams_list_v2':return {items:[team],next_cursor:null};
    case 'portal_runs_list_v2':return {items:[],next_cursor:null,source:'server',coverage:'owned runs'};
    case 'portal_runs_start_v2': assert.equal(args.budget.start_calls,1000);assert.equal(args.intent_id,'proposal-1');state.started=args;if(state.loseStart){state.loseStart=false;fail('timeout_no_reply');}return {...detail(),run:{...baseRun,status:'accepted'}};
    case 'portal_grants_list_v2':return {items:[{id:'grant-1',revision:1,coverage_kind:'project',project_id:'project-1',auto_escalate:false,call_ceiling:null,expires_at:null,revoked:false}],next_cursor:null};
    case 'portal_grants_revoke_v2':state.revoked=args;return {grant:{id:args.grant_id,revoked:true},affected_runs:['run-1']};
    default:throw new Error('Unexpected '+cmd);
  }
});
function reset(overrides={}) {localStorage.clear();hits=[];state={active:false,revision:5,pool:{project_id:'project-1',configured:false,call_limit:null,consumed:'9007199254740993',reserved:0,revision:null,counter_coverage:'durable_runs_only'},...overrides};}
async function mount(node) {root=createRoot(document.getElementById('root'));await flush(()=>root.render(node));}
async function unmount() {await flush(()=>root.unmount());noFactors();}
function Basic() {const action=useOwnerAction(client,catalog);const [message,setMessage]=React.useState('');return React.createElement(React.Fragment,null,action.dialog,React.createElement('button',{onClick:()=>void action.run('runs.resume',{run_id:'run-1'},()=>client.runResumeV2({contract_version:2,run_id:'run-1',expected_revision:5,request_key:'basic',budget:{start_calls:300}})).then(()=>setMessage('done')).catch(e=>setMessage(String(e)))},'Owner action'),React.createElement('p',null,message));}
async function proof(label='Confirm and continue') {await input('Owner password','synthetic-password');await input('Owner authenticator code','456789');await click(label);noFactors();}
(async()=>{
  assert.ok(ownerEligible(identity,catalog));assert.ok(!ownerEligible({...identity,token_type:'agent'},catalog));assert.ok(!ownerEligible({...identity,owner_controls_eligible:false},catalog));
  assert.ok(validInteger('9223372036854775807'));for(const x of ['01','1e3','9223372036854775808',9007199254740992])assert.ok(!validInteger(x));
  reset();await mount(React.createElement(Basic));await click('Owner action');assert.ok(field("Confirm it's you"));assert.match(text(),/Server reviewed task/);await proof();assert.match(text(),/done/);assert.equal(field("Confirm it's you"),null);
  const confirms=hits.filter(h=>h.cmd==='portal_owner_confirm_v2').length;await click('Owner action');assert.equal(hits.filter(h=>h.cmd==='portal_owner_confirm_v2').length,confirms,'live session reused');await unmount();
  reset({signedIn:true});await mount(React.createElement(Basic));await click('Owner action');
  assert.match(text(),/Review permission/);assert.equal(field('Owner password'),null);assert.equal(field('Owner authenticator code'),null);
  assert.equal(hits.filter(h=>h.cmd==='portal_owner_confirm_v2').length,0,'review click is required');
  await click('Confirm and continue');assert.match(text(),/done/);assert.ok(state.resumed);await unmount();
  reset({signedIn:true});await mount(React.createElement(Basic));await click('Owner action');await click('Cancel');
  assert.equal(hits.filter(h=>h.cmd==='portal_owner_confirm_v2').length,0);assert.equal(state.resumed,undefined);await unmount();
  for(const code of ['gateway_http_401','gateway_http_429','timeout_no_reply']) {
    reset({confirmError:code});await mount(React.createElement(Basic));await click('Owner action');await proof();assert.match(text(),new RegExp(code==='gateway_http_429' ? "wasn't applied" : code));assert.ok(field("Confirm it's you"));noFactors();
    assert.equal(hits.filter(h=>h.cmd==='portal_owner_confirm_v2').length,1,'never automatically resend proof');
    if(code==='gateway_http_429'){assert.doesNotMatch(text(),/unknown|gateway_http_429/);assert.ok(button('Confirm and continue').disabled);}
    await click('Cancel');await unmount();
  }
  reset({active:true,expired:true});await mount(React.createElement(Basic));await click('Owner action');assert.match(text(),/owner_session_invalid/);assert.equal(field("Confirm it's you"),null);state.expired=false;await click('Owner action');await proof();assert.match(text(),/done/);await unmount();
  for(const coverage of ['task','project']) {
    reset({active:true});await mount(React.createElement(RunResume,{client,catalog,scope:'resume-'+coverage,run:baseRun,owner:true,onResult:()=>{},onRefresh:()=>{}}));
    await input('Resume call allowance','1000');await click('Resume run');assert.match(text(),/Starting allowance: 1000/);assert.match(text(),/No ceiling/);assert.match(text(),/coming with build jobs/);
    assert.equal(field('Owner password'),null,'active session still reviews coverage without proof');await input('Approval coverage',coverage);await click('Allow and resume');
    assert.equal(state.issued.coverage_kind,coverage);assert.equal(state.issued.call_ceiling,null);assert.equal(state.issued.auto_escalate,false);assert.equal(state.resumed.expected_revision,6,'grant binding increments revision');assert.equal(state.resumed.budget.start_calls,1000);await unmount();
  }
  reset();await mount(React.createElement(RunsPanel,{client,catalog,scope:'start-1000',runs:[],onRuns:()=>{},identity,seed:{team:team.id,task:'1000-call custom task',budget:{...budget,start_calls:50},project_id:'project-1',at:1}}));
  await input('Call budget preset','custom');await input('Custom calls','1000');await click('Start run');assert.equal(state.started,undefined);await input('Approval coverage','project');await input('Approval call ceiling','9007199254740993');await proof('Allow and start');
  assert.equal(state.started.budget.start_calls,1000);assert.equal(state.started.intent_id,'proposal-1');assert.equal(state.issued.call_ceiling,'9007199254740993');await unmount();
  // A committed issue followed by a failed read must not issue another grant.
  reset({active:true,loseRead:true});await mount(React.createElement(RunResume,{client,catalog,scope:'issue-read',run:baseRun,owner:true,onResult:()=>{},onRefresh:()=>{}}));
  await input('Resume call allowance','1000');await click('Resume run');await click('Allow and resume');assert.match(text(),/timeout_no_reply/);
  await click('Resume run');await click('Allow and resume');assert.equal(hits.filter(h=>h.cmd==='portal_grants_issue_v2').length,1);assert.equal(state.resumed.expected_revision,6);await unmount();
  reset({active:true,loseStart:true});const pendingStart=()=>React.createElement(RunsPanel,{client,catalog,scope:'start-reload',runs:[],onRuns:()=>{},identity,seed:{team:team.id,task:'1000-call custom task',budget,project_id:'project-1',at:1}});
  await mount(pendingStart());await click('Start run');await click('Allow and start');const originalStart=state.started;await unmount();
  await mount(pendingStart());await click('Reconcile start');assert.deepEqual(state.started,originalStart);assert.equal(hits.filter(h=>h.cmd==='portal_grants_issue_v2').length,1);assert.ok(!Object.keys(localStorage).some(k=>k.includes('execution-approval:')||k.includes('execution-issued:')));await unmount();
  reset({active:true});await mount(React.createElement(ProjectBudget,{client,catalog,scope:'budget',projectId:'project-1',owner:true}));assert.match(text(),/Used: 9007199254740993/);await input('Project budget mode','limit');await input('Project call limit','9223372036854775807');await click('Set project budget');assert.equal(state.pool.call_limit,'9223372036854775807');assert.match(text(),/limit: 9223372036854775807/);
  state.budgetInUse=true;await click('Remove limit');assert.match(text(),/budget_in_use/);state.budgetInUse=false;await click('Remove limit');assert.equal(state.pool.call_limit,null);assert.match(text(),/No limit/);await unmount();
  reset({active:true});await mount(React.createElement(ExecutionApprovals,{client,catalog,scope:'list',owner:true}));await click('Read active approvals');assert.match(text(),/Future matching tasks/);await click('Revoke');assert.match(text(),/Already dispatched calls remain charged/);await click('Revoke approval');assert.equal(state.revoked.expected_revision,1);assert.match(text(),/Affected runs.*run-1/);await unmount();
  for(const op of ops) {const old={capabilities:{...catalog.capabilities,operations:{...catalog.capabilities.operations}}};delete old.capabilities.operations[op];assert.match(operationReason(old,op),new RegExp(op.replace('.','\\.')));for(const other of ops.filter(x=>x!==op))assert.equal(operationReason(old,other),'');}
  reset({active:true});const old={capabilities:{...catalog.capabilities,operations:{...catalog.capabilities.operations,'budgets.remove':{contract_versions:[2],available:false,reason:'fixture_remove_unavailable'}}}};
  await mount(React.createElement(ProjectBudget,{client,catalog:old,scope:'old',projectId:'project-1',owner:true}));assert.ok(button('Remove limit').disabled);assert.ok(!button('Read project budget').disabled);assert.match(text(),/fixture_remove_unavailable/);await unmount();
  reset({active:true});await mount(React.createElement(OwnerSessionIndicator,{client,catalog,eligible:true}));assert.match(text(),/Confirmed as owner/);await click('Lock');assert.ok(!state.active);await unmount();
  reset({active:true});await mount(React.createElement(OwnerSessionIndicator,{client,catalog,eligible:true}));await click('Log out');assert.ok(!state.active);await unmount();
  console.log('owner controls UI: PASS (proof success/wrong code/throttle/timeout/expiry, no factor snapshots/replay, task/project approvals, 1000-call start/resume, exact integers, budget read/set/remove/in-use, list/revoke, per-op degradation, lock/logout)');
})().catch(e=>{console.error(e);process.exit(1);});
