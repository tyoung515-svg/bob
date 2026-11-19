// Offline GL6 regressions: real PortalApp, native/event fixtures, explicit scroll geometry.
const assert = require('node:assert/strict');
process.env.TZ = 'Asia/Bangkok';
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost'});
for (const name of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','localStorage']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const listeners = new Set();
const events = require.resolve('@tauri-apps/api/event');
require.cache[events] = {id:events,filename:events,loaded:true,exports:{listen:async(_topic,fn)=>{listeners.add(fn);return ()=>listeners.delete(fn);}}};
const React = require('react'), {act} = React, {createRoot} = require('react-dom/client');
const PortalApp = require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE} = require('../src/portal/SettingsView.tsx');
const {localMessageTime,localSessionTime} = require('../src/portal/local-time.ts');
const hello = {identity:{sub:'gl6',gateway:'fixture',faces:[{id:'assistant',name:'Assistant'}],may_touch:[],may_not_touch:[]},server:{boot:'test',version:'test'},gaps:[]};
const conversation = {id:'c',title:'GL6 session',face_id:'assistant'};
const card = (id,conversation_id='c') => ({request:{id,conversation_id,task_id:'task-'+id,agent:'assistant',operation:'pc.run',args:{shell:'powershell',argv:['powershell','-Command','$c=1'],cwd:'C:\\task'},expires_at:Date.now()/1000+600},mode:'approve',root:null,until:null,protected_reason:null,running:false});
let queue, state, approvals, root, calls;
const timers = new Map(), originalTimeout = global.setTimeout, originalClear = global.clearTimeout;
// Drive the adaptive PC and repair polls without wall-clock waits.
global.setTimeout = (fn,ms,...args) => [5000,15000,30000].includes(ms) ? (timers.set(ms,fn),{gl6:ms}) : originalTimeout(fn,ms,...args);
global.clearTimeout = id => {if(id?.gl6)timers.delete(id.gl6);else originalClear(id);};
async function invoke(cmd,args) {
  calls.push({cmd,args});
  switch(cmd) {
    case 'portal_connection_get': return {connection_configured:true};
    case 'portal_hello': return hello;
    case 'portal_catalog': return {faces:hello.identity.faces,models:[],backends:[],capabilities:{actions:[],warnings:[]}};
    case 'portal_conversations': return {items:[conversation],limit:50,offset:0};
    case 'portal_conversation_open': return {conversation,messages:[{id:'m',role:'assistant',content:'fixture',created_at:'2026-11-16T08:51:00Z'}],has_more:false};
    case 'portal_conversation_new': return {conversation:{...conversation,title:args.title}};
    case 'portal_pins': return {};
    case 'portal_approvals': return {items:approvals};
    case 'pc_poll': return queue;
    case 'runtime_repair_status': return {repairable:true,observation:{state,observed:state==='healthy'?'Health checks passed.':'Two consecutive session probes failed.'}};
    case 'portal_send': return {request_id:'r',accepted:true};
    default: throw Error('Unexpected GL6 fixture '+cmd);
  }
}
const flush = async (fn=()=>{}) => act(async()=>{await fn();await new Promise(resolve=>originalTimeout(resolve,0));});
const panel = () => document.getElementById('activity-panel');
const edge = () => document.querySelector('[aria-label^="Expand Activity"]');
const strip = () => document.querySelector('.activity-attention');
const jump = () => document.querySelector('.activity-jump button');
async function click(node) {assert.ok(node);await flush(()=>node.click());}
async function emit(event,data) {await flush(()=>{for(const fn of listeners)fn({payload:JSON.stringify({event,data:{conversation_id:'c',...data}})});});}
async function poll(ms) {if(ms===2000)ms=timers.has(15000)?15000:5000;assert.ok(timers.has(ms));const fn=timers.get(ms);timers.delete(ms);await flush(fn);}
async function mount(experience) {
  localStorage.clear(); timers.clear(); calls=[]; state='healthy'; approvals=[]; queue={requests:[],outcomes:{}};
  localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));
  localStorage.setItem('bob:panels:v1:'+JSON.stringify(['fixture','gl6']),JSON.stringify({left:false,right:true}));
  root=createRoot(document.getElementById('root'));
  await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));
  await click(document.querySelector('.conversation-list button'));
}
async function unmount() {await flush(()=>root.unmount());root=null;}
function geometry(height=1200) {
  let size=height, top=0;
  Object.defineProperties(panel(),{
    clientHeight:{configurable:true,get:()=>300},
    scrollHeight:{configurable:true,get:()=>size},
    scrollTop:{configurable:true,get:()=>top,set:value=>{top=Math.max(0,Math.min(value,size-300));}},
  });
  panel().getBoundingClientRect=()=>({top:0,bottom:300});
  // Keep 200 px of sessions/routing below the trail: jump must not bury the last step.
  const rect=dom.window.HTMLElement.prototype.getBoundingClientRect;
  dom.window.HTMLElement.prototype.getBoundingClientRect=function(){return this.classList.contains('turn-activity')?{top:-top,bottom:size-200-top}:rect.call(this);};
  return {grow:()=>{size+=150;},scroll:async value=>flush(()=>{panel().scrollTop=value;panel().dispatchEvent(new Event('scroll'));})};
}
(async()=>{
  const sent=new Date('2026-11-16T08:51:00Z'), today=new Date('2026-11-16T12:00:00Z');
  assert.equal(sent.getHours(),15,'fixed +07 test timezone');
  assert.equal(localMessageTime(sent.toISOString(),today),sent.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}));
  const prior=new Date('2026-11-16T16:59:00Z'), afterMidnight=new Date('2026-11-16T17:01:00Z');
  assert.equal(localMessageTime(prior.toISOString(),afterMidnight),prior.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})+' · '+prior.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}),'local calendar day boundary adds a date');
  assert.equal(localMessageTime(afterMidnight.toISOString(),afterMidnight),afterMidnight.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}));
  assert.equal(localSessionTime(sent),sent.toLocaleString(undefined,{year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}));
  assert.equal(localMessageTime('invalid'), 'invalid');

  for (const experience of ['simple','pro']) {
    await mount(experience);
    assert.ok(panel().hidden);
    const composer=document.getElementById('composer'); composer.focus();
    queue={requests:[card('a'),card('other','other')],outcomes:{}};await poll(2000);
    assert.ok(panel().hidden,'new PC request cannot reopen');
    assert.equal(document.activeElement,composer,'attention never steals focus');
    assert.match(edge().textContent,/\(2\)/,'all conversations count');
    assert.match(edge().getAttribute('aria-label'),/2 items.*attention/);
    assert.ok(edge().classList.contains('attention'));
    assert.match(strip().textContent,/2 items need your attention/,'background approvals surface in the aggregate strip');
    assert.ok(document.querySelector('.transcript .inline-pc-approval [aria-label="Native PC approval"]'));
    await click(edge());assert.ok(!panel().hidden);assert.equal(strip(),null);
    await click(document.querySelector('[aria-label="Collapse Activity"]'));
    queue={requests:[card('b')],outcomes:{}};await poll(2000);assert.ok(panel().hidden,'a second request also stays hidden');
    state='wedged';await poll(30000);assert.ok(panel().hidden,'new stuck incident stays hidden');
    assert.match(edge().getAttribute('aria-label'),/2 items/);assert.equal(document.querySelectorAll('.activity-attention').length,1);
    queue={requests:[],outcomes:{}};await poll(2000);
    assert.equal(strip().querySelector('span').textContent,document.querySelector('#repair-card p').textContent);
    await click(strip().querySelector('button'));assert.ok(!panel().hidden);
    await click(document.querySelector('[aria-label="Collapse Activity"]'));
    state='healthy';await poll(30000);assert.equal(strip(),null);assert.ok(!edge().classList.contains('attention'));
    approvals=[{id:'approval',conversation_id:'c'}];
    await emit('approval_request',{approval_id:'approval',action:'fixture'});
    assert.ok(panel().hidden,'server approval stays collapsed');
    assert.match(document.querySelector('.notice').textContent,/The server asked for approval.*fixture.*Needs you/);
    assert.ok([...document.querySelectorAll('button')].some(n=>n.title==='Needs you · 1 waiting'));
    await click(edge());
    await emit('approval_request',{approval_id:'approval',action:'fixture'});
    assert.equal(panel().getAttribute('aria-label'),'Activity','arriving approval does not change the open Activity panel');
    assert.equal(document.querySelector('.msg time').getAttribute('datetime'),'2026-11-16T08:51:00Z');
    assert.equal(document.querySelector('.msg time').textContent,' · '+localMessageTime('2026-11-16T08:51:00Z'));
    const before=localSessionTime();
    await click([...document.querySelectorAll('button')].find(n=>n.textContent==='New session'));
    const title=calls.find(c=>c.cmd==='portal_conversation_new').args.title;
    assert.ok([before,localSessionTime()].some(time=>title==='Assistant · '+time),'new session title uses local date/time');
    assert.ok(!calls.some(c=>['pc_decide','pc_set_mode','runtime_repair_start'].includes(c.cmd)),'display never grants or repairs');
    await unmount();
  }

  await mount('simple');
  const g=geometry();
  await emit('tool_activity',{request_id:'old',phase:'result',tool:'finished',preview:'long finished trail'});
  await click(edge());
  assert.equal(panel().scrollTop,0,'opening finished history starts at top');
  assert.equal(jump().textContent,'Skip to bottom ↓');
  await click(jump());assert.equal(panel().scrollTop,700);assert.equal(jump(),null);
  await g.scroll(900);queue={requests:[],outcomes:{}};await poll(2000);
  assert.equal(panel().scrollTop,900,'unchanged native polls do not move readers in the footer');
  await g.scroll(850);g.grow();
  await emit('tool_activity',{request_id:'old',phase:'result',tool:'near-bottom'});
  assert.equal(panel().scrollTop,850,'within 80 px of panel bottom follows new steps to trail end');assert.equal(jump(),null);
  await g.scroll(800);g.grow();
  await emit('tool_activity',{request_id:'old',phase:'result',tool:'trail-bottom'});
  assert.equal(panel().scrollTop,1000,'following remains active at the trail end above routing');
  await g.scroll(200);g.grow();
  await emit('tool_activity',{request_id:'old',phase:'result',tool:'scrolled-up'});
  assert.equal(panel().scrollTop,200,'scrolled-up position is preserved');assert.ok(jump());
  await click(jump());assert.equal(panel().scrollTop,1150);assert.equal(jump(),null);
  const box=document.getElementById('composer');
  const setter=Object.getOwnPropertyDescriptor(dom.window.HTMLTextAreaElement.prototype,'value').set;
  await flush(()=>{setter.call(box,'fixture turn');box.dispatchEvent(new Event('input',{bubbles:true}));});
  await click([...document.querySelectorAll('button')].find(n=>n.textContent==='Send to Bob'));
  await emit('tool_activity',{request_id:'r',phase:'start',tool:'live'});
  await g.scroll(0);assert.equal(jump().textContent,'Jump to latest ↓');
  await click(jump());assert.equal(jump(),null);
  await click(document.querySelector('[aria-label="Collapse Activity"]'));
  g.grow();await emit('tool_activity',{request_id:'r',phase:'result',tool:'hidden update'});
  assert.ok(panel().hidden);await click(edge());assert.equal(panel().scrollTop,0,'reopening does not follow hidden updates');
  await unmount();
  console.log('GL6 UI: PASS (Simple/Pro collapse, attention/Show/focus/isolation, server approval notice/badge, scroll follow/jump/history, +07 local times)');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(root)await unmount();global.setTimeout=originalTimeout;global.clearTimeout=originalClear;dom.window.close();});
