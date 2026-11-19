import { useContext, useRef, useState } from 'react';
import { WorkflowContext } from './WorkflowViews';
import { useExperience } from './experience';
import type { WorkflowDefinition, WorkflowRecord, WorkflowStage } from './workflow-types';
import { authoringErrors, blankStage, cloneDefinition, editableDefinition, faceLabel, policyLabel, stageErrors, stageLabel, type AuthoringError } from './workflow-library';
import { executionRefused, mutationDone, mutationKey, pendingMutation } from './durable-runs';
import { isDefiniteRefusal, toPortalError } from './portal-protocol';

type Ref = {input?: string; stage?: string; output?: string; item?: boolean; literal?: unknown};
function ReferenceField({label,value,onChange,definition,stages,nested}: {label:string;value:unknown;onChange:(value:Ref)=>void;definition:WorkflowDefinition;stages:WorkflowStage[];nested:boolean}) {
  const ref = (value ?? {}) as Ref;
  const choice = ref.input ? 'input:' + ref.input : ref.stage ? 'stage:' + ref.stage : ref.item ? 'item' : 'literal';
  return <label>{label}<select aria-label={label} value={choice} onChange={e => {
    const [type,id] = e.target.value.split(':'); onChange(type === 'input' ? {input:id} : type === 'stage' ? {stage:id,output:'text'} : type === 'item' ? {item:true} : {literal:null});
  }}><option value="literal">A fixed value</option>{Object.keys((definition.inputs ?? {}) as object).map(id => <option key={id} value={'input:' + id}>Original {id.replaceAll('_',' ')}</option>)}{nested && <option value="item">The current planned item</option>}{stages.map((s,i) => <option key={s.id} value={'stage:' + s.id}>Stage {i + 1}: {stageLabel(s.kind)}</option>)}</select>
    {ref.stage && <select aria-label={label + ' result'} value={ref.output} onChange={e => onChange({stage:ref.stage,output:e.target.value})}>{['text','units','verdict','snapshot','items','report'].map(output => <option key={output} value={output}>{output === 'snapshot' ? 'Frozen workspace' : output === 'units' ? 'Planned items' : output === 'items' ? 'Item results' : output.replaceAll('_',' ')}</option>)}</select>}
    {'literal' in ref && <input aria-label={label + ' fixed value'} value={typeof ref.literal === 'string' ? ref.literal : JSON.stringify(ref.literal)} onChange={e => { let literal:unknown = e.target.value; try {literal=JSON.parse(e.target.value);} catch { /* string */ } onChange({literal}); }} />}
  </label>;
}

