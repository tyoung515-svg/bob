// AU5: real app in both views, mocked native transport, no model calls.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost',pretendToBeVisual:true});
for (const k of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage']) global[k]=dom.window[k];
global.IS_REACT_ACT_ENVIRONMENT=true;
const listeners=new Set(), events=require.resolve('@tauri-apps/api/event');
require.cache[events]={id:events,filename:events,loaded:true,exports:{listen:async(_,fn)=>{listeners.add(fn);return()=>listeners.delete(fn);}}};
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const PortalApp=require('../src/portal/PortalApp.tsx').default;
const {DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const faces=[{id:'assistant',name:'Assistant'}];
const hello={identity:{sub:'au5-picker',gateway:'fixture',faces,may_touch:[],may_not_touch:[]},server:{},gaps:[]};
const sessions=[{id:'c1',title:'First',face_id:'assistant',model:null,backend:null},{id:'c2',title:'Second',face_id:'assistant',model:null,backend:null}];
const catalog={faces,models:[{id:'ds',backend:'ds',model:'deepseek-v4-flash',label:'DeepSeek Flash',available:true},
  {id:'openai_codex',backend:'openai_codex',model:'visible-gpt',label:'GPT from the account',available:true,choices:[{model:'visible-gpt',label:'visible-gpt',default:true},{model:'other-gpt',label:'other-gpt',default:false}]},
  {id:'down',backend:'down',model:'down-model',available:false,reason:'Key expired'}],
  backends:[{id:'ds',model:'deepseek-v4-flash',available:true}],capabilities:{actions:[],warnings:[]}};
let root,calls=[],history={},pins={},empty=false,known=true,projectBackend=null,projectReadFails=false;
async function invoke(cmd,args){calls.push({cmd,args});switch(cmd){
  case 'portal_connection_get':return {connection_configured:true,mode:{value:'this_pc'}};
  case 'portal_hello':return hello;
  case 'portal_catalog':return empty?{...catalog,models:[],backends:[]}:catalog;
  case 'portal_conversations':return {items:sessions,limit:50,offset:0};
  case 'portal_conversation_open':return {conversation:sessions.find(s=>s.id===args.id),messages:history[args.id]??[],has_more:false};
  case 'portal_pins':return pins[args.conversation_id]??{};
  case 'portal_projects':if(projectReadFails)throw Error('Project settings unavailable');return {items:[{id:'project',default_backend:projectBackend}]};
  case 'runtime_model_status':return {lane:'owner',providers:{},default_backend:known?'ds':null,ready:true};
  case 'portal_send':pins[args.conversation_id]={model:args.model,backend:args.backend};return {accepted:true,request_id:'r1'};
  case 'portal_approvals':return {items:[]};
  case 'local_auth_status':return [];
  case 'pc_poll':return {requests:[],outcomes:{}};
  case 'runtime_repair_status':return {observation:{state:'healthy'}};
  default:throw Error('Unexpected '+cmd);
}}
const flush=async(fn=()=>{})=>act(async()=>{await fn();await Promise.resolve();});
const entry=()=>document.getElementById('portal-chat-model');
const selected=()=>entry().selectedOptions[0].textContent;
const button=label=>[...document.querySelectorAll('button')].find(b=>b.textContent===label || b.getAttribute('aria-label')===label);
async function open(title){if(![...document.querySelectorAll('.agent-sessions button')].some(b=>b.textContent.includes(title))){const exp=document.querySelector('.agent-expander[aria-expanded="false"]');if(exp)await flush(()=>exp.click());}await flush(()=>[...document.querySelectorAll('.conversation-list button')].find(b=>b.textContent.includes(title)).click());}
async function choose(value,id='portal-chat-model'){await flush(()=>{const select=document.getElementById(id);select.value=value;select.dispatchEvent(new Event('change',{bubbles:true}));});}
async function mount(experience,openSession=true){localStorage.setItem('bobportal.appearance',JSON.stringify({...DEFAULT_APPEARANCE,experience}));root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));if(openSession)await open('First');}
async function unmount(){await flush(()=>root.unmount());root=null;}
(async()=>{
  for(const experience of ['simple','pro']){
    calls=[];pins={};history={};empty=false;known=true;localStorage.clear();await mount(experience);
    assert.equal(document.querySelector('.portal').classList.contains('simple'),experience==='simple');
    assert.ok(!document.getElementById('activity-panel').contains(entry()),'composer picker is outside hidden routing');
    assert.match(selected(),/Default.*deepseek-v4-flash/);
    assert.match(document.querySelector('.conversation-header').textContent,/deepseek-v4-flash.*default/);
    assert.equal(entry().options.length,4);
    const down=[...entry().options].find(o=>o.value==='down');assert.ok(down.disabled);assert.match(down.textContent,/Key expired/);
    const gpt=[...entry().options].find(o=>o.textContent.includes('GPT from the account'));assert.ok(gpt);
    await choose(gpt.value);assert.match(selected(),/GPT from the account/);assert.match(document.querySelector('.conversation-header').textContent,/visible-gpt/);
    assert.equal(document.getElementById('portal-model').value,gpt.value,'Pro control shares pin state');
    assert.equal(document.getElementById('portal-chat-model-choice').value,'visible-gpt','one click pins provider default');
    assert.equal(document.getElementById('portal-model-choice').value,'visible-gpt');
    await open('Second');await open('First');assert.equal(entry().value,gpt.value,'unsent choice survives leaving and reopening');
    const box=document.getElementById('composer');await flush(()=>{Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set.call(box,'Hello');box.dispatchEvent(new Event('input',{bubbles:true}));});
    await flush(()=>button('Send to Bob').click());
    const sends=calls.filter(c=>c.cmd==='portal_send');assert.equal(sends.length,1);assert.equal(sends[0].args.model,'visible-gpt');assert.equal(sends[0].args.backend,'openai_codex');
    assert.ok(entry().disabled);assert.ok(document.getElementById('portal-model').disabled);
    await flush(()=>{for(const fn of listeners)fn({payload:JSON.stringify({event:'stopped',data:{request_id:'r1',code:'stopped'}})});});
    assert.ok(!entry().disabled);await unmount();await mount(experience);assert.equal(entry().value,gpt.value,'server pins restore after remount');
    await choose('');assert.match(selected(),/Default.*visible-gpt/,'Default names the effective server pin');await unmount();
    known=false;pins={};await mount(experience,false);assert.ok(entry().disabled);assert.match(document.querySelector('.composer-actions').textContent,/Open or start a session/);assert.match(selected(),/server.project default/);await unmount();
    empty=true;await mount(experience,false);assert.match(selected(),/No model set up/);assert.ok(button('Add a provider'));await unmount();
  }
  // Project overrides are read through the existing project catalog, never guessed.
  empty=false;known=true;pins={};sessions[0].project_id='project';projectBackend='openai_codex';
  catalog.backends.push({id:'openai_codex',model:'visible-gpt',available:true});
  await mount('simple');assert.match(selected(),/Default.*visible-gpt/);await unmount();
  projectReadFails=true;await mount('simple');assert.match(selected(),/server.project default/,'unread project settings cannot be replaced by a guessed lane default');await unmount();
  delete sessions[0].project_id;projectReadFails=false;projectBackend=null;catalog.backends.pop();
  const css=fs.readFileSync(path.join(__dirname,'../src/portal/portal.css'),'utf8');
  assert.match(css,/simple details.pin-bar[^}]*display: none/);assert.ok(!entry());
  console.log('AU5 model picker: PASS (Simple/Pro direct choice, labels/defaults, disabled reasons, shared pin, reopen/remount, exact send pair, lock, unknown default and setup recovery)');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(root)await unmount();dom.window.close();});
