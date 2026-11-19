const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url:'http://localhost' });
for (const name of ['window','document','HTMLElement','Event','MouseEvent','CustomEvent','localStorage']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const { BuilderApprovalCard } = require('../src/portal/BuilderApprovalCard.tsx');
const { BuilderModeBar } = require('../src/portal/BuilderModeBar.tsx');
const { permissionDisplay } = require('../src/portal/PermissionSummary.tsx');
const ops = ['builder.approve','builder.approval.get','builder.mode.get','builder.mode.set','owner.status','owner.begin','owner.confirm'];
const catalog = {capabilities:{operations:Object.fromEntries(ops.map(op=>[op,{contract_versions:[2],available:true,implemented:true}]))}};
let seq=0, hits=[], active=true, expired=false, permitted=ops, key=0, decision, record;
let mode={conversation_id:'c',project_id:'p',mode:'approve',revision:1,expires_at:null,network:{allowlist:[],project_extra:[]}};
const display={title:'Write app/hello.html (+166 lines)',lines:['Workspace: hello'],scope:'Allow file edits in hello for this session. Covers this operation kind only; protected paths are excluded.'};
const client=createPortalClient(async (op,args)=>{
  hits.push([op,args]);
  switch(op) {
    case 'portal_owner_status_v2': return {owner_session:{active,expires_at:Date.now()/1000+(expired?-1:250),allowed_operations:permitted}};
    case 'portal_owner_begin_v2':
      return {challenge_id:'challenge',confirmation_mode:'signed_in_user',summary:{display:args.operation==='builder.mode.set'?{title:'Change Builder mode: Approve → '+args.resource.requested_mode,lines:[],scope:'This conversation.'}:display,complete:'x'.repeat(900)+'FULL-END'}};
    case 'portal_owner_confirm_v2': return {owner_session:{active:true}};
    case 'portal_builder_approval_get_v2': return structuredClone(record);
    case 'portal_builder_approve_v2': decision=args.decision; record.state=decision==='deny'?'denied':'allowed'; return {approval_id:record.approval_id,state:record.state};
    case 'portal_builder_mode_get_v2': return structuredClone(mode);
    case 'portal_builder_mode_set_v2':
      mode={...mode,mode:args.mode,revision:mode.revision+1,expires_at:args.mode==='yolo'?Date.now()/1000+7200:null};
      return structuredClone(mode);
    default: throw Error(op);
  }
});
const root=createRoot(document.getElementById('root')), flush=()=>new Promise(r=>setTimeout(r,0));
const count=op=>hits.filter(([x])=>x===op).length;
const button=text=>[...document.querySelectorAll('button')].find(b=>b.textContent===text);
async function click(text) {assert.ok(button(text),text);await act(async()=>{button(text).click();await flush();});}
async function render(Component,props={}) {await act(async()=>{root.render(React.createElement(Component,{key:++key,client,catalog,conversationId:'c',scope:'s10',approvalId:record.approval_id,...props}));await flush();});}
async function choose(value) {await act(async()=>{const e=document.querySelector('select');Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype,'value').set.call(e,value);e.dispatchEvent(new Event('change',{bubbles:true}));await flush();});}
function reset(changes={}) {decision=null;record={schema_version:1,approval_id:'a'+(++seq),conversation_id:'c',kind:'write',summary:'fs_write',display,detail:{path:'app/hello.html',diff:'FULL-DIFF-END'},mode:'approve',state:'pending',expires_at:Date.now()/1000+600,...changes};}
(async()=>{
  for (const [label,expected,changes] of [['Allow once','allow_once',{}],['Allow for session','allow_session',{}],['Deny','deny',{}],['Apply','allow_once',{mode:'suggest'}]]) {
    reset(changes);await render(BuilderApprovalCard);
    const before=count('portal_owner_begin_v2');
    assert.ok(document.body.textContent.includes(display.title));
    assert.ok(document.body.textContent.includes('Allow file edits in hello for this session'));
    assert.ok(!document.querySelector('details').open,'full record is collapsed');
    assert.ok(document.querySelector('details').textContent.includes('FULL-DIFF-END'));
    await click(label);assert.equal(decision,expected);assert.ok(!document.querySelector('.owner-modal'));
    assert.equal(count('portal_owner_begin_v2'),before,'active owner uses existing proof');
  }
  for (const state of ['inactive','expired','wrong-operation']) {
    active=state!=='inactive';expired=state==='expired';permitted=state==='wrong-operation'?[]:ops;
    reset();await render(BuilderApprovalCard);await click('Allow once');
    assert.equal(decision,null);assert.ok(document.body.textContent.includes('Confirm owner session'));
    assert.ok(document.body.textContent.includes(display.title));
    assert.ok(document.querySelector('.owner-modal details').textContent.includes('FULL-END'),'complete summary is never truncated');
    await click('Cancel');assert.equal(decision,null);
    await click('Allow once');await click('Confirm and continue');assert.equal(decision,'allow_once');
  }
  active=true;expired=false;permitted=ops;
  await render(BuilderModeBar);
  for (const next of ['suggest','auto','approve']) {
    const before=count('portal_owner_begin_v2');await choose(next);
    assert.equal(mode.mode,next);assert.ok(!document.querySelector('.owner-modal'));
    assert.equal(count('portal_owner_begin_v2'),before);
  }
  active=false;await choose('auto');assert.equal(mode.mode,'approve');
  assert.ok(document.body.textContent.includes('Change Builder mode: Approve → auto'));
  const challenge=hits.filter(([op])=>op==='portal_owner_begin_v2').at(-1)[1];
  assert.equal(challenge.resource.requested_mode,'auto');
  await click('Confirm and continue');assert.equal(mode.mode,'auto');
  active=true;const before=count('portal_builder_mode_set_v2');
  await choose('yolo');assert.ok(document.querySelector('.owner-modal'),'yolo keeps normal owner review');
  await click('Confirm and continue');assert.ok(button('Enable yolo').disabled);assert.equal(count('portal_builder_mode_set_v2'),before);
  assert.ok(document.body.textContent.includes('Mistakes'));
  await click('Cancel');assert.equal(count('portal_builder_mode_set_v2'),before);
  await choose('yolo');await click('Confirm and continue');
  await act(async()=>{document.querySelector('input[type=checkbox]').click();await flush();});await click('Enable yolo');
  assert.equal(mode.mode,'yolo');assert.equal(count('portal_builder_mode_set_v2'),before+1);
  assert.equal(permissionDisplay({display:{...display,lines:[{}]}}),null);
  console.log('S10 UI passed: four active-owner decisions, inactive/expired/scoped auth and cancellation, three one-click modes, mode challenge, yolo double confirmation, human summaries and complete collapsed details.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await act(async()=>root.unmount());});
