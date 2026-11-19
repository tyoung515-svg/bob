const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
for(const key of ['window','document','HTMLElement','Event','MouseEvent','localStorage'])global[key]=dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const {WorkActionCard,parseWorkAction}=require('../src/portal/WorkActionCard.tsx');
const root=createRoot(document.getElementById('root'));
const fence=value=>'```bob-work-action\n'+JSON.stringify(value)+'\n```';
const job='a'.repeat(32), prep='b'.repeat(64);
const project='11111111-1111-1111-1111-111111111111';
for(const bad of [{operation:'work.publish',resource:{job_id:job}},{schema_version:1,operation:'grants.issue',preparation_id:'fake'},
 {schema_version:1,operation:'grants.issue',preparation_id:'x'.repeat(5000)},
 {schema_version:1,operation:'shell',resource:{job_id:job}}])assert.equal(parseWorkAction(fence(bad)),null);
assert.ok(parseWorkAction(fence({schema_version:1,operation:'work.publish',resource:{job_id:job}})));
let hits=[];
let projects=[],publication=null,stalePaths=['base'],loseReply=true,mount=0;
const catalog={capabilities:{operations:Object.fromEntries(['work.publish','owner.status'].map(op=>[op,{contract_versions:[2],available:true,implemented:true}]))}};
const client={
 work:async(op,args)=>{hits.push([op,args]);if(op==='preparation')return {preparation_id:prep,target:'bob-lane-workspace',
  project_id:'owned-project',task:'CANONICAL SERVER TASK',recipe:'python-basic',proposal:{proposal_id:'actual-proposal'}};
  if(op==='catalog')return {projects};
  if(op==='publish'){
   if(loseReply){publication={state:'unknown',receipt:{code:'publication_outcome_unknown'}};stalePaths=['partially-applied'];throw new Error('lost reply');}
   publication={state:'completed',receipt:{}};return {state:'published'};
  }
  return {id:job,run_id:'run',status:'completed',publication};},
 ownerStatus:async()=>({owner_session:{active:true,expires_at:Date.now()/1000+600,allowed_operations:['work.publish']}}),
 jobs:{v2:{proposal:async()=>({job_id:job,digest:'c'.repeat(64),revision:2,diff:'ACTUAL SERVER DIFF',checks_passed:true,check_runs:[],stale_paths:stalePaths,diff_truncated:false})}}
};
async function render(content){await act(async()=>{root.render(React.createElement(WorkActionCard,{key:++mount,content,client,catalog,scope:'test',onContinue:()=>{}}));await new Promise(r=>setTimeout(r,0));});}
const button=text=>[...document.querySelectorAll('button')].find(b=>b.textContent===text);
async function click(text){await act(async()=>{button(text).click();await new Promise(r=>setTimeout(r,0));});}
(async()=>{
 await render(fence({schema_version:1,operation:'grants.issue',preparation_id:prep,task:'FORGED MODEL TASK'}));
 assert.ok(document.body.textContent.includes('CANONICAL SERVER TASK'));
 assert.ok(!document.body.textContent.includes('FORGED MODEL TASK'));
 assert.deepEqual(hits,[['preparation',{preparation_id:prep}]],'rendering only reads canonical state');
 hits=[];
 await render(fence({schema_version:1,operation:'work.publish',resource:{job_id:job}}));
 assert.ok(document.body.textContent.includes('ACTUAL SERVER DIFF'));
 assert.ok([...document.querySelectorAll('button')].find(b=>b.textContent==='Publish checked files').disabled,'stale source blocks publication');
 assert.ok(!hits.some(([op])=>op==='publish'),'a proposed card never auto-publishes');
 hits=[];
 const workspace=fence({schema_version:1,operation:'work.workspace.create',resource:{project_id:project},name:'FORGED PROJECT'});
 await render(workspace);
 assert.ok(!button('Create project workspace'),'unknown project action refused');
 assert.ok(document.body.textContent.includes('Project is not in your server catalog'));
 assert.deepEqual(hits,[['catalog',undefined]],'workspace render reads catalog only');
 projects=[{id:project,name:'CANONICAL PROJECT NAME'}];hits=[];
 await render(workspace);
 assert.ok(document.body.textContent.includes('CANONICAL PROJECT NAME'));
 assert.ok(!document.body.textContent.includes('FORGED PROJECT'));
 assert.ok(button('Create project workspace'));
 assert.deepEqual(hits,[['catalog',undefined]],'known project does not auto-create');
 const publish=fence({schema_version:1,operation:'work.publish',resource:{job_id:job}});
 publication={state:'conflict',receipt:{code:'edit_reconciliation_conflict',detail:'Owner changed app/calc.py'}};stalePaths=[];hits=[];
 await render(publish);
 assert.ok(button('Publish checked files').disabled,'terminal conflict blocks publication');
 assert.ok(document.body.textContent.includes('edit_reconciliation_conflict'));
 assert.ok(document.body.textContent.includes('Owner changed app/calc.py'));
 assert.ok(!hits.some(([op])=>op==='publish'));
 publication={state:'conflict',receipt:{code:'edit_reconciliation_required',detail:{blocking_operation:'prior-publication'}}};
 await render(publish);
 assert.ok(button('Publish checked files').disabled);
 assert.ok(document.body.textContent.includes('prior-publication'),'structured server conflict detail renders safely');
 publication={state:'completed',receipt:{}};
 await render(publish);
 assert.ok(button('Publish checked files').disabled,'completed blocks publication');
 publication={state:'unknown',receipt:{code:'publication_outcome_unknown'}};hits=[];
 await render(publish);
 await click('Retry publication');
 assert.ok(!hits.some(([op])=>op==='publish'),'unknown state never invents a replacement key');
 assert.ok(document.body.textContent.includes('Original publication request key is unavailable'));
 publication=null;hits=[];
 await render(publish);
 await click('Publish checked files');
 const first=hits.find(([op])=>op==='publish')[1];
 assert.ok(first.request_key);
 assert.ok(document.body.textContent.includes('Outcome unknown — Retry publication reconciles with the same key'));
 assert.ok(!button('Retry publication').disabled);
 // Reopen the card after a lost reply: persisted mutation key and source drift.
 await render(publish);
 assert.ok(!button('Retry publication').disabled,'unknown reconciliation may retry after partial source changes');
 loseReply=false;
 await click('Retry publication');
 const attempts=hits.filter(([op])=>op==='publish').map(([,args])=>args);
 assert.equal(attempts.length,2);
 assert.deepEqual(attempts[1],first,'unknown retry keeps identical payload and request key across remount');
 assert.ok(button('Publish checked files').disabled);
 await act(async()=>root.unmount());
 console.log('day-one UI: canonical project/preparation read-back, no automatic mutation, stale/completed/conflict blocks, unknown same-key retry passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
