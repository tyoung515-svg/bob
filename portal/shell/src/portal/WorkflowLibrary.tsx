import { useContext, useEffect, useRef, useState } from 'react';
import { WorkflowContext, WorkflowProposalCard, useWorkflows, orderedStages } from './WorkflowViews';
import { WorkflowEditor } from './WorkflowEditor';
import { useExperience } from './experience';
import type { WorkflowDefinition, WorkflowPropose, WorkflowRecord, WorkflowStage } from './workflow-types';
import type { Sketch } from './library';
import { authoringErrors, definitionSketch, faceLabel, stageLabel, workflowDrafts } from './workflow-library';
import { executionRefused, mutationDone, mutationKey, pendingMutation } from './durable-runs';
import { isDefiniteRefusal, toPortalError } from './portal-protocol';

function releaseRefusal(store: NonNullable<React.ContextType<typeof WorkflowContext>>['store'], operation:string, error:unknown) {
  const typed=toPortalError(error);
  if(executionRefused(typed.code)||isDefiniteRefusal(typed)||['revision_conflict','unsupported_fields','invalid_definition','validation_error','invalid_args'].includes(typed.code))mutationDone(store.scope,operation);
}

export type WorkflowChoices = Pick<WorkflowPropose,'bindings'|'project_id'|'workspace'|'run_mode'>;
export function WorkflowRunForm({record,initialInputs={},initialChoices,onPrepare,onClose}: {record:WorkflowRecord;initialInputs?:Record<string,unknown>;initialChoices?:WorkflowChoices;onPrepare?:(choices:WorkflowChoices,inputs:Record<string,unknown>)=>void;onClose:()=>void}) {
  const {store}=useContext(WorkflowContext)!;useWorkflows(store);
  const simple=useExperience()==='simple',lock=useRef(false);
  const [inputs,setInputs]=useState<Record<string,unknown>>(initialInputs),[bindings,setBindings]=useState<Record<string,string>>(initialChoices?.bindings ?? {}),[workspace,setWorkspace]=useState(initialChoices?.project_id ?? 'new'),[name,setName]=useState(initialChoices?.workspace?.create ?? ''),[mode,setMode]=useState<'auto'|'approve'>(initialChoices?.run_mode ?? 'auto'),[busy,setBusy]=useState(false),[error,setError]=useState(''),[proposalId,setProposalId]=useState<string|null>(null);
  const [workspaces,setWorkspaces]=useState<{id:string;name:string;root:string}[]>([]),[workspaceError,setWorkspaceError]=useState('');
  const operation=`workflow-propose:${record.id}:${record.revision}`;
  const saved=onPrepare ? null : pendingMutation<Omit<WorkflowPropose,'request_key'>>(store.scope,operation);
  useEffect(()=>{let alive=true;void store.loadCatalog().catch(e=>{if(alive)setError(authoringErrors(e).map(e=>e.message).join(' '));});
    void store.client.projects().then(async page=>{
      const results=await Promise.allSettled(page.items.map(async project=>{const result=await store.client.workspaceBrowse(project.id);return {id:project.id,name:project.name,root:result.root_label};}));
      if(alive) {setWorkspaces(results.flatMap(r=>r.status==='fulfilled' ? [r.value] : []));if(results.some(r=>r.status==='rejected'))setWorkspaceError('Some projects have no readable registered workspace. Choose another workspace or name a new one.');}
    }).catch(()=>{if(alive)setWorkspaceError('Existing workspaces could not be read. You can refresh or name a new workspace.');});
    return()=>{alive=false;};
  },[store]);
  async function propose() {
    if(lock.current)return;setError('');
    const declared=(record.definition.inputs ?? {}) as Record<string,{type:string;required:boolean}>;
    if(!saved){
      if(!String(inputs.task ?? '').trim()){setError('Describe the task before requesting a proposal.');return;}
      if(!declared.task){setError('This definition needs a task input before it can receive this task. Edit the definition first.');return;}
      for(const [alias,agent] of Object.entries(record.definition.agents)) {
        const selected=agent.binding_id ?? bindings[alias], binding=store.catalog?.bindings.find(b=>b.binding_id===selected);
        if(!selected){setError(`Choose a model for ${alias.replaceAll('_',' ')} (${faceLabel(agent.face_id)}).`);return;}
        if(!binding || !binding.face_ids.includes(agent.face_id) || !binding.runnable){setError(`The model for ${alias.replaceAll('_',' ')} is unavailable for ${faceLabel(agent.face_id)}. ${binding?.reasons.join('. ') ?? 'Choose an enrolled model.'}`);return;}
        const writes = (stages:WorkflowStage[]):boolean => stages.some(s=>s.agent===alias && s.access==='write' || s.kind==='repair' && (s.build?.agent===alias || s.fix?.agent===alias) || s.body && writes(s.body));
        if(writes(record.definition.stages) && !binding.tool_capable){setError(`The model for ${alias.replaceAll('_',' ')} cannot make workspace changes. Choose a model with workspace tools.`);return;}
      }
      if(workspace==='new' && !name.trim()){setError('Name the workspace to create when you press Start.');return;}
      for(const [key,spec] of Object.entries(declared)) if(spec.required && (!inputs[key] || Array.isArray(inputs[key]) && !(inputs[key] as unknown[]).length)){setError(`Please supply ${key.replaceAll('_',' ')}.`);return;}
    }
    const choices:WorkflowChoices={bindings:Object.fromEntries(Object.entries(bindings).filter(([alias])=>!record.definition.agents[alias]?.binding_id)),project_id:workspace==='new' ? null : workspace,run_mode:mode,...(workspace==='new' ? {workspace:{create:name.trim()}} : {})};
    if(onPrepare){onPrepare(choices,inputs);return;}
    lock.current=true;setBusy(true);
    try {
      const payload=saved ?? {definition_ref:{id:record.id,revision:record.revision},inputs:Object.fromEntries(Object.entries(inputs).filter(([key,value])=>key in declared && value !== '' && value !== undefined)),...choices,origin_conversation_id:null,expires_at:new Date(Date.now()+3600_000).toISOString()};
      const {proposal}=await store.client.workflowPropose({...payload,request_key:mutationKey(store.scope,operation,payload)});mutationDone(store.scope,operation);
      store.proposals.set(proposal.id,{proposal,source:{conversationId:null,name:'you',revision:proposal.revision,digest:proposal.digest}});store.changed();setProposalId(proposal.id);
    } catch(e) {releaseRefusal(store,operation,e);setError(authoringErrors(e).map(e=>e.message).join(' '));}
    finally {lock.current=false;setBusy(false);}
  }
  if(proposalId){const entry=store.proposals.get(proposalId);return <div><p>Review this proposal. Work begins only when you press Start.</p>{entry && <WorkflowProposalCard id={proposalId} source={entry.source} />}<button onClick={onClose}>Close run form</button></div>;}
  const unavailable=store.catalog && !store.catalog.available;
  return <form className="lib-form workflow-run-form" aria-label="Run this workflow" onSubmit={e=>{e.preventDefault();void propose();}}><h3>{onPrepare ? 'Choose how to run' : 'Run'} {record.definition.name}</h3><p>{onPrepare ? 'Save the workspace and model choices for this reminder. This creates no proposal and starts no work.' : 'Review a proposal before starting. The saved version shown here will be used.'}</p>
    {unavailable && <p role="status">Workflows are unavailable right now. {store.catalog?.reasons.map(r=>r.replaceAll('_',' ')).join('. ')}.</p>}
    <fieldset disabled={busy || !!saved}><label>Task<textarea aria-label="Workflow task" required value={String(inputs.task ?? '')} onChange={e=>setInputs({...inputs,task:e.target.value})} /></label>
      {'acceptance' in ((record.definition.inputs ?? {}) as object) && <label>Acceptance notes (optional)<textarea aria-label="Acceptance notes" value={String(inputs.acceptance ?? '')} onChange={e=>setInputs({...inputs,acceptance:e.target.value})} /></label>}
      {Object.entries((record.definition.inputs ?? {}) as Record<string,{type:string;required:boolean}>).filter(([key])=>key!=='task'&&key!=='acceptance').map(([key,spec])=><label key={key}>{spec.type==='evidence_ids' ? 'Earlier messages to include as evidence' : key.replaceAll('_',' ')}{spec.required ? ' (required)' : ' (optional)'}<textarea value={spec.type==='evidence_ids' ? (inputs[key] as string[] ?? []).join('\n') : String(inputs[key] ?? '')} onChange={e=>setInputs({...inputs,[key]:spec.type==='evidence_ids' ? e.target.value.split('\n').filter(Boolean) : e.target.value})} />{spec.type==='evidence_ids' && <span>One saved evidence reference per line.</span>}</label>)}
      <label>Workspace<select aria-label="Workflow workspace" value={workspace} onChange={e=>setWorkspace(e.target.value)}><option value="new">Create a new workspace at Start</option>{workspaces.map(w=><option key={w.id} value={w.id}>{w.name} ({w.root})</option>)}</select></label>{workspace==='new' && <label>New workspace name<input aria-label="New workspace name" required value={name} onChange={e=>setName(e.target.value)} /></label>}{workspaceError && <p>{workspaceError}</p>}
      {Object.entries(record.definition.agents).map(([alias,agent])=>{const bound=agent.binding_id ? store.catalog?.bindings.find(b=>b.binding_id===agent.binding_id) : null;return <div key={alias}>{agent.binding_id ? <p>{faceLabel(agent.face_id)}: {bound?.model ?? 'The saved model is unavailable'}.</p> : <label>Model for {alias.replaceAll('_',' ')} ({faceLabel(agent.face_id)})<select aria-label={'Model for '+alias} value={bindings[alias] ?? ''} onChange={e=>setBindings({...bindings,[alias]:e.target.value})}><option value="">Choose a model</option>{store.catalog?.bindings.filter(b=>b.face_ids.includes(agent.face_id)).map(b=><option key={b.binding_id} value={b.binding_id} disabled={!b.runnable}>{b.model}{b.runnable ? '' : ' (unavailable)'}</option>)}</select></label>}{!simple && <details><summary>Details for {alias.replaceAll('_',' ')}</summary><pre>{JSON.stringify(bound ?? store.catalog?.bindings.find(b=>b.binding_id===bindings[alias]) ?? {face_id:agent.face_id},null,2)}</pre></details>}</div>;})}
      <label>Run mode<select aria-label="Run form mode" value={mode} onChange={e=>setMode(e.target.value as 'auto'|'approve')}><option value="auto">Work without asking in this workspace</option><option value="approve">Ask before each change</option></select></label>
      {!simple && <details><summary>Details</summary><p>Revision {record.revision}. Digest {record.digest}.</p><pre>{JSON.stringify({budget:record.definition.budget,limits:store.catalog?.limits},null,2)}</pre></details>}
    </fieldset>{saved && <p>The earlier proposal request has an unknown outcome. Retry its saved details before changing them.</p>}{error && <p role="alert">{error}</p>}<button className="primary" disabled={busy || !store.catalog}>{onPrepare ? 'Save run choices' : saved ? 'Retry the saved proposal request' : 'Review proposal'}</button><button type="button" disabled={busy} onClick={onClose}>Close run form</button></form>;
}

