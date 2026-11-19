import {useEffect,useLayoutEffect,useMemo,useRef,useState,type CSSProperties} from 'react';
import type {AppSnapshot,Event as NativeEvent,SavedSession} from './functional-protocol';
import {TurnActivity} from './workbench/TurnActivity';
type SubmittedDraft={sid:string;text:string;files:StagedFile[];baseline:Set<string>;outcome?:string;accepted?:boolean};
import {ConversationMessage} from './workbench/Markdown';
import {Inspector,State,type InspectorTab} from './workbench/Inspector';
import {createWorkbenchClient,decodeWorkbench,type NativeInvoke,type WorkbenchView} from './workbench/workbench-client';
import {AttachmentPicker,type StagedFile} from './workbench/Attachments';
import {RecoveryActions} from './portal/RecoveryActions';
import {Settings,BobControls} from './workbench/Settings';
import {ProjectNavigation,CurrentProjectContext,groupName} from './workbench/ProjectContext';

export default function WorkbenchApp({invoke}:{invoke:NativeInvoke}){
 const client=useMemo(()=>createWorkbenchClient(invoke),[invoke]);
 const [view,setView]=useState<WorkbenchView|null>(null),[snapshot,setSnapshot]=useState<AppSnapshot|null>(null);
 const [input,setInput]=useState(''),[project,setProject]=useState(''),[selected,setSelected]=useState(''),[search,setSearch]=useState('');
 const [busyLabel,setBusyLabel]=useState('');
 const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[cancelReady,setCancelReady]=useState(false);
 const [archiveFilter,setArchiveFilter]=useState('active'),[settings,setSettings]=useState(false),[projectFilter,setProjectFilter]=useState(''),[inspectorWidth,setInspectorWidth]=useState<number|null>(null);
 const [inspector,setInspector]=useState(true),[navigation,setNavigation]=useState(true),[tab,setTab]=useState<InspectorTab>('Overview'),[workflowId,setWorkflowId]=useState<string|null>(null);
 const scroller=useRef<HTMLDivElement>(null),following=useRef(true),shownSession=useRef<string|null>(null);
 const [files,setFiles]=useState<StagedFile[]>([]);
 const [submitted,setSubmittedState]=useState<SubmittedDraft|null>(null),[unsentDrafts,setUnsentDrafts]=useState<SubmittedDraft[]>([]),[away,setAway]=useState(false);
 const submittedRef=useRef<SubmittedDraft|null>(null);
 function setSubmitted(next:SubmittedDraft|null){submittedRef.current=next;setSubmittedState(next)}
 function setUnsent(sent:SubmittedDraft){setUnsentDrafts(prior=>[...prior,sent])}
 const authoritative=useRef<AppSnapshot|null>(null),observedEvents=useRef<NativeEvent[]>([]),requestToken=useRef<string|null>(null);
 const outcome=useRef<{dispatched:boolean;recovered:AppSnapshot|null}>({dispatched:false,recovered:null});
 function receiveSnapshot(next:AppSnapshot){
  authoritative.current=next;observedEvents.current=next.session.events;setSnapshot(next);
  const sent=submittedRef.current;
  if(sent?.outcome&&sent.sid===next.session.id){
   if(next.session.events.some(e=>e.kind==='user.message'&&!sent.baseline.has(e.event_id)))setSubmitted(null);
   else if(!sent.accepted){setUnsent(sent);setSubmitted(null)}
  }
 }
 const [headerExpanded,setHeaderExpanded]=useState(()=>{try{return window.localStorage.getItem('bob.ui.header-expanded.v1')==='true'}catch{return false}});
 function toggleHeader(){setHeaderExpanded(prior=>{const next=!prior;try{window.localStorage.setItem('bob.ui.header-expanded.v1',String(next))}catch{/* optional UI preference only */}return next})}
 const [groupFilter,setGroupFilter]=useState('');
 const drafts=useRef(new Map<string,{text:string;files:StagedFile[]}>()),activeDraft=useRef('');
 const modelsRequested=useRef(false);
 const draftRevision=useRef(0),composing=useRef(false);
 const epoch=useRef(0),operation=useRef(false),compatibility=useRef(true),booted=useRef(false);
 const session=snapshot?.session,runtime=view?.runtime,profile=runtime?.profiles.find(p=>p.id===runtime.active_profile_id);
 const bobViewer=profile?.id==='bob_team';
 const locked=busy||!!session?.pending||!!submitted?.outcome;
 useLayoutEffect(()=>{
  const sid=session?.id??'';
  if(activeDraft.current===sid)return;
  activeDraft.current=sid;draftRevision.current++;
  const draft=drafts.current.get(sid);setInput(draft?.text??'');setFiles(draft?.files??[]);
 },[session?.id]);
 function changeFiles(next:StagedFile[],sid:string){
  const prior=drafts.current.get(sid);drafts.current.set(sid,{text:prior?.text??'',files:next});
  if(activeDraft.current===sid){draftRevision.current++;setFiles(next)}
 }

 useLayoutEffect(()=>{
  if(session?.id!==shownSession.current){following.current=true;shownSession.current=session?.id??null}
  if(following.current&&scroller.current)scroller.current.scrollTop=scroller.current.scrollHeight;
 },[session?.id,session?.messages.length,session?.pending?.id]);
 function showLatest(){following.current=true;setAway(false);if(scroller.current)scroller.current.scrollTop=scroller.current.scrollHeight}
 function applyView(next:WorkbenchView){
  compatibility.current=next.compatibility;setView(next);
  if(next.app)receiveSnapshot(next.app);
  else {const o=next.runtime?.local_observation;if(o&&o.session_id===authoritative.current?.session.id&&o.request_id===requestToken.current)observedEvents.current=o.events}
 }
 useEffect(()=>{
  let active=true,observing=false;
  async function poll(){
   if(observing||(operation.current&&compatibility.current))return;
   observing=true;const version=epoch.current;
   try{let next=await client.snapshot();
    // A cold extended launch has no native local session yet: workbench_snapshot
    // is a projection and honestly reports app:null. Establish the session once
    // through the same app_boot authority the compatibility entry uses. Never
    // boot during an in-flight operation and never twice per mount.
    if(active&&version===epoch.current&&!next.app&&!next.compatibility&&!booted.current&&!operation.current){
     booted.current=true;
     try{next={...next,app:await invoke<AppSnapshot>('app_boot')}}catch(e){if(active&&version===epoch.current)setError(String(e))}
    }
    if(active&&version===epoch.current)applyView(next)}
   catch(e){if(active&&version===epoch.current)setError(String(e))}
   finally{observing=false}
  }
  void poll();const timer=window.setInterval(()=>void poll(),1500);
  return()=>{active=false;window.clearInterval(timer)};
 },[client]);
 useEffect(()=>{
  // The panel opens from the toolbar, so focus may sit outside its section.
  // Escape must close Settings wherever focus is while it is open.
  if(!settings)return;
  const close=(event:KeyboardEvent)=>{if(event.key==='Escape')setSettings(false)};
  window.addEventListener('keydown',close);
  return()=>window.removeEventListener('keydown',close);
 },[settings]);
 useEffect(()=>{
  if(modelsRequested.current||!session||busy||!view?.runtime||!('models' in view.runtime)||profile?.id!=='local_bob_model')return;
  modelsRequested.current=true;void workbenchAction('workbench_refresh_models',{});
 },[session?.id,busy,profile?.id]);
 async function refresh(){const version=epoch.current;try{const next=await client.snapshot();if(version===epoch.current){applyView(next);setError('')}}catch(e){if(version===epoch.current)setError(String(e))}}
 async function run(command:string,args?:Record<string,unknown>){
  if(operation.current)return false;
  operation.current=true;epoch.current++;outcome.current={dispatched:false,recovered:null};setBusy(true);setBusyLabel(command==='app_send'||command==='app_resolve'?'Request active — observed activity in conversation':'Updating native local session…');setCancelReady(false);setError('');setNotice('');let token:string|undefined;
  try{
   if(command==='app_send'||command==='app_resolve'){token=await invoke<string>('app_begin');requestToken.current=token;setCancelReady(true)}
   outcome.current.dispatched=true;
   const result=await invoke<AppSnapshot>(command,token?{...args,token}:args);receiveSnapshot(result);return true;
  }catch(e){setError(String(e));try{const recovered=await invoke<AppSnapshot>('app_boot');outcome.current.recovered=recovered;receiveSnapshot(recovered)}catch{/* keep prior session, never retry effects */}return false}
  finally{if(token)try{await invoke('app_release',{token})}catch(e){setError(`Request release failed: ${String(e)}`)}epoch.current++;operation.current=false;requestToken.current=null;setBusy(false);setCancelReady(false)}
 }
 async function workbenchAction(command:string,args:Record<string,unknown>){
  if(operation.current)return false;
  operation.current=true;epoch.current++;setBusy(true);setBusyLabel(command.includes('_bob_')?'Waiting for Bob runtime observation; outcome not confirmed…':'Updating native workbench settings…');setError('');setNotice('');
  try{applyView(decodeWorkbench(await invoke(command,args)));setNotice('Native workbench state updated.');return true}
  catch(e){setError(String(e));return false}
  finally{epoch.current++;operation.current=false;setBusy(false)}
 }
 async function cancel(){try{setNotice(await invoke<string>('app_cancel'));if(!busy)receiveSnapshot(await invoke<AppSnapshot>('app_boot'))}catch(e){setError(String(e))}}
 async function submitDraft(){
  if(!input.trim()||locked||!session||chatUnavailable||operation.current)return;
  if(files.some(f=>f.image)){setError('Current backend has no vision; image not sent/understood. Remove staged images to send.');return;}
  const text=input,sid=session.id;
  const sent:SubmittedDraft={sid,text,files,baseline:new Set((session.events??[]).map(e=>e.event_id))};
  const args=files.length?{input:text,sessionId:sid,attachments:files.map(({name,mime,bytes})=>({name,mime,bytes}))}:{input:text};
  const revision=++draftRevision.current;
  drafts.current.set(sid,{text:'',files:[]});setInput('');setFiles([]);setSubmitted(sent);
  const ok=await run('app_send',args);
  const accepted=observedEvents.current.some(e=>e.kind==='user.message'&&!sent.baseline.has(e.event_id));
  if(ok){setSubmitted(null);return;}
  if(accepted){
   const present=authoritative.current?.session.id===sid&&authoritative.current.session.events.some(e=>e.kind==='user.message'&&!sent.baseline.has(e.event_id));
   setSubmitted(present?null:{...sent,accepted:true,outcome:'Accepted by native app — request failed; awaiting history refresh.'});return;
  }
  const recovered=outcome.current.recovered;
  if(outcome.current.dispatched&&(!recovered||recovered.session.id!==sid)){
   setSubmitted({...sent,outcome:'Submission outcome unknown — inspect native history before retrying.'});return;
  }
  setSubmitted(null);
  if(activeDraft.current===sid&&revision===draftRevision.current){drafts.current.set(sid,{text,files:sent.files});setInput(text);setFiles(sent.files)}
  else setUnsent(sent);
 }
 function restoreUnsent(unsent:SubmittedDraft){if(unsent.sid!==session?.id||input||files.length)return;draftRevision.current++;drafts.current.set(unsent.sid,{text:unsent.text,files:unsent.files});setInput(unsent.text);setFiles(unsent.files);setUnsentDrafts(prior=>prior.filter(d=>d!==unsent))}
 const liveObservation=runtime?.local_observation;
 const events=busy&&liveObservation?.session_id===session?.id&&liveObservation?.request_id===requestToken.current?liveObservation.events:session?.events??[];
 const acceptedSubmission=submitted&&submitted.sid===session?.id&&events.some(e=>e.kind==='user.message'&&!submitted.baseline.has(e.event_id));
 const inTranscript=submitted&&submitted.sid===session?.id&&session?.events.some(e=>e.kind==='user.message'&&!submitted.baseline.has(e.event_id));
 useLayoutEffect(()=>{if(following.current&&scroller.current)scroller.current.scrollTop=scroller.current.scrollHeight},[events.length,submitted,busy,error,notice]);
 const modelChoices=runtime?.models??[],nextModel=snapshot?.selected_model_id??'bob-ds-flash';
 const lastReceipt=session?.receipts[session.receipts.length-1] as {model?:unknown;requested_model?:unknown;bob?:{model_pin?:unknown}}|undefined;
 const title=snapshot?.saved.find(s=>s.id===session?.id)?.title??'New conversation';
 const namedProjects=snapshot?.organizational_projects??[];
 const saved=snapshot?.saved.filter(s=>(archiveFilter==='all'||(archiveFilter==='archived'?s.archived:!s.archived))&&(!projectFilter||(projectFilter===':chat'?s.project===null:s.project===projectFilter))&&(!groupFilter||(groupFilter===':unfiled'?!s.group_id:s.group_id===groupFilter))&&`${s.title} ${s.project??''} ${groupName(namedProjects,s.group_id)} ${namedProjects.find(p=>p.id===s.group_id)?.description??''}`.toLowerCase().includes(search.toLowerCase())).sort((a,b)=>Number(!!b.pinned)-Number(!!a.pinned))??[];
 const projects=[...new Set(snapshot?.saved.flatMap(s=>s.project?[s.project]:[])??[])];
 const chatUnavailable=profile&&(!profile.capabilities.chat||!profile.configured);
 return <main style={{'--inspector-width':inspectorWidth?`min(${inspectorWidth}px, 38vw)`:undefined} as CSSProperties} className={`workbench ${!navigation?'navigation-hidden':''} ${!inspector?'inspector-hidden':''}`}>
  <div className="app-toolbar"><div><button aria-expanded={navigation} onClick={()=>setNavigation(!navigation)}>{navigation?'Hide navigation':'Show navigation'}</button><span className="app-name">BoB <span>WORKBENCH</span></span></div><div><button aria-expanded={settings} onClick={()=>setSettings(!settings)}>Settings</button><button disabled={busy} onClick={()=>void refresh()}>Refresh runtime</button>{!inspector&&<button onClick={()=>setInspector(true)}>Show inspector</button>}</div></div>
  {navigation&&<nav aria-label="Project and session navigation" className="sidebar">
   <button className="new-session" disabled={locked||!snapshot||!!(bobViewer&&groupFilter&&groupFilter!==':unfiled')} onClick={()=>{if(groupFilter&&groupFilter!==':unfiled')void workbenchAction('workbench_new_project_session',{groupId:groupFilter});else void run('app_new')}}>New session</button>
   <label htmlFor="session-search">Search conversations</label><input id="session-search" type="search" value={search} onChange={e=>setSearch(e.target.value)} placeholder="Title or project…"/>
   {snapshot?.organizational_projects&&<ProjectNavigation app={snapshot} filter={groupFilter} onFilter={setGroupFilter} search={search} locked={locked||bobViewer} onAction={workbenchAction}/>}
   <label htmlFor="project-filter">Browse project conversations</label><select id="project-filter" value={projectFilter} onChange={e=>setProjectFilter(e.target.value)}><option value="">All projects & chat</option><option value=":chat">Chat only</option>{projects.map(path=><option key={path} value={path}>{path}</option>)}</select>
   <label htmlFor="archive-filter">Conversation visibility</label><select id="archive-filter" value={archiveFilter} onChange={e=>setArchiveFilter(e.target.value)}><option value="active">Active conversations</option><option value="archived">Archived conversations</option><option value="all">All conversations</option></select>
   <section aria-label="Sessions"><h2>Conversations <small>{snapshot?.saved.length??0}</small></h2><div className="session-list">{[null,...namedProjects.map(p=>p.id)].map(id=>{const items=saved.filter(s=>(s.group_id??null)===id);if(!items.length)return null;const group=namedProjects.find(p=>p.id===id);return <section aria-label={`Conversations in ${group?.name??'Unfiled'}`} key={id??':unfiled'}><h3>{group?.name??'Unfiled'} <small>{items.length}{group?.collapsed&&!search?' · collapsed':''}</small></h3>{(!group?.collapsed||!!search)&&items.map(s=><SessionRow key={s.id} item={s} selected={session?.id===s.id} locked={locked} onOpen={()=>{setSelected(s.id);setWorkflowId(null);void run('app_open',{id:s.id})}} onAction={workbenchAction}/>)}</section>})}{!saved.length&&<p className="subtle">{search?'No matching conversations.':'Your conversations appear here after native save.'}</p>}</div>
   <details><summary>Open by saved selection</summary><label htmlFor="saved-session">Saved session</label><select id="saved-session" value={selected} onChange={e=>setSelected(e.target.value)} disabled={locked}><option value="">Choose a saved conversation</option>{snapshot?.saved.map(s=><option disabled={!!s.error} key={s.id} value={s.id}>{s.title}</option>)}</select><button disabled={locked||!selected} onClick={()=>void run('app_open',{id:selected})}>Open saved session</button></details></section>
   <section aria-label="Tasks"><h2>Tasks & workflows</h2>{runtime?.workflows.length?runtime.workflows.map(w=><button className="task-link" key={w.id} onClick={()=>{setWorkflowId(w.id);setTab('Workflow');setInspector(true)}}>{w.title}<State value={w.state}/></button>):<p className="subtle">No workflow observations yet.</p>}</section>
   <section aria-label="Project attachment" className="project-controls"><h2>Project context</h2><p>Attached project: <code>{session?.project?.root??'None — chat only'}</code></p><label htmlFor="project-path">Project directory</label><input id="project-path" value={project} onChange={e=>setProject(e.target.value)} placeholder="/home/you/Projects/my-project" disabled={locked||!!session?.project||chatUnavailable}/><button disabled={locked||!project.trim()||!!session?.project||chatUnavailable} onClick={()=>void run('app_attach',{path:project})}>Attach project</button><small>Enter an absolute directory. Native folder chooser unavailable. One project per session; each write needs its own exact diff decision.</small></section>
  </nav>}
  <section className="workspace" aria-label="Conversation workspace"><header className={`workspace-header ${headerExpanded?'expanded':'compact'}`}><div className="header-summary"><h1>{bobViewer?'Bob runtime':title}</h1><p>{profile?.label??'Local agent'} · {profile?.id==='local_bob_model'?nextModel:profile?.model_label??'Model unknown'} · {session?.project?.root??'Chat only'} · {groupName(namedProjects,snapshot?.group_id??null)}</p><button type="button" aria-expanded={headerExpanded} aria-controls="conversation-header-details" onClick={toggleHeader}>{headerExpanded?'Collapse conversation header':'Expand conversation header'}</button></div><div id="conversation-header-details" hidden={!headerExpanded}><p className="eyebrow">{profile?.label??'LOCAL AGENT'} · CONVERSATION</p><div className="identity-badges"><State value={view?.connection??'unknown'}/><span>{profile?.id==='local_bob_model'?`Requested next model: ${nextModel}`:`Model: ${profile?(profile.model_label??'unknown'):'Bob / DeepSeek Flash'}`}</span><span>Host: {profile?.execution_host??'local app'}</span><span>Effects: {profile?.effect_owner??'cleanroom_local'}</span></div><div className="chat-model-picker"><label htmlFor="chat-model">Model for next turn</label><select id="chat-model" value={nextModel} disabled={locked||profile?.id!=='local_bob_model'||!modelChoices.length} onChange={e=>void workbenchAction('workbench_select_model',{modelId:e.target.value})}>
   {!modelChoices.some(m=>m.id===nextModel)&&<option value={nextModel} disabled>{nextModel} · catalog not loaded / unavailable</option>}
   {modelChoices.map(m=><option key={m.id} value={m.id} disabled={!m.configured}>{m.label} · {m.configured?'configured, reachability unverified':'unavailable'} · Text only</option>)}
   </select><button disabled={locked||profile?.id!=='local_bob_model'} onClick={()=>void workbenchAction('workbench_refresh_models',{})}>Refresh model choices</button><small>{profile?.id==='local_bob_model'?'Next turn only. Text only; no image delivery. Local tools and approval owner unchanged.':'This runtime uses its own configured model; chat pinning is available on local Bob-text inference only.'}</small></div>
   {lastReceipt&&<p className="model-receipt">Last returned: {String(lastReceipt.model??'not reported')} · pin {String(lastReceipt.bob?.model_pin??'not reported')} · requested {String(lastReceipt.requested_model??'not recorded in legacy receipt')}</p>}
   {snapshot?.organizational_projects&&<CurrentProjectContext app={snapshot} locked={locked||bobViewer} bobOwned={!!bobViewer} onAction={workbenchAction}/>}
   </div></header>
   <div ref={scroller} className="conversation-scroll" onScroll={e=>{const el=e.currentTarget;following.current=el.scrollHeight-el.scrollTop-el.clientHeight<80;setAway(!following.current)}}><BobControls view={view} busy={locked} onAction={workbenchAction}/><section aria-label="Conversation" className="conversation">{bobViewer?<p className="subtle">Your local conversation remains saved under local engine authority. Switch back to a local model profile to continue it. Bob run observations appear in Agents, Workflow and Outputs.</p>:<>{!session?.messages.length&&!submitted&&<div className="empty-state"><span className="eyebrow">A CLEAR PLACE TO WORK</span><h2>What are we working on?</h2><p>Start a conversation, or attach a project for bounded file tools. Nothing runs until you send a message.</p><p className="subtle">Local execution. Explicit write approvals. Durable conversations.</p></div>}{session?.messages.map((m,i)=><ConversationMessage key={`${session.id}:${i}`} role={m.role} content={m.content} provenance={snapshot?.message_provenance?.[i]}/>)}{submitted&&submitted.sid===session?.id&&!inTranscript&&<article className="message user" aria-label="Submitted message"><h2>You · {submitted.outcome??(acceptedSubmission?'Accepted by native app':'Submitting — awaiting native acceptance')}</h2><p>{submitted.text}</p>{submitted.files.length>0&&<small>{submitted.files.length} submitted attachments</small>}</article>}<TurnActivity events={events} busy={busy} preparing={!!submitted&&!acceptedSubmission} pending={!!session?.pending} error={error} notice={notice}/></>}</section>
   {runtime?.workflows.length? <section className="progress-strip" aria-label="Task progress">{runtime.workflows.map(w=><button key={w.id} onClick={()=>{setWorkflowId(w.id);setTab('Workflow');setInspector(true)}}>{w.title} <State value={w.state}/></button>)}</section>:null}
   {session?.pending&&<section aria-label="File write approval" className="write-approval"><h2>Review proposed file write</h2><p>Target: <code>{session.pending.path}</code></p><details><summary>Approval identity & scope</summary><p>Approval <code>{session.pending.id}</code> · digest <code>{session.pending.digest}</code></p><p>Full replacement diff; old/new byte counts and missing final newlines are explicit. Expires after 15 minutes. Stale files need a new proposal.</p></details><pre aria-label="Exact file diff" className="diff">{session.pending.diff}</pre><div className="approval-actions"><button className="primary" disabled={busy} onClick={()=>void run('app_resolve',{id:session.pending!.id,digest:session.pending!.digest,allow:true})}>Allow this write once</button><button disabled={busy} onClick={()=>void run('app_resolve',{id:session.pending!.id,digest:session.pending!.digest,allow:false})}>Deny this write</button></div></section>}</div>
   <div className="composer-area">{away&&<button type="button" className="review-jump" onClick={showLatest}>Return to latest activity</button>}{unsentDrafts.some(d=>d.sid===session?.id)&&<section aria-label="Unsent draft recovery">{unsentDrafts.filter(d=>d.sid===session?.id).map((unsent,i)=><div key={i}><p>Unsent draft retained: {unsent.text} · {unsent.files.length} attachments</p><button type="button" disabled={!!input||!!files.length} onClick={()=>restoreUnsent(unsent)}>Restore unsent draft</button></div>)}<small>Clear the current draft first. Never automatically retried.</small></section>}<p role="status" aria-live="polite" className="turn-status">{busy?busyLabel:session?.pending?'Waiting for your explicit file-write decision':session?'Ready':'Connecting to native app…'}</p>{session?.pending&&<button type="button" className="review-jump" onClick={showLatest}>Review pending write</button>}{notice&&<p role="status" className="notice">{notice}</p>}{error&&<><p role="alert">{error}</p>{!snapshot&&<RecoveryActions invoke={invoke}/>}</>}<form onSubmit={e=>{e.preventDefault();void submitDraft()}}><AttachmentPicker files={files} sessionId={session?.id??''} onFiles={changeFiles} locked={locked||!session||!!chatUnavailable} onError={setError}/><label htmlFor="composer">Message</label><textarea id="composer" value={input} onChange={e=>{draftRevision.current++;setInput(e.target.value);if(session)drafts.current.set(session.id,{text:e.target.value,files})}} onCompositionStart={()=>{composing.current=true}} onCompositionEnd={()=>{composing.current=false}} onKeyDown={e=>{
 if(e.key!=='Enter'||e.shiftKey||e.nativeEvent.isComposing||composing.current||e.keyCode===229)return;
 e.preventDefault();if(!e.repeat)void submitDraft();
}} disabled={!session||chatUnavailable} rows={3} placeholder="Ask, plan, or work with this project…"/><div className="composer-actions"><small>{chatUnavailable?'This profile is a read-only runtime viewer, not local chat.':'Enter to send · Shift+Enter for newline. Every write is reviewed.'}</small><button type="button" onClick={()=>void cancel()} disabled={!session||profile?.id==='bob_team'||(busy&&!cancelReady)||!!profile&&!profile.capabilities.cancel}>Cancel current turn</button><button className="primary" disabled={locked||!input.trim()||!session||chatUnavailable||files.some(f=>f.image)}>Send message</button></div></form></div>
  </section>
  {settings&&<Settings error={error} notice={notice} inspectorWidth={inspectorWidth} onInspectorWidth={setInspectorWidth} view={view} app={snapshot} busy={busy} onAction={workbenchAction} onClose={()=>setSettings(false)}/>}
  {inspector&&<Inspector view={view} app={snapshot} tab={tab} onTab={next=>{setTab(next);setWorkflowId(null)}} onClose={()=>setInspector(false)} workflowId={workflowId}/>}
 </main>
}

