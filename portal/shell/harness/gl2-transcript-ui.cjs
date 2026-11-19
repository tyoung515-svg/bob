// Inject the observed stale copy into its conversation record, leaving the real
// PortalApp transcript, server reads, approval cards and finish controls mounted.
const assert=require('node:assert/strict'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost'});
for(const key of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage'])global[key]=dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT=true;
const eventModule=require.resolve('@tauri-apps/api/event');
require.cache[eventModule]={id:eventModule,filename:eventModule,loaded:true,exports:{listen:async()=>()=>{}}};
const React=require('react'),{act}=React,{createRoot}=require('react-dom/client');
const {turnReducer}=require('../src/portal/portal-state.ts');
const {hasTurnRow,pendingReadbackRows}=require('../src/portal/transcript-copies.ts');
const answer='Changed the page. Try index.html.';
const block='```bob-builder-approval\n'+JSON.stringify({schema_version:1,conversation_id:'c',approval_id:'a'})+'\n```';
const content=answer+'\n'+block;
const user={id:'u',role:'user',content:'Make a page',created_at:'2026-11-16T00:00:00Z'};
const row={id:'server-answer',role:'assistant',content,created_at:'2026-11-16T00:00:01Z',metadata:{request_id:'r'}};
const record={schema_version:1,conversation_id:'c',approval_id:'a',kind:'write',summary:'fs_write',detail:{path:'index.html'},mode:'auto',state:'allowed',expires_at:Date.now()/1000+300,execution_state:'completed',result:{status:'completed',exit_code:0}};
let held='terminal', requestId='r', browses=0;
const conversationState=require('../src/portal/conversation-state.ts');
const original=conversationState.useConversationStates;
conversationState.useConversationStates=function(){const state=original();state.scope('c').turnRef.current={pending:null,stream:held==='live'?{request_id:requestId,conversation_id:'c',content,done:false,stopped:null,error:null}:null,terminal:held!=='live'?{kind:held==='stopped'?'stopped':'complete',conversation_id:'c',request_id:requestId,message_id:'server-answer',partial:content,in_transcript:false,line:held==='stopped'?'Stopped — nothing will be replayed.':'complete',code:null,detail:null}:null};return state;};
const PortalApp=require('../src/portal/PortalApp.tsx').default;
const conversation={id:'c',face_id:'builder',project_id:'p',title:'GL2',created_at:'2026-11-16T00:00:00Z'};
const catalog={faces:[{id:'builder',name:'Builder',allowed:true}],backends:[],models:[],capabilities:{warnings:[],actions:[]}};
const hello={identity:{sub:'fixture',gateway:'fixture',faces:['builder'],may_touch:[],may_not_touch:[],expires_at:'2099-01-01'},server:{version:'test',boot:'test'},gaps:[]};
const invoke=async(cmd)=>{
  switch(cmd){case 'portal_connection_get':return {connection_configured:true};case 'portal_hello':return hello;case 'portal_catalog':return catalog;
  case 'portal_conversations':return {items:[conversation],limit:50,offset:0};case 'portal_conversation_open':return {conversation,messages:[user,row],has_more:false};
  case 'portal_pins':return {face_id:null,backend:null,model:null,profile:null,locale:null};
  case 'portal_builder_mode_get_v2':return {conversation_id:'c',project_id:'p',mode:'auto',revision:1,expires_at:null,network:{allowlist:[],project_extra:[]},approval_records:[record]};
  case 'portal_workspace_browse_v2':browses++;return {project_id:'p',path:'',root_label:'Project',truncated:false,entries:[{name:'index.html',kind:'file',size:1,mtime:1}]};
  case 'portal_builder_approval_get_v2':return record;case 'store_load':return '[]';default:throw JSON.stringify({code:'bad_request',detail:'fixture '+cmd,hint:''});}
};
const root=createRoot(document.getElementById('root'));
async function flush(fn=()=>{}){await act(async()=>{await fn();await new Promise(r=>setTimeout(r,0));});}
(async()=>{
  assert.equal(hasTurnRow([user,row],'c','r','server-answer',content,null),true);
  assert.equal(hasTurnRow([user,row],'c','r','different-completion',content,null),true);
  assert.equal(hasTurnRow([user,{...row,metadata:{}}],'c','other','other',content,null),false,'identical older answers are not enough');
  assert.equal(pendingReadbackRows([user,{...row,metadata:{history_readback:'pending'}}]),true);
  assert.equal(pendingReadbackRows([user,row]),false);
  await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));
  const session=document.querySelector('.conversation-list button');assert.ok(session);await flush(()=>session.click());
  assert.equal(document.querySelectorAll('.messages .msg.assistant, .transcript .msg.assistant').length||document.querySelectorAll('.msg.assistant').length,1);
  assert.equal(document.querySelectorAll('[aria-label="Builder approval"]').length,1,'one set of cards even with terminal chunks held');
  assert.equal(document.querySelectorAll('.builder-finish').length,1,'one finish strip at the final answer');
  assert.equal(document.querySelector('.terminal.complete'),null);
  assert.equal(browses,1,'GL4: opening a Builder session loads files once without the Workspace panel');
  assert.equal(document.querySelector('.workspace-file-link').textContent,'index.html');
  assert.ok(!document.querySelector('.composer').textContent.includes('not readable from history yet'));
  held='live';await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));
  assert.equal(document.querySelector('.msg.streaming'),null,'stored row wins over a held live copy');
  assert.equal(document.querySelectorAll('[aria-label="Builder approval"]').length,1);
  assert.equal(document.querySelectorAll('.builder-finish').length,0,'no finish while a turn is live');
  assert.equal(browses,1,'GL4: a live turn does not refresh the listing');
  held='stopped';await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));
  assert.equal(document.querySelectorAll('.msg.assistant .md').length,1,'stored body appears once after a stopped turn');
  assert.equal(document.querySelectorAll('[aria-label="Builder approval"]').length,1);
  assert.match(document.querySelector('.terminal.stopped .typed-refusal').textContent,/nothing will be replayed/);
  assert.equal(browses,1,'GL4: a stopped turn does not refresh');
  held='terminal';requestId='next-request';
  await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));
  assert.equal(browses,2,'GL4: a completed turn refreshes once');
  await flush(()=>root.render(React.createElement(PortalApp,{invoke,profiles:null,onProfiles:()=>{}})));
  assert.equal(browses,2,'GL4: held terminal and stored transcript do not double-refresh');
  console.log('GL2 transcript: PASS (completed stored row + held terminal/live chunks render one answer and card set; one finish strip; pending readback only while absent)');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await flush(()=>root.unmount());conversationState.useConversationStates=original;});
