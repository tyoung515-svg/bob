// DOM-level supplement, NOT an Orca or native-keyboard acceptance claim.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {url:'http://localhost'});
Object.assign(global, {window:dom.window, document:dom.window.document, HTMLElement:dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT:true});
let onLine;
const originalLoad=Module._load;
Module._load=function(name,parent,...args) {
  if(name==='./client' && parent?.filename.endsWith('App.tsx')) return {
    onLine: fn => {onLine=fn;return Promise.resolve(()=>{});},
    onStderr:()=>Promise.resolve(()=>{}),onExit:()=>Promise.resolve(()=>{}),
    sliceSend:()=>Promise.resolve(),sliceStart:()=>Promise.resolve('/fixture'),sliceShutdown:()=>Promise.resolve()
  };
  return originalLoad.call(this,name,parent,...args);
};
for (const ext of ['.ts','.tsx']) require.extensions[ext]=(module,file)=>module._compile(ts.transpileModule(fs.readFileSync(file,'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText,file);
const React=require('react'); const {act}=React; const {createRoot}=require('react-dom/client');
const App=require('../src/App.tsx').default;
const transcript=fs.readFileSync(path.join(__dirname,'../../engine/tests/fixtures/transcripts/v0_allow.wire.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
const frames=transcript.filter(x=>x.event);
let passed=0,failed=0;
async function test(name,fn) {try {await fn();passed++;console.log('PASS',name);}catch(e){failed++;console.error('FAIL',name,e.stack);}}
async function mounted() {
  document.body.innerHTML='<div id="root"></div>';
  const root=createRoot(document.getElementById('root'));
  await act(async()=>root.render(React.createElement(App)));
  const emit=async f=>act(async()=>onLine(JSON.stringify(f)));
  return {root,emit};
}
const button=(text)=>[...document.querySelectorAll('button')].find(x=>x.textContent.trim()===text);
(async()=>{
 await test('approval_expansion_has_named_control_and_user_initiated_focus',async()=>{
  const {root,emit}=await mounted();
  try {
    const opener=button('Open session and begin the turn');opener.focus();
    for(const f of frames.filter(f=>f.seq<=4))await emit(f);
    assert.equal(document.activeElement,opener,'arrival must not steal focus');
    const deny=button('deny');assert.ok(deny);
    assert.equal(deny.getAttribute('aria-controls'),'deny-reason-panel');
    await act(async()=>deny.click());
    assert.equal(deny.getAttribute('aria-expanded'),'true');
    assert.equal(document.activeElement,document.getElementById('deny-reason-input'));
    assert.ok(document.querySelector('label[for="deny-reason-input"]'));
    for(const f of frames.filter(f=>f.seq>4))await emit(f);
    assert.equal(button('allow_once').disabled,true,'closed controls remain as disabled controls');
    assert.equal(button('deny').disabled,true);
  } finally {await act(async()=>root.unmount());}
 });
 for(const status of ['succeeded','failed','denied','aborted','indeterminate']) await test('polite_outcome_'+status,async()=>{
  const {root,emit}=await mounted();
  try {
   for(const f of frames) {
    const copy=structuredClone(f);
    if(f.event==='tool.result') {copy.payload.status=status;if(status!=='succeeded'){copy.payload.output=null;copy.payload.error={code:status,message:status,retryable:false};}}
    await emit(copy);
   }
   const live=document.querySelector('[role="status"][aria-live="polite"][aria-atomic="true"]');
   assert.ok(live,'one persistent atomic polite status region');
   assert.match(live.textContent,new RegExp('tool.result: '+status));
   if(status==='indeterminate')assert.match(live.textContent,/unknown/);
   assert.match(document.querySelector('.result').textContent,/Executor evidence/,'capabilities also visible on success');
   assert.match(document.body.textContent,/accessibility acceptance pending/);
   assert.match(document.body.textContent,/Windows: platform_deferred/);
  }finally {await act(async()=>root.unmount());}
 });
 await test('closed_card_renders_resolution_and_actor_as_readable_text',async()=>{
  const {root,emit}=await mounted();
  try {
   for(const f of frames) await emit(f);
   const closed=[...document.querySelectorAll('.closed')].map(x=>x.textContent).join(' ');
   assert.ok(closed.includes('resolution:'),'closed card states the resolution');
   assert.ok(!/\[object Object\]/.test(document.body.textContent),'no [object Object] leaks into the UI');
   assert.match(closed,/actor: \{[\s\S]*"kind"/,'actor renders its actual fields, not a stringified object');
  } finally {await act(async()=>root.unmount());}
 });
 console.log(`a11y DOM: ${passed} passed, ${failed} failed`);process.exitCode=failed?1:0;})().catch(e=>{console.error(e);process.exitCode=1;});
