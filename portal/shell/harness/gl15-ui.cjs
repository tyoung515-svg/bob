// GL15: real PortalApp, concurrent conversations and live managed hand-offs.
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
const originalTimeout=global.setTimeout,originalClear=global.clearTimeout,timers=new Map();
global.setTimeout=(fn,ms,...args)=>{if(ms>=1000){const id={ms};timers.set(id,fn);return id;}return originalTimeout(fn,ms,...args);};
global.clearTimeout=id=>{if(timers.has(id))timers.delete(id);else originalClear(id);};
const clone=x=>JSON.parse(JSON.stringify(x));
const row=(id,role,content)=>({id,role,content,created_at:new Date(1800000000000+(++serial)*1000).toISOString()});
const approval=(id,c)=>({schema_version:1,approval_id:id,conversation_id:c,kind:'write',mode:'approve',state:'pending',summary:'fs_write',expires_at:Date.now()/1000+300,detail:{path:'index.html',diff:'+planner'}});
async function invoke(cmd,args){
  calls.push({cmd,args});
  switch(cmd){
    case 'portal_connection_get':return {connection_configured:true};
    case 'portal_hello':return hello;
    case 'portal_catalog':return {faces,models:[],backends:[],capabilities:{actions:[],warnings:[]}};
    case 'portal_conversations':return {items:sessions,limit:50,offset:0};
    case 'portal_conversation_open':return {conversation:sessions.find(c=>c.id===args.id),messages:clone(history[args.id]??[]),has_more:false};
    case 'portal_messages_more':return {items:clone(history[args.id]??[]),has_more:false};
    case 'portal_conversation_new':{const conversation={id:'c'+(++serial),title:args.title,face_id:args.face_id};sessions=[conversation,...sessions];history[conversation.id]=[];return {conversation};}
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
async function poll(){const item=[...timers].find(([id])=>id.ms===2000);assert.ok(item);timers.delete(item[0]);await flush(item[1]);}
async function mount(){root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes('Assistant')));}
async function unmount(){await flush(()=>root.unmount());root=null;}
function reset(experience){pendingApprovals=[];localStorage.clear();timers.clear();calls=[];serial=0;requests=0;records={};queue={requests:[],outcomes:{}};sessions=[{id:'a',face_id:'assistant',title:'Assistant planning'}];history={a:[row('card','assistant',block(action))]};localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));}
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
(async()=>{
  for(const experience of ['simple','pro']){
    await pair(experience);
    await send('A prompt');assert.equal(sends().length,1);
    await type('A saved draft');await open('b');assert.equal(document.getElementById('composer').value,'');
    await send('B prompt');assert.equal(sends().length,2,'B sends while A is live');
    await type('B saved draft');
    await emit('chunk',{request_id:'r1',conversation_id:'a',content:'ALPHA '});
    await emit('chunk',{request_id:'r2',conversation_id:'b',content:'BRAVO '});
    await emit('chunk',{request_id:'r1',conversation_id:'a',content:'one'});
    await emit('chunk',{request_id:'r2',conversation_id:'b',content:'two'});
    assert.match(transcript(),/BRAVO two/);assert.doesNotMatch(transcript(),/ALPHA/);
    await open('a');assert.match(transcript(),/ALPHA one/);assert.doesNotMatch(transcript(),/BRAVO/);
    assert.equal(document.getElementById('composer').value,'A saved draft');assert.ok(button('Send to Bob').disabled);
    await open('b');assert.equal(document.getElementById('composer').value,'B saved draft');assert.match(transcript(),/BRAVO two/);
    await emit('tool_activity',{request_id:'r1',conversation_id:'a',phase:'start',tool:'fs_write',args:'a-only'});
    await emit('tool_activity',{request_id:'r2',conversation_id:'b',phase:'start',tool:'fs_read',args:'b-only'});
    assert.doesNotMatch(document.getElementById('activity-panel').textContent,/a-only/);
    await emit('question',{request_id:'r2',conversation_id:'b',question_id:'b-q',question:'B question?',choices:['B answer']});
    const bCard=document.querySelector('.ask-card');document.getElementById('composer').focus();
    await emit('question',{request_id:'r1',conversation_id:'a',question_id:'a-q',question:'A question?',choices:['A answer']});
    assert.equal(document.querySelector('.ask-card'),bCard,'background question never replaces open question');
    assert.equal(document.activeElement,document.getElementById('composer'),'background attention preserves focus');
    assert.match(needsButton().title,/2 waiting/);
    await click(needsButton());
    const inbox=document.querySelector('.needs-page');
    assert.match(inbox.textContent,/Assistant · Assistant planning[\s\S]*A question/);assert.match(inbox.textContent,/Fixer · Fixer review[\s\S]*B question/);
    await click([...inbox.querySelectorAll('button')].find(b=>b.textContent==='Open conversation' && b.closest('.need-card').dataset.needId==='question:a-q'));
    assert.match(current(),/Assistant planning/);assert.match(document.querySelector('.ask-card').textContent,/A question/);
    await click(button('A answer'));assert.equal(document.querySelector('.ask-card'),null);
    await open('b');assert.equal(document.querySelector('.ask-card').textContent.includes('B question'),true);
    pendingApprovals=['a','b'].map(id=>({id:'approval-'+id,conversation_id:id,user_id:'gl12',action_type:'Write '+id.toUpperCase(),details:{path:id+'.txt'},status:'pending',created_at:'2026-11-16T00:00:00Z'}));
    await emit('approval_request',{request_id:'r1',conversation_id:'a',approval_id:'approval-a',action:'write A'});
    await emit('approval_request',{request_id:'r2',conversation_id:'b',approval_id:'approval-b',action:'write B'});
    assert.match(document.querySelector('.conversation-approvals').textContent,/Write B/);
    assert.doesNotMatch(document.querySelector('.conversation-approvals').textContent,/Write A/,'B renders only B approval cards');
    assert.doesNotMatch(document.querySelector('.composer').textContent,/write A/,'tagged approval stays in A');
    assert.match(document.querySelector('.ask-card').textContent,/B question/,'B card survives approval in A');
    await open('a');assert.match(document.querySelector('.composer').textContent,/write A/);
    assert.match(document.querySelector('.conversation-approvals').textContent,/Write A/);
    assert.doesNotMatch(document.querySelector('.conversation-approvals').textContent,/Write B/,'A renders only A approval cards');
    await open('b');await emit('approval_request',{approval_id:'legacy-two',action:'legacy ambiguous'});
    assert.match(document.querySelector('.window-notice').textContent,/conversation is unknown/);
    await emit('error',{code:'legacy_error',message:'ambiguous fault'});
    assert.match(document.querySelectorAll('.window-notice')[1].textContent,/ambiguous fault.*conversation is unknown/);
    assert.match(transcript(),/BRAVO two/);assert.ok(button('Send to Bob').disabled,'ambiguous error ends neither stream');
    await open('a');await click(button('Stop turn'));
    assert.equal(calls.filter(c=>c.cmd==='portal_stop').at(-1).args.conversation_id,'a');
    await emit('stopped',{request_id:'r1',conversation_id:'a',code:'stop_requested'});
    assert.match(transcript(),/ALPHA one/);assert.match(transcript(),/stopped by you/);
    await open('b');await emit('chunk',{request_id:'r2',conversation_id:'b',content:' still running'});assert.match(transcript(),/BRAVO two still running/);
    await emit('approval_request',{approval_id:'legacy-one',action:'single legacy'});
    assert.match(document.querySelector('.composer').textContent,/single legacy/,'untagged approval maps to only live B');
    await emit('error',{code:'legacy_single',message:'B stopped by legacy error'});
    assert.match(transcript(),/B stopped by legacy error/);assert.ok(!document.querySelector('.streaming'));
    await unmount();

    // Several questions within one conversation remain independently actionable.
    await pair(experience);await send('A prompt');
    await emit('question',{request_id:'r1',conversation_id:'a',question_id:'a-one',question:'First A question?',choices:['First answer']});
    await emit('question',{request_id:'r1',conversation_id:'a',question_id:'a-two',question:'Second A question?',choices:['Second answer']});
    assert.equal(document.querySelectorAll('.ask-card').length,2);assert.match(needsButton().title,/2 waiting/);
    await type('Draft one',document.querySelectorAll('.ask-card input')[0]);
    await type('Draft two',document.querySelectorAll('.ask-card input')[1]);
    assert.equal(document.querySelectorAll('.ask-card input')[0].value,'Draft one');
    await click(button('First answer'));assert.equal(document.querySelector('.ask-card input').value,'Draft two');assert.equal(document.querySelectorAll('.ask-card').length,1);assert.match(document.querySelector('.ask-card').textContent,/Second A question/);
    await click(button('Second answer'));assert.equal(document.querySelectorAll('.ask-card').length,0);await unmount();

    // An untagged notice with no live turn cannot be assigned to the open session.
    await pair(experience);await emit('approval_request',{approval_id:'idle-legacy',action:'idle approval'});
    await emit('error',{code:'idle_error',message:'idle error'});
    assert.equal(document.querySelectorAll('.window-notice').length,2);assert.ok([...document.querySelectorAll('.window-notice')].every(n=>n.textContent.includes('conversation is unknown')));await unmount();

    // Link faults terminate every live turn, preserve each partial, and never replay.
    await pair(experience);await send('A prompt');await open('b');await send('B prompt');
    await emit('chunk',{request_id:'r1',content:'A partial'});await emit('chunk',{request_id:'r2',content:'B partial'});
    await emit('transport_fault',{code:'transport_unavailable',detail:'fixture link closed'});
    assert.match(transcript(),/B partial/);assert.ok(!document.querySelector('.streaming'));
    await open('a');assert.match(transcript(),/A partial/);assert.match(transcript(),/stream ended unexpectedly/);assert.ok(!document.querySelector('.streaming'));
    assert.equal(sends().length,2);await unmount();

    // A fault reported by Stop closes both streams and keeps each partial once.
    await pair(experience);await send('A prompt');await open('b');await send('B prompt');
    await emit('chunk',{request_id:'r1',content:'Stop fault A partial'});await emit('chunk',{request_id:'r2',content:'Stop fault B partial'});
    await open('a');stopFault=true;await click(button('Stop turn'));
    assert.match(transcript(),/Stop fault A partial/);await open('b');assert.match(transcript(),/Stop fault B partial/);assert.ok(!document.querySelector('.streaming'));await unmount();

    // A send RPC fault also terminates an already-running conversation.
    await pair(experience);await send('A prompt');await emit('chunk',{request_id:'r1',content:'Send fault A partial'});await open('b');sendFault=true;await send('Unknown B prompt');
    assert.ok(button('Send to Bob').disabled);await open('a');assert.match(transcript(),/Send fault A partial/);assert.ok(!document.querySelector('.streaming'));await unmount();

    // Both RPC and async stream limit refusals retain the submitted draft locally.
    await pair(experience);await send('A prompt');await open('b');sendFault='limit';await send('Keep this draft');
    assert.equal(document.getElementById('composer').value,'Keep this draft');
    assert.match(document.querySelector('.composer').textContent,/Four agents are already working\. Wait for one to finish or stop one\./);
    assert.ok(!button('Send to Bob').disabled);await open('a');assert.ok(document.querySelector('.streaming'));
    await open('b');await send('Keep this async draft');
    await emit('error',{request_id:'r2',conversation_id:'b',code:'concurrency_limit',message:'Too many concurrent streams'});
    assert.equal(document.getElementById('composer').value,'Keep this async draft');
    await open('a');assert.ok(document.querySelector('.streaming'));await unmount();

    // Completion/readback in the background survives switching without text leakage.
    await pair(experience);await send('A prompt');await open('b');await send('B prompt');
    await finish('a','r1','A final');assert.match(current(),/Fixer review/);assert.doesNotMatch(transcript(),/A final/);
    await type('Assistant',document.getElementById('conversation-search'));
    const aSession=[...document.querySelectorAll('.agent-sessions button')].find(b=>b.textContent.includes('Assistant planning'));
    assert.match(aSession.textContent,/finished since you last looked/);
    await type('',document.getElementById('conversation-search'));
    await open('a');assert.equal((transcript().match(/A final/g)??[]).length,1);await open('b');await finish('b','r2','B final');
    await open('a');assert.match(transcript(),/A final/);assert.doesNotMatch(transcript(),/B final/);await unmount();

    // A legacy error before acknowledgement ends the pending turn without revival.
    await pair(experience);sendFault='delayed';await send('Pending A');
    await emit('error',{code:'legacy_pending',message:'A refused before acknowledgement'});
    assert.match(transcript(),/A refused before acknowledgement/);await flush(()=>releaseSend());
    assert.ok(!document.querySelector('.streaming'));await unmount();

    // An acknowledgement can land after navigation, or after link death.
    await pair(experience);sendFault='delayed';await send('Delayed A');await open('b');
    await emit('chunk',{request_id:'r1',conversation_id:'a',content:'Early A chunk'});
    await flush(()=>releaseSend());assert.match(current(),/Fixer review/);assert.ok(!document.querySelector('.streaming'));
    await open('a');assert.match(transcript(),/Early A chunk/);await unmount();
    await pair(experience);sendFault='delayed';await send('Unknown A');await open('b');await send('B prompt');
    await emit('transport_fault',{code:'transport_unavailable',detail:'ack lost'});await flush(()=>releaseSend());
    await open('a');assert.ok(!document.querySelector('.streaming'));assert.ok(button('Send to Bob').disabled,'unknown A remains gated');await unmount();

    // Assistant stays usable during managed work. Report queues behind its live turn.
    reset(experience);await mount();const worker=await start();await type('Can you explain the plan?');
    assert.ok(!button('Send to Bob').disabled,'Assistant can send while Builder works');await click(button('Send to Bob'));
    assert.equal(sends().length,2);assert.equal(sends()[1].args.conversation_id,'a');
    await emit('chunk',{request_id:'r2',conversation_id:'a',content:'Assistant answer during Builder'});
    await emit('question',{request_id:'r1',conversation_id:worker,question_id:'a-managed',question:'Builder question?',choices:['Continue']});
    assert.ok(run().querySelector('.ask-card'));assert.match(transcript(),/Assistant answer during Builder/);
    await click(button('Continue',run()));assert.ok(!run().querySelector('.ask-card'));
    records.managed=approval('managed',worker);await emit('builder_wait',{request_id:'r1',conversation_id:worker,waiting:true});
    await flush(()=>window.dispatchEvent(new Event('bob-builder-changed')));
    assert.ok(run().querySelector('[aria-label="Builder approval"]'));
    await click(button('Open Builder',run()));await flush();
    assert.ok(document.querySelector('[aria-label="Builder approval"]'),'same approval is visible in Builder session');
    await click(button('Deny'));await open('a');
    assert.equal(records.managed.state,'denied');assert.equal(button('Deny',run()),undefined,'decision settles both views');
    await finish(worker,'r1','Builder done');assert.equal(sends().length,2,'report waits while Assistant answers');
    assert.match(run().textContent,/report waiting/);assert.match(transcript(),/Assistant answer during Builder/);
    history.a.push(row('answer-during-builder','assistant','Assistant answer during Builder'));
    await emit('complete',{request_id:'r2',conversation_id:'a',message_id:'answer-during-builder'});
    assert.equal(sends().length,3,'one waiting report starts after Assistant terminal');assert.equal(sends()[2].args.conversation_id,'a');
    assert.match(sends()[2].args.content,/Builder report: Build Dayline/);
    await emit('complete',{request_id:'r1',conversation_id:worker,message_id:'late'});await emit('stopped',{request_id:'r1',conversation_id:worker,code:'stopped'});
    assert.equal(sends().length,3,'late worker terminals cannot repeat the report');
    await unmount();
  }
  console.log('GL15 UI: PASS (Simple/Pro concurrent streams, navigation/drafts/history/activity, questions/needs, tagged and legacy approvals/errors, isolated stop, shared link faults, limit refusals, late acknowledgements, live hand-off and exactly-once queued report)');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(root)await unmount();global.setTimeout=originalTimeout;global.clearTimeout=originalClear;dom.window.close();});
