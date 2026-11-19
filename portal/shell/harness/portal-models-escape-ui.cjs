'use strict';
const assert = require('node:assert/strict'), path = require('node:path');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
for (const name of ['window', 'document', 'HTMLElement', 'Event', 'CustomEvent', 'MouseEvent', 'KeyboardEvent']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
let copied = '';
Object.defineProperty(global, 'navigator', { value: { userAgent: 'node', clipboard: { writeText: async (text) => { copied = text; } } }, configurable: true });
const listeners = new Map();
const eventModule = require.resolve('@tauri-apps/api/event', { paths: [path.resolve(__dirname, '..')] });
require.cache[eventModule] = { id: eventModule, filename: eventModule, loaded: true, children: [], paths: [], exports: { listen: async (topic, callback) => { listeners.set(topic, callback); return () => listeners.delete(topic); } } };
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { ModelSetup } = require('../src/portal/ModelSetup.tsx');
const { SettingsView, DEFAULT_APPEARANCE } = require('../src/portal/SettingsView.tsx');
const { PortalShell } = require('../src/portal/PortalShell.tsx');
const { PortalBoundary } = require('../src/portal/PortalBoundary.tsx');
const { RecoveryActions } = require('../src/portal/RecoveryActions.tsx');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const CONNECTION = { connection_configured: false, mode: { value: 'this_pc', source: 'settings' }, lane: { value: 'luna', source: 'settings' }, user: {value:'operator'}, host:{value:'example-server'},broker:{value:'/opt/bob/portal/portal-broker.py'} };
const PROFILES = { active: 'bob_server', profiles: ['bob_server','bob_team','local_bob_model','standalone_http'].map((id) => ({id,label:id,available:true})), settings: {}, transport: { targets: [] } };
let calls, status, loginResolve, loginReject, keyResolve, keyReject, keyDelay, activeProfiles, profileError, selections;
function reset() {
  calls = []; status = {lane:'luna',providers:{openai_signin:false,openai_key:false,anthropic_key:false,deepseek_key:false,google_key:false},default_backend:null,ready:false};
  keyDelay=false; activeProfiles={...PROFILES}; profileError=false; selections=[];
}
async function invoke(cmd,args) {
  calls.push({cmd,args});
  switch(cmd) {
    case 'runtime_model_providers': return require('./model-provider-fixture.cjs');
    case 'runtime_model_status': return {...status};
    case 'runtime_model_openai_login': return new Promise((resolve,reject) => {loginResolve=resolve;loginReject=reject;});
    case 'runtime_model_cancel': loginReject?.('Sign-in cancelled'); return {ok:true};
    case 'runtime_model_open_url': return;
    case 'runtime_model_set_key': if(keyDelay) return new Promise((resolve,reject) => {keyResolve=resolve;keyReject=reject;}); return {ok:true};
    case 'runtime_model_set_default': status.default_backend=args.backend; return {ok:true};
    case 'portal_profiles': if(profileError) throw 'Profile settings unavailable'; return activeProfiles;
    case 'portal_select_profile': selections.push(args.profileId); profileError=false;activeProfiles={...PROFILES,active:args.profileId};return activeProfiles;
    case 'portal_connection_get':return CONNECTION;
    case 'local_auth_status':return [];
    case 'workbench_snapshot': case 'app_boot': throw 'WSL-dependent package missing bob-wsl-host; no native fallback';
    default: throw `Unexpected fake command ${cmd}`;
  }
}
async function flush(fn) { await act(async()=>{if(fn) await fn();await Promise.resolve();}); }
async function mount(component,props) { const root=createRoot(document.getElementById('root'));await flush(()=>root.render(React.createElement(component,props)));await flush();return root; }
function button(text) {return [...document.querySelectorAll('button')].find((b)=>b.textContent===text);}
async function click(text) {assert.ok(button(text),`button ${text} exists`);await flush(()=>button(text).click());}
async function input(id,value) {await flush(()=>{const node=document.getElementById(id);Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(node,value);node.dispatchEvent(new Event('input',{bubbles:true}));});}
async function event(payload) {assert.ok(listeners.has('runtime://model'));await flush(()=>listeners.get('runtime://model')({payload}));}
async function submitKey() {await flush(()=>document.querySelector('.model-setup form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));}
function settingsProps(profiles) {return {invoke,client:createPortalClient(invoke),hello:null,catalog:null,profiles,onProfiles:()=>{},onReconnect:()=>{},appearance:DEFAULT_APPEARANCE,onAppearance:()=>{},pendingApprovals:0};}
(async()=>{
  reset();let continued=[];let root=await mount(ModelSetup,{invoke,onContinue:(skip)=>continued.push(skip)});
  assert.match(document.body.textContent,/Chat needs a model/);await click('Skip for now');assert.deepEqual(continued,[true]);
  await click('Sign in with OpenAI (ChatGPT)');assert.match(document.body.textContent,/Starting OpenAI/);
  await event({event:'device_code',url:'https://auth.openai.com/device',code:'ABCD-1234'});
  assert.equal(document.querySelector('[aria-label="Device code"]').textContent,'ABCD-1234');assert.match(document.body.textContent,/Waiting for you/);
  await click('Copy code');assert.equal(copied,'ABCD-1234');await click('Open sign-in page');
  assert.deepEqual(calls.find(c=>c.cmd==='runtime_model_open_url').args,{url:'https://auth.openai.com/device'});
  await click('Cancel sign-in');assert.ok(calls.some(c=>c.cmd==='runtime_model_cancel'));assert.match(document.body.textContent,/cancelled/);assert.equal(listeners.has('runtime://model'),false);assert.equal(document.querySelector('[aria-label="Device code"]'),null);
  await click('Sign in with OpenAI (ChatGPT)');await event({event:'error',detail:'Provider denied sign-in'});await flush(()=>loginReject('Provider denied sign-in'));assert.match(document.body.textContent,/Provider denied sign-in/);
  await click('Sign in with OpenAI (ChatGPT)');await event({event:'device_code',url:'https://auth.openai.com/device',code:'EFGH-5678'});
  status={...status,ready:true,default_backend:'openai_signin',providers:{...status.providers,openai_signin:true}};
  await event({event:'done',ok:true});await flush(()=>loginResolve({ok:true}));assert.match(document.body.textContent,/OpenAI sign-in saved/);assert.equal(document.querySelector('[aria-label="Device code"]'),null);await click('Continue');assert.deepEqual(continued,[true,false]);
  await flush(()=>root.unmount());
  reset();root=await mount(ModelSetup,{invoke});await click('Use an API key');
  assert.deepEqual([...document.querySelectorAll('#model-provider option')].map(o=>o.textContent),['OpenAI','Anthropic','DeepSeek','Google','GLM (Z.ai)','Kimi (Moonshot)','Qwen (Alibaba Model Studio)']);
  assert.equal(document.getElementById('model-key').type,'password');keyDelay=true;
  const secret='fake-secret-NEVER-RENDER';await input('model-key',secret);await submitKey();
  assert.deepEqual(calls.find(c=>c.cmd==='runtime_model_set_key').args,{provider:'openai',key:secret});
  assert.equal(document.getElementById('model-key').value,'');assert.ok(!document.body.innerHTML.includes(secret),'key cleared while pending');
  status={...status,ready:true,default_backend:'openai_key',providers:{...status.providers,openai_key:true}};
  await flush(()=>keyResolve({ok:true}));assert.match(document.body.textContent,/OpenAI saved/);assert.ok(!document.body.innerHTML.includes(secret));
  await input('model-key',secret);await submitKey();await flush(()=>keyReject(`helper echoed ${secret}`));assert.ok(!document.body.textContent.includes(secret));assert.equal(document.getElementById('model-key').value,'');assert.match(document.querySelector('[role="alert"]').textContent,/could not be saved/);
  await flush(()=>root.unmount());
  reset();root=await mount(ModelSetup,{invoke});await click('Sign in with OpenAI (ChatGPT)');await flush(()=>root.unmount());await flush();assert.ok(calls.some(c=>c.cmd==='runtime_model_cancel'));assert.equal(listeners.has('runtime://model'),false);
  reset();root=await mount(SettingsView,settingsProps(PROFILES));await click('Connection');
  assert.deepEqual([...document.querySelectorAll('#set-profile option')].map(o=>o.value),['bob_server','bob_team']);await click('Models');assert.ok(button('Sign in with OpenAI (ChatGPT)'));await flush(()=>root.unmount());
  root=await mount(SettingsView,settingsProps({...PROFILES,developer_mode:true}));await click('Connection');assert.equal(document.querySelectorAll('#set-profile option').length,4);await flush(()=>root.unmount());
  reset();activeProfiles={...PROFILES,active:'local_bob_model'};root=await mount(PortalShell,{invoke});
  assert.deepEqual(selections,['bob_server']);assert.match(document.body.textContent,/retired runtime profile was reset/);assert.ok(!calls.some(c=>c.cmd==='app_boot'||c.cmd==='workbench_snapshot'));await flush(()=>root.unmount());
  reset();profileError=true;root=await mount(PortalShell,{invoke});assert.match(document.body.textContent,/Profile settings unavailable/);assert.ok(button('Back to BoB'));await click('Open Settings');assert.ok(button('Models'));await click('Models');assert.ok(button('Use an API key'));await flush(()=>root.unmount());
  reset();profileError=true;root=await mount(PortalShell,{invoke});await click('Back to BoB');assert.deepEqual(selections,['bob_server']);assert.ok(button('Set up Bob on this PC'));await flush(()=>root.unmount());
  reset();activeProfiles={...PROFILES,active:'local_bob_model',developer_mode:true};root=await mount(PortalShell,{invoke});assert.match(document.body.textContent,/missing bob-wsl-host/);assert.ok(button('Back to BoB'));await click('Back to BoB');assert.ok(button('Set up Bob on this PC'));assert.deepEqual(selections,['bob_server']);await flush(()=>root.unmount());
  reset();let opened=false,recovered=false;const oldError=console.error;console.error=()=>{};
  function Crash(){throw new Error('fake full-screen render fault');}
  root=createRoot(document.getElementById('root'));
  await flush(()=>root.render(React.createElement(PortalBoundary,{recovery:React.createElement(RecoveryActions,{invoke,onRecovered:()=>{recovered=true;},onSettings:()=>{opened=true;}})},React.createElement(Crash))));
  console.error=oldError;assert.match(document.body.textContent,/fake full-screen render fault/);await click('Open Settings');assert.equal(opened,true);await click('Back to BoB');assert.equal(recovered,true);assert.deepEqual(selections,['bob_server']);await flush(()=>root.unmount());
  console.log('portal-models-escape-ui: legacy routing/picker, developer escape, boot/Workbench/render recovery, Settings Models, device code/copy/open/progress/error/done/cancel/unmount, write-only key pending/save/error, skip PASS');
})().catch(error=>{console.error(error);process.exit(1);});
