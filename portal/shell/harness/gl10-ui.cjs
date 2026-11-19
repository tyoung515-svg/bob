// Offline GL10 regressions: real PortalApp/PcCard, native queue and broker fixtures.
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost'});
for (const name of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','localStorage']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const listeners = new Set(), events = require.resolve('@tauri-apps/api/event');
require.cache[events] = {id:events,filename:events,loaded:true,exports:{listen:async(_topic,fn)=>{listeners.add(fn);return ()=>listeners.delete(fn);}}};
const React = require('react'), {act} = React, {createRoot} = require('react-dom/client');
const PortalApp = require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE} = require('../src/portal/SettingsView.tsx');
const hello = {identity:{sub:'gl10',gateway:'fixture',faces:[{id:'assistant',name:'Assistant'}],may_touch:[],may_not_touch:[]},server:{boot:'test',version:'test'},gaps:[]};
const conversation = {id:'c',title:'GL10 session',face_id:'assistant'};
let root, queue, calls, history, request, pendingQuestion, now = 1800000000000;
const originalNow=Date.now, originalTimeout=global.setTimeout, originalClear=global.clearTimeout, timers=new Map();
Date.now=()=>now;
global.setTimeout=(fn,ms,...args)=>{if(ms>=1000){const id={ms};timers.set(id,fn);return id;}return originalTimeout(fn,ms,...args);};
global.clearTimeout=id=>{if(timers.has(id))timers.delete(id);else originalClear(id);};
const card=(id,conversation_id='c',seconds=121)=>({request:{id,conversation_id,task_id:'task-'+id,agent:'assistant',operation:'pc.run',args:{shell:'powershell',argv:['-Command','Get-Date'],cwd:'C:\\task'},expires_at:now/1000+seconds},mode:'approve',root:null,until:null,protected_reason:null,running:false});
async function invoke(cmd,args) {
  calls.push({cmd,args});
  switch(cmd) {
    case 'portal_connection_get': return {connection_configured:true};
    case 'portal_hello': return hello;
    case 'portal_catalog': return {faces:hello.identity.faces,models:[],backends:[],capabilities:{actions:[],warnings:[]}};
    case 'portal_conversations': return {items:[conversation],limit:50,offset:0};
    case 'portal_conversation_open': return {conversation,messages:history,has_more:false};
    case 'portal_messages_more': return {items:history,has_more:false};
    case 'portal_pins': return {};
    case 'portal_approvals': return {items:[]};
    case 'pc_poll': return queue;
    case 'runtime_repair_status': return {repairable:true,observation:{state:'healthy',observed:'Health checks passed.'}};
    case 'portal_send': return {request_id:'r'+(++request),accepted:true};
    case 'portal_stop': return {stopped:true,code:'stopped'};
    case 'pc_decide': queue={...queue,requests:queue.requests.filter(c=>c.request.id!==args.requestId)};return {};
    case 'portal_answer':
      assert.equal(args.question_id,'q');
      history=[...history,{id:'answer',role:'user',content:'Operator answer to: '+pendingQuestion+'\n'+args.answer}];
      return {resolved:true,delivered_to_turn:true};
    default: throw Error('Unexpected GL10 fixture '+cmd);
  }
}
const flush=async(fn=()=>{})=>act(async()=>{await fn();await new Promise(resolve=>originalTimeout(resolve,0));});
const button=(label,scope=document)=>[...scope.querySelectorAll('button')].find(b=>b.textContent===label);
const inline=()=>document.querySelector('.transcript .inline-pc-approval');
const panel=()=>document.getElementById('activity-panel');
async function click(node){assert.ok(node,'click target');await flush(()=>node.click());}
async function type(node,value){const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node),'value').set;await flush(()=>{setter.call(node,value);node.dispatchEvent(new Event('input',{bubbles:true}));});}
async function emit(event,data){await flush(()=>{for(const fn of listeners)fn({payload:JSON.stringify({event,data})});});}
async function timer(ms,advance=0){const item=[...timers].find(([id])=>id.ms===ms);assert.ok(item,'timer '+ms);timers.delete(item[0]);now+=advance;await flush(item[1]);}
async function poll(){const delay=[...timers.keys()].find(id=>id.ms===5000 || id.ms===15000)?.ms;assert.ok(delay,'adaptive PC poll');await timer(delay);}
async function send(){await type(document.getElementById('composer'),'Fixture turn '+(request+1));await click(button('Send to Bob'));return 'r'+request;}
async function mount(experience){
  localStorage.clear();timers.clear();calls=[];history=[];queue={requests:[],outcomes:{}};request=0;
  localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));
  localStorage.setItem('bob:panels:v1:'+JSON.stringify(['fixture','gl10']),JSON.stringify({left:false,right:true}));
  root=createRoot(document.getElementById('root'));
  await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));
  await click(document.querySelector('.conversation-list button'));
}
async function unmount(){await flush(()=>root.unmount());root=null;}
const timeoutLine='Bob ran out of time on this turn. What it wrote is kept above; send a message to carry on.';
function checkTerminal(line,text){
  const terminal=document.querySelector('.transcript .terminal');assert.ok(terminal);
  assert.equal(terminal.querySelector('.typed-refusal').textContent,line);
  assert.ok(terminal.textContent.indexOf(text)<terminal.textContent.indexOf(line),'outcome follows the answer');
  assert.equal(document.querySelectorAll('.typed-refusal').length,1);
  assert.ok(!document.querySelector('.transcript').textContent.includes('Turn ended · stopped'));
  assert.equal(terminal.querySelector('details').open,false,'technical words start behind Details');
  assert.ok(!document.querySelector('.composer').textContent.includes(line),'no duplicate composer notice');
}
(async()=>{
  for(const experience of ['simple','pro']) {
    await mount(experience);
    let rid=await send();
    const streaming=document.querySelector('.streaming');
    assert.match(streaming.textContent,experience==='simple'?/Thinking…/:/Waiting for the first server chunk…/);
    assert.ok(document.querySelector('.portal-sidebar .agent-row').classList.contains('working'));
    assert.equal(document.querySelector('.portal-sidebar .caveat'),null,'no window-wide navigation lock');
    await emit('tool_activity',{request_id:rid,phase:'start',tool:'pc_run'});
    const transcript=document.querySelector('.transcript');
    Object.defineProperty(transcript,'scrollHeight',{configurable:true,get:()=>1500});
    const composer=document.getElementById('composer');composer.focus();
    queue={requests:[card('a'),card('other','other')],outcomes:{}};await poll();
    assert.ok(panel().hidden);assert.ok(inline());
    assert.equal(document.activeElement,composer,'arrival preserves focus');
    assert.equal(transcript.scrollTop,1500,'a following reader sees the arriving card without another chunk');
    assert.match(document.querySelector('.activity-attention').textContent,/2 items need your attention/,'background approval is visible in aggregate strip');
    assert.match(document.querySelector('[aria-label^="Expand Activity"]').textContent,/\(2\)/,'window attention includes background conversations');
    assert.equal(document.querySelector('.conversation-header .chip').textContent,'Assistant needs your OK to use this PC');
    assert.equal(document.querySelector('.working-line').textContent.trim(),'Assistant needs your OK to use this PC');
    assert.ok(!document.querySelector('.streaming h2').textContent.includes('typing'));
    assert.match(inline().textContent,/3 minutes left to decide/);
    const announcement=inline().querySelector('[role="status"]');assert.equal(announcement.getAttribute('aria-live'),'polite');
    assert.ok(!inline().querySelector('.set-card').hasAttribute('aria-live'),'countdown is outside the live region');
    assert.ok(!button('Allow once on this PC',inline()).disabled,'native button remains keyboard reachable');
    assert.ok(!calls.some(c=>c.cmd==='pc_decide'),'rendering grants nothing');
    await timer(1000,1000);assert.match(inline().textContent,/2 minutes left to decide/);
    assert.equal(inline().querySelector('[role="status"]'),announcement,'countdown does not replace announcement');
    assert.equal(document.activeElement,composer);
    await click(document.querySelector('[aria-label^="Expand Activity"]'));
    assert.ok(!panel().hidden);assert.ok(inline(),'inline also shown with Activity open');
    const activityCard=panel().querySelector('[aria-label="Native PC approval"]');assert.ok(activityCard);
    const labels=node=>[...node.querySelectorAll('button')].map(b=>b.textContent);
    assert.deepEqual(labels(inline()),labels(activityCard),'same PcCard actions');
    await click(button('Allow once on this PC',inline()));assert.equal(inline(),null,'decision disappears via native refresh');
    assert.deepEqual(calls.filter(c=>c.cmd==='pc_decide').at(-1).args,{requestId:'a',decision:'allow_once',taskFolder:'C:\\task'});
    queue={requests:[card('activity')],outcomes:{}};await poll();
    await click(button('Deny PC action',panel()));assert.equal(inline(),null,'Activity uses same decision/refresh path');
    assert.deepEqual(calls.filter(c=>c.cmd==='pc_decide').at(-1).args,{requestId:'activity',decision:'deny',taskFolder:'C:\\task'});
    queue={requests:[{...card('running'),running:true}],outcomes:{}};await poll();assert.equal(inline(),null,'running requests disappear');
    queue={requests:[card('decided')],outcomes:{decided:{status:'denied',conversation_id:'c'}}};await poll();assert.equal(inline(),null,'decided native receipts cannot show as waiting');
    await click(document.querySelector('[aria-label="Collapse Activity"]'));
    queue={requests:[card('expires','c',61)],outcomes:{}};await poll();
    await timer(1000,1000);assert.match(inline().textContent,/1 minute left to decide/);
    await timer(60000,60000);assert.equal(inline(),null,'expiry without another native poll removes card');
    queue={requests:[card('stop')],outcomes:{}};await poll();assert.ok(inline());
    await click(button('Stop turn'));await emit('stopped',{request_id:rid,code:'stop_requested'});
    assert.equal(inline(),null,'stop clears a still-stale native request');await poll();assert.equal(inline(),null);
    queue={requests:[],outcomes:{}};await poll();
    rid=await send();await emit('chunk',{request_id:rid,content:'An answer kept before timeout.'});
    const sendsBefore=calls.filter(c=>c.cmd==='portal_send').length;
    await emit('stopped',{request_id:rid,code:'stream_deadline'});
    checkTerminal(timeoutLine,'An answer kept before timeout.');
    assert.match(document.querySelector('.terminal details').textContent,/without a complete frame/);
    assert.match(document.querySelector('.terminal details').textContent,/stream_deadline/);
    assert.equal(calls.filter(c=>c.cmd==='portal_send').length,sendsBefore,'no automatic retry');
    await emit('stopped',{request_id:rid,code:'stream_deadline'});checkTerminal(timeoutLine,'An answer kept before timeout.');
    rid=await send();await emit('chunk',{request_id:rid,content:'Before approval expired.'});
    await emit('stopped',{request_id:rid,code:'approval_expired'});
    checkTerminal('The approval request expired before you answered. What Bob wrote is kept above; send a message to carry on.','Before approval expired.');
    rid=await send();pendingQuestion='Which folder should I read?';
    await emit('question',{question_id:'q',question:pendingQuestion,choices:['The project folder']});
    await emit('stopped',{request_id:rid,code:'stream_deadline'});
    assert.equal(document.querySelector('.ask-card'),null,'an ended question is no longer answerable');
    assert.match(document.querySelector('.terminal .typed-refusal').textContent,/waiting for your answer/);
    rid=await send();await emit('question',{question_id:'q',question:pendingQuestion,choices:['The project folder']});
    await click(button('The project folder'));
    assert.deepEqual(calls.find(c=>c.cmd==='portal_answer').args,{question_id:'q',answer:'The project folder'},'outbound answer unchanged');
    const user=document.querySelector('.transcript .msg.user:not(.pending)');
    assert.match(user.querySelector('h2').textContent,/^You/);assert.equal(user.querySelector('.answered-question small').textContent,'You answered');
    assert.equal(user.querySelector('blockquote').textContent,pendingQuestion);assert.equal(user.querySelector('.content').textContent,'The project folder');
    assert.ok(!user.textContent.includes('Operator'));assert.ok(history[0].content.startsWith('Operator answer to: '),'stored content unchanged');
    await emit('stopped',{request_id:rid,code:'stopped'});
    await click(document.querySelector('.conversation-list button'));
    const loaded=document.querySelector('.msg.user');
    assert.equal(loaded.querySelector('blockquote').textContent,pendingQuestion,'history quotes the question too');
    assert.equal(loaded.querySelector('.content').textContent,'The project folder','history shows only the answer as content');
    assert.ok(!loaded.textContent.includes('Operator'));
    assert.equal(loaded.innerHTML,user.innerHTML,'history and live readback use the same presentation');
    history=[{id:'ordinary',role:'user',content:'My ordinary message\nkeeps its lines.'}];
    await click(document.querySelector('.conversation-list button'));
    assert.equal(document.querySelector('.msg.user .content').textContent,history[0].content);
    assert.equal(document.querySelector('.answered-question'),null,'ordinary user messages unchanged');
    assert.ok(timers.size<10,'no countdown busy loop or leaked timers');
    assert.ok(!calls.some(c=>c.cmd==='pc_set_mode'),'display never changes native modes');
    await unmount();assert.equal(timers.size,0,'timers clean up on unmount');
  }
  console.log('GL10 UI: PASS (Simple/Pro inline shared PC decisions, focus/status/countdown/expiry/stop, ordered terminal explanations, live/history answers, Simple wording, no retries)');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(root)await unmount();Date.now=originalNow;global.setTimeout=originalTimeout;global.clearTimeout=originalClear;dom.window.close();});
