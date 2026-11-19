// GL19: real PortalApp refusals, notice ownership and native RPC budgets.
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost',pretendToBeVisual:true});
for(const k of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage'])global[k]=dom.window[k];
global.requestAnimationFrame=fn=>setTimeout(fn,0);global.IS_REACT_ACT_ENVIRONMENT=true;
const listeners=new Set(),events=require.resolve('@tauri-apps/api/event');
require.cache[events]={id:events,filename:events,loaded:true,exports:{listen:async(_,fn)=>{listeners.add(fn);return()=>listeners.delete(fn);}}};
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const PortalApp=require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const faces=['assistant','builder','fixer','orchestrator'].map(id=>({id,name:id[0].toUpperCase()+id.slice(1)}));
const hello={identity:{sub:'gl12',gateway:'fixture',faces,may_touch:[],may_not_touch:[]},server:{},gaps:[]};
hello.operations=Object.fromEntries(['builder.approve','owner.status'].map(op=>[op,{contract_versions:[2],available:true,implemented:true}]));
const block=a=>'```bob-action\n'+JSON.stringify(a)+'\n```';
const action={type:'handoff',agent:'builder',title:'Build Dayline',brief:'Build this approved planner.'};
let root,calls,sessions,history,serial,requests,queue,records,sendFault=false,releaseSend=null,stopFault=false,pendingApprovals=[];
let faults = {}, hiddenSession = null;
const refusal = (legacy=false, seconds=2) => legacy
  ? {code:'gateway_http_429',detail:'rate limit exceeded',hint:'AFTER the request was sent, outcome unknown',retry_after_seconds:seconds}
  : {code:'rate_limited',applied:false,retry_after_seconds:seconds};
const originalTimeout=global.setTimeout,originalClear=global.clearTimeout,originalInterval=global.setInterval,originalClearInterval=global.clearInterval,originalNow=Date.now;
let now=1800000000000;Date.now=()=>now;const timers=new Map();
function timeout(fn,ms=0,...args){const id={ms,due:now+Math.max(1,ms),interval:false};timers.set(id,()=>fn(...args));return id;}
function interval(fn,ms=0,...args){const id={ms,due:now+Math.max(1,ms),interval:true};timers.set(id,()=>fn(...args));return id;}
global.setTimeout=window.setTimeout=timeout;global.clearTimeout=window.clearTimeout=id=>timers.delete(id);
global.setInterval=window.setInterval=interval;global.clearInterval=window.clearInterval=id=>timers.delete(id);
global.requestAnimationFrame=fn=>timeout(fn,16);
async function advance(ms){const end=now+ms;let ticks=0;while(true){const entry=[...timers].sort((a,b)=>a[0].due-b[0].due)[0];if(!entry||entry[0].due>end)break;assert.ok(++ticks<10000,'bounded timers');const [id,fn]=entry;now=id.due;if(id.interval)id.due+=Math.max(1,id.ms);else timers.delete(id);await flush(fn);}now=end;await flush();}
const clone=x=>JSON.parse(JSON.stringify(x));
const row=(id,role,content)=>({id,role,content,created_at:new Date(1800000000000+(++serial)*1000).toISOString()});
const approval=(id,c)=>({schema_version:1,approval_id:id,conversation_id:c,kind:'write',mode:'approve',state:'pending',summary:'fs_write',expires_at:Date.now()/1000+300,detail:{path:'index.html',diff:'+planner'}});
async function invoke(cmd,args){
  calls.push({cmd,args});
  if (faults[cmd]?.length) throw JSON.stringify(faults[cmd].shift());
  switch(cmd){
    case 'portal_connection_get':return {connection_configured:true};
    case 'portal_hello':return hello;
    case 'portal_catalog':return {faces,models:[],backends:[],capabilities:{actions:[],warnings:[]}};
    case 'portal_conversations':return {items:sessions.filter(s=>s.id!==hiddenSession),limit:50,offset:0};
    case 'portal_conversation_open':return {conversation:sessions.find(c=>c.id===args.id),messages:clone(history[args.id]??[]),has_more:false};
    case 'portal_messages_more':return {items:clone(history[args.id]??[]),has_more:false};
    case 'portal_conversation_new':{const conversation={id:'c'+(++serial),title:args.title,face_id:args.face_id};sessions=[conversation,...sessions];history[conversation.id]=[];return {conversation};}
    case 'portal_runs_list':return {items:[]};
    case 'calendar_read':return [];
    case 'portal_teams_list_v2':return {items:[],next_cursor:null};
    case 'portal_pins':return {};
    case 'portal_approvals':return {items:clone(pendingApprovals)};
    case 'portal_owner_status_v2':return {owner_session:{active:true,expires_at:Date.now()/1000+300,allowed_operations:['builder.approve']}};
    case 'pc_poll':return queue;
    case 'runtime_repair_status':return {repairable:true,observation:{state:'healthy',observed:'Healthy'}};
    case 'portal_send':{
      if(sendFault==='limit'){sendFault=false;throw JSON.stringify({code:'concurrency_limit',detail:'Too many concurrent streams'});}
      if(sendFault==='delayed'){sendFault=false;return new Promise(resolve=>{releaseSend=()=>resolve({request_id:'r'+(++requests),accepted:true});});}
      if(sendFault){if(sendFault==='afterStore')history[args.conversation_id].push(row('uncertain-report','user',args.content));sendFault=false;throw JSON.stringify({code:'transport_unavailable',detail:'uncertain send'});}
      const request_id='r'+(++requests);history[args.conversation_id].push(row('u'+serial,'user',args.content));return {request_id,accepted:true};
    }
    case 'portal_reconnect':return {hello,target:'fixture'};
    case 'portal_stop':if(stopFault){stopFault=false;throw JSON.stringify({code:'transport_unavailable',detail:'stop lost the link'});}return {stopped:true,code:'stopped'};
    case 'portal_answer':{const c=sessions.find(c=>c.id===(args.question_id.startsWith('b')?'b':'a'));history[c.id].push(row('answer'+serial,'user','Operator answer to: Which colour?\n'+args.answer));return {resolved:true,delivered_to_turn:true};}
    case 'pc_decide':queue={...queue,requests:queue.requests.filter(c=>c.request.id!==args.requestId)};return {};
    case 'portal_builder_mode_get_v2':return {conversation_id:args.conversation_id,project_id:null,mode:'approve',expires_at:null,network:{allowlist:[],project_extra:[]},revision:0,approvals:Object.values(records).filter(r=>r.conversation_id===args.conversation_id&&r.state==='pending'),approval_records:clone(Object.values(records).filter(r=>r.conversation_id===args.conversation_id))};
    case 'portal_builder_approval_get_v2':return clone(records[args.approval_id]);
    case 'portal_builder_approve_v2':records[args.approval_id]={...records[args.approval_id],state:'denied'};return {approval_id:args.approval_id,state:'denied'};
    default:throw Error('Unexpected GL12 fixture '+cmd);
  }
}
const flush=async(fn=()=>{})=>act(async()=>{await fn();await new Promise(r=>originalTimeout(r,0));});
const button=(label,scope=document)=>[...scope.querySelectorAll('button')].find(b=>b.textContent===label);
const run=()=>document.querySelector('.managed-run');
const sends=()=>calls.filter(c=>c.cmd==='portal_send');
const current=()=>document.querySelector('.session-divider').textContent;
async function click(n){assert.ok(n,'click target');await flush(()=>n.click());}
async function emit(event,data){await flush(()=>{for(const fn of listeners)fn({payload:JSON.stringify({event,data})});});}
async function mount(){root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes('Assistant')));}
async function unmount(){await flush(()=>root.unmount());root=null;}
function reset(experience){faults={};hiddenSession=null;pendingApprovals=[];localStorage.clear();timers.clear();calls=[];serial=0;requests=0;records={};queue={requests:[],outcomes:{}};sessions=[{id:'a',face_id:'assistant',title:'Assistant planning'}];history={a:[row('card','assistant',block(action))]};localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));}
async function start(){await click(button('Start Builder'));assert.equal(sends().length,1);assert.equal(sends()[0].args.content,action.brief);assert.match(current(),/Assistant planning/);return sessions.find(c=>c.face_id==='builder').id;}
async function finish(c,request,text='Builder tool results:\nfs_write: exit code 0'){
  await emit('chunk',{request_id:request,content:text});
  history[c].push(row('final'+serial,'assistant',text));
  await emit('complete',{request_id:request,message_id:'different-completion-id'});
}
async function type(value,node=document.getElementById('composer')) {
  const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node),'value').set;
  await flush(()=>{setter.call(node,value);node.dispatchEvent(new Event('input',{bubbles:true}));});
}
async function open(id){
  const session=sessions.find(s=>s.id===id), row=[...document.querySelectorAll('.agent-row')].find(b=>b.querySelector('strong').textContent===faces.find(f=>f.id===session.face_id).name);
  const siblings=sessions.filter(s=>s.face_id===session.face_id).sort((a,b)=>String(b.updated_at??b.created_at??'').localeCompare(String(a.updated_at??a.created_at??'')));
  if(siblings[0].id===id)return click(row);
  const expand=row.closest('.agent-contact').querySelector('.agent-expander');
  if(expand.getAttribute('aria-expanded')==='false')await click(expand);
  await click(document.getElementById(expand.getAttribute('aria-controls')).querySelectorAll('button')[siblings.findIndex(s=>s.id===id)]);
}
async function send(text){await type(text);await click(button('Send to Bob'));}
const transcript=()=>document.querySelector('.transcript').textContent;
const needsButton=()=>[...document.querySelectorAll('button')].find(b=>b.title.startsWith('Needs you'));
async function pair(experience){reset(experience);sendFault=false;sessions.push({id:'b',face_id:'fixer',title:'Fixer review'});history.b=[];await mount();}
const count = command => calls.filter(c=>c.cmd===command).length;
const composer = () => document.querySelector('.composer');
const {createPortalClient} = require('../src/portal/portal-client.ts');
const {toPortalError,isDefiniteRefusal,RATE_LIMIT_SEND_LINE,RATE_LIMIT_WRITE_LINE} = require('../src/portal/portal-protocol.ts');
async function refusalChecks(experience,legacy) {
  await pair(experience);
  await send('Keep A running'); await open('b');
  faults.portal_send=[refusal(legacy)]; await send('Keep B draft');
  assert.equal(document.getElementById('composer').value,'Keep B draft');
  assert.equal(composer().textContent.split(RATE_LIMIT_SEND_LINE).length-1,1);
  assert.doesNotMatch(composer().textContent,/gateway_http|unknown|AFTER/);
  assert.ok(!button('Send to Bob').disabled); assert.equal(sends().length,2);
  await advance(10000); assert.equal(sends().length,2,'no send replay');
  await open('a');assert.ok(document.querySelector('.streaming'));
  assert.ok(!composer().textContent.includes(RATE_LIMIT_SEND_LINE));
  await open('b');assert.equal(document.getElementById('composer').value,'Keep B draft');
  assert.ok(composer().textContent.includes(RATE_LIMIT_SEND_LINE));
  await unmount();

  // Tagged asynchronous refusal has the same definite outcome.
  await pair(experience);await send('Async draft');await open('b');
  await emit('error',{request_id:'r1',conversation_id:'a',...refusal(legacy)});
  assert.ok(!composer().textContent.includes(RATE_LIMIT_SEND_LINE));
  await open('a');assert.equal(document.getElementById('composer').value,'Async draft');
  assert.ok(!button('Send to Bob').disabled);assert.ok(!document.querySelector('.streaming'));
  assert.doesNotMatch(transcript(),/unknown|rate_limited|gateway_http_429/);await unmount();

  // The server's list can lag a newly-created conversation.
  await pair(experience);await click(button('New session'));
  const created=sessions.find(c=>!['a','b'].includes(c.id));assert.ok(created);hiddenSession=created.id;
  faults.portal_send=[refusal(legacy)];await send('First refused draft');
  assert.equal(document.querySelector('.agent-row .agent-preview').textContent,'New conversation');
  await open('a');await open(created.id);
  assert.equal(document.getElementById('composer').value,'First refused draft');await unmount();

  // Question and stop refusals retain the live turn and usable controls.
  await pair(experience);await send('Question prompt');
  await emit('question',{request_id:'r1',conversation_id:'a',question_id:'a-q',question:'Which?',choices:['Choose this']});
  faults.portal_answer=[refusal(legacy)];await click(button('Choose this'));
  assert.ok(document.querySelector('.ask-card'));assert.ok(!button('Choose this').disabled);
  assert.match(composer().textContent,/wasn't applied/);assert.equal(count('portal_answer'),1);
  faults.portal_stop=[refusal(legacy)];await click(button('Stop turn'));
  assert.ok(document.querySelector('.streaming'));assert.ok(!button('Stop turn').disabled);
  await advance(10000);assert.equal(count('portal_answer'),1);assert.equal(count('portal_stop'),1);await unmount();

  // Both generic and Builder approval cards remain actionable.
  await pair(experience);
  pendingApprovals=[{id:'ga',conversation_id:'a',action_type:'Write A',details:{},status:'pending'}];
  await emit('approval_request',{conversation_id:'a',approval_id:'ga',action:'Write A'});
  faults.portal_approval_decide=[refusal(legacy)];await click(button('Approve'));
  assert.ok(!button('Approve').disabled);assert.match(document.querySelector('.typed-refusal').textContent,/wasn't applied/);
  await open('b');assert.ok(!document.querySelector('.typed-refusal'));await open('a');
  assert.match(document.querySelector('.typed-refusal').textContent,/wasn't applied/);
  records.ba=approval('ba','a');await flush(()=>window.dispatchEvent(new CustomEvent('bob-builder-changed',{detail:{conversation_id:'a'}})));await advance(1);
  faults.portal_builder_approve_v2=[refusal(legacy)];await click(button('Deny'));
  assert.ok(!button('Deny').disabled);assert.equal(records.ba.state,'pending');
  assert.ok(document.querySelector('[aria-label="Builder approval"]').textContent.includes(RATE_LIMIT_WRITE_LINE));
  await advance(10000);assert.equal(count('portal_approval_decide'),1);assert.equal(count('portal_builder_approve_v2'),1);await unmount();

  // Managed worker dispatches and reports are writes too.
  await pair(experience);faults.portal_send=[refusal(legacy)];const worker=await start();
  await click(button('Open Builder',run()));
  assert.equal(document.getElementById('composer').value,action.brief);
  assert.ok(composer().textContent.includes(RATE_LIMIT_SEND_LINE));
  await advance(10000);assert.equal(sends().length,1);await unmount();
  await pair(experience);const reportingWorker=await start();faults.portal_send=[refusal(legacy)];
  await finish(reportingWorker,'r1','Worker finished');
  assert.equal(sends().length,2);assert.match(document.getElementById('composer').value,/Builder report:/);
  assert.ok(composer().textContent.includes(RATE_LIMIT_SEND_LINE));assert.ok(!button('Send to Bob').disabled);
  assert.doesNotMatch(run().textContent,/report delivery uncertain/i);
  await advance(10000);assert.equal(sends().length,2);await unmount();
}
async function readRetryChecks() {
  await pair('simple');await open('b');faults.portal_conversation_open=[refusal(false,3)];
  const before=count('portal_conversation_open');
  await click([...document.querySelectorAll('.agent-row')].find(b=>b.textContent.includes('Assistant planning')));
  assert.equal(count('portal_conversation_open'),before+1);assert.ok(!document.querySelector('.fault-inline'));
  await advance(2999);assert.equal(count('portal_conversation_open'),before+1);
  await advance(1);assert.equal(count('portal_conversation_open'),before+2);assert.match(current(),/Assistant planning/);
  await unmount();
  for(const legacy of [false,true]) {
    let n=0;const c=createPortalClient(async()=>{n++;throw JSON.stringify(refusal(legacy,1));});
    let outcome;const pending=c.messagesMore('a',null).catch(e=>outcome=toPortalError(e));
    await flush();await advance(2000);await pending;assert.equal(n,3);assert.equal(outcome.code,'rate_limited');
    await advance(10000);assert.equal(n,3,'retry bound');
  }
  let n=0,release;const shared=createPortalClient(async()=>{n++;return new Promise(resolve=>release=resolve);});
  const a=shared.builderModeGet('a'),b=shared.builderModeGet('a');assert.equal(n,1);release({});await Promise.all([a,b]);
  // Every write category uses exactly one native call, including unknown ops.
  for(const call of [c=>c.send({conversation_id:'a',content:'x'}),c=>c.stop('a'),c=>c.answerQuestion('q','yes'),
    c=>c.approvalDecide('a','approve'),c=>c.builderApprove('a','deny','key'),c=>c.memorySave('x','note'),
    c=>c.projectSave({name:'x'}),c=>c.conversationNew({title:'x'}),c=>c.changePassword('x','y','123456'),
    c=>c.secretSet('provider','value'),c=>c.work('start')]) {
    let n=0;const c=createPortalClient(async()=>{n++;throw JSON.stringify(refusal());});
    await assert.rejects(call(c),e=>isDefiniteRefusal(toPortalError(e)));await advance(10000);assert.equal(n,1);
  }
  // Save failure is plain and leaves Save usable.
  const {SaveToLibrary}=require('../src/portal/SaveToLibrary.tsx');
  const c=createPortalClient(async(cmd)=>{if(cmd==='portal_builder_project_result_v2')return {project_id:'p',conversation_id:'a',path:'index.html'};if(cmd==='portal_projects')return {items:[]};throw Error(cmd);});
  let saves=0;const store={apps:async()=>[],saveApps:async()=>{saves++;throw JSON.stringify(refusal());}};
  root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(SaveToLibrary,{client:c,store,projectId:'p'})));
  await click(button('Save to Library'));assert.ok(!button('Save to Library').disabled);assert.match(document.body.textContent,/Not saved:.*wasn't applied/);
  await advance(10000);assert.equal(saves,1);await unmount();
}
async function windowNoticeChecks() {
  await pair('simple');
  faults.portal_conversation_new=[refusal()];await click(button('New session'));
  assert.ok(document.querySelector('.window-fault-notice').textContent.includes(RATE_LIMIT_WRITE_LINE));
  assert.ok(!composer().textContent.includes(RATE_LIMIT_WRITE_LINE));
  assert.ok(!button('New session').disabled);await open('b');
  assert.ok(document.querySelector('.window-fault-notice').textContent.includes(RATE_LIMIT_WRITE_LINE));
  await unmount();await pair('simple');
  await emit('error',{code:'broker_fault',message:'A window-only broker fault'});
  await emit('error',{code:'broker_fault',message:'A window-only broker fault'});
  assert.equal(document.querySelectorAll('.window-notice').length,1);
  assert.ok(!document.querySelector('.window-notice').closest('.composer-area'));
  await open('b');assert.equal(document.querySelectorAll('.window-notice').length,1);assert.doesNotMatch(composer().textContent,/window-only broker fault/);
  await emit('transport_fault',{code:'transport_unavailable',detail:'One link fault'});
  assert.equal([...document.querySelectorAll('.window-notice')].filter(n=>n.textContent.includes('One link fault')).length,1);
  await open('a');assert.doesNotMatch(composer().textContent,/One link fault/);await unmount();
}
async function measure(label,setup=async()=>{}) {
  await pair('simple');await setup();await advance(1000);calls=[];await advance(60000);
  const counts={};for(const c of calls)counts[c.cmd]=(counts[c.cmd]??0)+1;
  const result={state:label,total:calls.length,gateway:calls.filter(c=>c.cmd.startsWith('portal_')).length,counts};
  console.log(JSON.stringify(result));await unmount();return result;
}
(async()=>{
  const results=[];
  results.push(await measure('Idle Agents'));
  results.push(await measure('One live turn',()=>send('A live prompt')));
  results.push(await measure('Two live turns',async()=>{await send('A live prompt');await open('b');await send('B live prompt');}));
  results.push(await measure('Needs you open',async()=>{await click(needsButton());}));
  results.push(await measure('Runs open',async()=>{await click(button('Tasks'));await click(button('Run history'));}));
  if(!process.env.GL19_BASELINE) {
    for(const result of results)assert.ok(result.total<20,JSON.stringify(result));
    assert.ok(results[1].total-results[0].total<=3);assert.ok(results[2].total-results[1].total<=3);
    for(const experience of ['simple','pro'])for(const legacy of [false,true])await refusalChecks(experience,legacy);
    await readRetryChecks();await windowNoticeChecks();
  }
  console.log('GL19: PASS (RPC budgets, Simple/Pro legacy/new refusals, drafts/sidebar, async sends, decisions/save, managed sends/reports, quiet bounded reads, no write replay, window notice ownership)');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(root)await unmount();global.setTimeout=originalTimeout;global.clearTimeout=originalClear;global.setInterval=originalInterval;global.clearInterval=originalClearInterval;Date.now=originalNow;dom.window.close();});
