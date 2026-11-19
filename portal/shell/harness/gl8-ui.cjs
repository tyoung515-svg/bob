// Offline GL8 checks against real components and PortalApp IPC/event paths.
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost',pretendToBeVisual:true});
for (const name of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','localStorage']) global[name] = dom.window[name];
global.requestAnimationFrame = fn => setTimeout(fn,0);
global.IS_REACT_ACT_ENVIRONMENT = true;
const listeners = new Set(), events = require.resolve('@tauri-apps/api/event');
require.cache[events] = {id:events,filename:events,loaded:true,exports:{listen:async(_topic,fn)=>{listeners.add(fn);return ()=>listeners.delete(fn);}}};
const React = require('react'), {act} = React, {createRoot} = require('react-dom/client'), el = React.createElement;
const {ExperienceContext} = require('../src/portal/experience.ts');
const {ActivityTrail,activitySteps} = require('../src/portal/ActivityTrail.tsx');
const {ActionCardsContext,validateAction} = require('../src/portal/ActionCard.tsx');
const {PortalMarkdown} = require('../src/portal/PortalMarkdown.tsx');
const {PcCard} = require('../src/portal/PcAccess.tsx');
const {createCalendarStore,isDue} = require('../src/portal/calendar.ts');
const {rememberDefaultTitle,firstMessageTitle} = require('../src/portal/session-names.ts');
const PortalApp = require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE} = require('../src/portal/SettingsView.tsx');
const faces = ['assistant','builder','fixer','orchestrator'].map(id=>({id,name:id[0].toUpperCase()+id.slice(1)}));
const calendar = {type:'calendar',title:'Review the draft',date:'2028-02-29',start:'14:30',minutes:45,repeat:'weekly',note:'<img src=x onerror=alert(1)>'};
const handoff = {type:'handoff',agent:'builder',title:'Build the tracker',brief:'Create a simple tracker.\nAsk before sending anything.'};
const block = a => '```bob-action\n'+JSON.stringify(a)+'\n```';
let root;
const flush = async (fn=()=>{}) => act(async()=>{await fn();await new Promise(resolve=>setTimeout(resolve,0));});
async function mount(node) {root=createRoot(document.getElementById('root'));await flush(()=>root.render(node));}
async function unmount() {await flush(()=>root.unmount());root=null;}
const button = label => [...document.querySelectorAll('button')].find(b=>b.textContent===label);
async function click(node) {assert.ok(node,'click target');await flush(()=>node.click());}
const simple = node => el(ExperienceContext.Provider,{value:'simple'},node);
async function type(value,id='composer') {const box=document.getElementById(id);const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(box),'value').set;await flush(()=>{setter.call(box,value);box.dispatchEvent(new Event('input',{bubbles:true}));});}