function SessionRow({item:s,selected,locked,onOpen,onAction}:{item:SavedSession;selected:boolean;locked:boolean;onOpen:()=>void;onAction:(cmd:string,args:Record<string,unknown>)=>Promise<boolean>}){
 const [editing,setEditing]=useState(false),[title,setTitle]=useState(s.title);
 return <div role="group" aria-label={`Conversation: ${s.title}`} className={selected?'selected-session':''}>
  <button disabled={locked||!!s.error} title={s.error??s.project??'Chat only'} aria-current={selected?'page':undefined} onClick={onOpen}>{s.pinned?'★ ':''}{s.title}</button>
  <small>{s.project??'Chat only'}{s.error?' · unavailable':''}{s.archived?' · archived':''}</small>
  {editing?<form onSubmit={e=>{e.preventDefault();if(title.trim())void onAction('workbench_rename_session',{sessionId:s.id,title}).then(ok=>{if(ok)setEditing(false)})}}><label>Conversation title<input value={title} maxLength={160} onChange={e=>setTitle(e.target.value)} disabled={locked}/></label><button disabled={locked||!title.trim()}>Save title</button><button type="button" onClick={()=>setEditing(false)}>Cancel rename</button></form>:<div className="session-actions"><button disabled={locked||!!s.error} onClick={()=>{setTitle(s.title);setEditing(true)}}>Rename</button><button disabled={locked||!!s.error} onClick={()=>void onAction('workbench_organize_session',{sessionId:s.id,pinned:!s.pinned,archived:!!s.archived})}>{s.pinned?'Unpin':'Pin'}</button><button disabled={locked||!!s.error} onClick={()=>void onAction('workbench_organize_session',{sessionId:s.id,pinned:!!s.pinned,archived:!s.archived})}>{s.archived?'Restore':'Archive'}</button></div>}
 </div>
}
