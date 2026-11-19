import { useContext, useEffect, useState } from 'react';
import { WorkflowContext } from './WorkflowViews';
import type { WorkflowRecord } from './workflow-types';
import type { AgentPlan } from './calendar';
import { authoringErrors } from './workflow-library';

/** Only mounted while editing an agent task; no calendar background reads. */
export function CalendarWorkflowPicker({plan,onChange,onConfigure}: {plan:AgentPlan;onChange:(plan:AgentPlan)=>void;onConfigure:(plan:AgentPlan)=>void}) {
  const context=useContext(WorkflowContext),[records,setRecords]=useState<WorkflowRecord[]>([]),[error,setError]=useState('');
  useEffect(()=>{if(!context)return;let alive=true;void (async()=>{
    const catalog=await context.store.loadCatalog(),all=new Map(catalog.templates.map(r=>[r.id,r]));let cursor:string|undefined;const seen=new Set<string>();
    do{const page=await context.store.client.workflowList(cursor);page.items.forEach(r=>all.set(r.id,r));cursor=page.next_cursor ?? undefined;if(cursor&&seen.has(cursor))throw new Error('The server repeated a definition page.');if(cursor)seen.add(cursor);}while(cursor);
    if(alive)setRecords([...all.values()].filter(r=>!r.deleted));
  })().catch(e=>{if(alive)setError(authoringErrors(e).map(e=>e.message).join(' '));});return()=>{alive=false;};},[context?.store]);
  if(!context)return null;
  return <div><label>Saved workflow (optional)<select aria-label="Calendar workflow" value={plan.workflow_ref?.id ?? ''} onChange={e=>{
    const record=records.find(r=>r.id===e.target.value);
    onChange({...plan,workflow_ref:record ? {id:record.id,revision:record.revision,digest:record.digest,scope:context.store.scope} : undefined,workflow_choices:undefined});
  }}><option value="">Use a team task</option>{plan.workflow_ref && !records.some(r=>r.id===plan.workflow_ref?.id) && <option value={plan.workflow_ref.id}>Saved workflow version</option>}{records.map(r=><option key={r.id} value={r.id}>{r.definition.name}{r.id===plan.workflow_ref?.id ? ' (saved version)' : ''}</option>)}</select></label>{plan.workflow_ref && <><p>This task keeps its saved workflow version. Run now opens a proposal review. Repeating the reminder never starts work.</p><button type="button" onClick={()=>onConfigure(plan)}>Choose workspace and models</button><label>Acceptance notes (optional)<textarea aria-label="Calendar acceptance notes" value={String(plan.workflow_inputs?.acceptance ?? '')} onChange={e=>onChange({...plan,workflow_inputs:{...plan.workflow_inputs,acceptance:e.target.value}})} /></label></>}{error && <p role="alert">{error}</p>}</div>;
}
