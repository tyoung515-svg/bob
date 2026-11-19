// WA2: real React surfaces and native payloads against WB1/WB2B golden evidence.
// Synthetic validation paths/new revisions supplement the pathless WB1 error fixture.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost',pretendToBeVisual:true});
for(const k of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage'])global[k]=dom.window[k];
global.requestAnimationFrame=fn=>setTimeout(fn,0);global.IS_REACT_ACT_ENVIRONMENT=true;
const listeners=new Set(),events=require.resolve('@tauri-apps/api/event');
require.cache[events]={id:events,filename:events,loaded:true,exports:{listen:async(_,fn)=>{listeners.add(fn);return()=>listeners.delete(fn);}}};
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const {WorkflowContext,WorkflowNeeds}=require('../src/portal/WorkflowViews.tsx');
const {WorkflowStore}=require('../src/portal/workflow-store.ts');
const {WorkflowLibrary,WorkflowRunForm,WorkflowDraftReview}=require('../src/portal/WorkflowLibrary.tsx');
const {WorkflowEditor}=require('../src/portal/WorkflowEditor.tsx');
const {WorkflowSketch}=require('../src/portal/WorkflowSketch.tsx');
const {LibraryView}=require('../src/portal/LibraryView.tsx');
const {TasksCalendar}=require('../src/portal/TasksCalendar.tsx');
const {definitionSketch,workflowDrafts}=require('../src/portal/workflow-library.ts');
const {newApp,sketchText}=require('../src/portal/library.ts');
const {newEntry,ymd}=require('../src/portal/calendar.ts');
const {createPortalClient}=require('../src/portal/portal-client.ts');
const {ExperienceContext}=require('../src/portal/experience.ts');
const PortalApp=require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const base=path.resolve(__dirname,'../../../../runs/golive/workflow-fixtures');
const fixture=(name,part='result')=>JSON.parse(fs.readFileSync(path.join(base,name+'.'+part+'.json'),'utf8'));
const clone=x=>JSON.parse(JSON.stringify(x));
const realNow=Date.now;Date.now=()=>Date.parse('2026-11-16T12:01:00Z');
let root,store,client,calls,catalog,records,heads,proposal,inbox,faults,serial=0,savedRecords,sketches,experience='simple';
const count=cmd=>calls.filter(c=>c.cmd===cmd).length;
const flush=async(fn=()=>{})=>act(async()=>{await fn();await new Promise(r=>setTimeout(r,0));});
const button=(label,scope=document)=>[...scope.querySelectorAll('button')].find(b=>b.textContent.trim()===label);
async function click(node){assert.ok(node,'click target');await flush(()=>node.click());await flush();}
async function type(node,value){assert.ok(node,'input target');await flush(()=>{Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node),'value').set.call(node,value);node.dispatchEvent(new Event(node.tagName==='SELECT'?'change':'input',{bubbles:true}));});}
async function submit(node){await flush(()=>node.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));await flush();}
const text=()=>document.getElementById('root').textContent;
const input=label=>[...document.querySelectorAll('label')].find(l=>l.firstChild?.textContent===label)?.querySelector('input,textarea,select');
function put(record){records.set(record.id+':'+record.revision,clone(record));heads.set(record.id,clone(record));}
function reset(){
  calls=[];faults={};savedRecords=[];sketches=[];localStorage.clear();records=new Map();heads=new Map();
  catalog=fixture('catalog');catalog.available=true;catalog.reasons=[];
  catalog.templates.forEach(put);put(fixture('copy').definition);
  proposal=fixture('propose').proposal;inbox=fixture('tools/wb2b-owner-needs-you');
  client=createPortalClient(invoke);store=new WorkflowStore(client,'wa2-'+(++serial));
}
async function invoke(cmd,args={}){
  calls.push({cmd,args:clone(args)});if(faults[cmd]?.length)throw JSON.stringify(faults[cmd].shift());
  switch(cmd){
    case 'portal_workflow_catalog_v1':return clone(catalog);
    case 'portal_workflow_list_v1':return {items:[...heads.values()].filter(r=>r.origin==='user'&&!r.deleted),next_cursor:null};
    case 'portal_workflow_get_v1':return {definition:clone(args.revision===undefined ? heads.get(args.id) : records.get(args.id+':'+args.revision))};
    case 'portal_workflow_copy_v1':{const source=records.get(args.id+':'+args.revision),copied={...clone(source),id:'copy-'+(++serial),origin:'user',read_only:false,definition:{...clone(source.definition),name:args.name},revision:1};put(copied);return {definition:clone(copied)};}
    case 'portal_workflow_save_v1':{
      const old=heads.get(args.id);if(old && String(old.revision)!==String(args.expected_revision))throw JSON.stringify({code:'revision_conflict'});
      const record={...clone(old??fixture('copy').definition),id:args.id??'new-'+(++serial),revision:old?Number(old.revision)+1:1,definition:clone(args.definition)};
      put(record);savedRecords.push(record);return {definition:clone(record)};
    }
    case 'portal_workflow_delete_v1':{const old=heads.get(args.id);const record={...old,deleted:true,revision:Number(old.revision)+1};put(record);return {id:record.id,revision:record.revision,deleted:true};}
    case 'portal_workflow_propose_v1':proposal={...clone(proposal),id:'proposal-'+(++serial),definition_ref:args.definition_ref,coverage:{...clone(proposal.coverage),definition:clone(records.get(args.definition_ref.id+':'+args.definition_ref.revision).definition),inputs:args.inputs,run_mode:args.run_mode,workspace:args.workspace??{path_label:'Existing workspace'},project_id:args.project_id}};return {proposal:clone(proposal)};
    case 'portal_workflow_proposal_v1':return {proposal:clone(proposal)};
    case 'portal_workflow_proposals_v1':return clone(inbox);
    case 'portal_workflow_runs_v1':return {items:[],next_cursor:null};
    case 'portal_workflow_watch_v1':return {run_id:args.run_id,watching:args.watch};
    case 'portal_workflow_run_v1':return fixture('run-accepted');
    case 'portal_projects':return {items:[{id:'project-1',name:'Existing workspace'}]};
    case 'portal_workspace_browse_v2':return {project_id:args.project_id,root_label:'Registered project',path:'',entries:[],truncated:false};
    default:throw new Error('Unexpected WA2 call '+cmd);
  }
}
const context=()=>({store,catalog:{faces:[],models:[],backends:[],capabilities:{operations:{}}},onOpen:()=>{},onFresh:()=>{},pc:()=>null});
async function mount(child){store.activate();root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(ExperienceContext.Provider,{value:experience},React.createElement(WorkflowContext.Provider,{value:context()},child))));await flush();}
async function unmount(){if(root){await flush(()=>root.unmount());root=null;}store?.dispose();await flush();}
const library=()=>React.createElement(WorkflowLibrary,{onSketch:async s=>{sketches.push(s);}});
const selectDefinition=async record=>click([...document.querySelectorAll('nav button')].find(b=>b.textContent.startsWith(record.definition.name)));
async function inboxChecks(){
  reset();await store.refreshProposals();assert.equal(count('portal_workflow_proposals_v1'),0,'not advertised is silent and sends nothing');
  store.proposalsAdvertised=true;await store.refreshProposals();assert.equal(store.proposals.size,1);await mount(React.createElement(WorkflowNeeds));
  assert.match(text(),/Proposed by outside-token \(outside agent\)/);assert.equal(count('portal_workflow_start_v1'),0);
  await click(button('Refresh workflow proposals'));const before=count('portal_workflow_proposals_v1');
  await flush(()=>store.observe('workflow_event',{run_id:'not-open',seq:1,revision:1,payload:{}}));assert.equal(count('portal_workflow_proposals_v1'),before+1);
  inbox={items:[],next_cursor:null};await flush(()=>store.refreshProposals());assert.equal(document.querySelectorAll('.workflow-proposal').length,0,'consumed/expired rows disappear after authoritative refresh');await unmount();
  reset();store.proposalsAdvertised=true;const outside=clone(inbox.items[0]),assistant={...clone(proposal),id:'assistant-proposal',kind:'workflow_proposal',proposed_by:{actor:'assistant',token_type:'human'}};
  client.workflowProposals=async cursor=>cursor ? {items:[assistant],next_cursor:null} : {items:[outside],next_cursor:'page-two'};
  await store.refreshProposals();assert.equal(store.proposals.size,2);assert.equal(store.proposals.get(assistant.id).source.name,'the Assistant');await unmount();
}
async function libraryChecks(){
  reset();await mount(library());const premade=catalog.templates[0],owner=fixture('copy').definition;
  assert.equal(document.querySelectorAll('nav button').length,4);await selectDefinition(premade);assert.ok(button('Copy to edit'));assert.equal(button('Edit workflow'),undefined);assert.equal(button('Delete workflow'),undefined);
  assert.match(text(),/Plan one bounded task/);assert.equal(count('library_launch'),0);
  await click(button('Copy to edit'));await type(document.querySelector('[aria-label="Copy name"]'),'My workflow');await submit(document.querySelector('[aria-label="Copy workflow"]'));
  assert.equal(count('portal_workflow_copy_v1'),1);assert.ok(document.querySelector('[aria-label="Workflow stage editor"]'));assert.match(text(),/read back from the server/);
  await type(input('Name'),'My renamed workflow');await submit(document.querySelector('.workflow-editor'));
  const save=calls.filter(c=>c.cmd==='portal_workflow_save_v1').at(-1);assert.equal(save.args.expected_revision,1);assert.equal(save.args.definition.name,'My renamed workflow');assert.equal(save.args.contract_version,1);assert.equal(count('portal_workflow_get_v1'),2);
  await click(button('Close editor'));await click(button('Open as sketch'));assert.equal(sketches.length,1);assert.equal(sketches[0].definition_ref.revision,2);assert.equal(sketches[0].definition_ref.digest,premade.digest);assert.equal(sketches[0].nodes.length,premade.definition.stages.length);assert.deepEqual(sketches[0].edges,premade.definition.stages.flatMap(s=>s.depends_on.map(from=>({from,to:s.id}))));
  await selectDefinition(owner);await click(button('Edit workflow'));await type(input('Name'),'Unsaved work');
  put({...owner,revision:2,definition:{...owner.definition,name:'Concurrent edit'}});await submit(document.querySelector('.workflow-editor'));assert.match(text(),/newer revision/);assert.equal(input('Name').value,'Unsaved work');assert.equal(calls.filter(c=>c.cmd==='portal_workflow_save_v1').at(-1).args.expected_revision,1);
  await click(button('Use the newer revision'));assert.equal(input('Name').value,'Concurrent edit');await type(input('Name'),'Merged edit');await submit(document.querySelector('.workflow-editor'));assert.equal(heads.get(owner.id).revision,3);
  await click(button('Close editor'));await click(button('Delete workflow'));assert.match(text(),/Existing runs keep their saved version/);await click(button('Delete definition'));assert.equal(heads.get(owner.id).deleted,true);assert.equal(count('portal_workflow_stop_v1'),0);await unmount();
  // A refresh offers a newer revision without changing the displayed pin.
  reset();await mount(library());await selectDefinition(owner);put({...owner,revision:2,definition:{...owner.definition,name:'New revision'}});await click(button('Refresh saved workflows'));assert.match(text(),/A newer revision is available/);assert.equal(document.querySelector('.workflow-definition h3').textContent,owner.definition.name);await click(button('Use the newer revision'));assert.equal(document.querySelector('.workflow-definition h3').textContent,'New revision');await unmount();
}
async function editorChecks(){
  reset();const owner=fixture('copy').definition;let readback=null;
  await mount(React.createElement(WorkflowEditor,{record:owner,definition:owner.definition,onSaved:r=>{readback=r;},onCancel:()=>{}}));
  faults.portal_workflow_save_v1=[{code:'validation_error',detail:{errors:[{path:['definition','stages',1,'max_fixes'],message:'Repair limit must be at most ten.'},{path:['definition','stages',0,'brief'],message:'Describe the work for this stage.'}]}}];
  await submit(document.querySelector('.workflow-editor'));assert.match(document.querySelector('[data-stage-path="stages.1"]').textContent,/Repair limit must be at most ten/);assert.doesNotMatch(document.querySelector('[data-stage-path="stages.0"]').textContent,/Repair limit must/);assert.match(document.querySelector('[data-stage-path="stages.0"]').textContent,/Describe the work/);
  await click(button('Remove stage',document.querySelector('[data-stage-path="stages.0"]')));assert.match(text(),/Another stage receives work/);assert.equal(document.querySelectorAll('[data-stage-path]').length,4);
  await type(input('New stage kind'),'gate');await click(button('Add stage'));assert.equal(document.querySelectorAll('[data-stage-path]').length,5);
  const added=document.querySelector('[data-stage-path="stages.3"]');assert.match(added.textContent,/Your review/);await click(button('Remove stage',added));assert.equal(document.querySelectorAll('[data-stage-path]').length,4);
  if(experience==='pro'){
    await click(button('Edit as JSON'));const edited=clone(owner.definition);edited.name='JSON revision';await type(document.querySelector('[aria-label="Definition JSON"]'),JSON.stringify(edited));await submit(document.querySelector('.workflow-editor'));assert.equal(readback.definition.name,'JSON revision');assert.equal(calls.filter(c=>c.cmd==='portal_workflow_get_v1').at(-1).args.revision,2);
  } else {assert.equal(button('Edit as JSON'),undefined);await submit(document.querySelector('.workflow-editor'));assert.ok(readback);}
  await unmount();
  reset();const sprint=clone(catalog.templates[2]);sprint.read_only=false;await mount(React.createElement(WorkflowEditor,{record:sprint,definition:sprint.definition,onSaved:()=>{},onCancel:()=>{}}));
  faults.portal_workflow_save_v1=[{code:'validation_error',detail:{errors:[{path:'stages.1.body.0.max_fixes',message:'The nested repair limit is too large.'}]}}];await submit(document.querySelector('.workflow-editor'));assert.match(document.querySelector('[data-stage-path="stages.1.body.0"]').textContent,/nested repair/);await unmount();
  reset();const independent={...clone(catalog.templates[1]),id:'ordering',read_only:false};put(independent);await mount(React.createElement(WorkflowEditor,{record:independent,definition:independent.definition,onSaved:()=>{},onCancel:()=>{}}));await click(button('Move up',document.querySelector('[data-stage-path="stages.1"]')));await submit(document.querySelector('.workflow-editor'));const moved=calls.find(c=>c.cmd==='portal_workflow_save_v1').args.definition;assert.equal(moved.stages[0].id,'risks');assert.ok(moved.stages[1].depends_on.includes('risks'),'reordering records explicit dependencies rather than relying on array order');await unmount();
  reset();await mount(React.createElement(WorkflowEditor,{record:owner,definition:owner.definition,onSaved:()=>{},onCancel:()=>{}}));faults.portal_workflow_save_v1=[{code:'transport_unavailable'}];await submit(document.querySelector('.workflow-editor'));const lost=calls.find(c=>c.cmd==='portal_workflow_save_v1').args;assert.ok(button('Retry the saved workflow request'));await unmount();await mount(React.createElement(WorkflowEditor,{record:owner,definition:owner.definition,onSaved:()=>{},onCancel:()=>{}}));await submit(document.querySelector('.workflow-editor'));assert.deepEqual(calls.filter(c=>c.cmd==='portal_workflow_save_v1')[1].args,lost);await unmount();
}
async function runChecks(){
  reset();const record=catalog.templates[0];await mount(React.createElement(WorkflowRunForm,{record,onClose:()=>{}}));
  await type(document.querySelector('[aria-label="Workflow task"]'),'Build a bounded fixture');await type(document.querySelector('[aria-label="New workspace name"]'),'Garden');await submit(document.querySelector('.workflow-run-form'));assert.match(text(),/Choose a model for planner/);assert.equal(count('portal_workflow_propose_v1'),0);
  for(const [alias,binding] of [['planner','one'],['builder','two'],['reviewer','three']])await type(document.querySelector('[aria-label="Model for '+alias+'"]'),binding);
  await type(document.querySelector('[aria-label="Acceptance notes"]'),'Keep it bounded');await type(document.querySelector('[aria-label="Run form mode"]'),'approve');await submit(document.querySelector('.workflow-run-form'));
  const proposed=calls.find(c=>c.cmd==='portal_workflow_propose_v1');assert.deepEqual(proposed.args.definition_ref,{id:record.id,revision:1});assert.deepEqual(proposed.args.workspace,{create:'Garden'});assert.deepEqual(proposed.args.bindings,{planner:'one',builder:'two',reviewer:'three'});assert.equal(proposed.args.inputs.acceptance,'Keep it bounded');assert.equal(proposed.args.run_mode,'approve');assert.equal(proposed.args.origin_conversation_id,null);assert.ok(button('Start'));assert.equal(count('portal_workflow_start_v1'),0);await unmount();
  reset();catalog.available=false;await mount(React.createElement(WorkflowRunForm,{record,onClose:()=>{}}));assert.match(text(),/Workflows are unavailable/);await type(document.querySelector('[aria-label="Workflow task"]'),'An unavailable run');await type(document.querySelector('[aria-label="New workspace name"]'),'Garden');for(const alias of Object.keys(record.definition.agents))await type(document.querySelector('[aria-label="Model for '+alias+'"]'),'one');await submit(document.querySelector('.workflow-run-form'));assert.ok(button('Start').disabled);assert.equal(count('portal_workflow_start_v1'),0);await unmount();
  reset();await mount(React.createElement(WorkflowRunForm,{record,initialInputs:{task:'Existing workspace task'},onClose:()=>{}}));await type(document.querySelector('[aria-label="Workflow workspace"]'),'project-1');for(const alias of Object.keys(record.definition.agents))await type(document.querySelector('[aria-label="Model for '+alias+'"]'),'one');await submit(document.querySelector('.workflow-run-form'));assert.equal(calls.find(c=>c.cmd==='portal_workflow_propose_v1').args.project_id,'project-1');assert.equal(calls.find(c=>c.cmd==='portal_workflow_propose_v1').args.workspace,undefined);await unmount();
  reset();catalog.bindings[0].tool_capable=false;await mount(React.createElement(WorkflowRunForm,{record,initialInputs:{task:'Needs workspace tools'},onClose:()=>{}}));await type(document.querySelector('[aria-label="New workspace name"]'),'Garden');for(const alias of Object.keys(record.definition.agents))await type(document.querySelector('[aria-label="Model for '+alias+'"]'),'one');await submit(document.querySelector('.workflow-run-form'));assert.match(text(),/model for builder cannot make workspace changes/);assert.equal(count('portal_workflow_propose_v1'),0);await unmount();
}
async function sketchChecks(){
  assert.deepEqual(workflowDrafts('```json\n'+JSON.stringify({schema:'workflow/v1',name:'Incomplete',agents:{},authority:{},budget:{},stages:[{}]})+'\n```'),[],'incomplete model JSON cannot open an unsafe editor');
  reset();const record=fixture('copy').definition;let sketch=definitionSketch(record,store.scope),sent=null;
  function Canvas(){const [value,setValue]=React.useState(sketch);return React.createElement(WorkflowSketch,{sketch:value,onChange:s=>{sketch=s;setValue(s);},onSend:s=>{sent=s;}});}
  await mount(React.createElement(Canvas));assert.match(text(),/Sketch — run the saved definition/);const original=clone(record.definition);await type(document.querySelector('[aria-label="Sketch annotations"]'),'Add a review note');await click(button('Note'));assert.equal(sketch.nodes.length,5);assert.deepEqual(sketch.definition_ref,{id:record.id,revision:record.revision,digest:record.digest,scope:store.scope});assert.deepEqual(record.definition,original);
  await click(button('Ask the Assistant to turn this into a workflow'));assert.ok(sent);assert.match(sketchText(sent),/Add a review note/);assert.match(sketchText(sent),new RegExp(record.digest));assert.equal(count('portal_workflow_save_v1'),0);assert.equal(count('portal_workflow_propose_v1'),0);assert.equal(button('Run'),undefined);await unmount();
  reset();const content='Here is the changed workflow.\n```json\n'+JSON.stringify({definition_ref:{id:record.id,revision:record.revision},definition:{...record.definition,name:'Assistant draft'}})+'\n```';await mount(React.createElement(WorkflowDraftReview,{content}));assert.equal(count('portal_workflow_save_v1'),0);await click(button('Review proposed workflow: Assistant draft'));assert.equal(input('Name').value,'Assistant draft');await submit(document.querySelector('.workflow-editor'));assert.equal(calls.find(c=>c.cmd==='portal_workflow_save_v1').args.expected_revision,record.revision);assert.equal(count('portal_workflow_propose_v1'),0);await unmount();
}
async function calendarChecks(){
  reset();const record=catalog.templates[0],entry=newEntry(ymd(new Date()),'agent');entry.title='Workflow reminder';entry.repeat='daily';entry.agent={...entry.agent,task:'Build this calendar task',workflow_ref:{id:record.id,revision:record.revision,digest:record.digest,scope:store.scope},workflow_inputs:{acceptance:'Small changes'}};
  let started=0;const calendarStore={load:async()=>[entry],save:async()=>{},addOnce:async()=>{}};
  await mount(React.createElement(TasksCalendar,{store:calendarStore,ledger:[],onRunNow:async()=>{started++;return null;},onTeamSetup:()=>{},onShowRuns:()=>{}}));assert.equal(started,0);assert.equal(count('portal_workflow_propose_v1'),0,'repeating entries are reminders');
  await click([...document.querySelectorAll('.cal-cell')].find(b=>b.textContent.includes(entry.title)));await click(button('Run now'));assert.ok(document.querySelector('[aria-label="Calendar workflow review"]'));assert.equal(document.querySelector('[aria-label="Workflow task"]').value,entry.agent.task);assert.equal(document.querySelector('[aria-label="Acceptance notes"]').value,'Small changes');assert.equal(started,0);assert.equal(count('portal_workflow_start_v1'),0);assert.equal(calls.find(c=>c.cmd==='portal_workflow_get_v1').args.revision,1);await unmount();
  reset();entry.agent.workflow_ref.scope=store.scope;entry.agent.workflow_choices={bindings:{planner:'one',builder:'two',reviewer:'three'},project_id:'project-1',run_mode:'approve'};
  await mount(React.createElement(TasksCalendar,{store:calendarStore,ledger:[],onRunNow:async()=>{started++;return null;},onTeamSetup:()=>{},onShowRuns:()=>{}}));assert.equal(count('portal_workflow_propose_v1'),0);await click([...document.querySelectorAll('.cal-cell')].find(b=>b.textContent.includes(entry.title)));await click(button('Run now'));assert.ok(document.querySelector('.workflow-proposal'),'configured reminders open the proposal card directly');assert.equal(count('portal_workflow_propose_v1'),1);assert.equal(count('portal_workflow_start_v1'),0);assert.equal(started,0);await unmount();
  reset();entry.agent.workflow_ref.scope=store.scope;delete entry.agent.workflow_choices;let saved=null;
  await mount(React.createElement(TasksCalendar,{store:{...calendarStore,save:async rows=>{saved=clone(rows);}},ledger:[],onRunNow:async()=>{started++;return null;},onTeamSetup:()=>{},onShowRuns:()=>{}}));await click([...document.querySelectorAll('.cal-cell')].find(b=>b.textContent.includes(entry.title)));await click(document.querySelector('.cal-item-main'));await click(button('Choose workspace and models'));await type(document.querySelector('[aria-label="New workspace name"]'),'Recurring workspace');for(const [alias,binding] of [['planner','one'],['builder','two'],['reviewer','three']])await type(document.querySelector('[aria-label="Model for '+alias+'"]'),binding);await click(button('Save run choices'));assert.equal(count('portal_workflow_propose_v1'),0);await click(button('Save',document.querySelector('.cal-editor')));assert.equal(saved[0].agent.workflow_choices.workspace.create,'Recurring workspace');assert.equal(saved[0].repeat,'daily');assert.equal(count('portal_workflow_start_v1'),0);await unmount();
}
async function localLibraryChecks(){
  reset();const app=newApp('Local tool','tool');app.command='example';const launched=[];const libraryStore={apps:async()=>[app],saveApps:async()=>'',sketches:async()=>[],saveSketches:async rows=>{sketches=rows;return '';},launch:async(...args)=>{launched.push(args);return 123;}};
  await mount(React.createElement(LibraryView,{client,store:libraryStore,boardStore:{load:async()=>({}),save:async()=>{}},onPlan:()=>{},onAsk:()=>{},onSendSketch:()=>{},onOpenFolder:()=>{},onOpenProjects:()=>{}}));await click([...document.querySelectorAll('.lib-item')].find(b=>b.textContent.includes(app.name)));await click(button('▶ Launch'));assert.deepEqual(launched,[[app.command,app.folder]]);await click(button('Workflows'));await selectDefinition(catalog.templates[0]);await click(button('Open as sketch'));assert.equal(sketches.length,1);assert.match(text(),/Sketch — run the saved definition/);assert.ok(sketches[0].definition_ref);await unmount();
}
async function appChecks(){
  reset();const faces=['assistant','builder'].map(id=>({id,name:id[0].toUpperCase()+id.slice(1)}));let sessions=[{id:'assistant-a',face_id:'assistant',title:'Assistant planning'}];let messages=[];const extra=[];
  const hello={identity:{sub:'wa2-owner',gateway:'fixture',faces,may_touch:[],may_not_touch:[]},server:{},gaps:[],operations:Object.fromEntries(['workflows.catalog','workflows.proposals'].map(op=>[op,{contract_versions:[1],implemented:true,available:true}]))};
  const sketch=definitionSketch(fixture('copy').definition,JSON.stringify(['fixture','wa2-owner',[],[]]));sketch.annotations='Owner annotation';
  async function appInvoke(cmd,args={}){
    extra.push({cmd,args});switch(cmd){
      case 'portal_connection_get':return {connection_configured:true};case 'portal_hello':return hello;
      case 'portal_catalog':return {faces,models:[],backends:[],capabilities:{actions:[],warnings:[]}};
      case 'portal_conversations':return {items:sessions,limit:50,offset:0};
      case 'portal_conversation_open':return {conversation:sessions.find(c=>c.id===args.id),messages,has_more:false};
      case 'portal_conversation_new':{const conversation={id:'assistant-new',face_id:args.face_id,title:args.title};sessions.unshift(conversation);return {conversation};}
      case 'portal_messages_more':return {items:messages,has_more:false};case 'portal_runs_list':return {items:[]};case 'calendar_read':return [];
      case 'portal_teams_list_v2':return {items:[],next_cursor:null};case 'portal_pins':return {};case 'portal_approvals':return {items:[]};
      case 'pc_poll':return {requests:[],outcomes:{}};case 'runtime_repair_status':return {repairable:true,observation:{state:'healthy',observed:'Healthy'}};
      case 'store_load':return JSON.stringify(args.name==='sketches' ? [sketch] : []);case 'store_save':return '';
      default:return invoke(cmd,args);
    }
  }
  localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(PortalApp,{invoke:appInvoke,profiles:null,onProfiles:()=>{}})));await flush();assert.equal(count('portal_workflow_proposals_v1'),1,'inbox reads on connect');
  let before=count('portal_workflow_proposals_v1');await flush(()=>window.dispatchEvent(new Event('focus')));assert.equal(count('portal_workflow_proposals_v1'),before+1,'focus reads inbox');
  await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes('Assistant')));await click([...document.querySelectorAll('[role="tab"]')].find(b=>b.textContent.includes('Library')));await click(button('Workflow builder'));await click(button('Ask the Assistant to turn this into a workflow'));
  const composer=document.getElementById('composer');assert.ok(composer);assert.match(composer.value,/Owner annotation/);assert.match(composer.value,new RegExp(sketch.definition_ref.digest));assert.match(composer.value,/complete workflow\/v1/);assert.equal(extra.filter(c=>c.cmd==='portal_send').length,0,'sketch becomes an unsent Assistant draft');assert.equal(count('portal_workflow_propose_v1'),0);await unmount();
  // Absence is feature detection, with the legacy observed-card behavior retained.
  reset();delete hello.operations['workflows.proposals'];root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(PortalApp,{invoke:appInvoke,profiles:null,onProfiles:()=>{}})));await flush();await flush(()=>window.dispatchEvent(new Event('focus')));assert.equal(count('portal_workflow_proposals_v1'),0);assert.doesNotMatch(text(),/pending proposals could not/i);await unmount();
}
(async()=>{for(const mode of ['simple','pro']){experience=mode;await inboxChecks();await libraryChecks();await editorChecks();await runChecks();await sketchChecks();await calendarChecks();await localLibraryChecks();await appChecks();}console.log('WA2: PASS (pending inbox, capability fallback, focus/events, Library lifecycle/CAS/readback, stage validation, model choices/propose-only, sketches/drafts, Calendar manual review, local app launch).');})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await unmount();Date.now=realNow;dom.window.close();});
