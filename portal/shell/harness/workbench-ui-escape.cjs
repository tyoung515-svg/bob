require('./workbench-ui-register.cjs');
// Regression: the Settings panel is opened from the toolbar, so keyboard focus
// stays on the toolbar button. Escape must still close the panel (SPEC keyboard
// navigation), not only when focus happens to be inside the panel section.
const assert=require('node:assert/strict'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
for(const n of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent'])global[n]=dom.window[n];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const caps={chat:true,project_tools:true,writes:true,multi_agent:false,workflows:true,cancel:true,steer:false,reconnect:true};
const app={session:{id:'escape-session',messages:[],events:[],receipts:[],project:null,pending:null},saved:[],data_root:'/private/test-only'};
const fixture={schema_version:'workbench-view-v1',app,active_profile_id:'local_bob_model',connection_state:'unknown',profiles:[{id:'local_bob_model',label:'Local agent + Bob model',adapter_kind:'model_only',execution_host:'local',model_label:'bob-ds-flash',memory_owner:'native',effect_owner:'cleanroom_local',configured:true,capabilities:caps,unavailable_reason:null}],agents:[],workflows:[],outputs:[],notices:[]};
const invoke=async(cmd)=>{if(cmd==='workbench_snapshot')return fixture;if(cmd==='app_boot')return app;return fixture};
const button=t=>[...document.querySelectorAll('button')].find(b=>b.textContent===t);
(async()=>{
 const App=require('../src/WorkbenchApp.tsx').default;
 const root=createRoot(document.getElementById('root'));
 await act(async()=>root.render(React.createElement(App,{invoke})));
 const open=button('Settings');
 await act(async()=>open.click());
 assert.ok(document.querySelector('[aria-label="Runtime settings"]'),'settings panel opens from the toolbar');
 // The real keystroke goes to the focused toolbar button, outside the panel.
 await act(async()=>{open.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',bubbles:true}))});
 assert.ok(!document.querySelector('[aria-label="Runtime settings"]'),'Escape must close Settings from the focused toolbar control');
 await act(async()=>root.unmount());
 console.log('workbench Escape closes Settings from outside the panel PASS');
 process.exit(0);
})().catch(e=>{console.error(e);process.exit(1)});
