require('./workbench-ui-register.cjs');
// Regression: a cold extended-path launch gets workbench-view-v1 with app:null
// (native local session not yet established). The workbench entry must establish
// the real native app session once via app_boot; otherwise the product stays on
// "Connecting to native app…" with no conversation and no durable sessions.
const assert=require('node:assert/strict'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
for(const n of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent'])global[n]=dom.window[n];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const profile={id:'local_bob_model',label:'Local agent + Bob model',adapter_kind:'model_only',execution_host:'local',model_label:'bob-ds-flash',memory_owner:'functional-app-v2 local session',effect_owner:'cleanroom_local',configured:true,capabilities:{chat:true,project_tools:true,writes:true,multi_agent:false,workflows:true,cancel:true,steer:false,reconnect:true},unavailable_reason:null};
const cold={schema_version:'workbench-view-v1',app:null,profiles:[profile],active_profile_id:'local_bob_model',connection_state:'unknown',agents:[],workflows:[],outputs:[],notices:['Session busy/unavailable; retaining observed event projection. No session absence is implied.']};
const booted={session:{id:'cold-session',messages:[{role:'assistant',content:'Cold boot assistant line'}],events:[],receipts:[],project:null,pending:null,rounds_left:8},saved:[{id:'cold-session',title:'Cold boot conversation',project:null,error:null}],data_root:'/private/test-only'};
let boots=0;
const calls=[];
const invoke=async(cmd,args)=>{calls.push({cmd,args});
 if(cmd==='workbench_snapshot')return cold;
 if(cmd==='app_boot'){boots++;return booted}
 throw new Error('unexpected command '+cmd)};
const button=t=>[...document.querySelectorAll('button')].find(b=>b.textContent===t);
(async()=>{
 const App=require('../src/WorkbenchApp.tsx').default;
 const root=createRoot(document.getElementById('root'));
 await act(async()=>root.render(React.createElement(App,{invoke})));
 await act(async()=>{await new Promise(r=>setTimeout(r,60))});
 assert.equal(boots,1,'cold start must call app_boot exactly once to establish the native local session');
 assert.ok(document.body.textContent.includes('Cold boot conversation'),'durable saved session from the booted snapshot must be listed');
 assert.ok(document.body.textContent.includes('Cold boot assistant line'),'booted conversation content must be visible');
 assert.ok(!document.body.textContent.includes('Connecting to native app'),'composer must leave the unbound cold-start status');
 await act(async()=>button('Refresh runtime').click());
 await act(async()=>{await new Promise(r=>setTimeout(r,40))});
 assert.equal(boots,1,'later projections with app:null must not boot twice or replay effects');
 await act(async()=>root.unmount());
 console.log('workbench cold-start native boot regression PASS');
 process.exit(0);
})().catch(e=>{console.error(e);process.exit(1)});
