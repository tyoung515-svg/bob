// S4g: real React components and typed client over an offline native seam.
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
for (const key of ['window','document','HTMLElement','Event','MouseEvent','localStorage']) global[key] = dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const { RunsPanel } = require('../src/portal/DurableRunsPanel.tsx');
const { RunFlags, RunJobs, FlagBadge } = require('../src/portal/RunFlags.tsx');
const { JobTimePicker } = require('../src/portal/JobTimePicker.tsx');
const { jobPolicy, jobTimeReason, jobGrantReason, operationReason, openFlags } = require('../src/portal/durable-runs.ts');
const ops = ['flags.list','flags.get','flags.resolve','grants.prepare','grants.issue','owner.begin','owner.confirm','owner.status'];
const catalog = {capabilities:{execution_v2:{contract_versions:[2],execution_enabled:true,job_time:{version:1,available:true,presets_seconds:[900,1800,3600],matcher_version:3,max_job_attempts_default:32}},operations:Object.fromEntries(ops.map(op=>[op,{contract_versions:[2],available:true,implemented:true,job_grants_available:true,job_grant_matcher_version:3}]))}};
const identity={sub:'owner',actor:'owner',owner:'owner',token_type:'human'};
const budget={start_calls:50,wall_mode:'none',wall_seconds:null,max_job_attempts:'9007199254740993',job_seconds_budget:'9223372036854775807'};
const coverage={profiles:{acceptance:'a'.repeat(64)},workspaces:{fixture:['.']},attachments:['attachment-1'],max_job_attempts:'9007199254740993',job_seconds_budget:'9223372036854775807',allow_cli:false};
const summary={id:'flag-1',run_id:'run-1',episode_id:'episode-1',ordinal:7,cause:'worker_outcome_unknown',phase:'checking',revision:'9007199254740993',status:'open',created_at:'2026-11-04',updated_at:'2026-11-04'};
const account={max_job_attempts:32,job_seconds_budget:7200,dispatched:'9007199254740993',held:1,completed:1,unknown:1,seconds_used:900,seconds_held:1800,cleanup_seconds:2,cli_usage_basis:'estimated_or_unknown'};
const run={id:'run-1',status:'paused',revision:3,flags:[summary,summary],job_account:account,job_waits:[{execution_id:'execution-2',selected_tier:1800,effective_seconds:1700,parent_execution_id:'execution-1',timeout_episode:1,reconciliation:'established',state:'queued',limiting_reason:'run_wall'}]};
const event={seq:1,kind:'timeout_continuation',at:'now',detail:{parent_execution_id:'execution-1',selected_tier:1800,continuation_requires_reconciliation:true}};
const fullFlag={...summary,scope:{original:'scope'},policy:{team_revision:4},budget:{requested:budget,job_account:account},stopped_context:{calls_used:3},usage_estimates:{billing_accurate:false},effects:'unknown',cleanup_pending:true,cleanup_source:'worker_job_receipts',live_job_cleanup:[{execution_id:'execution-1',outcome:'unknown',cleanup:'idle_proved',reconciliation:'unknown'}],safe_next_actions:['inspect','resume with guidance','authorize budget','cancel'],action_contract:{cancel:'runs.cancel'},episode:{audit:{cause:'transient failure',reviewer:'approved different model',independently_verified:false}},attempts:[{id:'attempt-1'}],output_links:[{digest:'checked-output'}],resolution_does_not_resume:true};
const team={id:'fixture-team',revision:1,runnable:true,topology:'four-step-readonly/v1',blocked_reasons:[]};
let hits=[],root,state={};
const fail=(code)=>{throw JSON.stringify({code,detail:code})};
const client=createPortalClient(async(cmd,args)=>{hits.push({cmd,args});switch(cmd){
case 'portal_teams_list_v2':return {items:[team],next_cursor:null};
case 'portal_runs_list_v2':return {items:[],next_cursor:null,source:'server',coverage:'authorized'};
case 'portal_owner_status_v2':return {owner_session:{active:state.active,expires_at:Date.now()/1000+200,allowed_operations:ops}};
case 'portal_owner_begin_v2':return {challenge_id:'challenge-1',expires_at:Date.now()/1000+120,summary:{operation:args.operation,resource:args.resource}};
case 'portal_owner_confirm_v2':assert.equal(field('Owner password').value,'');assert.equal(field('Owner authenticator code').value,'');state.active=true;return {owner_session:{active:true}};
case 'portal_grants_prepare_v2':return {proposal_id:'proposal-1',intent_id:'proposal-1',revision:1,digest:'server-digest',coverage:{request:args.request,snapshot:{models:'configured'},initiator:'original-scope',planned_run_id:run.id,matcher_version:3}};
case 'portal_grants_issue_v2':state.issued=args;return {grant:{id:'grant-1'}};
case 'portal_runs_start_v2':state.started=args;return {run:{...run,status:'accepted'},events:[],receipts:[],next_cursor:null};
case 'portal_flags_list_v2':assert.equal(args.run_id,run.id);return {items:[summary,summary],next_cursor:args.cursor?null:'flags-page-2',source:'server'};
case 'portal_flags_get_v2':return {flag:{...fullFlag,live_job_cleanup:args.job_cursor?[{execution_id:'execution-2',outcome:'timed_out',cleanup:'idle_proved',reconciliation:'established'}]:fullFlag.live_job_cleanup},run,events:args.after_event_seq?[{...event,seq:2}]:[event],next_cursor:args.after_event_seq?null:'9007199254740993',next_job_cursor:args.job_cursor?null:'execution-1'};
case 'portal_flags_resolve_v2':if(state.denied)fail('forbidden');if(state.unknown){state.unknown=false;fail('timeout_no_reply')}state.resolved=args;return {flag_id:args.flag_id,run_id:run.id,resolved:true,run_resumed:false,operation_id:'operation-1'};
default:throw Error('Unexpected '+cmd)}});
const text=()=>document.body.textContent;
const field=label=>document.querySelector('[aria-label="'+label+'"]');
const button=label=>[...document.querySelectorAll('button')].find(b=>b.textContent===label);
async function flush(fn=()=>{}){await act(async()=>{await fn();await new Promise(r=>setTimeout(r,0))})}
async function click(label){assert.ok(button(label),label);assert.ok(!button(label).disabled,label+' enabled');await flush(()=>button(label).click())}
async function input(label,value){const el=field(label);assert.ok(el,label);const proto=el.tagName==='SELECT'?window.HTMLSelectElement.prototype:el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;await flush(()=>{Object.getOwnPropertyDescriptor(proto,'value').set.call(el,value);el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input',{bubbles:true}))})}
async function mount(node){root=createRoot(document.getElementById('root'));await flush(()=>root.render(node))}
async function unmount(){await flush(()=>root.unmount())}
function reset(){hits=[];state={active:false};localStorage.clear()}
async function proof(label){await input('Owner password','synthetic-factor');await input('Owner authenticator code','456789');await click(label)}
function setup(seed={}){return React.createElement(RunsPanel,{client,catalog,scope:'jobs',runs:[],onRuns:()=>{},identity,seed:{team:team.id,task:'Review supplied work',budget,job_coverage:coverage,job_time:{proposed_seconds:1800,rationale:'Agent expects a longer check'},at:1,...seed}})}
(async()=>{
  assert.equal(jobTimeReason(catalog),'');assert.equal(jobGrantReason(catalog),'');assert.equal(openFlags([summary,summary]).length,1);
  reset();await mount(setup());assert.equal(field('Job time tier').value,'1800');assert.match(text(),/Agent expects a longer check/);
  await input('Job time tier','3600');await click('Start run');assert.equal(state.started,undefined);assert.equal(field('Approval job tier').value,'3600');await proof('Allow and start');
  assert.equal(state.issued.job_override_seconds,3600);assert.equal(state.issued.allow_extended_jobs,false);assert.equal(state.started.budget.max_job_attempts,'9007199254740993');assert.equal(state.started.budget.job_seconds_budget,'9223372036854775807');assert.equal(state.started.job_coverage.profiles.acceptance,'a'.repeat(64));assert.equal(state.started.intent_id,'proposal-1');await unmount();
  reset();await mount(setup({job_time:{proposed_seconds:7200,rationale:'Known long check'}}));await click('Start run');assert.equal(state.started,undefined);assert.equal(field('Allow extended jobs').checked,true);await proof('Allow and start');assert.equal(state.issued.job_override_seconds,7200);assert.equal(state.issued.allow_custom_job_limit,false);await unmount();
  for(const seconds of ['7200','1234']){reset();await mount(setup());await input('Job time tier','custom');await input('Custom job seconds',seconds);await click('Start run');assert.equal(state.started,undefined);assert.equal(field('Approval custom job seconds').value,seconds);if(seconds==='7200' && !field('Allow extended jobs').checked)await flush(()=>field('Allow extended jobs').click());await proof('Allow and start');assert.equal(state.issued.job_override_seconds,Number(seconds));assert.equal(state.issued.allow_custom_job_limit,true);assert.equal(state.issued.allow_extended_jobs,seconds==='7200');await unmount()}
  reset();await mount(React.createElement(RunJobs,{run,events:[event]}));assert.match(text(),/Jobs used: 9007199254740993/);assert.match(text(),/Job time used: 900/);assert.match(text(),/Current tier: 1800/);assert.match(text(),/timeout episode 1/);assert.match(text(),/continuation_requires_reconciliation/);await unmount();
  reset();await mount(React.createElement(FlagBadge,{run}));assert.equal(text(),'1 open flag');await unmount();
  reset();await mount(React.createElement(RunFlags,{client,catalog,run,scope:'flags',owner:true}));assert.equal(document.querySelectorAll('article').length,1,'duplicate flag deliveries coalesce');await click('More flags');await click('Inspect flag flag-1');
  for(const value of ['worker_outcome_unknown','Effects: unknown','Attempt trail','Audit and failure episode','Budgets, calls and job accounting','Live job cleanup','Safe next actions','checked-output'])assert.ok(text().includes(value),value);
  await click('More flag events');assert.equal(hits.at(-1).args.after_event_seq,'9007199254740993');await click('More job cleanup');assert.match(text(),/execution-2/);
  await input('Flag decision','resume_with_guidance');await input('Flag guidance','Inspected; authority and reconciliation remain separate.');await click('Resolve flag');assert.ok(field("Confirm it's you"));assert.equal(state.resolved,undefined);await proof('Confirm and continue');
  assert.equal(state.resolved.expected_revision,'9007199254740993');assert.match(text(),/The run was not resumed/);assert.ok(!hits.some(h=>/resume|cancel|grants_issue/.test(h.cmd)),'resolve invokes no run-control operation');await unmount();
  reset();await mount(React.createElement(RunFlags,{client,catalog,run,scope:'agent',owner:false}));await click('Inspect flag flag-1');assert.equal(button('Resolve flag'),undefined);assert.equal(field('Flag guidance'),null);state.denied=true;await assert.rejects(client.flagResolve({contract_version:2,flag_id:summary.id,expected_revision:summary.revision,request_key:'agent-denial',decision:'inspected',guidance:'not authorized'}),/forbidden/);await unmount();
  reset();state.active=true;state.denied=true;await mount(React.createElement(RunFlags,{client,catalog,run,scope:'denied',owner:true}));await click('Inspect flag flag-1');await input('Flag guidance','Inspected');await click('Resolve flag');assert.match(text(),/forbidden/);assert.equal(button('Reconcile resolution'),undefined);await unmount();
  reset();state.active=true;state.unknown=true;const flags=()=>React.createElement(RunFlags,{client,catalog,run,scope:'unknown',owner:true});await mount(flags());await click('Inspect flag flag-1');await input('Flag guidance','Inspected');await click('Resolve flag');const original=hits.filter(h=>h.cmd==='portal_flags_resolve_v2').at(-1).args;await unmount();await mount(flags());await click('Inspect flag flag-1');await click('Reconcile resolution');assert.deepEqual(state.resolved,original);await unmount();
  for(const missing of ['flags.list','flags.get','flags.resolve']){reset();const old={capabilities:{...catalog.capabilities,operations:{...catalog.capabilities.operations}}};delete old.capabilities.operations[missing];assert.ok(operationReason(old,missing));for(const op of ops.filter(x=>x!==missing))assert.equal(operationReason(old,op),'');await mount(React.createElement(RunFlags,{client,catalog:old,run,scope:'old',owner:true}));if(missing==='flags.list'){assert.ok(button('Refresh flags').disabled);assert.ok(!button('Inspect flag flag-1').disabled)}else if(missing==='flags.get'){assert.ok(button('Inspect flag flag-1').disabled);assert.ok(!button('Refresh flags').disabled)}else{await click('Inspect flag flag-1');assert.ok(button('Resolve flag').closest('fieldset').disabled)}await unmount()}
  reset();await mount(React.createElement(JobTimePicker,{catalog,budget,proposal:{proposed_seconds:1800,rationale:'Agent proposal'},onBudget:()=>{},onPolicy:()=>{},owner:false}));assert.equal(field('Job time tier'),null);assert.equal(field('Maximum job attempts'),null);assert.match(text(),/Agent proposal/);await unmount();
  reset();const old={capabilities:{...catalog.capabilities,execution_v2:{contract_versions:[2]}}};await mount(React.createElement(JobTimePicker,{catalog:old,budget,onBudget:()=>{},onPolicy:()=>{},owner:true}));assert.match(text(),/coming with build jobs/);assert.equal(field('Job time tier'),null);assert.equal(operationReason(old,'flags.get'),'');await unmount();
  console.log('S4g ladder/flags UI: PASS (proposed tier, override, extended/custom grants, exact budgets/coverage, account, timeout continuation, list/detail/paging, owner proof, agent denial, no resume, unknown reconciliation, per-operation compatibility)');
})().catch(e=>{console.error(e);process.exit(1)});