function StageList({definition,stages,onChange,errors,nested=false,prefix='stages',keyPrefix='',invalid}: {definition:WorkflowDefinition;stages:WorkflowStage[];onChange:(stages:WorkflowStage[])=>void;errors:AuthoringError[];nested?:boolean;prefix?:string;keyPrefix?:string;invalid:(text:string)=>void}) {
  const [adding,setAdding] = useState('agent');
  const aliases=Object.keys(definition.agents), builder=aliases.find(a => definition.agents[a].face_id === 'builder') ?? aliases[0], reviewer=aliases.find(a => definition.agents[a].face_id === 'reviewer') ?? aliases[0];
  const update = (index:number,patch:Partial<WorkflowStage>) => onChange(stages.map((s,i) => i === index ? {...s,...patch} : s));
  function move(index:number,delta:number) {
    const next=[...stages], [item]=next.splice(index,1); next.splice(index+delta,0,item);
    // Order never rewrites dependencies or references behind the owner's back.
    const position=new Map(next.map((s,i)=>[s.id,i]));
    if (next.some((s,i)=>s.depends_on.some(id=>(position.get(id) ?? -1)>=i)) || next.some((s,i)=>s.kind === 'report' && i !== next.length-1)) {invalid('This move would put a stage before its dependencies or after the report. Change its dependencies first.');return;}
    invalid('');onChange(next.map((s,i)=>i ? {...s,depends_on:[...new Set([...s.depends_on,next[i-1].id])]} : s));
  }
  function remove(index:number) {
    const target=stages[index];
    if (stages.some((s,i) => i !== index && (s.depends_on.includes(target.id) || JSON.stringify(s).includes(`"stage":"${target.id}"`) || s.review_of === target.id))) {invalid('Another stage receives work from this stage. Remove those connections before removing it.');return;}
    invalid('');onChange(stages.filter((_,i)=>i !== index));
  }
  const agentPicker = (label:string,alias:string,onPick:(alias:string)=>void) => <label>{label}<select aria-label={label} value={alias} onChange={e=>onPick(e.target.value)}>{aliases.map(a=><option key={a} value={a}>{a.replaceAll('_',' ')} ({faceLabel(definition.agents[a].face_id)})</option>)}</select></label>;
  return <div className="workflow-stage-editor"><ol>{stages.map((stage,index)=>{
    const path=`${prefix}.${index}`, earlier=stages.slice(0,index), refs=(stage.inputs ?? {}) as Record<string,Ref>;
    return <li key={stage.id} data-stage-path={path}><fieldset><legend>Stage {index+1}: {stageLabel(stage.kind)}</legend>
      {stageErrors(errors,path,keyPrefix+stage.id).map((error,i)=><p role="alert" key={i}>{error.message}</p>)}
      <label>Kind<select aria-label={`Stage ${index+1} kind`} value={stage.kind} onChange={e=>{const next=blankStage(e.target.value,stage.id,aliases[0],builder,reviewer,nested);update(index,{...Object.fromEntries(Object.keys(stage).map(k=>[k,undefined])),...next,depends_on:stage.depends_on});}}>{['agent','repair','check','gate',...(nested ? [] : ['foreach','report'])].filter(k=>k !== 'report' || !stages.some((s,i)=>i !== index && s.kind === 'report')).map(k=><option key={k} value={k}>{stageLabel(k)}</option>)}</select></label>
      <div role="group" aria-label={`Stage ${index+1} dependencies`}><p>Waits for earlier stages</p>{earlier.length ? earlier.map((s,i)=><label key={s.id}><input type="checkbox" checked={stage.depends_on.includes(s.id)} onChange={e=>update(index,{depends_on:e.target.checked ? [...stage.depends_on,s.id] : stage.depends_on.filter(id=>id !== s.id)})} />Stage {i+1}: {stageLabel(s.kind)}</label>) : <p>This stage can begin immediately.</p>}</div>
      {stage.kind !== 'report' && <div><p>What it receives from earlier stages</p>{Object.entries(refs).map(([name,ref])=><div key={name}><ReferenceField label={name.replaceAll('_',' ')} value={ref} onChange={value=>update(index,{inputs:{...refs,[name]:value}})} definition={definition} stages={earlier} nested={nested} /><button type="button" onClick={()=>update(index,{inputs:Object.fromEntries(Object.entries(refs).filter(([key])=>key!==name))})}>Remove this input</button></div>)}<button type="button" onClick={()=>{let n=1;while(refs['input_'+n])n++;update(index,{inputs:{...refs,['input_'+n]:{input:'task'}}});}}>Add an input</button></div>}
      {stage.kind === 'agent' && <>{agentPicker('Agent',stage.agent ?? '',agent=>update(index,{agent}))}<label>Instructions<textarea value={String(stage.brief ?? '')} onChange={e=>update(index,{brief:e.target.value})} /></label><label>Workspace access<select value={String(stage.access)} onChange={e=>update(index,{access:e.target.value})}><option value="read">Read the workspace</option><option value="write">Make changes in the workspace</option></select></label><label>Result<select value={String(stage.output)} onChange={e=>update(index,{output:e.target.value})}><option value="text">Written result</option><option value="units">Planned items</option><option value="verdict">Review verdict</option></select></label>{stage.output === 'verdict' && <label>Work being reviewed<select value={String(stage.review_of ?? '')} onChange={e=>update(index,{review_of:e.target.value})}><option value="">Choose earlier work</option>{earlier.filter(s=>s.kind==='agent').map(s=><option key={s.id} value={s.id}>Stage {stages.indexOf(s)+1}: {stageLabel(s.kind)}</option>)}</select></label>}</>}
      {stage.kind === 'repair' && <>{(['build','review','fix'] as const).map(role=>{
        const step=stage[role] as {agent:string;brief:string;skills:unknown[]};
        return <div key={role}>{agentPicker(role === 'build' ? 'Build agent' : role === 'review' ? 'Review agent' : 'Fix agent',step.agent,agent=>update(index,{[role]:{...step,agent}}))}<label>{role === 'build' ? 'Build instructions' : role === 'review' ? 'Review instructions' : 'Fix instructions'}<textarea value={step.brief} onChange={e=>update(index,{[role]:{...step,brief:e.target.value}})} /></label></div>;
      })}<label>Repair limit<input type="number" min={0} max={10} value={Number(stage.max_fixes)} onChange={e=>update(index,{max_fixes:Number(e.target.value)})} /></label></>}
      {(stage.kind === 'agent' && stage.review_of || stage.kind === 'repair') && <p>Review policy: {policyLabel(definition.review_policy)}.</p>}
      {(stage.kind === 'repair' || stage.kind === 'check') && <div><p>Checks</p>{definition.authority.checks.length ? definition.authority.checks.map(check=><label key={check.id}><input type="checkbox" checked={(stage.check_ids as string[] ?? []).includes(check.id)} onChange={e=>update(index,{check_ids:e.target.checked ? [...stage.check_ids as string[],check.id] : (stage.check_ids as string[]).filter(id=>id!==check.id)})} />{check.command}</label>) : <p>No checks are defined. A check stage needs at least one check.</p>}</div>}
      {stage.kind === 'check' && <ReferenceField label="Workspace to check" value={stage.snapshot} onChange={snapshot=>update(index,{snapshot})} definition={definition} stages={earlier} nested={nested} />}
      {stage.kind === 'gate' && <label>Review question<textarea value={stage.prompt} onChange={e=>update(index,{prompt:e.target.value})} /></label>}
      {stage.kind === 'foreach' && <><ReferenceField label="Planned items to work on" value={stage.source} onChange={source=>update(index,{source})} definition={definition} stages={earlier} nested={nested} /><label>Maximum planned items<input type="number" min={1} max={64} value={Number(stage.max_units)} onChange={e=>update(index,{max_units:Number(e.target.value)})} /></label><label>Requested parallel work<input type="number" min={1} max={16} value={Number(stage.width)} onChange={e=>update(index,{width:Number(e.target.value)})} /></label><StageList definition={definition} stages={stage.body ?? []} onChange={body=>update(index,{body})} errors={errors} nested prefix={path+'.body'} keyPrefix={keyPrefix+stage.id+'/'} invalid={invalid} /></>}
      <div className="row-actions"><button type="button" disabled={index===0} onClick={()=>move(index,-1)}>Move up</button><button type="button" disabled={index===stages.length-1 || stage.kind==='report'} onClick={()=>move(index,1)}>Move down</button><button type="button" onClick={()=>remove(index)}>Remove stage</button></div>
    </fieldset></li>;
  })}</ol><label>New stage kind<select value={adding} onChange={e=>setAdding(e.target.value)}>{['agent','repair','check','gate',...(nested ? [] : ['foreach','report'])].map(k=><option key={k} value={k}>{stageLabel(k)}</option>)}</select></label><button type="button" disabled={stages.length >= (nested ? 16 : 32) || adding === 'report' && stages.some(s=>s.kind==='report')} onClick={()=>{
    let n=1;while(stages.some(s=>s.id==='stage_'+n))n++;
    const row=blankStage(adding,'stage_'+n,aliases[0],builder,reviewer,nested), at=adding==='report' ? stages.length : stages.findIndex(s=>s.kind==='report');
    const next=[...stages];next.splice(at<0 ? next.length : at,0,row);onChange(next);
  }}>Add stage</button></div>;
}

