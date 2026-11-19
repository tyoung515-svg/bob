#!/usr/bin/env node
// UNIT only: mount the actual default entry; never live/provider acceptance.
const fs=require('node:fs'),path=require('node:path'),Module=require('node:module'),assert=require('node:assert/strict');
const ts=require('typescript'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
for(const k of ['window','document','HTMLElement','Event','MouseEvent','navigator'])Object.defineProperty(global,k,{value:dom.window[k],configurable:true});
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'),{act}=React;
const session={id:'unit-session',messages:[{role:'assistant',content:'UNIT ONLY <img src="https://invalid.example/pixel" onerror="window.P1_XSS=1">\n```js\nconst sample=1;\n```'}],receipts:[],events:[],project:null,pending:null};
const app={session,saved:[],data_root:'/UNIT-NOT-LIVE'};
const profile=(id,kind,owner,multi)=>({id,label:id,adapter_kind:kind,execution_host:owner==='bob_runtime'?'remote':'local',model_label:null,memory_owner:owner,effect_owner:owner,configured:false,capabilities:{chat:!multi,project_tools:!multi,writes:!multi,multi_agent:multi,workflows:true,cancel:false,steer:false,reconnect:false},unavailable_reason:'UNIT: unavailable'});
const snapshot={schema_version:'workbench-view-v1',app,profiles:[profile('local_bob_model','model_only','cleanroom_local',false),profile('standalone_http','model_only','cleanroom_local',false),profile('bob_team','agent_runtime','bob_runtime',true)],active_profile_id:'local_bob_model',connection_state:'unknown',agents:[],workflows:[],outputs:[],notices:['UNIT ONLY; no live runtime']};
const calls=[];const originalLoad=Module._load;
Module._load=function(id,parent,isMain){
 if(id==='@tauri-apps/api/core')return {invoke:async(cmd,args)=>{calls.push({cmd,args});if(cmd==='app_developer_mode')return false;if(cmd==='workbench_snapshot')return snapshot;if(cmd==='app_boot')return app;throw new Error('UNIT unsupported native command '+cmd)}};
 if(id==='@tauri-apps/api/event')return {listen:async()=>()=>{}};
 return originalLoad.call(this,id,parent,isMain);
};
for(const ext of ['.ts','.tsx'])Module._extensions[ext]=(m,f)=>m._compile(ts.transpileModule(fs.readFileSync(f,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true,target:ts.ScriptTarget.ES2022}}).outputText,f);
Module._extensions['.css']=()=>{};
const checks=[];function check(id,fn){try{fn();checks.push({id,status:'PASS'})}catch(e){checks.push({id,status:'FAIL',error:e.message})}}
(async()=>{
 await act(async()=>{require(path.resolve('src/main.tsx'));await new Promise(r=>setTimeout(r,30))});
 check('CONTROL-default-functional-composer',()=>assert.ok(document.querySelector('textarea'),'actual default entry must render the composer'));
 check('J4-frozen-view-command-used',()=>assert.ok(calls.some(c=>c.cmd==='workbench_snapshot'),'default entry never requests real workbench_snapshot; compatibility app is not P1'));
 check('J1-project-task-navigation',()=>assert.ok(document.querySelector('nav, [role="navigation"], aside'),'missing workbench navigation region'));
 check('J4-agent-workflow-inspection',()=>{const named=[...document.querySelectorAll('[aria-label],h1,h2,h3,button,summary')].map(n=>n.getAttribute('aria-label')||n.textContent).join('\n');assert.match(named,/agents?/i);assert.match(named,/workflow|steps|timeline/i)});
 check('J6-profile-selection',()=>assert.match(document.body.textContent,/standalone/i));
 check('J5-no-start-on-mount',()=>assert.ok(!calls.some(c=>c.cmd==='workbench_start_bob_run'),'opening the app dispatched Bob generation'));
 check('J2-untrusted-model-html-inert',()=>{assert.equal(document.querySelectorAll('img[src^="http"],iframe,script[src^="http"]').length,0);assert.equal(window.P1_XSS,undefined)});
 console.log(JSON.stringify({tier:'UNIT',live_acceptance:false,checks,calls: calls.map(c=>c.cmd)},null,2));
 process.exit(checks.some(c=>c.status==='FAIL')?1:0);
})().catch(e=>{console.error(e);process.exit(2)});
