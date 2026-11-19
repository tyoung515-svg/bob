// AU3: standalone Needs you page, using the real app and existing operations.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>',{url:'http://localhost',pretendToBeVisual:true});
for (const k of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage','MutationObserver']) global[k] = dom.window[k];
global.IS_REACT_ACT_ENVIRONMENT = true;
const listeners = new Set(), events = require.resolve('@tauri-apps/api/event');
require.cache[events] = {id:events,filename:events,loaded:true,exports:{listen:async(_,fn)=>{listeners.add(fn);return()=>listeners.delete(fn);}}};
const React = require('react'), {act} = React, {createRoot} = require('react-dom/client');
const PortalApp = require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE} = require('../src/portal/SettingsView.tsx');
const realTimeout = global.setTimeout, realClear = global.clearTimeout, realInterval = global.setInterval, realClearInterval = global.clearInterval, realNow = Date.now;
let now = Date.parse('2026-11-17T12:00:00Z'); Date.now = () => now;
const timers = new Map();
function timeout(fn,ms=0,...args) {const id={due:now+Math.max(1,ms),ms,interval:false};timers.set(id,()=>fn(...args));return id;}
function interval(fn,ms=0,...args) {const id={due:now+Math.max(1,ms),ms,interval:true};timers.set(id,()=>fn(...args));return id;}
global.setTimeout=window.setTimeout=timeout; global.clearTimeout=window.clearTimeout=id=>timers.delete(id);
global.setInterval=window.setInterval=interval; global.clearInterval=window.clearInterval=id=>timers.delete(id);
global.requestAnimationFrame=fn=>timeout(fn,16);
const clone=x=>JSON.parse(JSON.stringify(x));
const faces=['assistant','builder','fixer'].map(id=>({id,name:id[0].toUpperCase()+id.slice(1)}));
const base=path.resolve(__dirname,'../../../../runs/golive/workflow-fixtures');
const fixture=name=>JSON.parse(fs.readFileSync(path.join(base,name+'.result.json'),'utf8'));
let root,calls,operations,approvals,queue,proposals,runs,failures,history,serial=0,experience='simple',againRefusal=false,cap=false;
const sessions=[{id:'a',face_id:'assistant',title:'Plan the garden'},{id:'b',face_id:'builder',title:'Build the garden'}];
const count=cmd=>calls.filter(c=>c.cmd===cmd).length;
const flush=async(fn=()=>{})=>act(async()=>{await fn();await new Promise(r=>realTimeout(r,0));});
async function advance(ms) {
  const end=now+ms;let n=0;
  for (;;) {const next=[...timers].sort((a,b)=>a[0].due-b[0].due)[0];if(!next||next[0].due>end)break;
    assert.ok(++n<10000);const [id,fn]=next;now=id.due;if(id.interval)id.due+=Math.max(1,id.ms);else timers.delete(id);await flush(fn);
  } now=end;await flush();
}
const button=(label,scope=document)=>[...scope.querySelectorAll('button')].find(b=>b.textContent.trim()===label);
const needs=()=>document.querySelector('nav[aria-label="Bob areas"] button[title^="Needs you"]');
const page=()=>document.querySelector('.needs-page');
const cards=()=>[...page().querySelectorAll('.need-card')];
async function click(node) {assert.ok(node,'click target');await flush(()=>node.click());await flush();}
async function emit(event,data) {await flush(()=>{for(const fn of listeners)fn({payload:JSON.stringify({event,data})});});}
async function mount() {root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));await flush();}
async function unmount() {if(root){await flush(()=>root.unmount());root=null;}}
function reset(mode='simple') {
  experience=mode;localStorage.clear();timers.clear();calls=[];operations={};approvals=[];queue={requests:[],outcomes:{}};proposals=[];runs=[];failures={};history={a:[],b:[]};againRefusal=false;cap=false;
  localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));
}
function proposal(id,state='proposed') {
  const p=clone(fixture('propose').proposal);
  return {...p,id,kind:'workflow_proposal',origin_conversation_id:'a',created_at:new Date(now-10*60000).toISOString(),expires_at:new Date(now+(state==='expired'?-1:5)*60000).toISOString(),state,proposed_by:{actor:'assistant',token_type:'human',via:'assistant'},coverage:{...p.coverage,inputs:{task:'Make a garden plan.'}}};
}
function workflowSupport(earlier=false,again=earlier) {
  operations=Object.fromEntries(['workflows.proposals','workflows.catalog','workflows.runs','workflows.run'].map(op=>[op,{available:true,implemented:true,contract_versions:[1]}]));
  if(earlier)operations['workflows.proposals'].include_expired=true;
  if(again)operations['workflows.propose_again']={available:true,implemented:true,contract_versions:[1]};
}
async function invoke(cmd,args={}) {
  calls.push({cmd,args:clone(args)});
  if(failures[cmd])throw JSON.stringify({code:'unavailable',detail:failures[cmd]});
  switch(cmd) {
    case 'portal_connection_get':return {connection_configured:true};
    case 'portal_hello':return {identity:{sub:'au3-'+serial,gateway:'fixture',faces,may_touch:[],may_not_touch:[]},operations,server:{},gaps:['Fixture gap text unchanged.']};
    case 'portal_catalog':return {faces,models:[],backends:[],capabilities:{actions:[],warnings:[],operations}};
    case 'portal_conversations':return {items:clone(sessions),limit:50,offset:0};
    case 'portal_conversation_open':return {conversation:sessions.find(c=>c.id===args.id),messages:clone(history[args.id]),has_more:false};
    case 'portal_messages_more':return {items:clone(history[args.id]),has_more:false};
    case 'portal_pins':return {};
    case 'portal_approvals':return {items:clone(approvals)};
    case 'portal_runs_list':return {items:[]};
    case 'portal_teams_list_v2':return {items:[],next_cursor:null};
    case 'calendar_read':return [];
    case 'pc_poll':return clone(queue);
    case 'runtime_repair_status':return {repairable:true,observation:{state:'healthy',observed:'Healthy'}};
    case 'portal_answer':return {resolved:true,question_id:args.question_id,delivered_to_turn:true};
    case 'portal_builder_mode_get_v2':return {conversation_id:args.conversation_id,project_id:null,mode:'approve',revision:0,expires_at:null,network:{allowlist:[],project_extra:[]},approval_records:[],approvals:[]};
    case 'portal_workflow_catalog_v1':return {...clone(fixture('catalog')),available:true,reasons:[]};
    case 'portal_workflow_proposals_v1':return {items:clone(proposals.filter(p=>p.state!=='expired'||args.include_expired)),next_cursor:null};
    case 'portal_workflow_proposal_v1':return {proposal:clone(proposals.find(p=>p.id===args.proposal_id))};
    case 'portal_workflow_runs_v1':return {items:clone(runs),next_cursor:null};
    case 'portal_workflow_run_v1':return {run:clone(runs.find(r=>r.id===args.run_id))};
    case 'portal_workflow_watch_v1':return {run_id:args.run_id,watching:args.watch};
    case 'portal_workflow_propose_again_v1':
      if(againRefusal)throw JSON.stringify({code:'workflow_deleted',detail:'This workflow was deleted.'});
      const fresh=proposal('fresh-'+(++serial));proposals.push(fresh);return {proposal:clone(fresh)};
    default:throw Error('Unexpected AU3 fixture '+cmd);
  }
}
function assertPage() {
  assert.ok(page());assert.equal(document.querySelector('.agent-row'),null);assert.equal(document.getElementById('composer'),null);assert.equal(document.querySelector('[aria-label="Conversation"]'),null);
  assert.equal(needs().getAttribute('aria-current'),'page');assert.equal(needs().getAttribute('aria-pressed'),'true');
  assert.doesNotMatch(page().textContent,/Back to activity|Gaps ·|Workflows needing you|Questions needing you|Approvals needing you|PC actions needing you/);
}
async function emptyChecks() {
  for(const mode of ['simple','pro']) {
    reset(mode);await mount();await click(needs());assertPage();
    assert.equal(page().querySelector('h1').textContent,'Nothing needs you right now.');
    assert.match(page().textContent,/not started within 15 minutes runs out/);
    assert.match(page().textContent,/Last check on opening or with Check again:/);assert.equal(page().querySelectorAll('h2').length,0);
    assert.equal(needs().querySelector('.rail-badge'),null);assert.equal(needs().getAttribute('aria-label'),'Needs you');
    const before=calls.length;await advance(60000);
    assert.equal(calls.slice(before).filter(c=>c.cmd.startsWith('portal_')).length,0,'no new gateway polling');
    const counts=['portal_hello','portal_conversations','portal_approvals','pc_poll'].map(count);
    await click(button('Check again'));['portal_hello','portal_conversations','portal_approvals','pc_poll'].forEach((cmd,i)=>assert.equal(count(cmd),counts[i]+1));
    await click(button('Settings'));assert.equal(!![...document.querySelectorAll('summary')].find(s=>s.textContent==='Developer notes: known gaps'),mode==='pro');
    if(mode==='pro') {const notes=[...document.querySelectorAll('summary')].find(s=>s.textContent==='Developer notes: known gaps').parentElement;assert.equal(notes.open,false);assert.match(notes.textContent,/Fixture gap text unchanged/);assert.ok(button('Ask to fix',notes));}
    await unmount();
  }
}
async function questionChecks() {
  reset();await mount();
  await emit('question',{conversation_id:'a',question_id:'q1',question:'Keep making the garden plan?',choices:['Keep going','Stop here'],origin:'step_limit',created_at:new Date(now-120000).toISOString()});
  await click(needs());assertPage();assert.equal(page().querySelector('h1').textContent,'1 thing needs you');
  assert.match(needs().getAttribute('aria-label'),/1 waiting/);assert.equal(needs().querySelector('.rail-badge').dataset.count,'1');
  const card=cards()[0];assert.match(card.textContent,/BoB asks/);assert.match(card.textContent,/Assistant · Plan the garden/);assert.match(card.textContent,/Keep making the garden plan/);assert.match(card.textContent,/Waiting for 2 minutes/);
  await flush(()=>{button('Keep going',card).click();button('Keep going',card).click();});
  assert.equal(count('portal_answer'),1);assert.deepEqual(calls.find(c=>c.cmd==='portal_answer').args,{question_id:'q1',answer:'Keep going'});
  assert.equal(cards().length,0);assert.equal(page().querySelector('h1').textContent,'Nothing needs you right now.');assert.equal(needs().querySelector('.rail-badge'),null);
  await unmount();
}
async function mixedChecks() {
  reset('pro');
  approvals=[{id:'approval-1',conversation_id:'b',action_type:'Write the garden plan.',details:{path:'garden.txt',diff:'+Plant roses'},status:'pending',created_at:new Date(now-600000).toISOString(),user_id:'au3'}];
  queue.requests=[{request:{id:'pc1',task_id:'t',agent:'Builder',conversation_id:'b',operation:'pc.read',args:{path:'C:/garden.txt'},created_at:now/1000-300,expires_at:now/1000+600},mode:'approve',root:null,until:null,protected_reason:null,running:false}];
  await mount();await emit('question',{conversation_id:'a',question_id:'q2',question:'Which flowers?',choices:['Roses','Lilies'],created_at:new Date(now-60000).toISOString()});
  await click(needs());assertPage();assert.equal(page().querySelector('h1').textContent,'3 things need you');assert.equal(needs().querySelector('.rail-badge').dataset.count,'3');
  assert.deepEqual(cards().map(c=>c.dataset.needId),['approval:approval-1','pc:pc1','question:q2']);
  for(const card of cards())assert.match(card.textContent,/Assistant · Plan the garden|Builder · Build the garden/);
  assert.match(cards()[0].textContent,/Write the garden plan.*Waiting for 10 minutes/s);assert.match(cards()[1].textContent,/Read a file on this PC.*garden.txt.*Waiting for 5 minutes/s);
  assert.ok(button('Open conversation',cards()[0]));assert.ok(cards()[0].querySelector('summary'));assert.ok(button('Ask to fix',cards()[0]));
  for(const card of cards().slice(0,2))assert.ok(![...card.querySelectorAll('button')].some(b=>/Approve|Reject|Allow|Deny|Set PC/.test(b.textContent)));
  await click(button('Open conversation',cards()[0]));assert.equal(calls.filter(c=>c.cmd==='portal_conversation_open').at(-1).args.id,'b');await advance(16);assert.equal(document.activeElement.id,'approval-approval-1');
  await click(needs());await click(button('Open conversation',cards()[1]));assert.equal(calls.filter(c=>c.cmd==='portal_conversation_open').at(-1).args.id,'b');await advance(16);assert.equal(document.activeElement.id,'pc-pc1');
  assert.equal(count('portal_approval_decide'),0);assert.equal(count('pc_decide'),0);
  await unmount();
}
async function failureChecks() {
  reset();failures.portal_approvals='Cannot read approvals.';await mount();await click(needs());
  assert.match(page().textContent,/Approvals could not be checked\./);assert.doesNotMatch(page().querySelector('h1').textContent,/Nothing needs/);
  delete failures.portal_approvals;await click(button('Check again'));assert.equal(page().querySelector('h1').textContent,'Nothing needs you right now.');
  failures.pc_poll='PC is unavailable.';failures.portal_conversations='Titles unavailable.';failures.portal_hello='Connection unavailable.';
  await click(button('Check again'));assert.match(page().textContent,/Actions on this PC could not be checked/);assert.match(page().textContent,/Conversation titles could not be checked/);assert.match(page().textContent,/Questions could not be checked/);await unmount();
  reset();workflowSupport();failures.portal_workflow_proposals_v1='Proposals unavailable.';failures.portal_workflow_runs_v1='Runs unavailable.';
  await mount();await click(needs());assert.match(page().textContent,/Workflow proposals could not be checked/);assert.match(page().textContent,/Workflow runs could not be checked/);assert.doesNotMatch(page().querySelector('h1').textContent,/Nothing needs/);await unmount();
}
async function workflowChecks() {
  reset();workflowSupport();proposals=[proposal('p1')];const run={...clone(fixture('run-accepted').run),id:'run1',origin_conversation_id:'b',status:'paused',name:'Paused garden plan',updated_at:new Date(now-180000).toISOString(),needs_you:[]};runs=[run];
  await mount();await click(needs());assertPage();assert.equal(cards().length,2);
  assert.match(cards().find(c=>c.dataset.needId==='proposal:p1').textContent,/A workflow waiting to start.*Assistant · Plan the garden.*Make a garden plan.*Waiting for 10 minutes/s);
  assert.match(cards().find(c=>c.dataset.needId==='workflow:run1').textContent,/A workflow that is paused.*Builder · Build the garden.*Paused garden plan.*Waiting for 3 minutes/s);
  assert.ok(button('Start',page()));assert.ok(button('Resume run',page()));assert.equal(count('portal_workflow_start_v1'),0);assert.equal(count('portal_workflow_resume_v1'),0);
  const before=['portal_workflow_proposals_v1','portal_workflow_runs_v1','portal_workflow_run_v1'].map(count);await click(button('Check again'));
  ['portal_workflow_proposals_v1','portal_workflow_runs_v1','portal_workflow_run_v1'].forEach((cmd,i)=>assert.equal(count(cmd),before[i]+1));
  // An observed run update removes the settled card without an owner refresh.
  runs=[{...run,status:'failed',reason:'The provider returned a different model from the frozen binding.',revision:3,event_seq:2,updated_at:new Date(now).toISOString()}];
  await emit('workflow_event',{run_id:'run1',seq:2,revision:3,at:new Date(now).toISOString(),kind:'run_state',payload:{status:'failed',reason:runs[0].reason}});
  assert.equal(cards().length,1);assert.equal(needs().querySelector('.rail-badge').dataset.count,'1');
  assert.match(page().querySelector('.earlier-run').textContent,/Paused garden plan.*This run failed.*different model/s);
  await advance(5*60000);assert.equal(cards().length,0,'local proposal expiry removes its waiting card');assert.ok(page().querySelector('.earlier-run'),'the ended run remains after waiting items settle');await unmount();
  for(const [historyCapability,againCapability] of [[false,false],[true,false],[true,true]]) {
    reset();workflowSupport(historyCapability,againCapability);proposals=[proposal('expired','expired')];await mount();await click(needs());
    assert.equal(!!page().querySelector('[aria-label="Earlier"]'),historyCapability);assert.equal(!!button('Ask again',page()),againCapability);
    assert.equal(calls.find(c=>c.cmd==='portal_workflow_proposals_v1').args.include_expired,historyCapability?true:undefined);
    if(historyCapability) {assert.match(page().textContent,/This proposal ran out after 15 minutes without being started\./);assert.match(page().textContent,/Proposed by the Assistant on/);}
    if(againCapability) {
      againRefusal=true;await click(button('Ask again',page()));assert.match(page().textContent,/This workflow was deleted\./);assert.equal(cards().length,0);againRefusal=false;
      await flush(()=>{button('Ask again',page()).click();button('Ask again',page()).click();});
      assert.equal(count('portal_workflow_propose_again_v1'),2);assert.equal(cards().length,1);assert.ok(button('Start',page()));assert.equal(count('portal_workflow_start_v1'),0);
      assert.deepEqual(calls.filter(c=>c.cmd==='portal_workflow_propose_again_v1').at(-1).args,{contract_version:1,proposal_id:'expired'});
    }
    await unmount();
  }
}
// AU4 extends this app-level harness already wired into test:harness.
async function outcomeChecks() {
  reset();workflowSupport();
  const makeRun=(id,status,age,reason=null)=>({...clone(fixture('run-accepted').run),id,status,name:'Garden '+id,reason,created_at:new Date(now-age-60000).toISOString(),updated_at:new Date(now-age).toISOString(),needs_you:[],grant:{id:'g',active:false,expires_at:new Date(now-1000).toISOString()}});
  runs=[makeRun('failed','failed',1000,'The provider returned a different model from the frozen binding.'),makeRun('stopped','stopped',2000,'You stopped the run.'),makeRun('interrupted','interrupted',3000,'Recovery is no longer available.'),makeRun('completed','completed',4000),makeRun('budget','over_budget',5000),makeRun('cancelled','cancelled',6000),makeRun('old','failed',7*86400000+1),makeRun('future','completed',-86400000)];
  await mount();const discovery=count('portal_workflow_runs_v1'),reads=count('portal_workflow_run_v1');await click(needs());
  const earlier=()=>page().querySelector('[aria-label="Earlier"]'),rows=()=>[...earlier().querySelectorAll('.earlier-run')];
  assert.equal(page().querySelector('h1').textContent,'Nothing needs you right now.');assert.equal(needs().querySelector('.rail-badge'),null);
  assert.deepEqual(rows().map(r=>r.dataset.runId),['failed','stopped','interrupted','completed','budget']);
  assert.match(rows()[0].textContent,/This run failed.*The provider returned a different model from the frozen binding.*Ended on/s);
  assert.match(rows()[1].textContent,/This run was stopped.*You stopped the run/s);assert.match(rows()[2].textContent,/interrupted and cannot be recovered/);assert.match(rows()[3].textContent,/This run completed/);
  assert.ok(rows()[0].classList.contains('earlier-problem'));assert.ok(rows()[1].classList.contains('earlier-problem'));assert.ok(rows()[3].classList.contains('earlier-completed'));
  assert.equal(rows()[0].querySelector('time').dateTime,runs[0].updated_at);
  await click(button('Show all',earlier()));assert.deepEqual(rows().map(r=>r.dataset.runId),['failed','stopped','interrupted','completed','budget','cancelled']);
  await click(button('Show fewer',earlier()));assert.equal(rows().length,5);
  assert.equal(count('portal_workflow_runs_v1'),discovery);assert.equal(count('portal_workflow_run_v1'),reads,'Earlier consumes existing summaries only');
  const before=calls.length;await advance(60000);assert.equal(calls.slice(before).filter(c=>c.cmd.startsWith('portal_')).length,0);
  await click(button('Open the run',rows()[0]));assert.ok(earlier().querySelector('.workflow-run'));assert.match(earlier().querySelector('.workflow-run').textContent,/Garden failed/);assert.equal(count('portal_workflow_run_v1'),reads+1);
  await click(button('Close the run view'));assert.equal(earlier().querySelector('.workflow-run'),null);await unmount();
  // A recoverable interruption remains waiting and stays out of Earlier.
  reset();workflowSupport();runs=[{...makeRun('recover','interrupted',1000),grant:{id:'g',active:true,expires_at:new Date(now+60000).toISOString()}}];await mount();await click(needs());assert.equal(cards().length,1);assert.ok(!page().querySelector('[aria-label="Earlier"]'));await unmount();
  // Every additional terminal outcome has a full sentence, too.
  reset();workflowSupport();runs=['expired','blocked','unknown'].map((s,i)=>makeRun(s,s,1000+i));await mount();await click(needs());assert.equal(page().querySelectorAll('.earlier-run').length,3);assert.match(page().textContent,/permission or budget expired/);assert.match(page().textContent,/ended because it was blocked/);assert.match(page().textContent,/ended with an unknown outcome/);await unmount();
}
async function wordingChecks() {
  reset();workflowSupport(true);const library={...proposal('library'),origin_conversation_id:null,proposed_by:{actor:'owner',via:'owner',token_type:'human'}};proposals=[library];
  await mount();await click(needs());const card=cards()[0];assert.match(card.textContent,/From the Library/);assert.doesNotMatch(card.textContent,/you ·|title was not reported/);assert.match(card.textContent,/Proposed by you/);assert.equal(card.textContent.split('Make a garden plan.').length-1,1);
  const checkLine=page().querySelector('header > p').textContent;assert.match(checkLine,/Last check on opening or with Check again:.*Live updates also appear here/s);assert.doesNotMatch(checkLine,/Last checked at/);
  await advance(1000);await emit('question',{conversation_id:'a',question_id:'fresh-question',question:'An arriving question.',choices:['Yes']});assert.equal(page().querySelector('header > p').textContent,checkLine);assert.equal(cards().length,2);await unmount();
  reset();workflowSupport();proposals=[{...proposal('unknown-conversation'),origin_conversation_id:'unknown'}];await mount();await click(needs());assert.match(cards()[0].textContent,/The conversation title was not reported/);assert.doesNotMatch(cards()[0].textContent,/From the Library/);await unmount();
  reset();workflowSupport(true);proposals=[{...proposal('renewed','expired'),proposed_again_ids:['other']},proposal('previous','expired'),{...proposal('fresh'),previous_proposal_id:'previous'},{...proposal('started','expired'),run_id:'a-run'},proposal('eligible','expired')];
  proposals.forEach(p=>p.coverage.definition.name=p.id);await mount();await click(needs());const earlier=page().querySelector('[aria-label="Earlier"]');assert.deepEqual([...earlier.querySelectorAll('h3')].map(h=>h.textContent),['eligible']);
  await click(button('Ask again',earlier));assert.ok(!page().querySelector('[aria-label="Earlier"]'),'successful renewal immediately removes the old expiry even on older servers');assert.equal(count('portal_workflow_start_v1'),0);await unmount();
}
async function continueChecks() {
  reset();history.a=[{id:'old',role:'assistant',content:'An unfinished garden plan.',metadata:{stopped_reason:'tool_round_limit'}},{id:'u2',role:'user',content:'What next?'},{id:'latest',role:'assistant',content:'Another unfinished reply.',metadata:{stopped_reason:'tool_round_limit'}}];
  history.a.forEach((m,i)=>m.created_at=new Date(now-60000+i*1000).toISOString());
  await mount();await click([...document.querySelectorAll('.agent-row')].find(b=>b.textContent.includes('Assistant')));
  const statuses=[...document.querySelectorAll('.reply-status')];assert.equal(statuses.length,2);assert.equal(statuses[0].querySelector('button'),null);assert.match(statuses[0].textContent,/Not finished/);assert.ok(button('Continue',statuses[1]));
  history.a.push({id:'new-user',role:'user',content:'One more question.',created_at:new Date(now).toISOString()});
  await click([...document.querySelectorAll('.agent-row')].find(b=>b.textContent.includes('Assistant')));assert.equal(button('Continue'),undefined);await unmount();
}
(async()=>{
  await emptyChecks();await questionChecks();await mixedChecks();await failureChecks();await workflowChecks();await outcomeChecks();await wordingChecks();await continueChecks();
  console.log('AU3/AU4: PASS (standalone page, empty/singular/plural, all card metadata/order, ended-run outcomes/reasons/order/cap/open/event updates, Library wording and task once, renewal/start filtering, truthful check line, exact answer once/removal, conversation links/anchors, read failures/recheck, navigation count, capability-gated history/retry/refusal, no implicit starts, Settings gaps and latest-only Continue).');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await unmount();global.setTimeout=realTimeout;global.clearTimeout=realClear;global.setInterval=realInterval;global.clearInterval=realClearInterval;Date.now=realNow;dom.window.close();});