export function WorkflowEditor({record,definition,onSaved,onCancel}: {record?:WorkflowRecord;definition:WorkflowDefinition;onSaved:(record:WorkflowRecord)=>void;onCancel:()=>void}) {
  const context=useContext(WorkflowContext)!;const {store}=context;
  const simple=useExperience()==='simple', lock=useRef(false);
  const [draft,setDraft]=useState(()=>cloneDefinition(definition)),[errors,setErrors]=useState<AuthoringError[]>([]),[localError,setLocalError]=useState(''),[busy,setBusy]=useState(false),[json,setJson]=useState<string|null>(null);
  const [latest,setLatest]=useState<WorkflowRecord|null>(null),[saved,setSaved]=useState(false);
  const operation=`workflow-save:${record?.id ?? 'new'}`;
  async function save() {
    if(lock.current || record?.read_only)return;lock.current=true;setBusy(true);setErrors([]);setLocalError('');
    let acknowledged=false;
    try {
      const parsed=json === null ? draft : JSON.parse(json) as WorkflowDefinition;
      const payload=pendingMutation<{id?:string;definition:WorkflowDefinition;expected_revision:WorkflowRecord['revision']|null}>(store.scope,operation) ?? { ...(record ? {id:record.id} : {}),definition:parsed,expected_revision:record?.revision ?? null};
      const result=await store.client.workflowSave({...payload,request_key:mutationKey(store.scope,operation,payload)});
      acknowledged=true;
      const readback=(await store.client.workflowGet(result.definition.id,result.definition.revision)).definition;
      mutationDone(store.scope,operation);
      setDraft(cloneDefinition(readback.definition));setJson(null);setSaved(true);onSaved(readback);
    } catch(e) {
      setErrors(acknowledged ? [{message:'Bob accepted the save, but the saved definition could not be read back yet. Retry the saved request.'}] : authoringErrors(e));const typed=toPortalError(e);
      if(!acknowledged && (executionRefused(typed.code) || isDefiniteRefusal(typed) || ['revision_conflict','unsupported_fields','invalid_definition','validation_error','invalid_args'].includes(typed.code)))mutationDone(store.scope,operation);
      if(typed.code==='revision_conflict' && record) try {setLatest((await store.client.workflowGet(record.id)).definition);} catch { /* retain original edits */ }
    } finally {lock.current=false;setBusy(false);}
  }
  if(record?.read_only)return <p>This premade is read-only. Copy it to edit.</p>;
  const assigned=new Set<string>();
  function collect(stages:WorkflowStage[],prefix='stages',keyPrefix=''){stages.forEach((s,i)=>{stageErrors(errors,`${prefix}.${i}`,keyPrefix+s.id).forEach(e=>{if(e.path)assigned.add(e.path);});if(s.body)collect(s.body,`${prefix}.${i}.body`,keyPrefix+s.id+'/');});}collect(draft.stages);
  return <form className="workflow-editor lib-form" aria-label="Workflow stage editor" onSubmit={e=>{e.preventDefault();void save();}}><h3>Edit the workflow</h3><fieldset disabled={busy}><fieldset disabled={json !== null}><label>Name<input required maxLength={80} value={draft.name} onChange={e=>{setSaved(false);setDraft({...draft,name:e.target.value});}} /></label><label>Description<textarea maxLength={1000} value={draft.summary} onChange={e=>setDraft({...draft,summary:e.target.value})} /></label><label>Review policy<select value={draft.review_policy} onChange={e=>setDraft({...draft,review_policy:e.target.value})}>{['any','different_model','different_family'].map(p=><option key={p} value={p}>{policyLabel(p)}</option>)}</select></label>
    <details><summary>Agents and model slots</summary>{Object.entries(draft.agents).map(([alias,agent])=><fieldset key={alias}><legend>{alias.replaceAll('_',' ')}</legend><label>Agent<select value={agent.face_id} onChange={e=>setDraft({...draft,agents:{...draft.agents,[alias]:{...agent,face_id:e.target.value}}})}>{[...new Set([...(context.catalog?.faces.length ? context.catalog.faces.map(face=>face.id) : ['assistant','orchestrator','builder','reviewer','fixer']),agent.face_id])].map(face=><option key={face} value={face}>{context.catalog?.faces.find(row=>row.id===face)?.name ?? faceLabel(face)}</option>)}</select></label><label>Saved model slot<select value={agent.binding_id ?? ''} onChange={e=>setDraft({...draft,agents:{...draft.agents,[alias]:{...agent,binding_id:e.target.value || null}}})}><option value="">Choose the model when running</option>{agent.binding_id && !store.catalog?.bindings.some(b=>b.binding_id===agent.binding_id) && <option value={agent.binding_id}>The saved model is unavailable</option>}{store.catalog?.bindings.filter(b=>b.face_ids.includes(agent.face_id)).map(b=><option key={b.binding_id} value={b.binding_id}>{b.model}</option>)}</select></label></fieldset>)}</details>
    <p>Moving a stage makes the following stages wait in the displayed order. Existing input connections are kept.</p>
    <StageList definition={draft} stages={draft.stages} onChange={stages=>{setSaved(false);setDraft({...draft,stages});}} errors={errors} invalid={setLocalError} />
    <details><summary>Checks for this workflow</summary>{draft.authority.checks.map((check,index)=><fieldset key={check.id}><legend>Check {index+1}</legend>{(['command','cwd','timeout_seconds'] as const).map(key=><label key={key}>{key==='command' ? 'Check command' : key==='cwd' ? 'Folder within the workspace' : 'Time limit in seconds'}<input type={key==='timeout_seconds' ? 'number' : 'text'} min={1} max={600} value={check[key]} onChange={e=>setDraft({...draft,authority:{...draft.authority,checks:draft.authority.checks.map((c,i)=>i===index ? {...c,[key]:key==='timeout_seconds' ? Number(e.target.value) : e.target.value} : c)}})} /></label>)}<button type="button" onClick={()=>setDraft({...draft,authority:{...draft.authority,checks:draft.authority.checks.filter((_,i)=>i!==index)}})}>Remove check</button></fieldset>)}<button type="button" onClick={()=>{let n=1;while(draft.authority.checks.some(c=>c.id==='check_'+n))n++;setDraft({...draft,authority:{...draft.authority,checks:[...draft.authority.checks,{id:'check_'+n,command:'',cwd:'',timeout_seconds:60}]}});}}>Add a check</button></details>
    {!simple && <details><summary>Details</summary><p>Saved revision: {record?.revision ?? 'New definition'}. Digest: {record?.digest ?? 'Assigned when saved'}.</p><fieldset><legend>Budget</legend>{(Object.keys(draft.budget) as (keyof WorkflowDefinition['budget'])[]).map(key=><label key={key}>{({calls:'Model calls',active_seconds:'Active work in seconds',job_seconds:'Check work in seconds',max_job_attempts:'Check attempts'})[key]}<input type="number" min={1} value={draft.budget[key]} onChange={e=>setDraft({...draft,budget:{...draft.budget,[key]:Number(e.target.value)}})} /></label>)}</fieldset><pre>{JSON.stringify(draft,null,2)}</pre><button type="button" onClick={()=>setJson(JSON.stringify(draft,null,2))}>Edit as JSON</button></details>}
    </fieldset>{!simple && json !== null && <label>Definition JSON<textarea aria-label="Definition JSON" rows={20} value={json} onChange={e=>setJson(e.target.value)} /><button type="button" onClick={()=>{try{const value=JSON.parse(json);if(!editableDefinition(value))throw new Error('Use a complete workflow definition.');setDraft(value);setJson(null);setLocalError('');}catch{setLocalError('The JSON must be a complete workflow definition.');}}}>Use JSON in the stage list</button><button type="button" onClick={()=>{setJson(null);setLocalError('');}}>Cancel JSON editing</button></label>}
    {!!pendingMutation(store.scope,operation) && <p>The earlier save has an unknown outcome. Retry its saved details before changing the definition.</p>}
    {localError && <p role="alert">{localError}</p>}{errors.filter(e=>!e.path||!assigned.has(e.path)).map((e,i)=><p role="alert" key={i}>{e.message}</p>)}
    {latest && <section><p>A newer revision is available. Your edits have not replaced it.</p><details><summary>Review the newer definition</summary><p>{latest.definition.name}. {latest.definition.summary}</p><ol>{latest.definition.stages.map(s=><li key={s.id}>{stageLabel(s.kind)}</li>)}</ol>{!simple && <pre>{JSON.stringify(latest.definition,null,2)}</pre>}</details><button type="button" onClick={()=>onSaved(latest)}>Use the newer revision</button></section>}
    {saved && <p role="status">Saved and read back from the server.</p>}<div className="row-actions"><button className="primary">{pendingMutation(store.scope,operation) ? 'Retry the saved workflow request' : 'Save workflow'}</button><button type="button" onClick={onCancel}>Close editor</button></div></fieldset></form>;
}
