// AU6: action cards stay visible outside transcript folds; identities and authority stay intact.
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost',pretendToBeVisual:true});
for (const k of ['window','document','HTMLElement','Event','MouseEvent','CustomEvent','localStorage']) global[k] = dom.window[k];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), {act} = React, {createRoot} = require('react-dom/client'), el = React.createElement;
const {ExperienceContext} = require('../src/portal/experience.ts');
const {ActionCardsContext} = require('../src/portal/ActionCard.tsx');
const {PortalMarkdown} = require('../src/portal/PortalMarkdown.tsx');
const {WorkflowStore} = require('../src/portal/workflow-store.ts');
const {WorkflowContext} = require('../src/portal/WorkflowViews.tsx');
const {createCalendarStore} = require('../src/portal/calendar.ts');
const faces = [{id:'builder',name:'Builder'}];
const handoff = {type:'handoff',agent:'builder',title:'Build the plan',brief:'Implement the reviewed plan.'};
const calendar = {type:'calendar',title:'Review the plan',date:'2028-02-29'};
const workflow = {type:'workflow',title:'Plan workflow',proposal_id:'p',proposal_revision:1,proposal_digest:'a'.repeat(64)};
const fence = String.fromCharCode(96).repeat(3);
const block = a => [fence+'bob-action',JSON.stringify(a),fence].join('\n');
const definition = {schema:'workflow/v1',name:'Plan workflow',summary:'Plan',agents:{plan:{face_id:'builder',binding_id:null}},stages:[{id:'plan',kind:'agent',agent:'plan',brief:'Plan',depends_on:[],access:'read',output:'text'}],review_policy:'any',authority:{checks:[],workspace_ops:[]},budget:{calls:4,active_seconds:60,job_seconds:10,max_job_attempts:1}};
const proposal = {kind:'workflow_proposal',id:'p',revision:1,digest:workflow.proposal_digest,state:'proposed',expires_at:'2099-01-01T00:00:00Z',origin_conversation_id:'c',proposed_by:{actor:'owner',via:'assistant',token_type:'portal'},runnable:true,reasons:[],run_id:null,coverage:{definition,inputs:{task:'Plan this'},bindings:{plan:{model:'visible-gpt'}},assistant_binding:{face_id:'assistant'},assistant_summary_calls:1,workspace:null,review:{policy:'any',policy_met:true,same_model_choice_required:false},checks:[],budget:definition.budget,run_mode:'approve',run_modes:[{id:'approve',label:'Ask before changes'}]}};
const catalog = {faces,models:[],backends:[],capabilities:{operations:{'workflows.grant_start':{contract_versions:[1],available:true,implemented:true},'owner.status':{contract_versions:[2],available:true,implemented:true}}}};
let root, store, workflows, data, handoffs, starts, saved, key = 0;
const flush = async (fn=()=>{}) => act(async()=>{await fn();await new Promise(r=>setTimeout(r,0));});
const button = label => [...document.querySelectorAll('button')].find(b=>b.textContent===label);
const id = index => 'bob-action:'+JSON.stringify(['c','m',index]);
async function reset() {
  if(root) await flush(()=>root.unmount());
  workflows?.dispose(); localStorage.clear(); data='[]';handoffs=[];starts=[];saved=0;
  store=createCalendarStore(async(cmd,args)=>{if(cmd==='store_load')return data;if(cmd==='store_save'){data=args.data;saved++;return '';}throw Error(cmd);});
  const client={workflowCatalog:async()=>({available:true,bindings:[],reasons:[]}),workflowProposal:async()=>({proposal}),ownerStatus:async()=>({owner_session:{active:true,expires_at:Date.now()/1000+600,allowed_operations:['workflows.grant_start']}}),workflowStart:async payload=>{starts.push(payload);throw JSON.stringify({code:'rate_limited',applied:false,retry_after_seconds:1});}};
  workflows=new WorkflowStore(client,'au6');
  workflows.catalog={available:true,bindings:[],reasons:[]};
  root=createRoot(document.getElementById('root'));
}
async function render(text,mode='simple',extra={}) {
  const context={conversationId:'c',messageId:'m',faces,store,onTasks:()=>{},onHandoff:async(action,id,managed)=>handoffs.push({action,id,managed}),...extra};
  await flush(()=>root.render(el(ExperienceContext.Provider,{value:mode,key:++key},el(WorkflowContext.Provider,{value:{store:workflows,catalog,onOpen:()=>{},onFresh:()=>{},pc:()=>null}},el(ActionCardsContext.Provider,{value:context},el(PortalMarkdown,{text,role:'assistant'}))))));
}
function visibleCards(count) {
  const cards=[...document.querySelectorAll('.md > .bob-action-card')];
  assert.equal(cards.length,count);
  assert.equal(document.querySelector('.md-subtask .bob-action-card'),null);
  assert.ok([...document.querySelectorAll('.md-subtask')].every(d=>!d.open));
  return cards;
}
const folds = [
  body=>'Answer.\n\nDetails: Evidence.\n\n'+body,
  body=>'Answer.\n### **Details:**\nEvidence.\n'+body,
  body=>'Answer.\n<details>\n<summary>Details</summary>\nEvidence.\n'+body+'\n</details>\nAfter.',
  body=>'Answer.\n### Subtask 1\nEvidence.\n'+body+'\n## Next\nAfter.',
  body=>'Answer.\nDetails:\nOuter.\n<details>\nEvidence.\n'+body+'\n</details>'
];
(async()=>{
  for(const mode of ['simple','pro']) {
    for(const fold of folds) {
      await reset();await render(fold(block(handoff)+'\nText stays folded.\n'+block(calendar)),mode);
      const cards=visibleCards(2);assert.deepEqual(cards.map(c=>c.querySelector('h3').textContent),[handoff.title,calendar.title]);
      const outer=document.querySelector('.md > .md-subtask');assert.equal(outer.nextElementSibling,cards[0]);assert.equal(cards[0].nextElementSibling,cards[1]);assert.match(outer.textContent,/Evidence/);assert.match(outer.textContent,/Text stays folded/);
      assert.equal(handoffs.length+saved+starts.length,0,'rendering authorizes nothing');
      await flush(()=>button('Start Builder').click());assert.deepEqual(handoffs,[{action:handoff,id:id(0),managed:true}]);
      await flush(()=>button('Open in Builder instead').click());assert.deepEqual(handoffs[1],{action:handoff,id:id(0),managed:false});
      await flush(()=>{button('Add to Tasks').click();button('Add to Tasks').click();});assert.equal(saved,1);assert.equal(JSON.parse(data)[0].id,id(1));
      await render(fold(block(handoff)+'\n'+block(calendar)),mode,{managedRun:actionId=>actionId===id(0)?el('p',null,'Already started'):null});
      assert.ok(button('Open Tasks'),'existing added receipt survives extraction and remount');assert.match(document.body.textContent,/Already started/);assert.equal(button('Start Builder'),undefined);
      await reset();await render(fold(block(workflow)),mode);const [card]=visibleCards(1);assert.ok(card.classList.contains('workflow-proposal'));assert.equal(button('Start').disabled,false);assert.equal(starts.length,0);
      await flush(()=>button('Start').click());assert.equal(starts.length,1);assert.deepEqual({...starts[0],request_key:undefined},{proposal_id:'p',proposal_revision:1,proposal_digest:workflow.proposal_digest,allow_same_model_review:false,request_key:undefined});assert.ok(starts[0].request_key);
      await reset();await render(fold(block(workflow)),mode,{streaming:true});const placeholder=[...document.querySelector('.md').children].find(n=>/being prepared/.test(n.textContent)&&n.tagName==='P');assert.ok(placeholder);assert.equal(placeholder.closest('.md-subtask'),null);assert.equal(button('Start'),undefined);assert.equal(starts.length,0);
      await render(fold(block(handoff)+'\n'+block(calendar)),mode,{streaming:true});visibleCards(2);for(const label of ['Start Builder','Open in Builder instead','Add to Tasks']){assert.ok(button(label).disabled);await flush(()=>button(label).click());}assert.equal(handoffs.length+saved+starts.length,0);
    }
    for(const wrap of [b=>'Details:\n'+b,b=>'<details>\n'+b+'\n</details>',b=>'### Subtask 1\n'+b,b=>'Details:\n<details>\n'+b+'\n</details>']){
      await reset();await render(wrap(block(calendar)),mode);visibleCards(1);assert.equal(document.querySelector('.md-subtask'),null,'action-only folds disappear');
    }
    await reset();await render('Details:\n'+fence+'bob-action\n{"type":"calendar"',mode,{streaming:true});assert.equal(document.querySelector('.md-subtask'),null);assert.match(document.querySelector('.md > p').textContent,/being prepared/);assert.equal(document.querySelector('.md-code'),null);
    await render(block(handoff)+'\n\nDetails: Evidence.\n'+block(calendar),mode);visibleCards(2);assert.equal(document.querySelector('.md').firstElementChild.querySelector('h3').textContent,handoff.title);await flush(()=>button('Add to Tasks').click());assert.equal(JSON.parse(data)[0].id,id(1));
    await reset();await render(block({...calendar,date:'invalid'})+'\nDetails:\n'+block(handoff)+'\n'+block(calendar),mode);visibleCards(1);await flush(()=>button('Start Builder').click());assert.equal(handoffs[0].id,id(1),'invalid fences still consume their original index; the third action stays capped');
    await reset();await render('### Subtask 1\nOrdinary folded text.\n## Answer\nVisible.',mode);assert.equal(document.querySelectorAll('.md-subtask').length,1);assert.match(document.querySelector('.md-subtask').textContent,/Ordinary folded text/);
  }
  console.log('AU6: PASS (Simple/Pro Details paragraph, heading, literal and nested folds, subtasks; visible ordered cards, working buttons, stable indices and receipts, workflow start payload and streaming placeholder, incomplete streaming fence, empty folds, before-Details actions, unchanged two-action cap)');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(root)await flush(()=>root.unmount());workflows?.dispose();dom.window.close();});
