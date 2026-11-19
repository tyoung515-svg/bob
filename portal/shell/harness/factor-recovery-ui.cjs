// S3i offline React/client seam. Secrets are represented only inside the native mock.
const assert=require('node:assert/strict'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
for(const k of ['window','document','HTMLElement','Event','MouseEvent','CustomEvent','localStorage'])global[k]=dom.window[k];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const {createPortalClient}=require('../src/portal/portal-client.ts');
const {FactorRecovery}=require('../src/portal/FactorRecovery.tsx');
const {SecurityNotice}=require('../src/portal/SecurityNotice.tsx');
const {SettingsView,DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const {operationReason}=require('../src/portal/durable-runs.ts');
const ops=['auth.factors.binding','auth.factors.prepare','auth.factors.commit','auth.factors.cancel','settings.status','auth.change_password','owner.begin','owner.confirm','owner.status','grants.list'];
const catalog=()=>({capabilities:{operations:Object.fromEntries(ops.map(op=>[op,{contract_versions:[op==='settings.status'?1:2],available:true,implemented:true}]))}});
let state,hits,root;
const text=()=>document.body.textContent;
const button=s=>[...document.querySelectorAll('button')].find(x=>x.textContent===s);
const fail=code=>{throw JSON.stringify({code,detail:code});};
const status=(phase='proof_verified',extra={})=>({handle:'opaque-fixture',pair:state.pair,phase,expires_at:Date.now()/1000+300,saved:false,outcome:null,...extra});
function clean(){const snapshot=document.body.innerHTML+JSON.stringify({...localStorage})+JSON.stringify(hits);for(const value of ['RECOVERY-SECRET-CANARY','SEED-CANARY','PASSWORD-CANARY','654321','BEARER-CANARY'])assert.ok(!snapshot.includes(value));assert.equal(document.querySelector('input[type="password"]'),null,'recovery secrets never enter DOM');}
const client=createPortalClient(async(cmd,args)=>{
  hits.push({cmd,args});clean();
  if(cmd==='portal_recovery_capability_v2')return {available:!state.nonWindows,reason:'Native USB and print recovery require Windows.'};
  if(cmd==='portal_recovery_status_v2')return state.pending;
  if(cmd==='portal_recovery_start_v2'){
    assert.deepEqual(Object.keys(args).sort(),['contract_version','pair','printed']);state.pair=args.pair;
    state.pending=status(['wrong','mixed'].includes(state.mode)?'not_changed':'proof_verified');return state.pending;
  }
  if(cmd==='portal_recovery_act_v2'){
    assert.deepEqual(Object.keys(args).sort(),['action','contract_version','handle']);assert.equal(args.handle,'opaque-fixture');
    switch(args.action){
      case 'seed':return state.pending;
      case 'usb':if(state.mode==='usbFail')fail('recovery_write_failed');break;
      case 'print':if(state.mode==='printCancel')fail('native_cancelled');break;
      case 'commit':assert.ok(state.pending.saved);state.pending=status(state.mode==='lost'?'unknown':'activated',{saved:true,outcome:state.mode==='lost'?null:{changed:true,restarted:true}});if(state.mode==='ipcLost')fail('timeout_no_reply');return state.pending;
      case 'cancel':state.pending=status('cancelled');return state.pending;
      case 'check':assert.equal(state.pending.phase,'unknown');if(state.wait)return state.pending;state.pending=status('activated',{saved:true,outcome:{changed:true,restarted:!state.readinessFailed}});return state.pending;
      case 'finish':state.pending=null;return null;
      default:throw Error('Unexpected action');
    }
    state.pending=status('material_saved',{saved:true});return state.pending;
  }
  if(cmd==='portal_owner_confirm_v2')return {owner_session:{active:true},security_notice:state.notice};
  throw Error('Unexpected command '+cmd);
});
function reset(extra={}){localStorage.clear();hits=[];state={pair:'password_totp',mode:'ready',pending:null,...extra};}
async function flush(fn=()=>{}){await act(async()=>{await fn();await new Promise(r=>setTimeout(r,0));});clean();}
async function mount(node){root=createRoot(document.getElementById('root'));await flush(()=>root.render(node));}
async function unmount(){await flush(()=>root.unmount());}
async function click(s){const b=button(s);assert.ok(b,s);assert.ok(!b.disabled,s+' enabled');await flush(()=>b.click());}
async function pair(p){const e=document.querySelector('[aria-label="Recovery factors"]');await flush(()=>{e.value=p;e.dispatchEvent(new Event('change',{bubbles:true}));});}
const form=cat=>React.createElement(FactorRecovery,{client,catalog:cat??catalog(),onReconnect:()=>{state.reconnected=true;}});
const settings=identity=>React.createElement(SettingsView,{client,invoke:async()=>[],hello:{identity},catalog:catalog(),profiles:null,onProfiles:()=>{},onReconnect:()=>{},appearance:DEFAULT_APPEARANCE,onAppearance:()=>{},pendingApprovals:0});
module.exports=(async()=>{
  for(const p of ['setup','password_totp','password_stick','totp_stick']){
    reset();await mount(form());await pair(p);await click('Continue in secure window');assert.match(text(),/PENDING/);assert.ok(button('Activate replacement').disabled);
    if(['setup','password_stick'].includes(p))await click('Show new authenticator setup');
    await click('Save to USB');assert.ok(!button('Activate replacement').disabled);await click('Activate replacement');assert.match(text(),/Replacement activated/);assert.match(text(),/all sessions are invalid/);
    assert.equal(hits.filter(x=>x.cmd==='portal_recovery_start_v2').length,1);assert.equal(hits.filter(x=>x.cmd==='portal_owner_confirm_v2').length,0);
    await click('Reconnect after recovery');assert.ok(state.reconnected);await unmount();
  }
  for(const mode of ['wrong','mixed']){reset({mode});await mount(form());await click('Continue in secure window');assert.match(text(),/Old factors remain authoritative/);assert.equal(button('Activate replacement'),undefined);await unmount();}
  reset();await mount(form());await click('Continue in secure window');state.pending.expires_at=1;await unmount();await mount(form());assert.match(text(),/Challenge expired/);assert.ok(button('Save to USB').disabled);assert.ok(button('Activate replacement').disabled);await click('Cancel recovery');assert.match(text(),/Old factors remain authoritative/);await unmount();
  for(const [mode,action] of [['usbFail','Save to USB'],['printCancel','Print recovery code']]){
    reset({mode});await mount(form());await click('Continue in secure window');await click(action);assert.ok(button('Activate replacement').disabled);assert.match(text(),/old factors remain authoritative/);assert.ok(!hits.some(x=>x.args?.action==='commit'));await unmount();
  }
  reset();await mount(form());await pair('totp_stick');await flush(()=>document.querySelector('[aria-label="Use printed recovery code"]').click());await click('Continue in secure window');assert.equal(hits.find(x=>x.cmd==='portal_recovery_start_v2').args.printed,true);await click('Print recovery code');await click('Activate replacement');assert.match(text(),/Replacement activated/);await unmount();
  reset({mode:'lost',wait:true});await mount(form());await click('Continue in secure window');await click('Save to USB');await click('Activate replacement');assert.match(text(),/Outcome unknown/);await click('Check recovery receipt');assert.match(text(),/Outcome unknown/);await unmount();
  state.wait=false;state.readinessFailed=true;await mount(form());await click('Check recovery receipt');assert.match(text(),/Replacement activated; readiness failed/);assert.equal(hits.filter(x=>x.args?.action==='commit').length,1);assert.equal(hits.filter(x=>x.cmd==='portal_recovery_start_v2').length,1);await unmount();
  reset({mode:'ipcLost'});await mount(form());await click('Continue in secure window');await click('Save to USB');await click('Activate replacement');assert.match(text(),/Replacement activated/);assert.equal(hits.filter(x=>x.args?.action==='commit').length,1);await unmount();
  for(const op of ops.filter(x=>x.startsWith('auth.factors.')||x==='settings.status'))for(const mode of ['missing','old','blocked']){
    reset();const cat=catalog();if(mode==='missing')delete cat.capabilities.operations[op];else if(mode==='old')cat.capabilities.operations[op].contract_versions=[0];else Object.assign(cat.capabilities.operations[op],{available:false,reason:'fixture_recovery_unavailable'});
    await mount(form(cat));assert.ok(button('Continue in secure window').disabled);assert.match(text(),mode==='blocked'?/fixture_recovery_unavailable/:/Update Bob/);assert.equal(operationReason(cat,'auth.change_password'),'');assert.equal(operationReason(cat,'grants.list'),'');assert.ok(!hits.some(x=>x.cmd==='portal_recovery_start_v2'));await unmount();
  }
  reset({nonWindows:true});await mount(form());assert.ok(button('Continue in secure window').disabled);assert.match(text(),/require Windows/);await unmount();
  for(const identity of [{sub:'o',token_type:'agent',owner_controls_eligible:true},{sub:'f',token_type:'portal',owner_controls_eligible:false},{sub:'h',token_type:'human'}]){reset();await mount(settings(identity));assert.equal(button('Security'),undefined);assert.equal(hits.length,0);await unmount();}
  reset();await mount(settings({sub:'o',token_type:'portal',owner_controls_eligible:true}));await click('Security');assert.ok(button('Continue in secure window'));await unmount();
  reset({notice:'two-factor was reset on 2026-11-04'});await mount(React.createElement(SecurityNotice));await flush(()=>client.ownerConfirm('fixture','', ''));assert.match(text(),/two-factor was reset on 2026-11-04/);await unmount();
  reset({notice:'BEARER-CANARY'});await mount(React.createElement(SecurityNotice));await flush(()=>client.ownerConfirm('fixture','',''));assert.equal(text(),'');await unmount();
  console.log('factor recovery UI: PASS (enrollment, all pairs, wrong/mixed factors, expiry, save failure, print cancel, PENDING, dropped commit/IPC reply, receipt-only recovery, login notice, snapshot canaries, per-operation capability isolation and owner visibility)');
})();
module.exports.catch(e=>{console.error(e);process.exitCode=1;});
