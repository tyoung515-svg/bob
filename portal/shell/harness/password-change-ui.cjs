// S3h: offline real React + client seam. Synthetic factors only.
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost'});
for (const key of ['window','document','HTMLElement','Event','MouseEvent','localStorage']) global[key]=dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT=true;
const React=require('react'), {act}=React, {createRoot}=require('react-dom/client');
const {createPortalClient}=require('../src/portal/portal-client.ts');
const {PasswordChange}=require('../src/portal/PasswordChange.tsx');
const {SettingsView,DEFAULT_APPEARANCE}=require('../src/portal/SettingsView.tsx');
const {operationReason}=require('../src/portal/durable-runs.ts');
const OLD='SYNTHETIC-OLD-PASSWORD', NEW='SYNTHETIC-NEW-PASSWORD', CODE='654321';
const ops=['auth.change_password','settings.status','owner.begin','owner.confirm','owner.status','grants.list','policy.get','secrets.set'];
const catalog=()=>({backends:[],models:[],faces:[],capabilities:{operations:Object.fromEntries(ops.map(op=>[op,{contract_versions:[op==='settings.status'?1:2],available:true,implemented:true}]))}});
const field=label=>document.querySelector('[aria-label="'+label+'"]');
const button=label=>[...document.querySelectorAll('button')].find(b=>b.textContent===label);
const text=()=>document.body.textContent;
const fail=(code,extra={})=>{throw JSON.stringify({code,detail:code,...extra});};
let root,state,hits;
function clean(){
  for(const label of ['Current password','New password','New password again','Password authenticator code','Owner password','Owner authenticator code']){
    const el=field(label);if(el)assert.equal(el.value,'',label+' cleared before native IPC');
  }
  // DOM serialization, all React hook state and persisted storage contain no factors.
  let snapshot=document.body.innerHTML+JSON.stringify({...localStorage});
  const el=field('Current password');
  if(el){
    const fiber=el[Object.keys(el).find(k=>k.startsWith('__reactFiber'))];
    for(let f=fiber;f;f=f.return) if(typeof f.type==='function') for(let h=f.memoizedState;h&&'memoizedState' in h;h=h.next){
      if(typeof h.memoizedState==='string')snapshot+=h.memoizedState;
    }
  }
  for(const secret of [OLD,NEW,CODE])assert.ok(!snapshot.includes(secret),'no factors in snapshots or hook state');
}
async function flush(fn=()=>{}){await act(async()=>{await fn();await new Promise(r=>setTimeout(r,0));});}
async function input(label,value){const el=field(label);assert.ok(el,label);await flush(()=>{Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));});}
async function click(label){const el=button(label);assert.ok(el,label);assert.ok(!el.disabled,label+' enabled');await flush(()=>el.click());}
async function mount(node){root=createRoot(document.getElementById('root'));await flush(()=>root.render(node));}
async function unmount(){await flush(()=>root.unmount());clean();}
function reset(extra={}){localStorage.clear();hits=[];state={mode:'ready',pending:null,active:false,phase:'ready',receipt:{changed:true,restarted:true},...extra};}
const client=createPortalClient(async(cmd,args)=>{
  clean();hits.push({cmd,args:cmd==='portal_auth_change_password_v2'||cmd==='portal_owner_confirm_v2'?{}:args});
  switch(cmd){
    case 'portal_password_pending_v1':assert.deepEqual(args,{contract_version:1});return state.pending;
    case 'portal_auth_change_password_v2':
      assert.deepEqual(Object.keys(args).sort(),['current','new','totp']);
      assert.equal(args.current,OLD);assert.equal(args.new,state.newPassword??NEW);assert.equal(args.totp,CODE);
      state.active=false;
      if(['wrong','spent','invalidTotp'].includes(state.mode))return {changed:false,restarted:false};
      if(['invalid_password','owner_session_expired','owner_session_invalid','authentication_throttled'].includes(state.mode))fail(state.mode,{request_key:'native-once',...(state.mode==='authentication_throttled'?{retry_after:30}:{})});
      if(['lost','timeout','malformed','unknownNoKey'].includes(state.mode)){
        state.pending={operation:'auth.change_password',status:'unknown',request_key:'native-once'};
        if(state.delay)await new Promise(resolve=>state.release=resolve);
        fail(state.mode==='malformed'?'invalid_receipt':'password_outcome_unknown',state.mode==='unknownNoKey'?{}:{request_key:'native-once'});
      }
      return {changed:true,restarted:state.mode!=='restartPending'};
    case 'portal_settings_status_v1':
      assert.deepEqual(args,{contract_version:1,request_key:'native-once'});
      if(state.down)fail('settings_receipt_unavailable');
      if(['ready','failed'].includes(state.phase))state.pending=null;
      return {operation_id:'password-op',phase:state.phase,...(state.phase==='ready'||state.phase==='failed'?{result:state.receipt}:{})};
    case 'portal_owner_status_v2':return {owner_session:{active:state.active,expires_at:Date.now()/1000+240,allowed_operations:ops}};
    case 'portal_owner_begin_v2':assert.equal(args.operation,'grants.list');return {challenge_id:'sign-in-challenge',summary:{operation:'grants.list'},expires_at:Date.now()/1000+100};
    case 'portal_owner_confirm_v2':assert.equal(args.password,NEW);assert.equal(args.totp,'123456');state.active=true;return {owner_session:{active:true}};
    case 'portal_grants_list_v2':assert.ok(state.active);return {items:[],next_cursor:null};
    default:throw Error('Unexpected native command '+cmd);
  }
});
const form=(cat=catalog())=>React.createElement(PasswordChange,{client,catalog:cat,scope:'password-fixture'});
const settings=(cat=catalog(),identity={sub:'owner',gateway:'fixture-gateway',token_type:'portal',owner_controls_eligible:true})=>React.createElement(SettingsView,{client,invoke:async()=>[],hello:{identity},catalog:cat,profiles:null,onProfiles:()=>{},onReconnect:()=>{},appearance:DEFAULT_APPEARANCE,onAppearance:()=>{},pendingApprovals:0});
const mutations=()=>hits.filter(h=>h.cmd==='portal_auth_change_password_v2');
async function submit(password=NEW,confirmation=password){
  await input('Current password',OLD);await input('New password',password);await input('New password again',confirmation);await input('Password authenticator code',CODE);
  await click('Change password');clean();
}
(async()=>{
  reset();await mount(form());await submit();assert.match(text(),/Password changed and gateway restarted/);assert.match(text(),/Other sessions were signed out/);
  assert.equal(hits.filter(h=>h.cmd==='portal_owner_confirm_v2').length,0,'password mutation uses factors once, no preceding owner.confirm');
  await click('Sign in again');assert.ok(document.querySelector('[role="dialog"]'));await input('Owner password',NEW);await input('Owner authenticator code','123456');
  // The first Sign in again button is in the existing confirmation dialog.
  await click('Sign in again');assert.match(text(),/This owner session is signed in again/);assert.equal(mutations().length,1);await unmount();
  reset({mode:'restartPending'});await mount(form());await submit();assert.match(text(),/Password changed; gateway restart pending/);assert.ok(!text().includes('not changed'));await unmount();
  for(const mode of ['wrong','spent','invalidTotp']){
    reset({mode});await mount(form());await submit();assert.match(text(),/Password not changed/);assert.equal(mutations().length,1);assert.ok(!button('Change password').disabled);await unmount();
  }
  for(const mode of ['invalid_password','owner_session_expired','owner_session_invalid','authentication_throttled']){
    reset({mode});await mount(form());await submit();assert.match(text(),new RegExp('Password not changed: '+mode));assert.equal(mutations().length,1);assert.equal(hits.filter(h=>h.cmd==='portal_owner_confirm_v2').length,0);if(mode==='authentication_throttled')assert.ok(button('Change password').disabled);await unmount();
  }
  // No local policy: a short/same password reaches the server and its safe rule
  // refusal is shown, rather than duplicating server validation.
  reset({mode:'invalid_password',newPassword:'short'});await mount(form());await submit('short');assert.match(text(),/invalid_password/);await unmount();
  reset();await mount(form());await submit(NEW,'mismatch');assert.equal(mutations().length,0);assert.match(text(),/matching new passwords/);await unmount();
  for(const mode of ['lost','timeout','malformed']){
    reset({mode});await mount(form());await submit();assert.match(text(),/Password changed and gateway restarted/);assert.equal(mutations().length,1);assert.equal(hits.filter(h=>h.cmd==='portal_settings_status_v1').length,1);await unmount();
  }
  reset({mode:'lost',down:true});await mount(form());await submit();assert.match(text(),/Outcome unknown/);assert.ok(button('Change password').disabled);assert.equal(mutations().length,1);await unmount();
  // Native metadata and UI lookup survive remount; only receipt requests occur.
  await mount(form());assert.match(text(),/Outcome unknown/);assert.equal(mutations().length,1);
  state.down=false;
  for(const phase of ['prepared','committed','restart-pending']){state.phase=phase;await click('Check password receipt');assert.match(text(),/Outcome unknown/);assert.ok(button('Change password').disabled);}
  state.phase='failed';state.receipt={changed:true,restarted:false};await click('Check password receipt');assert.match(text(),/Password changed; gateway restart pending/);assert.equal(mutations().length,1);await unmount();
  reset({mode:'lost',down:true});await mount(form());await submit();await unmount();state.down=false;state.phase='failed';state.receipt={changed:false,restarted:false};
  await mount(form());assert.match(text(),/Password not changed/);assert.equal(mutations().length,1);await unmount();
  reset({mode:'unknownNoKey'});await mount(form());await submit();assert.match(text(),/Outcome unknown/);assert.ok(button('Change password').disabled);await unmount();
  reset({pending:{operation:'auth.change_password',status:'unknown',request_key:'native-once'}});await mount(form());assert.match(text(),/Password changed/);assert.equal(mutations().length,0,'native pre-dispatch metadata alone enables recovery');await unmount();
  for(const missing of ['absent','old','blocked']){
    reset();const cat=catalog();if(missing==='absent')delete cat.capabilities.operations['auth.change_password'];
    else if(missing==='old')cat.capabilities.operations['auth.change_password'].contract_versions=[1];
    else Object.assign(cat.capabilities.operations['auth.change_password'],{available:false,reason:'fixture_generation_required'});
    await mount(form(cat));assert.ok(button('Change password').disabled);
    assert.match(text(),missing==='blocked'?/fixture_generation_required/:/Update Bob/);
    for(const op of ops.filter(op=>op!=='auth.change_password'))assert.equal(operationReason(cat,op,op==='settings.status'?1:2),'');
    assert.equal(mutations().length,0);await unmount();
  }
  reset({mode:'lost',down:true});await mount(form());await submit();await unmount();const removed=catalog();delete removed.capabilities.operations['auth.change_password'];state.down=false;
  await mount(form(removed));assert.match(text(),/Password changed/);assert.equal(mutations().length,1);await unmount();
  // Settings selects the in-app form or the original terminal fallback per capability.
  reset();await mount(settings());await click('Account & sign-in');
  assert.ok(field('Current password'),'v2 capability shows the in-app form');
  assert.ok(!button('Change password').disabled);
  assert.equal(document.querySelector('.set-steps'),null,'v2 capability hides the terminal guide');
  assert.equal(document.querySelector('.set-code'),null,'v2 capability hides terminal command blocks');
  await unmount();
  for(const missing of ['absent','old','blocked','unimplemented','unloaded']){
    reset();let cat=catalog();
    if(missing==='absent')delete cat.capabilities.operations['auth.change_password'];
    else if(missing==='old')Object.assign(cat.capabilities.operations['auth.change_password'],{contract_versions:[1],reason:'fixture_password_v2_required'});
    else if(missing==='blocked')Object.assign(cat.capabilities.operations['auth.change_password'],{available:false,reason:'fixture_generation_required'});
    else if(missing==='unimplemented')Object.assign(cat.capabilities.operations['auth.change_password'],{implemented:false,reason:'fixture_password_not_implemented'});
    else cat=null;
    await mount(settings(cat));await click('Account & sign-in');
    assert.equal(field('Current password'),null,missing+' capability hides the in-app form');
    assert.equal(document.querySelector('.pw-form'),null);
    const guide=document.querySelector('.set-advanced');
    assert.ok(guide?.open,missing+' capability shows the expanded terminal guide');
    const reason=document.querySelector('[aria-label="Change Bob password in a terminal"] [role="status"]');
    assert.equal(reason?.textContent,operationReason(cat,'auth.change_password'),'shows the server reason or missing-capability explanation');
    assert.equal(guide.querySelectorAll('.set-steps > li').length,3,'all original terminal steps');
    const commands=[...guide.querySelectorAll('pre')].map(el=>el.textContent);
    assert.equal(commands.length,2,'password and rollback command blocks');
    assert.match(commands[0],/^read -s -P 'New Bob password: '/);
    assert.match(commands[0],/set -e pw1; set -e pw2$/);
    assert.match(commands[1],/^rm -f ~\/.config\/systemd\/user\/bobclaw-gateway.service.d\/20-bob-password.conf/);
    assert.match(text(),/Only your gateway reads this file/);
    assert.match(text(),/This returns the gateway to the shared password it used before/);
    assert.equal([...guide.querySelectorAll('button')].filter(el=>el.textContent==='Copy').length,2,'copy controls restored');
    assert.equal(hits.length,0,'terminal fallback makes no password or owner IPC requests');
    await unmount();
  }
  for(const identity of [{sub:'owner',token_type:'agent',owner_controls_eligible:true},{sub:'foreign',token_type:'portal',owner_controls_eligible:false},{sub:'ordinary',token_type:'human'}]){
    reset();await mount(React.createElement(SettingsView,{client,invoke:async()=>[],hello:{identity},catalog:catalog(),profiles:null,onProfiles:()=>{},onReconnect:()=>{},appearance:DEFAULT_APPEARANCE,onAppearance:()=>{},pendingApprovals:0}));
    await click('Account & sign-in');assert.equal(field('Current password'),null);assert.equal(document.querySelector('.set-steps'),null,'nonowners never see the terminal guide');assert.equal(hits.length,0);await unmount();
  }
  console.log('password UI: PASS (all outcomes, one proof, server refusals, expiry, receipt-only recovery, native/remount metadata, cleared fields/snapshots/hook state, fresh sign-in, old capability isolation, Settings form/terminal capability selection and server reasons, nonowners)');
})().catch(e=>{console.error(e);process.exit(1);});
