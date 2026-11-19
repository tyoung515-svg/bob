// AU10: provider -> exact per-conversation model, using the real React surfaces.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost',pretendToBeVisual:true});
for (const k of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage']) global[k]=dom.window[k];
Object.defineProperty(global,'navigator',{configurable:true,value:{userAgent:'node'}});
global.IS_REACT_ACT_ENVIRONMENT=true;
const listeners=new Set(), events=require.resolve('@tauri-apps/api/event');
require.cache[events]={id:events,filename:events,loaded:true,exports:{listen:async(topic,fn)=>{const l={topic,fn};listeners.add(l);return()=>listeners.delete(l);}}};
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const {ModelPicker,modelName,providerName}=require('../src/portal/ModelPicker.tsx');
const PortalApp=require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const {notifyModelChanged}=require('../src/portal/model-reload.ts');
const faces=[{id:'assistant',name:'Assistant',allowed:true}];
const makeCatalog=()=>({faces,models:[
  {id:'gpt',backend:'gpt',model:'sol',label:'GPT / sol',provider_label:'GPT (ChatGPT sign-in)',available:true,reason:null,tool_capable:true,billing:'subscription',
    choices:[{model:'sol',label:'sol',default:true},{model:'astra',label:'astra',default:false}]},
  {id:'claude',backend:'claude',model:'opus',label:'Claude / opus',provider_label:'Claude subscription',available:true,reason:null,
    choices:[{model:'opus',label:'opus · claude-opus-5-5',default:true},{model:'sonnet',label:'sonnet',default:false}]},
  {id:'ds',backend:'ds',model:'flash',label:'DeepSeek / flash',available:true,reason:null},
  {id:'down',backend:'down',model:null,label:'Unavailable provider',available:false,reason:'Sign in first',choices:[]},
  {id:'null',backend:'null',model:null,label:'No model string',available:true,reason:null,choices:[{model:'invented',label:'invented',default:true}]}
],backends:[{id:'gpt',model:'sol',available:true}],capabilities:{faces:1,backends:1,actions:[],warnings:[]}});
const flush=async(fn=()=>{})=>act(async()=>{await fn();await Promise.resolve();});
const byId=id=>document.getElementById(id);
const choose=async(id,value,force=false)=>{const node=byId(id);assert.ok(node,id);if(!force)assert.ok(!node.disabled,id+' enabled');await flush(()=>{node.value=value;node.dispatchEvent(new Event('change',{bubbles:true}));});};
const button=label=>[...document.querySelectorAll('button')].find(b=>b.textContent===label||b.getAttribute('aria-label')===label);
const click=async(label)=>{const b=button(label);assert.ok(b,label);assert.ok(!b.disabled,label+' enabled');await flush(()=>b.click());};
const type=async(text)=>flush(()=>{const box=byId('composer');Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set.call(box,text);box.dispatchEvent(new Event('input',{bubbles:true}));});
const emit=async(event,data)=>flush(()=>{for(const l of listeners)if(l.topic==='portal://event')l.fn({payload:JSON.stringify({event,data})});});
let root;
async function mount(node){root=createRoot(byId('root'));await flush(()=>root.render(node));await flush();}
async function unmount(){if(root){await flush(()=>root.unmount());root=null;}}
async function unit(){
  let catalog=makeCatalog(),pin=null,changes=[];
  function Fixture(){const [value,setValue]=React.useState(pin);return React.createElement(ModelPicker,{id:'test',compact:true,catalog,pin:value,defaultName:'GPT (ChatGPT sign-in) / sol',defaultModel:'sol',disabled:false,onChange:p=>{pin=p;changes.push(p);setValue(p);}});}
  await mount(React.createElement(Fixture));
  assert.equal(byId('test').getAttribute('aria-label'),'Provider');assert.equal(byId('test-choice').tagName,'OUTPUT');assert.equal(byId('test-choice').textContent,'sol');
  assert.equal(byId('test').options[0].textContent,'Default · GPT (ChatGPT sign-in) / sol');
  assert.equal(byId('test').options.length,catalog.models.length+1);
  const option=id=>[...byId('test').options].find(o=>o.value===id);
  assert.ok(option('down').disabled);assert.match(option('down').textContent,/Sign in first/);
  assert.ok(option('null').disabled);assert.match(option('null').textContent,/no model string/);
  await choose('test','down',true);await choose('test','null',true);assert.equal(changes.length,0,'forged disabled provider changes cannot pin');
  await choose('test','gpt');assert.deepEqual(pin,{backend:'gpt',model:'sol'});assert.equal(changes.length,1);
  assert.equal(byId('test-choice').getAttribute('aria-label'),'Model for next turn');
  assert.deepEqual([...byId('test-choice').options].map(o=>o.textContent),['sol · default','astra']);
  await choose('test-choice','astra');assert.deepEqual(pin,{backend:'gpt',model:'astra'});
  await choose('test-choice','made-up',true);assert.deepEqual(pin,{backend:'gpt',model:'astra'},'unknown model is never invented');
  await choose('test','claude');assert.deepEqual(pin,{backend:'claude',model:'opus'});
  assert.equal(modelName(pin,catalog),'Claude subscription / opus · claude-opus-5-5');
  await choose('test','ds');assert.deepEqual(pin,{backend:'ds',model:'flash'});assert.ok(byId('test-choice').disabled);assert.equal(byId('test-choice').value,'flash');
  assert.equal(providerName(catalog.models[2]),'DeepSeek','legacy trailing model is stripped');
  assert.equal(providerName({...catalog.models[2],label:null}),'ds');
  assert.equal(providerName({...catalog.models[2],label:'Deployment label'}),'Deployment label');
  await choose('test','');assert.equal(pin,null);assert.equal(byId('test-choice').tagName,'OUTPUT');
  await unmount();pin={backend:'gpt',model:'removed'};await mount(React.createElement(Fixture));
  const retired=byId('test-choice').selectedOptions[0];assert.ok(retired.disabled);assert.equal(retired.textContent,'removed · not offered now');
  await choose('test-choice','astra');assert.deepEqual(pin,{backend:'gpt',model:'astra'});await unmount();
  pin={backend:'gone',model:'historic'};await mount(React.createElement(Fixture));assert.ok(byId('test').selectedOptions[0].disabled);assert.equal(byId('test-choice').selectedOptions[0].textContent,'historic · not offered now');await unmount();
}
let catalog,calls,sessions,pins,history,refusal,request=0;
const hello={identity:{sub:'au10',gateway:'fixture',faces,may_touch:[],may_not_touch:[]},server:{},gaps:[]};
async function invoke(cmd,args){calls.push({cmd,args});switch(cmd){
  case 'portal_connection_get':return{connection_configured:true,mode:{value:'this_pc'}};
  case 'portal_hello':return hello;
  case 'portal_catalog':return catalog;
  case 'portal_conversations':return{items:sessions,limit:50,offset:0};
  case 'portal_conversation_open':return{conversation:sessions.find(s=>s.id===args.id),messages:history[args.id]??[],has_more:false};
  case 'portal_conversation_new':{const conversation={id:'new',title:args.title,face_id:args.face_id,backend:null,model:null};sessions.push(conversation);return{conversation};}
  case 'portal_pins':return pins[args.conversation_id]??{backend:null,model:null};
  case 'portal_projects':return{items:[]};
  case 'runtime_model_status':return{lane:'owner',providers:{},default_backend:'gpt',ready:true};
  case 'portal_send':if(refusal)throw refusal;pins[args.conversation_id]={backend:args.backend??null,model:args.model??null};return{accepted:true,request_id:'r'+ ++request};
  case 'portal_approvals':return{items:[]};
  case 'local_auth_status':return[];
  case 'pc_poll':return{requests:[],outcomes:{}};
  case 'runtime_repair_status':return{observation:{state:'healthy'}};
  default:throw Error('Unexpected AU10 '+cmd);
}}
async function open(title){let item=[...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes(title));if(!item){const exp=document.querySelector('.agent-expander[aria-expanded="false"]');if(exp)await flush(()=>exp.click());item=[...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes(title));}assert.ok(item,title);await flush(()=>item.click());}
async function app(experience){localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));await mount(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}}));await open('First');}
async function integration(){for(const experience of ['simple','pro']){
  localStorage.clear();catalog=makeCatalog();calls=[];pins={};history={};refusal=null;
  sessions=[{id:'c1',title:'First',face_id:'assistant',model:null,backend:null},{id:'c2',title:'Second',face_id:'assistant',model:null,backend:null}];
  await app(experience);
  assert.equal(document.querySelector('.portal').classList.contains('simple'),experience==='simple');
  assert.ok(!byId('activity-panel').contains(byId('portal-chat-model')));
  assert.ok(byId('portal-chat-model').closest('.provider-model-picker').classList.contains('chat-model-picker'));
  assert.equal(byId('portal-chat-model').value,'');assert.equal(byId('portal-chat-model-choice').tagName,'OUTPUT');
  await choose('portal-chat-model','gpt');assert.equal(byId('portal-chat-model-choice').value,'sol');
  await choose('portal-chat-model-choice','astra');
  assert.equal(byId('portal-model').value,'gpt');assert.equal(byId('portal-model-choice').value,'astra');
  await choose('portal-model-choice','sol');assert.equal(byId('portal-chat-model-choice').value,'sol');
  await choose('portal-model-choice','astra');
  await open('Second');assert.equal(byId('portal-chat-model').value,'');await open('First');assert.equal(byId('portal-chat-model-choice').value,'astra');
  await flush(()=>notifyModelChanged(invoke));assert.equal(byId('portal-chat-model-choice').value,'astra','offered non-default survives catalog reload');
  catalog.models[0].choices=[{model:'sol',label:'sol',default:true}];await flush(()=>notifyModelChanged(invoke));assert.equal(byId('portal-chat-model-choice').value,'astra');assert.match(byId('portal-chat-model-choice').selectedOptions[0].textContent,/not offered now/);
  catalog=makeCatalog();await flush(()=>notifyModelChanged(invoke));assert.equal(byId('portal-chat-model-choice').value,'astra');
  await type('Hello');await click('Send to Bob');
  const send=calls.filter(c=>c.cmd==='portal_send').at(-1);assert.equal(send.args.backend,'gpt');assert.equal(send.args.model,'astra');assert.ok(!('choices' in send.args));
  assert.ok(byId('portal-chat-model').disabled);assert.ok(byId('portal-chat-model-choice').disabled);assert.ok(byId('portal-model-choice').disabled);
  await emit('stopped',{request_id:'r'+request,code:'stopped'});await unmount();
  // Reconstruct all React/session state: only the fixture server's durable pins remain.
  history.c1=[{id:'a1',role:'assistant',content:'An answer',created_at:'',metadata:{backend:'gpt',model:'astra'}}];
  await app(experience);assert.equal(byId('portal-chat-model').value,'gpt');assert.equal(byId('portal-chat-model-choice').value,'astra','non-default server pin survives app restart');
  assert.equal(document.querySelector('.model-receipt').textContent,'Answered by GPT (ChatGPT sign-in) / astra');
  await open('Second');await open('First');assert.equal(byId('portal-chat-model-choice').value,'astra');
  await choose('portal-chat-model','');assert.equal(byId('portal-chat-model-choice').tagName,'OUTPUT');assert.equal(byId('portal-chat-model-choice').textContent,'astra','Default follows effective server pin');
  await type('Use default');await click('Send to Bob');const defaultSend=calls.filter(c=>c.cmd==='portal_send').at(-1);assert.equal(defaultSend.args.backend,undefined);assert.equal(defaultSend.args.model,undefined);await emit('stopped',{request_id:'r'+request,code:'stopped'});
  await choose('portal-chat-model','gpt');await choose('portal-chat-model-choice','astra');
  const sentence='GPT (ChatGPT sign-in): astra is not offered.';
  refusal=sentence;await type('Keep this draft');await click('Send to Bob');assert.equal(byId('composer').value,'Keep this draft');assert.ok([...document.querySelectorAll('[role="alert"]')].some(n=>n.textContent.includes(sentence)));
  refusal=null;await click('Send to Bob');await emit('error',{request_id:'r'+request,code:'invalid_pin',message:sentence});assert.equal(byId('composer').value,'Keep this draft','acknowledged refusal restores draft too');
  await click('New session');assert.equal(byId('portal-chat-model').value,'','new conversation starts on Default');assert.equal(byId('portal-chat-model-choice').tagName,'OUTPUT');
  assert.equal(calls.filter(c=>/set_model|set_default|set_key|login|approval_decide/.test(c.cmd)).length,0,'chat picker never changes Settings or replays owner actions');await unmount();
}}
module.exports = (async()=>{
  await unit();await integration();
  const css=fs.readFileSync(path.join(__dirname,'../src/portal/portal.css'),'utf8');
  assert.match(css,/provider-model-controls\s*\{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(css,/provider-model-controls label\s*\{[^}]*min-width: 0/);
  assert.match(css,/provider-model-controls select,[^}]*width: 100%[^}]*text-overflow: ellipsis[^}]*white-space: nowrap/);
  assert.match(css,/composer-actions\s*\{[^}]*flex-wrap: wrap; min-width: 0/);
  assert.match(css,/simple details.pin-bar[^}]*display: none/);
  console.log('AU10 PASS: exact provider/default/model pins, legacy/null/unavailable/retired choices, shared mounts, Simple/Pro, reload/switch/restart/new, receipts, HTTP/stream refusal and constrained native controls');
  return {ok:true};
})().catch(e=>{console.error(e);process.exitCode=1;return {ok:false,error:e.stack};}).finally(async()=>{await unmount();dom.window.close();});