export function WorkflowLibrary({onSketch}: {onSketch:(sketch:Sketch)=>Promise<void>}) {
  const context=useContext(WorkflowContext),simple=useExperience()==='simple';
  if(!context)return <p>Connect to Bob to read saved workflows.</p>;
  return <DefinitionLibrary store={context.store} simple={simple} onSketch={onSketch} />;
}
function DefinitionLibrary({store,simple,onSketch}: {store:NonNullable<React.ContextType<typeof WorkflowContext>>['store'];simple:boolean;onSketch:(sketch:Sketch)=>Promise<void>}) {
  useWorkflows(store);
  const [items,setItems]=useState<WorkflowRecord[]>([]),[current,setCurrent]=useState<WorkflowRecord|null>(null),[mode,setMode]=useState<'view'|'edit'|'run'>('view'),[error,setError]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState(false),[copyName,setCopyName]=useState<string|null>(null),[deleting,setDeleting]=useState(false);
  const lock=useRef(false),alive=useRef(true);
  async function refresh() {
    setError('');try {
      const catalog=await store.client.workflowCatalog();store.catalog=catalog;store.changed();
      const found=new Map(catalog.templates.map(r=>[r.id,r]));let cursor:string|undefined;const seen=new Set<string>();
      do {const page=await store.client.workflowList(cursor);page.items.forEach(r=>found.set(r.id,r));cursor=page.next_cursor ?? undefined;if(cursor&&seen.has(cursor))throw new Error('The server repeated a definition page.');if(cursor)seen.add(cursor);} while(cursor);
      if(alive.current)setItems([...found.values()].filter(r=>!r.deleted));
    }catch(e){if(alive.current)setError(authoringErrors(e).map(e=>e.message).join(' '));}
  }
  useEffect(()=>{alive.current=true;void refresh();return()=>{alive.current=false;};},[store]);
  function saved(record:WorkflowRecord) {setCurrent(record);setItems(rows=>[...rows.filter(r=>r.id!==record.id),record]);setMessage('Saved and read back from the server.');setMode('edit');}
  async function copy() {
    if(!current||!copyName?.trim()||lock.current)return;lock.current=true;setBusy(true);setError('');const operation=`workflow-copy:${current.id}`;let acknowledged=false;
    try {const payload=pendingMutation<{id:string;revision:WorkflowRecord['revision'];name:string}>(store.scope,operation) ?? {id:current.id,revision:current.revision,name:copyName.trim()};
      const result=await store.client.workflowCopy({...payload,request_key:mutationKey(store.scope,operation,payload)});acknowledged=true;
      const readback=(await store.client.workflowGet(result.definition.id,result.definition.revision)).definition;mutationDone(store.scope,operation);saved(readback);setCopyName(null);
    }catch(e){if(!acknowledged)releaseRefusal(store,operation,e);setError(acknowledged ? 'Bob accepted the copy, but it could not be read back yet. Retry the saved copy request.' : authoringErrors(e).map(e=>e.message).join(' '));}finally{lock.current=false;setBusy(false);}
  }
  async function remove() {
    if(!current||current.read_only||lock.current)return;lock.current=true;setBusy(true);setError('');const operation=`workflow-delete:${current.id}`;
    try {const payload=pendingMutation<{id:string;expected_revision:WorkflowRecord['revision']}>(store.scope,operation) ?? {id:current.id,expected_revision:current.revision};await store.client.workflowDelete({...payload,request_key:mutationKey(store.scope,operation,payload)});mutationDone(store.scope,operation);setItems(rows=>rows.filter(r=>r.id!==current.id));setCurrent(null);setDeleting(false);setMessage('The definition was deleted. Existing runs keep their saved version.');}
    catch(e){releaseRefusal(store,operation,e);setError(authoringErrors(e).map(e=>e.message).join(' '));}finally{lock.current=false;setBusy(false);}
  }
  const newer=current ? items.find(r=>r.id===current.id && BigInt(r.revision)>BigInt(current.revision)) : null;
  return <section className="workflow-library" aria-label="Saved workflows"><header><h3>Workflows</h3><span className="preview-tag" title="Workflows are a preview in 0.99. A run may stop before it finishes. Chat, agents and Builder are not affected.">Preview</span><button disabled={busy} onClick={()=>void refresh()}>Refresh saved workflows</button></header><p>{store.catalog?.available ? 'Workflows are available right now.' : `Workflows are unavailable right now. ${store.catalog?.reasons.map(r=>r.replaceAll('_',' ')).join('. ') ?? 'Availability has not been read.'}`}</p>
    <div className="lib-main"><nav className="lib-list" aria-label={simple ? 'Premades and your workflows' : 'Workflow definitions'}>{items.map(record=><button className="lib-item" key={record.id} onClick={()=>{setCurrent(record);setMode('view');setCopyName(null);setDeleting(false);setError('');}}><span><strong>{record.definition.name}</strong><small>{record.read_only ? 'Premade. Copy to edit.' : 'Your workflow.'}</small><small>{record.definition.summary}</small></span></button>)}</nav><div className="lib-stage workflow-definition">
      {current ? <>{newer && <p>A newer revision is available. <button onClick={()=>{setCurrent(newer);setMode('view');}}>Use the newer revision</button></p>}{mode==='edit' ? <WorkflowEditor key={current.id+':'+current.revision} record={current} definition={current.definition} onSaved={saved} onCancel={()=>setMode('view')} /> : mode==='run' ? <WorkflowRunForm key={current.id+':'+current.revision} record={current} onClose={()=>setMode('view')} /> : <><h3>{current.definition.name}</h3><p>{current.definition.summary}</p><p>{current.runnable && store.catalog?.available ? 'This workflow is available.' : (store.catalog?.available ? 'Run this workflow to choose its workspace and models. You review a proposal before anything starts.' : 'Workflows are unavailable right now, so this cannot start.')}</p><ol>{orderedStages(current.definition.stages).map(s=><li key={s.id}>{stageLabel(s.kind)}{s.agent ? `: ${faceLabel(current.definition.agents[s.agent]?.face_id ?? s.agent)}` : ''}{s.body && <ol>{orderedStages(s.body).map(child=><li key={child.id}>{stageLabel(child.kind)}</li>)}</ol>}</li>)}</ol><div className="row-actions"><button onClick={()=>setMode('run')}>Run this workflow</button>{current.read_only ? <button onClick={()=>setCopyName(current.definition.name+' copy')}>Copy to edit</button> : <><button onClick={()=>setMode('edit')}>Edit workflow</button><button onClick={()=>setCopyName(current.definition.name+' copy')}>Copy workflow</button><button onClick={()=>setDeleting(true)}>Delete workflow</button></>}<button onClick={()=>{setBusy(true);void onSketch(definitionSketch(current,store.scope)).catch(e=>setError(String(e))).finally(()=>setBusy(false));}}>Open as sketch</button></div>
      {!simple && <details><summary>Details</summary><p>Definition {current.id}, revision {current.revision}, digest {current.digest}.</p><pre>{JSON.stringify(current.definition,null,2)}</pre></details>}</>}
      {copyName !== null && <form aria-label="Copy workflow" onSubmit={e=>{e.preventDefault();void copy();}}><label>Name for the copy<input aria-label="Copy name" value={copyName} onChange={e=>setCopyName(e.target.value)} /></label><button disabled={busy || !copyName.trim()}>Save copy</button><button type="button" onClick={()=>setCopyName(null)}>Cancel copy</button></form>}
      {deleting && <section aria-label="Delete workflow confirmation"><p>Delete this definition? Existing runs keep their saved version.</p><button disabled={busy} onClick={()=>void remove()}>Delete definition</button><button onClick={()=>setDeleting(false)}>Keep definition</button></section>}</> : <p>Choose a premade or one of your saved workflows.</p>}
    </div></div>{message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}</section>;
}

/** A completed Assistant answer is data for owner review; rendering never saves it. */
export function WorkflowDraftReview({content}: {content:string}) {
  const context=useContext(WorkflowContext),[selected,setSelected]=useState<{definition:WorkflowDefinition;record?:WorkflowRecord}|null>(null),[error,setError]=useState(''),[message,setMessage]=useState('');
  if(!context)return null;const drafts=workflowDrafts(content);
  return <>{drafts.map((draft,index)=><button key={index} onClick={()=>{void (async()=>{try{const record=draft.definition_ref ? (await context.store.client.workflowGet(draft.definition_ref.id,draft.definition_ref.revision)).definition : undefined;if(record?.read_only){setError('Copy this premade in the Library before saving an edited revision.');return;}setSelected({definition:draft.definition,record});setError('');}catch(e){setError(authoringErrors(e).map(e=>e.message).join(' '));}})();}}>Review proposed workflow: {draft.definition.name}</button>)}{selected && <WorkflowEditor key={selected.record?.revision ?? 'new'} definition={selected.definition} record={selected.record} onSaved={record=>{setSelected({definition:record.definition,record});setMessage('Saved and read back from the server.');}} onCancel={()=>setSelected(null)} />}{error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}</>;
}
