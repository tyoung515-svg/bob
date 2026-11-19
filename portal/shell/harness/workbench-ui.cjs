require('./workbench-ui-register.cjs');
const assert=require('node:assert/strict'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
for(const n of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent'])global[n]=dom.window[n];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
let fixture={session:{id:'session-native-1',messages:[],events:[],receipts:[],project:null,pending:null,rounds_left:8},saved:[{id:'other-session',title:'Research orchard',project:'/projects/orchard',error:null},{id:'broken',title:'Unavailable session',project:null,error:'corrupt record'}],data_root:'/private/test-only'};
const calls=[];
const invoke=async(cmd,args)=>{calls.push({cmd,args}); if(cmd==='workbench_snapshot')throw 'unknown command workbench_snapshot'; if(cmd==='app_begin')return 'native-turn-token'; if(cmd==='app_cancel')return 'Cancellation requested; waiting for model relay';return fixture};
const button=t=>[...document.querySelectorAll('button')].find(b=>b.textContent===t);
async function change(selector,value){await act(async()=>{const el=document.querySelector(selector),setter=Object.getOwnPropertyDescriptor(el.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype,'value').set;setter.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}))})}
(async()=>{
 const App=require('../src/WorkbenchApp.tsx').default;
 const root=createRoot(document.getElementById('root'));
 await act(async()=>root.render(React.createElement(App,{invoke})));
 assert.ok(document.querySelector('nav[aria-label="Project and session navigation"]'),'real workbench navigation');
 assert.ok(document.querySelector('[aria-label="Conversation"]'));
 assert.ok(document.querySelector('aside[aria-label="Inspector"]'));
 assert.ok(document.body.textContent.includes('Research orchard'));
 await change('#session-search','absent');assert.ok(!button('Research orchard'));
 await change('#session-search','orchard');assert.ok(document.body.textContent.includes('Research orchard'));
 await act(async()=>button('Research orchard').click());
 assert.ok(calls.some(c=>c.cmd==='app_open'&&c.args.id==='other-session'));
 assert.ok(document.body.textContent.includes('Compatibility mode'));
 assert.ok(!document.body.textContent.includes('Manager agent'));
 await act(async()=>root.unmount());console.log('workbench UI navigation/search/native-open PASS (test-injected snapshot, not live acceptance)');
})().catch(e=>{console.error(e);process.exitCode=1});