(async()=>{
  const activity = [
    {request_id:'r',phase:'start',tool:'web_search',args:JSON.stringify({query:'fixture query'})},
    {request_id:'r',phase:'result',tool:'web_search',preview:'full web result',elapsed_ms:25,ok:true},
    {request_id:'r',phase:'start',tool:'pc_run',args:JSON.stringify({argv:['cmd','/C','exit','7']})},
    {request_id:'r',phase:'result',tool:'pc_run',preview:'{"request_id":"native-id","status":"failed","result":{',elapsed_ms:50,ok:true},
    {request_id:'r',phase:'start',tool:'fs_read',args:'{"path":"C:\\folder\\report.txt"}'},
  ];
  const queue = {requests:[],outcomes:{'native-id':{conversation_id:'c',operation:'pc.run',status:'failed',exit_code:7,receipt_path:'fixture-receipt',stdout:'full native result'}}};
  assert.equal(activitySteps(activity,queue,'c').length,3,'native id joins even truncated JSON');
  const early={requests:[],outcomes:{early:{conversation_id:'c',operation:'pc.run',time:2,status:'done',request_args:{argv:['cmd','/C','echo','fixture']}}}};
  assert.equal(activitySteps([{request_id:'r',phase:'start',observed_ms:1000,tool:'pc_run',args:'{"argv":["cmd","/C","echo","fixture"]}'}],early,'c').length,1,'native polling before broker result does not duplicate');
  assert.equal(activitySteps([{request_id:'r',phase:'start',observed_ms:5000,tool:'pc_run',args:'{"argv":["cmd","/C","echo","fixture"]}'}],early,'c').length,2,'older receipts cannot masquerade as a current call');
  for(const mode of ['simple','pro']) {
    await mount(el(ExperienceContext.Provider,{value:mode},el(ActivityTrail,{activity,queue,conversationId:'c'})));
    assert.equal(document.querySelectorAll('.activity-step').length,3);
    assert.match(document.querySelector('.turn-activity > summary').textContent,/3 steps · 1 failed/);
    assert.equal(document.querySelector('.tool').textContent,mode==='simple'?'Searched the web':'web_search');
    assert.match(document.querySelectorAll('.activity-step')[2].textContent,/running/);
    assert.equal(document.querySelector('.step-target').textContent,'fixture query');
    const disclosure=document.querySelector('.activity-step');assert.equal(disclosure.open,false);
    await click(disclosure.querySelector('summary'));assert.equal(disclosure.open,true);
    assert.match(disclosure.textContent,/full web result/);
    await flush(()=>disclosure.querySelector('summary').dispatchEvent(new KeyboardEvent('keydown',{key:' ',bubbles:true,cancelable:true})));
    assert.equal(disclosure.open,false,'Space toggles disclosure');
    assert.ok(document.querySelector('li.failed .pc-receipt'));assert.match(document.querySelector('.pc-receipt').textContent,/fixture-receipt/);
    await unmount();
  }

  // Strict schema, real dates, exact types, bounds, catalog membership.
  assert.ok(validateAction(JSON.stringify(calendar),faces));
  assert.ok(validateAction(JSON.stringify({...calendar,title:'🗓'.repeat(80),note:'🗓'.repeat(500)}),faces),'length bounds count Unicode characters');
  assert.ok(validateAction(JSON.stringify({...calendar,date:'0001-01-01'}),faces));
  for(const patch of [{date:'2027-02-29'},{date:'2028-04-31'},{date:'0000-01-01'},{start:'24:00'},{minutes:'30'},{minutes:4},{minutes:1441},{minutes:30.5},{repeat:'yearly'},{title:'x'.repeat(81)},{note:'x'.repeat(501)},{extra:'no'},{note:null}]) assert.equal(validateAction(JSON.stringify({...calendar,...patch}),faces),null,JSON.stringify(patch));
  assert.ok(validateAction(JSON.stringify(handoff),faces));
  for(const patch of [{agent:'missing'},{brief:'x'.repeat(4001)},{brief:''},{title:4}]) assert.equal(validateAction(JSON.stringify({...handoff,...patch}),faces),null);
  assert.equal(validateAction(JSON.stringify(handoff),faces.filter(f=>f.id!=='builder')),null);
  assert.equal(validateAction(JSON.stringify(handoff),[{id:'builder',name:'Builder',allowed:false}]),null);
  let data='[]',saves=0,opens=0,handed=null;
  const store=createCalendarStore(async(cmd,args)=>{if(cmd==='store_load')return data;if(cmd==='store_save'){data=args.data;saves++;return '';}throw Error(cmd);});
  const context={conversationId:'c',messageId:'m',faces,store,onTasks:()=>opens++,onHandoff:async a=>{handed=a;}};
  const renderCards=(text,mode='simple',role='assistant',extra={})=>el(ExperienceContext.Provider,{value:mode},el(ActionCardsContext.Provider,{value:{...context,...extra}},el(PortalMarkdown,{text,role})));
  await mount(renderCards(block(calendar)+'\n'+block(handoff)+'\n'+block(calendar)));
  assert.equal(saves,0,'rendering never adds a task');assert.equal(document.querySelectorAll('.bob-action-card').length,2);
  assert.equal(document.querySelector('img'),null,'card text never becomes markup');
  await flush(()=>{button('Add to Tasks').click();button('Add to Tasks').click();});
  assert.equal(saves,1);assert.equal(JSON.parse(data).length,1);assert.equal(JSON.parse(data)[0].who,'person');
  assert.equal(isDue({entry:JSON.parse(data)[0],date:calendar.date},new Date('2028-02-29T18:00:00')),false);
  await click(button('Open Tasks'));assert.equal(opens,1);
  await click(button('Open in Builder instead'));assert.equal(handed.brief,handoff.brief);
  await unmount();await mount(renderCards(block(calendar)));
  assert.ok(button('Open Tasks'),'durable entry identity survives a remount');assert.equal(button('Add to Tasks'),undefined);assert.equal(saves,1);
  await unmount();
  data='[]';await mount(renderCards(block(calendar)));assert.ok(button('Open Tasks'),'added receipt survives intentional task deletion');await unmount();
  await mount(renderCards(block({...calendar,date:'invalid'})+'\n'+block(calendar)+'\n'+block(handoff),'pro'));
  assert.equal(document.querySelectorAll('.bob-action-card').length,1);assert.equal(document.querySelectorAll('.md-code').length,2,'invalid and surplus are ordinary code in Pro');await unmount();
  await mount(renderCards(block({...calendar,date:'invalid'}),'simple'));assert.equal(document.querySelectorAll('.md-code,.bob-action-card').length,0);await unmount();
  for(const mode of ['simple','pro']) {await mount(renderCards('```bob-action\n{"type":"calendar"',mode,'assistant',{streaming:true}));assert.equal(document.querySelectorAll('.md-code,.bob-action-card').length,0);await unmount();}
  await mount(renderCards(block(handoff),'simple','assistant',{streaming:true}));assert.ok(button('Open in Builder instead').disabled,'complete streaming card waits for durable message identity');await unmount();
  await mount(renderCards(block(calendar),'simple','user'));assert.equal(document.querySelectorAll('.bob-action-card').length,0);await unmount();

  let decisions=[];
  const infoCard={request:{id:'i',task_id:'t',agent:'Builder',conversation_id:'c',operation:'pc.info',args:{},expires_at:Date.now()/1000+600},mode:'approve',root:null,until:null,protected_reason:null,running:false};
  await mount(simple(el(PcCard,{card:infoCard,invoke:async(cmd,args)=>decisions.push({cmd,args}),refresh:async()=>{}})));
  assert.equal(document.querySelector('h3').textContent,"Share this PC's hardware summary (processor, graphics, memory, disk space, Windows version) with Builder?");
  assert.equal(document.querySelector('[aria-label="PC task folder"]'),null);
  for(const [label,decision] of [['Allow once','allow_once'],['Allow for this task','allow_task'],['Deny','deny']]) {await click(button(label));assert.equal(decisions.at(-1).args.decision,decision);assert.equal(decisions.at(-1).args.taskFolder,'');}
  await unmount();

  rememberDefaultTitle('d','Assistant · fixture');
  assert.equal(firstMessageTitle('d','Assistant · fixture','  First line\nSecond line'),'First line');
  const long='A useful first request with enough words to run past the sixty character limit in this title';
  const shortened=firstMessageTitle('d','Assistant · fixture',long);assert.ok(shortened.length<=60);assert.ok(long.startsWith(shortened+' '));
  assert.equal(firstMessageTitle('d','Assistant · fixture','🗓'.repeat(61)),'🗓'.repeat(60),'Unicode titles are not split inside a character');
  assert.equal(firstMessageTitle('d','My title','First line'),null);rememberDefaultTitle('d',null);assert.equal(firstMessageTitle('d','Assistant · fixture','First line'),null);
  assert.equal(firstMessageTitle('d','Assistant · fixture','First line','Assistant · fixture'),null,'explicitly named sessions stay named even if they match the timestamp title');
  assert.equal(firstMessageTitle('legacy','Assistant · fixture','First line','Assistant · fixture'),'First line','exact server creation title recognizes older defaults');
  assert.equal(firstMessageTitle('legacy','Owner choice','First line','Assistant · fixture'),null);

  // Full app: catalog-driven hand-off opens a new pinned session with an unsent draft.
  let sessions=[{id:'origin',title:'Owner session',face_id:'assistant'}], history={origin:[{id:'actions',role:'assistant',content:block(handoff),created_at:'2026-11-16T00:00:00Z'}]}, calls=[],serial=0;
  const hello={identity:{sub:'gl8',gateway:'fixture',faces,may_touch:[],may_not_touch:[]},server:{boot:'fixture',version:'test'},gaps:[]};
  async function invoke(cmd,args) {
    calls.push({cmd,args});
    switch(cmd) {
      case 'portal_connection_get':return {connection_configured:true};
      case 'portal_hello':return hello;
      case 'portal_catalog':return {faces,models:[],backends:[],capabilities:{actions:[],warnings:[]}};
      case 'portal_conversations':return {items:sessions,limit:50,offset:0};
      case 'portal_conversation_open':return {conversation:sessions.find(c=>c.id===args.id),messages:history[args.id]??[],has_more:false};
      case 'portal_conversation_new': {const conversation={id:'new'+(++serial),title:args.title,face_id:args.faceId??args.face_id??'assistant'};sessions=[conversation,...sessions];return {conversation};}
      case 'portal_conversation_rename': {const conversation={...sessions.find(c=>c.id===args.id),title:args.title};sessions=sessions.map(c=>c.id===args.id?conversation:c);return {conversation};}
      case 'portal_pins':return {};
      case 'portal_approvals':return {items:[]};
      case 'pc_poll':return {requests:[],outcomes:{}};
      case 'runtime_repair_status':return {repairable:true,observation:{state:'healthy',observed:'Health checks passed.'}};
      case 'store_load':return '[]';
      case 'portal_send':return {request_id:'r'+serial,accepted:true};
      default:throw Error('Unexpected GL8 fixture '+cmd);
    }
  }
  localStorage.clear();localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience:'simple'}));
  await mount(el(PortalApp,{invoke,profiles:null,onProfiles:()=>{}}));
  await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes('Assistant')));
  await click(button('Open in Builder instead'));
  const created=calls.filter(c=>c.cmd==='portal_conversation_new').at(-1);assert.equal(created.args.faceId??created.args.face_id,'builder');
  assert.equal(document.getElementById('composer').value,handoff.brief);assert.equal(calls.filter(c=>c.cmd==='portal_send').length,0);
  await click(button('Send to Bob'));assert.equal(calls.filter(c=>c.cmd==='portal_conversation_rename').length,0,'explicit seeded name stays named');
  await unmount();
  await mount(el(PortalApp,{invoke,profiles:null,onProfiles:()=>{}}));
  await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes('Assistant')));
  await click(button('New session'));
  const defaultSession=sessions[0];assert.match(defaultSession.title,/Assistant · /);
  // Reload before the first send verifies durable default-title provenance.
  await unmount();await mount(el(PortalApp,{invoke,profiles:null,onProfiles:()=>{}}));
  await click([...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes('Assistant')));
  await type('  Name from the first message\nSecond line');await click(button('Send to Bob'));
  assert.equal(calls.filter(c=>c.cmd==='portal_conversation_rename').at(-1).args.title,'Name from the first message');
  await unmount();
  console.log('GL8 UI: PASS (compact merge/labels/failure/expand, strict action cards/streaming/durable single add, unsent hand-off, first-message naming/reload, pc.info approval)');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{if(root)await unmount();dom.window.close();});
