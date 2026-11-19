// WA1: real workflow components/client/store against WB1 golden wire fixtures.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost',pretendToBeVisual:true});
for(const k of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage'])global[k]=dom.window[k];
global.requestAnimationFrame=fn=>setTimeout(fn,0);global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const {WorkflowContext,WorkflowProposalCard,WorkflowRunView,WorkflowNeeds,WorkflowRuns}=require('../src/portal/WorkflowViews.tsx');
const {WorkflowStore}=require('../src/portal/workflow-store.ts');
const {createPortalClient}=require('../src/portal/portal-client.ts');
const {ExperienceContext}=require('../src/portal/experience.ts');
const {validateAction,ActionCardsContext,ActionCard}=require('../src/portal/ActionCard.tsx');
const listeners=new Set(),events=require.resolve('@tauri-apps/api/event');
require.cache[events]={id:events,filename:events,loaded:true,exports:{listen:async(_,fn)=>{listeners.add(fn);return()=>listeners.delete(fn);}}};
const PortalApp=require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const fixtureDir=path.resolve(__dirname,'../../../../runs/golive/workflow-fixtures');
const fixture=(name,part='result')=>JSON.parse(fs.readFileSync(path.join(fixtureDir,name+'.'+part+'.json'),'utf8'));
const clone=x=>JSON.parse(JSON.stringify(x));
const realNow=Date.now;Date.now=()=>Date.parse('2026-11-16T12:01:00Z');
let root,store,client,calls,proposal,run,available,faults,fresh,opened,catalog,experience='simple',serial=0;
const count=cmd=>calls.filter(c=>c.cmd===cmd).length;
const flush=async(fn=()=>{})=>act(async()=>{await fn();await new Promise(r=>setTimeout(r,0));});
const button=(label,scope=document)=>[...scope.querySelectorAll('button')].find(b=>b.textContent===label);
async function click(n){assert.ok(n,'click target');await flush(()=>n.click());}
async function type(n,value){await flush(()=>{Object.getOwnPropertyDescriptor(Object.getPrototypeOf(n),'value').set.call(n,value);n.dispatchEvent(new Event(n.tagName==='SELECT'?'change':'input',{bubbles:true}));});}
const source=p=>({conversationId:'assistant-a',name:'the Assistant',revision:p.revision,digest:p.digest});
async function invoke(cmd,args){calls.push({cmd,args:clone(args??{})});if(faults[cmd]?.length)throw JSON.stringify(faults[cmd].shift());
  switch(cmd){
    case 'portal_workflow_catalog_v1':return {...fixture('catalog'),available,reasons:available?[]:fixture('catalog').reasons};
    case 'portal_workflow_proposal_v1':return {proposal:clone(proposal)};
    case 'portal_workflow_propose_v1':proposal={...clone(proposal),id:'mode-'+(++serial),coverage:{...clone(proposal.coverage),run_mode:args.run_mode}};return {proposal:clone(proposal)};
    case 'portal_workflow_start_v1':return {run:clone(run),grant_id:run.grant.id};
    case 'portal_workflow_run_v1':return {run:clone(run)};
    case 'portal_workflow_runs_v1':return {items:[],next_cursor:null};
    case 'portal_workflow_watch_v1':return {run_id:args.run_id,watching:args.watch};
    case 'portal_workflow_pause_v1':run={...run,status:'paused',revision:(BigInt(run.revision)+1n).toString()};return {run:clone(run)};
    case 'portal_workflow_resume_v1':run={...run,status:'queued',revision:(BigInt(run.revision)+1n).toString(),needs_you:[]};return {run:clone(run)};
    case 'portal_workflow_stop_v1':run={...clone(fixture('stop').run),id:run.id,origin_conversation_id:run.origin_conversation_id,revision:(BigInt(run.revision)+1n).toString()};return {run:clone(run)};
    case 'portal_workflow_gate_v1':run={...run,revision:(BigInt(run.revision)+1n).toString(),needs_you:[]};return {run:clone(run)};
    case 'portal_workflow_message_v1':return {message_id:'message',state:'queued'};
    case 'portal_owner_status_v2':return {owner_session:{active:true,expires_at:Date.now()/1000+300,allowed_operations:['workflows.grant_start','workflows.resume','workflows.gate','builder.approve']}};
    case 'portal_owner_begin_v2':return {challenge_id:'owner-click',confirmation_mode:'signed_in_user',summary:{},expires_at:Date.now()/1000+300};
    case 'portal_owner_confirm_v2':return {owner_session:{active:true}};
    case 'portal_answer':return {resolved:true,question_id:args.question_id};
    case 'portal_builder_mode_get_v2':return {conversation_id:args.conversation_id,project_id:null,mode:'approve',expires_at:null,network:{allowlist:[],project_extra:[]},revision:1,approvals:[approval],approval_records:[approval]};
    case 'portal_builder_approval_get_v2':return clone(approval);
    case 'portal_builder_approve_v2':return {approval_id:approval.approval_id,state:'denied'};
    default:throw new Error('Unexpected WA1 call '+cmd);
  }
}
const approval={schema_version:1,approval_id:'approve-1',conversation_id:'builder-stage',kind:'write',mode:'approve',state:'pending',summary:'Write the requested file.',expires_at:Date.parse('2026-11-16T12:10:00Z')/1000,detail:{path:'index.html',diff:'+ approved change'}};
function reset(name='propose-assistant'){
  calls=[];faults={};fresh=[];opened=[];proposal=fixture(name).proposal;run=fixture('run-accepted').run;available=true;localStorage.clear();
  catalog={faces:[],models:[],backends:[],capabilities:{operations:Object.fromEntries(['workflows.grant_start','workflows.resume','workflows.gate','owner.status','owner.begin','owner.confirm','builder.approve'].map(op=>[op,{contract_versions:[op.startsWith('workflows.')?1:2],available:true,implemented:true}]))}};
  client=createPortalClient(invoke);store=new WorkflowStore(client,'wa1-'+(++serial));
}
async function mount(child){store.activate();root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(ExperienceContext.Provider,{value:experience},React.createElement(WorkflowContext.Provider,{value:{store,catalog,onOpen:id=>opened.push(id),onFresh:(id,text)=>fresh.push({id,text}),pc:()=>React.createElement('p',null,'Review the PC request.')}},child))));await flush();}
async function unmount(){if(root){await flush(()=>root.unmount());root=null;}if(store)store.dispose();await flush();}
const card=()=>React.createElement(WorkflowProposalCard,{id:proposal.id,source:source(proposal)});
const text=()=>document.getElementById('root').textContent;
async function proposalChecks(){
  for(const name of ['propose-premade-plan-build-review-fix','propose-premade-council-build','propose-premade-sprint-board']){
    reset(name);await mount(card());assert.ok(text().includes(proposal.coverage.definition.name));
    for(const b of Object.values(proposal.coverage.bindings))assert.ok(text().includes(b.model));
    assert.match(text(),/No deterministic checks will run/);assert.ok(text().includes(proposal.coverage.budget.calls+' model calls'));assert.match(text(),/Review policy/);
    assert.equal(document.querySelector('select').value,'auto');assert.equal(count('portal_workflow_start_v1'),0);await unmount();
  }
  reset();available=false;await mount(card());assert.ok(button('Start').disabled);assert.match(text(),/cannot run workflows yet/);await unmount();
  reset('propose-same-model');await mount(card());assert.equal(button('Start'),undefined);assert.ok(button('Start anyway with same-model review'));await click(button('Start anyway with same-model review'));assert.equal(calls.find(c=>c.cmd==='portal_workflow_start_v1').args.allow_same_model_review,true);await unmount();
  reset();await mount(card());await type(document.querySelector('select'),'approve');assert.equal(count('portal_workflow_propose_v1'),1);assert.match(text(),/Each change will ask/);assert.equal(count('portal_workflow_start_v1'),0);await unmount();
  reset();proposal.coverage.workspace={create:'Garden'};proposal.coverage.project_id=null;await mount(card());assert.match(text(),/new workspace named Garden/);await unmount();
  reset();const oldSource=source(proposal);proposal.digest='b'.repeat(64);await mount(React.createElement(WorkflowProposalCard,{id:proposal.id,source:oldSource}));assert.match(text(),/out of date/);assert.equal(button('Start'),undefined);await click(button('Ask the agent for a fresh proposal'));assert.equal(fresh.length,1);await unmount();
  reset();faults.portal_workflow_start_v1=[{code:'proposal_stale'}];await mount(card());await click(button('Start'));assert.match(text(),/out of date/);await unmount();
  reset();faults.portal_workflow_start_v1=[{code:'rate_limited',applied:false}];await mount(card());await click(button('Start'));assert.match(text(),/wasn't applied/);assert.ok(!button('Start').disabled);assert.equal(count('portal_workflow_start_v1'),1);await unmount();
  reset();faults.portal_workflow_start_v1=[{code:'transport_unavailable'}];await mount(card());await click(button('Start'));assert.match(text(),/outcome is unknown/);const first=calls.find(c=>c.cmd==='portal_workflow_start_v1').args;
  await unmount();await mount(card());await click(button('Retry Start with the saved request'));const starts=calls.filter(c=>c.cmd==='portal_workflow_start_v1');assert.equal(starts.length,2);assert.deepEqual(starts[1].args,first);assert.match(text(),/No work has been reported/);assert.ok(!document.querySelector('[aria-label="Owner password"]'));await unmount();
  reset();faults.portal_workflow_start_v1=[{code:'transport_unavailable'}];await mount(card());await click(button('Start'));proposal.run_id=run.id;proposal.state='granted';await click(button('Retry Start with the saved request'));assert.equal(count('portal_workflow_start_v1'),1,'a recovered run id reconciles without a second start');assert.ok(document.querySelector('.workflow-run'));await unmount();
}
function event(kind,payload){const r=store.runs.get(run.id);return {run_id:run.id,seq:(BigInt(r.event_seq)+1n).toString(),revision:(BigInt(r.revision)+1n).toString(),at:'2026-11-16T12:02:00Z',kind,stage_key:null,conversation_id:null,request_id:null,payload};}
async function emit(kind,payload){await flush(()=>{store.observe('workflow_event',event(kind,payload));run=clone(store.runs.get(run.id));});}
async function runChecks(){
  reset();run.origin_conversation_id='assistant-a';await flush(()=>store.putRun(clone(run)));await mount(React.createElement(WorkflowRunView,{id:run.id}));assert.match(text(),/No work has been reported/);assert.equal(count('portal_workflow_watch_v1'),1);
  const stage={...run.stages[0],state:'running',conversation_id:'builder-stage',request_id:'stage-request',actual_models:[proposal.coverage.bindings.builder]};
  await emit('stage_state',{stage});assert.match(text(),/using two/);
  const q={wait_id:'q',kind:'question',stage_key:stage.key,conversation_id:stage.conversation_id,request_id:stage.request_id,record_id:'canonical-question',prompt:'Which colour?',expires_at:null,state:'open',requires_owner:true};
  await emit('wait',{wait:q});await type(document.querySelector('[aria-label="Workflow answer"]'),'Blue');await click(button('Send answer'));assert.equal(calls.find(c=>c.cmd==='portal_answer').args.question_id,'canonical-question');
  await emit('wait',{wait:{...q,wait_id:'approval',kind:'builder_approval',record_id:'approve-1',prompt:'Review this change.'}});assert.match(text(),/Review this change/);assert.ok(document.querySelector('.builder-approval-card, .builder-approval, [aria-label="Builder approval"]')||text().includes('Write the requested file'));
  await click(button('Deny'));assert.equal(calls.find(c=>c.cmd==='portal_builder_approve_v2').args.approval_id,'approve-1');assert.equal(calls.find(c=>c.cmd==='portal_builder_approve_v2').args.decision,'deny');
  await emit('wait',{wait:{...q,wait_id:'owner-gate',kind:'gate',prompt:'Approve completion?'}});await click(button('Approve this stage'));assert.equal(calls.find(c=>c.cmd==='portal_workflow_gate_v1').args.wait_id,'owner-gate');
  await click(button('Pause run'));assert.match(text(),/Paused/);await click(button('Resume run'));assert.equal(count('portal_workflow_resume_v1'),1);
  await flush(()=>store.observe('workflow_connection',{run_id:run.id,state:'stale',reason:'lost link'}));assert.match(text(),/out of date/);assert.ok(!text().includes('Workflow stopped.'));await click(button('Reconnect to this run'));assert.equal(store.stale.has(run.id),false);
  await click(button('Stop run'));assert.ok(text().includes(fixture('stop').run.report.summary));assert.match(text(),/No work was executed/);assert.equal(count('portal_workflow_stop_v1'),1);assert.ok(document.querySelector('.workflow-report details:not([open])'));assert.equal(button('Stop run'),undefined);await unmount();
  // Synthesized controller events are explicitly supplementary: WB1 executed no stage.
  for(const state of ['completed','failed','over_budget','interrupted','unknown','blocked']){reset();run.status=state;await flush(()=>store.putRun(clone(run)));await mount(React.createElement(WorkflowRunView,{id:run.id}));assert.ok(text().includes(({completed:'Completed.',failed:'Failed.',over_budget:'budget was exceeded',interrupted:'Interrupted.',unknown:'outcome is unknown',blocked:'Blocked.'})[state]));await unmount();}
  reset();await flush(()=>store.putRun(clone(run)));const large='9007199254740993';store.runs.set(run.id,{...run,event_seq:large,revision:large});await mount(React.createElement(WorkflowRunView,{id:run.id}));const e=event('stage_state',{stage});await flush(()=>{store.observe('workflow_event',e);store.observe('workflow_event',e);});assert.equal(store.runs.get(run.id).event_seq,'9007199254740994');assert.equal(store.runs.get(run.id).stages.filter(s=>s.key===stage.key).length,1);
  const compatibility={source:'workflow',run_id:run.id,stage_key:stage.key,conversation_id:stage.conversation_id,request_id:stage.request_id,event_seq:'9007199254740995'};assert.equal(store.compatibility(compatibility),true);assert.equal(store.compatibility(compatibility),false);await unmount();
  reset();await flush(()=>store.putRun(clone(run)));await mount(React.createElement(WorkflowRunView,{id:run.id}));
  await emit('wait',{wait:{...q,wait_id:'recovery',kind:'recovery',prompt:'The stage was interrupted.'}});await click(button('Resume this stage'));assert.equal(calls.find(c=>c.cmd==='portal_workflow_resume_v1').args.wait_id,'recovery');
  const report={...clone(fixture('stop').run.report),outcome:'completed',summary:'Built the fixture page. Checked the supplied snapshot. The visual result is unverified.',units:[{id:'one',verdict:'accepted',reason:'First unit.',snapshot_digest:null,stage_keys:[],receipt_ids:[]},{id:'two',verdict:'unknown',reason:'Second unit.',snapshot_digest:null,stage_keys:[],receipt_ids:[]}],caveats:['First caveat.','Second caveat.']};
  await emit('report',{report});await emit('run_state',{status:'completed',reason:null});assert.match(text(),/Completed/);const rendered=document.querySelector('.workflow-report');assert.equal(rendered.querySelector('.action-text').textContent,report.summary);assert.ok(rendered.textContent.indexOf('First unit.')<rendered.textContent.indexOf('Second unit.'));assert.ok(rendered.textContent.indexOf('First caveat.')<rendered.textContent.indexOf('Second caveat.'));assert.ok(!rendered.querySelector('details').open);await unmount();
  reset();await flush(()=>{store.proposals.set(proposal.id,{proposal:clone(proposal),source:source(proposal)});const b={...clone(proposal),id:'outside'};store.proposals.set(b.id,{proposal:b,source:{...source(b),conversationId:'assistant-b',name:'Outside researcher'}});store.catalog={...fixture('catalog'),available:true,reasons:[]};});await mount(React.createElement(WorkflowNeeds));assert.match(text(),/the Assistant/);assert.match(text(),/Outside researcher/);assert.equal(document.querySelectorAll('.workflow-proposal').length,2);assert.equal(count('portal_workflow_start_v1'),0);await unmount();
  reset();store.summaries.set(run.id,run);await mount(React.createElement(WorkflowRuns));await click([...document.querySelectorAll('.workflow-run-row')][0]);assert.ok(document.querySelector('.workflow-run'));await unmount();
}
async function wireChecks(){
  reset();const p=proposal;const a={type:'workflow',title:'Review work',proposal_id:p.id,proposal_revision:'9007199254740993',proposal_digest:p.digest};assert.ok(validateAction(JSON.stringify(a),[]));assert.equal(validateAction(JSON.stringify({...a,proposal_revision:9007199254740992}),[]),null);assert.equal(validateAction(JSON.stringify({...a,run_mode:'auto'}),[]),null);
  await mount(React.createElement(ActionCardsContext.Provider,{value:{conversationId:'assistant-a',messageId:'stream',streaming:true}},React.createElement(ActionCard,{action:a,index:0})));assert.equal(calls.length,0,'streamed card sends nothing');await unmount();
  reset();const args={proposal_id:p.id,proposal_revision:p.revision,proposal_digest:p.digest,request_key:'frozen'};faults.portal_workflow_start_v1=[{code:'rate_limited',applied:false}];await assert.rejects(client.workflowStart(args));assert.equal(count('portal_workflow_start_v1'),1,'write does not retry');
  await Promise.all([client.workflowProposal(p.id),client.workflowProposal(p.id)]);assert.equal(count('portal_workflow_proposal_v1'),1,'reads share an in-flight request');
  for(const c of calls)assert.equal(c.args.contract_version,1);
}
async function appChecks(){
  reset();const faces=['assistant','builder','reviewer'].map(id=>({id,name:id[0].toUpperCase()+id.slice(1)}));
  const sessions=[{id:'assistant-a',face_id:'assistant',title:'Assistant planning'},{id:'assistant-b',face_id:'assistant',title:'Assistant research'},{id:'builder-stage',face_id:'builder',title:'Builder stage'}];
  run.origin_conversation_id='assistant-a';run.status='running';run.stages[0]={...run.stages[0],state:'running',conversation_id:'builder-stage',request_id:'stage-request',actual_models:[proposal.coverage.bindings.builder]};
  proposal.run_id=run.id;proposal.state='granted';
  const action={type:'workflow',title:'Work',proposal_id:proposal.id,proposal_revision:proposal.revision,proposal_digest:proposal.digest};
  const history={'assistant-a':[{id:'card',role:'assistant',content:'```bob-action\n'+JSON.stringify(action)+'\n```',created_at:'2026-11-16T12:00:00Z'}],'assistant-b':[],'builder-stage':[]};
  const hello={identity:{sub:'wa1-owner',gateway:'fixture',faces,may_touch:[],may_not_touch:[]},server:{},gaps:[],operations:{...catalog.capabilities.operations,'workflows.catalog':{contract_versions:[1],available:true,implemented:true}}};
  async function appInvoke(cmd,args){
    switch(cmd){
      case 'portal_connection_get':return {connection_configured:true};
      case 'portal_hello':return hello;
      case 'portal_catalog':return {faces,models:[],backends:[],capabilities:{actions:[],warnings:[]}};
      case 'portal_conversations':return {items:sessions,limit:50,offset:0};
      case 'portal_conversation_open':return {conversation:sessions.find(c=>c.id===args.id),messages:history[args.id]??[],has_more:false};
      case 'portal_messages_more':return {items:history[args.id]??[],has_more:false};
      case 'portal_runs_list':return {items:[]};
      case 'calendar_read':return [];
      case 'portal_teams_list_v2':return {items:[],next_cursor:null};
      case 'portal_pins':return {};
      case 'portal_approvals':return {items:[]};
      case 'portal_builder_mode_get_v2':return {conversation_id:args.conversation_id,project_id:null,mode:'approve',expires_at:null,network:{allowlist:[],project_extra:[]},revision:1,approvals:[],approval_records:[]};
      case 'pc_poll':return {requests:[],outcomes:{}};
      case 'runtime_repair_status':return {repairable:true,observation:{state:'healthy',observed:'Healthy'}};
      case 'portal_workflow_runs_v1':calls.push({cmd,args});return {items:[clone(run)],next_cursor:null};
      default:return invoke(cmd,args);
    }
  }
  localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));
  root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(PortalApp,{invoke:appInvoke,profiles:null,onProfiles:()=>{}})));await flush();
  await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes('Assistant')));await flush();
  assert.equal(document.querySelectorAll('.workflow-run').length,1,'one originating conversation attachment');
  await click(button('Open in Agents'));assert.match(document.querySelector('.session-divider').textContent,/Builder stage/);
  const emit=async(event,data)=>flush(()=>{for(const fn of listeners)fn({payload:JSON.stringify({event,data})});});
  const frame={source:'workflow',run_id:run.id,stage_key:run.stages[0].key,conversation_id:'builder-stage',request_id:'stage-request',event_seq:2,content:'Observed workflow output.',model:'two',backend:'fixture'};
  const envelope={run_id:run.id,seq:2,revision:3,at:'2026-11-16T12:02:00Z',kind:'chat',stage_key:run.stages[0].key,conversation_id:'builder-stage',request_id:'stage-request',payload:{name:'chunk',data:frame}};
  await emit('workflow_event',envelope);await emit('chunk',frame);await emit('workflow_event',envelope);await emit('chunk',frame);
  assert.equal(document.querySelector('.streaming .content').textContent,'Observed workflow output.','compatibility chunks attach once');
  const wait={wait_id:'shared-question',kind:'question',stage_key:run.stages[0].key,conversation_id:'builder-stage',request_id:'stage-request',record_id:'shared-record',prompt:'Which colour?',expires_at:null,state:'open',requires_owner:true};
  await emit('workflow_event',{...envelope,seq:3,revision:4,kind:'wait',payload:{wait}});
  const question={...frame,event_seq:4,question_id:'shared-record',question:'Which colour?',choices:['Blue']};
  await emit('workflow_event',{...envelope,seq:4,revision:5,kind:'chat',payload:{name:'question',data:question}});await emit('question',question);
  const needs=()=>[...document.querySelectorAll('button')].find(b=>b.title.startsWith('Needs you'));
  assert.match(needs().title,/1 waiting/,'canonical workflow/chat question counts once');
  await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes('Assistant')));await type(document.querySelector('[aria-label="Workflow answer"]'),'Blue');await click(button('Send answer'));assert.doesNotMatch(needs().title,/1 waiting/,'answer settles both surfaces');
  await click(button('Open in Agents'));
  await emit('transport_fault',{code:'transport_unavailable',detail:'Watch link lost.'});assert.ok(document.querySelector('.streaming'),'lost observation does not terminate a workflow-owned stage');
  await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes('Assistant')));await flush();assert.match(text(),/out of date/);assert.match(text(),/Work is in progress/);assert.equal(count('portal_workflow_stop_v1'),0);
  await unmount();
}
(async()=>{for(const mode of ['simple','pro']){experience=mode;await proposalChecks();await runChecks();await appChecks();}await wireChecks();console.log('WA1: PASS (golden cards, modes, unavailable/stale/refused/replayed Start, owner path, event states/waits, stop, stale observation, reports, aggregation, exact integers, shared watches, single-attempt writes, real PortalApp workflow stream isolation)');})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await unmount();Date.now=realNow;dom.window.close();});
