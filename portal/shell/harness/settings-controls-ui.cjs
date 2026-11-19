// S3e: offline real React/client seam; synthetic keys only.
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost'});
for (const k of ['window','document','HTMLElement','Event','MouseEvent','localStorage']) global[k]=dom.window[k];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'), {act}=React, {createRoot}=require('react-dom/client');
const {createPortalClient}=require('../src/portal/portal-client.ts');
const {ProviderKeys,ApprovalPolicyEditor}=require('../src/portal/SettingsControls.tsx');
const {SettingsView,DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const {operationReason}=require('../src/portal/durable-runs.ts');
const CANARY='SYNTHETIC-S3E-KEY-CANARY', PROVIDER='fixture-provider';
const operations=['owner.begin','owner.confirm','owner.status','secrets.set','settings.status','policy.get','policy.set'];
const catalog=()=>({backends:[],models:[],faces:[],capabilities:{operations:Object.fromEntries(operations.map(op=>[op,{contract_versions:[op==='settings.status'?1:2],implemented:true,available:true,...(op==='secrets.set'?{storage_only:true,providers:[PROVIDER]}:{})}]))}});
const receipt={provider:PROVIDER,stored:true,fingerprint:'0123456789abcdef',operation_id:'receipt-1'};
const policy={posture:'scoped',scope:{auto_actions:['model_call','read'],escalate_actions:['write'],may_touch:[{attachment_id:'registered-folder',path:'src/**'}],may_not_touch:[]}};
const projection=rev=>({policy:{...policy,scope:{...policy.scope,may_touch:['/server/project/src/**']}},revision:rev,immutable_restrictions:{scope_intersection:true,always_human:['purchase']}});
let state, root, hits;
const text=()=>document.body.textContent;
const field=label=>document.querySelector(`[aria-label="${label}"]`);
const button=label=>[...document.querySelectorAll('button')].find(b=>b.textContent===label);
const failure=(code,extra={})=>{throw JSON.stringify({code,detail:'safe server refusal',...extra});};
function clean(){assert.ok(!document.body.innerHTML.includes(CANARY));assert.ok(!JSON.stringify({...localStorage}).includes(CANARY));const f=field(`New key for ${PROVIDER}`);if(f)assert.equal(f.value,'');}
async function flush(fn=()=>{}){await act(async()=>{await fn();await new Promise(r=>setTimeout(r,0));});}
async function input(label,value){const el=field(label);assert.ok(el,label);await flush(()=>{const proto=el.tagName==='TEXTAREA'?dom.window.HTMLTextAreaElement.prototype:dom.window.HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(proto,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));});}
async function click(label){const b=button(label);assert.ok(b,label);assert.ok(!b.disabled,label+' enabled');await flush(()=>b.click());}
async function mount(node){root=createRoot(document.getElementById('root'));await flush(()=>root.render(node));}
async function unmount(){await flush(()=>root.unmount());clean();}
function reset(extra={}){localStorage.clear();hits=[];state={active:true,mode:'success',revision:0,...extra};}
const client=createPortalClient(async(cmd,args)=>{
  // Verify clear BEFORE any native request, including proof and session reads.
  clean(); hits.push({cmd,args:cmd==='portal_secret_set_v2'?{provider:args.provider}:cmd==='portal_owner_confirm_v2'?{}:args});
  switch(cmd){
    case 'local_auth_status':return [];
    case 'portal_owner_status_v2':
      if(state.delayStatus)await new Promise(resolve=>{state.releaseStatus=resolve;});
      return {owner_session:{active:state.active,expires_at:Date.now()/1000+240,allowed_operations:operations}};
    case 'portal_owner_begin_v2':return {challenge_id:'challenge',expires_at:Date.now()/1000+100,summary:{operation:args.operation,resource:args.resource}};
    case 'portal_owner_confirm_v2':assert.equal(args.password,'synthetic-password');assert.equal(args.totp,'123456');state.active=true;return {owner_session:{active:true}};
    case 'portal_secret_set_v2':
      assert.deepEqual(Object.keys(args).sort(),['key','provider']);assert.equal(args.key,CANARY);
      if(state.mode==='expiry'){state.active=false;failure('owner_session_invalid',{request_key:'native-once'});}
      if(state.mode==='unsupported')failure('unsupported_provider',{request_key:'native-once'});
      if(state.mode==='refusal')failure('invalid_key',{request_key:'native-once'});
      if(state.mode==='timeout')failure('timeout_no_reply',{request_key:'native-once'});
      if(state.mode==='false')return {...receipt,stored:false};
      if(state.mode==='badFingerprint')return {...receipt,fingerprint:'local-fingerprint'};
      if(state.mode==='wrongProvider')return {...receipt,provider:'unadvertised'};
      if(state.mode==='noReceipt')return {...receipt,operation_id:undefined};
      return receipt;
    case 'portal_settings_status_v1':assert.deepEqual(args,{contract_version:1,request_key:'native-once'});return {operation_id:'receipt-1',phase:state.phase||'ready',result:receipt};
    case 'portal_policy_get_v2':return state.revision?projection(state.revision):{policy:null,revision:0,immutable_restrictions:{scope_intersection:true}};
    case 'portal_policy_set_v2':
      if(state.policyBadReceipt)return {...projection(1),paused_runs:[]};
      if(state.conflict){state.revision=2;state.conflict=false;failure('gateway_http_409',{detail:'revision_conflict'});}
      if(state.policyTimeout){state.policyTimeout=false;failure('timeout_no_reply');}
      assert.equal(args.expected_revision,state.revision);state.submitted=args;state.revision++;
      return {...projection(state.revision),operation_id:'policy-receipt',paused_runs:['run-paused']};
    default:throw new Error('Unexpected native command '+cmd);
  }
});
const keys=(cat=catalog(),owner=true)=>React.createElement(ProviderKeys,{client,catalog:cat,owner,scope:'settings-fixture'});
const editor=(cat=catalog(),owner=true)=>React.createElement(ApprovalPolicyEditor,{client,catalog:cat,owner,scope:'settings-fixture'});
async function store(){await input(`New key for ${PROVIDER}`,CANARY);await click('Store key');clean();}
async function proof(){await input('Owner password','synthetic-password');await input('Owner authenticator code','123456');await click('Confirm and continue');assert.equal(field('Owner password'),null);clean();}
(async()=>{
  reset();await mount(keys());await store();assert.match(text(),/Saved\./);assert.ok(!text().includes('Sent'));await unmount();
  for(const mode of ['unsupported','refusal','expiry','false','badFingerprint','wrongProvider','noReceipt']){
    reset({mode});await mount(keys());await store();assert.ok(!text().includes('Saved.'));assert.equal(hits.filter(h=>h.cmd==='portal_secret_set_v2').length,1);clean();
    if(mode==='expiry'){assert.match(text(),/owner_session_invalid/);state.mode='success';await store();assert.ok(field("Confirm it's you"));await proof();assert.match(text(),/Saved\./);}
    await unmount();
  }
  reset({active:false});await mount(keys());await store();assert.ok(field("Confirm it's you"));clean();await proof();assert.match(text(),/Saved\./);await unmount();
  reset({active:false});await mount(keys());await store();await click('Cancel');clean();assert.equal(hits.filter(h=>h.cmd==='portal_secret_set_v2').length,0);await unmount();
  for(const value of ['', 'space key', 'k'.repeat(16385)]){
    reset();await mount(keys());await input(`New key for ${PROVIDER}`,value);await click('Store key');clean();
    assert.equal(hits.length,0,'invalid keys stop before session or key IPC');await unmount();
  }
  // A late owner read cannot dispatch a captured key after navigation or loss
  // of eligibility. No secret ever enters React state or the pending journal.
  reset({delayStatus:true});await mount(keys());await store();assert.equal(typeof state.releaseStatus,'function');
  await unmount();await flush(()=>state.releaseStatus());assert.equal(hits.filter(h=>h.cmd==='portal_secret_set_v2').length,0);clean();
  reset({delayStatus:true});await mount(keys());await store();
  await flush(()=>root.render(React.createElement(ProviderKeys,{client:{...client},catalog:catalog(),owner:true,scope:'settings-fixture'})));
  await flush(()=>state.releaseStatus());assert.equal(hits.filter(h=>h.cmd==='portal_secret_set_v2').length,0,'a replaced client cannot receive the previous action');await unmount();
  reset({delayStatus:true});await mount(editor());await click('Read approval policy');
  await flush(()=>root.render(editor(catalog(),false)));await flush(()=>state.releaseStatus());
  assert.equal(hits.filter(h=>h.cmd==='portal_policy_get_v2').length,0);assert.equal(field('Policy draft'),null);await unmount();
  reset({mode:'timeout'});await mount(keys());await store();assert.match(text(),/Bob did not confirm the save\. Check whether it was stored/);assert.ok(button('Store key').disabled);await unmount();
  await mount(keys());assert.ok(button('Store key').disabled,'safe pending lookup survives remount');
  for(const phase of ['prepared','committed']){state.phase=phase;await click('Check storage receipt');assert.ok(!text().includes('Saved.'));clean();}
  state.phase='ready';await click('Check storage receipt');assert.match(text(),/Saved\./);assert.equal(hits.filter(h=>h.cmd==='portal_secret_set_v2').length,1,'lookup never replays key');await unmount();
  reset({mode:'timeout'});await mount(keys());await store();await unmount();
  const storageRemoved=catalog();delete storageRemoved.capabilities.operations['secrets.set'];
  await mount(keys(storageRemoved));assert.equal(field(`New key for ${PROVIDER}`),null);assert.equal(button('Store key'),undefined);
  await click('Check storage receipt');assert.match(text(),/Saved\./);assert.equal(hits.filter(h=>h.cmd==='portal_secret_set_v2').length,1);await unmount();
  reset();await mount(editor());await click('Read approval policy');assert.match(text(),/Revision 0/);await input('Policy draft',JSON.stringify(policy));await click('Save approval policy');assert.match(text(),/policy-receipt/);assert.match(text(),/run-paused/);assert.deepEqual(JSON.parse(field('Policy draft').value),policy,'submitted attachment paths retained');assert.deepEqual(state.submitted.policy,policy);assert.match(text(),/\/server\/project\/src/);await unmount();
  reset({conflict:true});await mount(editor());await click('Read approval policy');await input('Policy draft',JSON.stringify(policy));await click('Save approval policy');assert.match(text(),/Revision conflict/);assert.ok(button('Save approval policy').disabled);await click('Read approval policy');assert.match(text(),/Revision 2/);await click('Save approval policy');assert.equal(state.submitted.expected_revision,2);await unmount();
  reset({policyTimeout:true});await mount(editor());await click('Read approval policy');await input('Policy draft',JSON.stringify(policy));await click('Save approval policy');const sent=hits.find(h=>h.cmd==='portal_policy_set_v2').args;await unmount();
  await mount(editor());await click('Reconcile policy change');assert.deepEqual(hits.filter(h=>h.cmd==='portal_policy_set_v2').at(-1).args,sent);assert.match(text(),/Policy saved/);await unmount();
  reset({active:false});await mount(editor());await click('Read approval policy');assert.ok(field("Confirm it's you"));await proof();
  await input('Policy draft',JSON.stringify(policy));state.active=false;await click('Save approval policy');assert.ok(field("Confirm it's you"));
  await click('Cancel');assert.equal(hits.filter(h=>h.cmd==='portal_policy_set_v2').length,0);await unmount();
  reset({policyBadReceipt:true});await mount(editor());await click('Read approval policy');await input('Policy draft',JSON.stringify(policy));await click('Save approval policy');
  assert.ok(!text().includes('Policy saved'));assert.match(text(),/invalid_receipt/);const badSent=hits.find(h=>h.cmd==='portal_policy_set_v2').args;
  state.policyBadReceipt=false;await click('Reconcile policy change');assert.deepEqual(hits.filter(h=>h.cmd==='portal_policy_set_v2').at(-1).args,badSent);assert.match(text(),/Policy saved/);await unmount();
  // Each absent operation affects its own feature; capability reasons are displayed.
  for(const op of ['secrets.set','settings.status','policy.get','policy.set']){
    reset();const cat=catalog();delete cat.capabilities.operations[op];
    for(const other of operations.filter(x=>x!==op))assert.equal(operationReason(cat,other,other==='settings.status'?1:2),'');
    await mount(keys(cat));if(op==='secrets.set')assert.equal(field(`New key for ${PROVIDER}`),null);else assert.ok(!button('Store key').disabled);await unmount();
    await mount(editor(cat));assert.equal(button('Read approval policy').disabled,op==='policy.get');if(op!=='policy.get')await click('Read approval policy');if(op==='policy.set')assert.ok(button('Save approval policy').disabled);await unmount();
  }
  reset();const cat=catalog();cat.capabilities.operations['secrets.set'].available=false;cat.capabilities.operations['secrets.set'].reason='installed_helper_unavailable';await mount(keys(cat));assert.ok(button('Store key').disabled);assert.match(text(),/installed_helper_unavailable/);await unmount();
  for(const op of ['policy.get','policy.set']){
    reset();const cat=catalog();cat.capabilities.operations[op].available=false;cat.capabilities.operations[op].reason=`fixture_${op}_unavailable`;
    await mount(editor(cat));assert.match(text(),new RegExp(`fixture_${op}_unavailable`));assert.equal(button('Read approval policy').disabled,op==='policy.get');
    if(op==='policy.set'){await click('Read approval policy');assert.ok(button('Save approval policy').disabled);}await unmount();
  }
  reset({mode:'timeout'});await mount(keys());await store();await unmount();const noStatus=catalog();delete noStatus.capabilities.operations['settings.status'];await mount(keys(noStatus));assert.ok(button('Check storage receipt').disabled);await unmount();
  for(const identity of [{sub:'fixture-owner',token_type:'agent',owner_controls_eligible:true},{sub:'foreign',token_type:'portal',owner_controls_eligible:false}]){
    reset();await mount(React.createElement(SettingsView,{client,invoke:async()=>[],hello:{identity},catalog:catalog(),profiles:null,onProfiles:()=>{},onReconnect:()=>{},appearance:DEFAULT_APPEARANCE,onAppearance:()=>{},pendingApprovals:0}));
    await click('Keys & sign-ins');assert.equal(field(`New key for ${PROVIDER}`),null);await click('Approvals & scope');assert.equal(field('Policy draft'),null);await unmount();
  }
  console.log('settings controls UI: PASS (stored receipt, refusal, unsupported, expiry, proof/cancel, cleared fields and snapshots, malformed receipts, navigation/client/eligibility races, no replay, safe status/remount, policy conflict/reconciliation, attachment rules, per-op old servers, nonowners)');
})().catch(e=>{console.error(e);process.exit(1);});
