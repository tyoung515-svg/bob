// GL12: real PortalApp, shared approvals/questions, one sequential stream.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost',pretendToBeVisual:true});
for(const k of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage'])global[k]=dom.window[k];
global.requestAnimationFrame=fn=>setTimeout(fn,0);global.IS_REACT_ACT_ENVIRONMENT=true;
const listeners=new Set(),events=require.resolve('@tauri-apps/api/event');
require.cache[events]={id:events,filename:events,loaded:true,exports:{listen:async(_,fn)=>{listeners.add(fn);return()=>listeners.delete(fn);}}};
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const PortalApp=require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const {validateAction}=require('../src/portal/ActionCard.tsx');
const {managedOutcome,managedReport,managedFinal,readManaged,managedKey}=require('../src/portal/managed-handoff.ts');
const {turnAnswerRowId}=require('../src/portal/portal-state.ts');
const {singleRenderRows,hasTurnRow}=require('../src/portal/transcript-copies.ts');
const faces=['assistant','planner','builder','fixer','orchestrator'].map(id=>({id,name:id[0].toUpperCase()+id.slice(1)}));
const hello={identity:{sub:'gl12',gateway:'fixture',faces,may_touch:[],may_not_touch:[]},server:{},gaps:[]};
hello.operations=Object.fromEntries(['builder.approve','owner.status'].map(op=>[op,{contract_versions:[2],available:true,implemented:true}]));
const block=a=>'```bob-action\n'+JSON.stringify(a)+'\n```';
const action={type:'handoff',agent:'builder',title:'Build Dayline',brief:'Build this approved planner.'};
let root,calls,sessions,history,serial,requests,queue,records,sendFault=false,originFace='assistant',originLabel='Assistant';
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
    case 'portal_approvals':return {items:[]};
    case 'portal_owner_status_v2':return {owner_session:{active:true,expires_at:Date.now()/1000+300,allowed_operations:['builder.approve']}};
    case 'pc_poll':return queue;
    case 'runtime_repair_status':return {repairable:true,observation:{state:'healthy',observed:'Healthy'}};
    case 'portal_send':{
      if(sendFault){if(sendFault==='afterStore')history[args.conversation_id].push(row('uncertain-report','user',args.content));sendFault=false;throw JSON.stringify({code:'transport_unavailable',detail:'uncertain send'});}
      const request_id='r'+(++requests);history[args.conversation_id].push(row('u'+serial,'user',args.content));return {request_id,accepted:true};
    }
    case 'portal_stop':return {stopped:true,code:'stopped'};
    case 'portal_answer':{const c=sessions.find(c=>c.face_id==='builder');history[c.id].push(row('answer'+serial,'user','Operator answer to: Which colour?\n'+args.answer));return {resolved:true,delivered_to_turn:true};}
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
async function poll(){const item=[...timers].find(([id])=>id.ms===15000 || id.ms===5000);assert.ok(item);timers.delete(item[0]);await flush(item[1]);}
async function mount(){root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes(originLabel)));}
async function unmount(){await flush(()=>root.unmount());root=null;}
function reset(experience){localStorage.clear();timers.clear();calls=[];serial=0;requests=0;records={};queue={requests:[],outcomes:{}};sessions=[{id:'a',face_id:originFace,title:originLabel+' planning'}];history={a:[row('card','assistant',block(action))]};localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));}
async function start(){await click(button('Start Builder'));assert.equal(sends().length,1);assert.equal(sends()[0].args.content,action.brief);assert.match(current(),new RegExp(originLabel+' planning'));return sessions.find(c=>c.face_id==='builder').id;}
async function finish(c,request,text='Builder tool results:\nfs_write: exit code 0'){
  await emit('chunk',{request_id:request,content:text});
  history[c].push(row('final'+serial,'assistant',text));
  await emit('complete',{request_id:request,message_id:'different-completion-id'});
}
(async()=>{
  for(const patch of [{manage:'yes'},{session:4},{session:''},{session:'bad\nsession'},{title:'bad\ntitle'},{extra:true}])assert.equal(validateAction(JSON.stringify({...action,...patch}),faces),null);
  assert.ok(validateAction(JSON.stringify({...action,manage:true,session:'same'}),faces));
  for(const origin of ['assistant','planner']) for(const experience of ['simple','pro']){
    originFace=origin;originLabel=origin==='planner'?'Planner':'Assistant';
    reset(experience);await mount();assert.equal(sends().length,0,'rendering sends nothing');
    const contacts=[...document.querySelectorAll('.agent-row')], plannerContact=contacts.find(row=>row.querySelector('strong')?.textContent==='Planner');
    assert.ok(plannerContact.querySelector('.avatar'));assert.equal(plannerContact.querySelector('.avatar').textContent,'PL');
    assert.equal(contacts.indexOf(plannerContact),contacts.findIndex(row=>row.querySelector('strong')?.textContent==='Assistant')+1);
    if(originFace==='assistant')assert.match(plannerContact.textContent,/No conversations yet. Start one./);
    const startButton=button('Start Builder');await flush(()=>{startButton.click();startButton.click();});
    assert.equal(sends().length,1,'double click sends one brief');assert.match(current(),new RegExp(originLabel+' planning'));
    const c=sessions.find(c=>c.face_id==='builder').id;
    assert.match(run().textContent,/working · 0 steps/);
    assert.doesNotMatch(document.querySelector('.composer-area').textContent,/You can write to Assistant when it finishes/);
    assert.ok(button('Send to Bob').disabled);
    await emit('tool_activity',{request_id:'r1',phase:'start',tool:'fs_write'});
    await emit('tool_activity',{request_id:'r1',phase:'result',tool:'fs_write',ok:false});
    assert.match(run().textContent,/1 steps \(1 failed\) · Changed a project file/);
    await emit('chunk',{request_id:'r1',content:'Here is the plan before my question.\n'});
    await emit('question',{request_id:'r1',question_id:'q',question:'Which colour?',choices:['Blue']});
    assert.match(run().textContent,/needs you/);assert.match(run().textContent,/Here is the plan/);
    assert.equal(document.querySelectorAll('.ask-card').length,1);
    assert.ok(run().textContent.indexOf('Here is the plan')<run().textContent.indexOf('Which colour?'));
    await click(button('Open Builder',run()));assert.match(current(),/Build Dayline/);assert.equal(document.querySelectorAll('.ask-card').length,1);
    assert.match(document.querySelector('.streaming').textContent,/Here is the plan/);
    await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes(originLabel)));
    assert.ok(run().querySelector('.ask-card'),'return retains current question');
    await click(button('Blue',run()));assert.deepEqual(calls.find(c=>c.cmd==='portal_answer').args,{question_id:'q',answer:'Blue'});
    records.b=approval('b',c);
    await emit('builder_wait',{request_id:'r1',waiting:true,approval_id:'b'});
    await flush(()=>window.dispatchEvent(new Event('bob-builder-changed')));
    assert.ok(run().querySelector('[aria-label="Builder approval"]'),'pending Builder record appears without a markdown block');
    await click(button('Deny',run()));
    const decision=calls.find(c=>c.cmd==='portal_builder_approve_v2').args;
    assert.equal(decision.approval_id,'b');assert.equal(decision.decision,'deny');assert.equal(decision.contract_version,2);assert.ok(decision.request_key);
    await emit('builder_wait',{request_id:'r1',waiting:false});
    queue={requests:[{request:{id:'pc',conversation_id:c,task_id:'t',agent:'builder',operation:'pc.run',args:{shell:'powershell',argv:['-Command','Get-Date'],cwd:'C:\\task'},expires_at:Date.now()/1000+300},mode:'approve',root:null,until:null,protected_reason:null,running:false}],outcomes:{}};
    await poll();assert.match(run().textContent,/needs you/);assert.ok(run().querySelector('.inline-pc-approval'));
    await click(button('Allow once on this PC',run()));assert.deepEqual(calls.find(c=>c.cmd==='pc_decide').args,{requestId:'pc',decision:'allow_once',taskFolder:'C:\\task'});
    await finish(c,'r1');
    assert.equal(sends().length,2,'one automatic report');
    const report=sends()[1].args;
    assert.equal(report.conversation_id,'a');assert.equal(report.face_id,originFace,'report uses the actual origin face');
    assert.equal(report.content,`Builder report: Build Dayline\nsession: ${c}\noutcome: finished\nsteps: 1 (1 failed)\n--- report ---\nBuilder tool results:\nfs_write: exit code 0`);
    assert.match(run().textContent,/finished/);assert.match(current(),new RegExp(originLabel+' planning'));
    assert.match(run().textContent,/Here is the plan before my question/,'reporting keeps the pre-question text');
    assert.match(document.querySelector('.managed-report summary').textContent,new RegExp('Builder report sent to '+originLabel));
    assert.equal(document.querySelector('.managed-report').open,false);
    await emit('complete',{request_id:'r1',message_id:'duplicate'});await emit('stopped',{request_id:'r1',code:'stopped'});assert.equal(sends().length,2,'late terminal cannot affect Assistant stream');
    await emit('chunk',{request_id:'r2',content:'I reviewed the result.'});history.a.push(row('assistant-final','assistant','I reviewed the result.'));
    await emit('complete',{request_id:'r2',message_id:'assistant-final'});
    history.a.push(row('followup','assistant',block({...action,title:'Adjust Dayline',brief:'Change the colour.',session:c})));
    await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes(originLabel)));
    const creates=calls.filter(c=>c.cmd==='portal_conversation_new').length;
    await click(button('Start Builder'));assert.equal(calls.filter(c=>c.cmd==='portal_conversation_new').length,creates,'follow-up continues exact session');assert.equal(sends().at(-1).args.conversation_id,c);
    await click(button('Stop',document.querySelectorAll('.managed-run')[1]));assert.equal(calls.filter(c=>c.cmd==='portal_stop').at(-1).args.conversation_id,c);
    await emit('stopped',{request_id:'r3',code:'stop_requested'});assert.match(sends().at(-1).args.content,/outcome: stopped/);
    await unmount();await mount();assert.equal(sends().length,4,'reload never sends a report again');assert.match(document.querySelectorAll('.managed-run')[1].textContent,/stopped/);
    await unmount();

    reset(experience);history.a=[row('wrong-session','assistant',block({...action,session:'a'}))];await mount();await click(button('Start Builder'));
    assert.equal(sends().length,0,'session owned by another agent is rejected');assert.equal(run(),null);await unmount();

    reset(experience);await mount();await start();await unmount();await mount();
    assert.match(run().textContent,/interrupted/);assert.equal(sends().length,1,'closing a working app does not resume report dispatch');await unmount();

    reset(experience);await mount();const uncertain=await start();
    sendFault=true;await finish(uncertain,'r1');assert.equal(sends().length,2,'one attempted report');
    await unmount();await mount();assert.equal(sends().length,2,'uncertain delivery never auto-replays');
    await click(button('Send report to '+originLabel,run()));assert.equal(sends().length,2,'unknown send cannot be repeated without evidence');await unmount();

    reset(experience);await mount();const lostAck=await start();sendFault='afterStore';await finish(lostAck,'r1');await unmount();await mount();
    await click(button('Send report to '+originLabel,run()));assert.equal(sends().length,2,'stored report with lost acknowledgement is reconciled, never repeated');assert.match(run().textContent,new RegExp('report sent to '+originLabel));await unmount();

    reset(experience);await mount();const interrupted=await start();await emit('chunk',{request_id:'r1',content:'Partial work'});
    await emit('transport_fault',{code:'transport_unavailable',detail:'Disconnected'});
    assert.equal(sends().length,1);assert.match(run().textContent,/interrupted/);assert.ok(button('Send report to '+originLabel,run()));
    await unmount();await mount();assert.equal(sends().length,1,'interrupted run stays manual after reload');
    await click(button('Send report to '+originLabel,run()));assert.equal(sends().length,2);assert.match(sends()[1].args.content,/outcome: error/);assert.match(sends()[1].args.content,/Partial work/);
    await unmount();

    reset(experience);history.a=[row('old','assistant',block({...action,manage:false}))];await mount();assert.equal(button('Start Builder'),undefined);await click(button('Start in Builder'));assert.equal(sends().length,0);assert.equal(document.getElementById('composer').value,action.brief);assert.match(current(),/Build Dayline/);await unmount();
  }
  for(const badOrigin of ['builder','researcher',null]) {
    reset('simple');sessions[0].face_id=badOrigin;await mount();await click(button('Start Builder'));
    assert.equal(sends().length,0,'other origins cannot dispatch managed work');
    assert.equal(calls.filter(c=>c.cmd==='portal_conversation_new').length,0);await unmount();
  }
  reset('simple');await mount();const changedOrigin=await start();sessions.find(s=>s.id==='a').face_id='builder';await finish(changedOrigin,'r1');
  assert.equal(sends().length,1,'report refuses an origin whose face changed');await unmount();
  originFace='assistant';originLabel='Assistant';
  // Received pre-question text is preserved by the real stream and history renderer.
  reset('simple');history.a=[row('prompt','user','Plan'),row('lead','assistant','The plan before a question.'),row('answer','user','Operator answer to: Which?\nBlue'),row('final','assistant','Builder tool results:\ndone')];
  await mount();assert.match(document.querySelector('.transcript').textContent,/The plan before a question/);
  const mark={conversation_id:'a',count:0,last_id:null,text:'Plan'};
  assert.equal(turnAnswerRowId(history.a,mark,'Builder tool results:\ndone','a'),'final','operator answer does not end turn matching');
  assert.equal(turnAnswerRowId(history.a,mark,'The plan before a question.\nBuilder tool results:\ndone','a'),'final','multiple stored assistant segments match one accumulated stream');
  assert.equal(hasTurnRow([{...history.a[1],metadata:{request_id:'r'}}],'a','r',null,'The plan before a question.\nNew text',null),false,'a stored pre-question segment must not hide newer chunks from the same request');
  const duplicate={...history.a.at(-1),id:'second-id'};
  assert.equal(singleRenderRows([...history.a,duplicate]).filter(m=>m.content===duplicate.content).length,1);
  history.a.push(duplicate);await click(document.querySelector('.conversation-list button'));
  assert.equal([...document.querySelectorAll('.msg.assistant')].filter(n=>n.textContent.includes('Builder tool results:')).length,1,'final renders once from duplicate stored rows');await unmount();
  for(const [code,outcome] of [['stream_deadline','ran_out_of_time'],['approval_expired','approval_expired'],['question_unanswered','question_unanswered']])assert.equal(managedOutcome('stopped',code,code==='question_unanswered'),outcome);
  const sample={id:'x',origin:'a',agent:'builder',name:'Builder',title:'t',session:{id:'b'},request:'r',state:'working',content:'x'.repeat(7000),rows:[],activity:[],report:'pending'};
  assert.equal(managedFinal([row('p','user','Brief'),row('f','assistant','This run'),row('later','user','Another dispatch'),row('later-f','assistant','Unrelated')],{...sample,brief:'Brief'}),'This run','manual readback cannot borrow a later turn');
  assert.equal(managedReport({...sample,state:'finished'}).split('--- report ---\n')[1].length,6000);
  localStorage.setItem(managedKey('scope'),JSON.stringify([sample]));assert.equal(readManaged('scope')[0].state,'interrupted');
  localStorage.setItem(managedKey('scope'),JSON.stringify([{...sample,report:'sending',reportDispatched:false}]));assert.equal(readManaged('scope')[0].report,'pending','closing before report dispatch preserves the manual send');
  reset('simple');await mount();const same=await start();const sameText=`Builder report: Build Dayline\nsession: ${same}\noutcome: finished\nsteps: 0 (0 failed)\n--- report ---\nBuilder tool results:\nfs_write: exit code 0`;
  history.a.push(row('earlier-identical','user',sameText));await finish(same,'r1');assert.equal(sends().length,2,'an identical report from an older run does not suppress this dispatch');await unmount();
  // Execute the exact native shim before unguarded document code in an opaque origin.
  const native=fs.readFileSync(path.join(__dirname,'../src-tauri/src/portal.rs'),'utf8');
  const shim=/const PREVIEW_STORAGE_SHIM: &str = r#"([\s\S]*?)"#;/.exec(native)[1];
  const page=new JSDOM('<!doctype html>'+shim+`<script>localStorage.setItem('key',12);sessionStorage.x='yes';window.result=[localStorage.getItem('key'),localStorage.length,localStorage.key(0),sessionStorage.x];</script>`,{runScripts:'dangerously'});
  assert.deepEqual([...page.window.result],['12',1,'key','yes']);assert.equal(page.window.localStorage.getItem('absent'),null);page.window.localStorage.clear();assert.equal(page.window.localStorage.length,0);page.window.close();
  console.log('GL12 UI: PASS (Assistant/Planner Simple/Pro managed dispatch/navigation and invalid-origin refusal, shared decisions, report contract/dedup/reload/interruption, follow-up, manual mode, pre-question text, single final, opaque-origin storage)');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(root)await unmount();global.setTimeout=originalTimeout;global.clearTimeout=originalClear;dom.window.close();});
