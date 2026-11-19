import type { WorkflowDefinition, WorkflowRecord, WorkflowStage } from './workflow-types';
import { newSketch, type Sketch, type SketchNode } from './library';
import { workflowInteger } from './workflow-types';

export const stageLabel = (kind: string) => ({agent:'Agent work',repair:'Build, review and fix',foreach:'Work on each planned item',check:'Run checks',gate:'Your review',report:'Report'}[kind] ?? kind);
export const faceLabel = (face: string) => ({assistant:'Assistant',planner:'Planner',orchestrator:'Orchestrator',builder:'Builder',reviewer:'Reviewer',fixer:'Fixer'}[face] ?? face.replaceAll('_',' '));
export const policyLabel = (policy: string) => ({any:'Any model may review',different_model:'A different model must review',different_family:'A different model family must review'}[policy] ?? policy);
export const cloneDefinition = (d: WorkflowDefinition): WorkflowDefinition => JSON.parse(JSON.stringify(d));
export function orderedWorkflowStages(stages: WorkflowStage[]): WorkflowStage[] {
  const result: WorkflowStage[] = [], done = new Set<string>();
  while (result.length < stages.length) {
    const next=stages.filter(s=>!done.has(s.id) && s.depends_on.every(id=>done.has(id))).sort((a,b)=>a.id.localeCompare(b.id))[0];
    if(!next)break;result.push(next);done.add(next.id);
  }
  return result;
}
export type DefinitionReference = { id: string; revision: WorkflowRecord['revision']; digest: string; scope: string };

export function definitionSketch(record: WorkflowRecord, scope: string): Sketch {
  const sketch = newSketch(record.definition.name);
  const nodes: SketchNode[] = [], edges: Sketch['edges'] = [];
  function add(stages: WorkflowStage[], prefix = '', depth = 0) {
    orderedWorkflowStages(stages).forEach((stage, i) => {
      const key = prefix + stage.id;
      nodes.push({id:key, stage_key:key, type:stage.kind === 'gate' ? 'human' : stage.kind === 'report' ? 'output' : stage.kind === 'check' ? 'tool' : stage.kind === 'foreach' ? 'team' : 'agent', label:`${i + 1}. ${stageLabel(stage.kind)}${stage.agent ? ` — ${faceLabel(record.definition.agents[stage.agent]?.face_id ?? stage.agent)}` : ''}`, x:40 + depth * 240, y:40 + nodes.length * 100});
      stage.depends_on.forEach(from => edges.push({from:prefix + from,to:key}));
      if (stage.body) add(stage.body, key + '/', depth + 1);
    });
  }
  add(record.definition.stages);
  return {...sketch,nodes,edges,definition_ref:{id:record.id,revision:record.revision,digest:record.digest,scope},annotations:''};
}

export type AuthoringError = { message: string; path?: string };
/** Preserve structured server paths when present. The WB1 error fixture has no path. */
export function authoringErrors(error: unknown): AuthoringError[] {
  let value: any = error;
  if (value instanceof Error) return [{message:value.message}];
  if (typeof value === 'string') { try { value = JSON.parse(value); } catch { return [{message:value}]; } }
  value = value?.error ?? value;
  const code = value?.code ?? 'unknown';
  if (code === 'revision_conflict') return [{message:'A newer revision is saved on the server. Your edits are still here. Review the newer revision before saving again.'}];
  if (code === 'rate_limited' || code === 'gateway_http_429') return [{message:'Bob is handling a lot of requests. This was not applied. Try again in a few seconds.'}];
  let detail = value?.detail;
  if (typeof detail === 'string') { try { detail = JSON.parse(detail); } catch { /* plain detail */ } }
  const rows = Array.isArray(detail) ? detail : detail?.errors ?? value?.errors ?? (detail && typeof detail === 'object' ? [detail] : []);
  const friendly = (text: string) => text.replaceAll('_',' ');
  const pathText = (path: unknown) => typeof path === 'string' ? path.replace(/^\$\.?/,'').replace(/\//g,'.').replace(/\[(\d+)\]/g,'.$1').replace(/^\.?definition\./,'').replace(/^\./,'') : undefined;
  if (Array.isArray(rows) && rows.length) return rows.map((row: any) => ({message:friendly(String(row.message ?? row.msg ?? row.reason ?? row.code ?? 'Please check this value.')),path:Array.isArray(row.loc ?? row.path) ? (row.loc ?? row.path).filter((s: unknown,i:number) => !(i===0 && (s==='body'||s==='definition')) && !(i===1 && s==='definition')).join('.') : pathText(row.path) ?? (typeof row.stage_key === 'string' ? row.stage_key : undefined)}));
  return [{message:typeof detail === 'string' && detail ? friendly(detail) : code === 'unsupported_fields' ? 'Some fields are not supported. Review the definition and try again.' : code === 'unknown' || code === 'transport_unavailable' || code === 'timeout_no_reply' ? 'The reply was lost. The outcome is unknown. Retry the saved request before changing it.' : `Bob could not apply this change: ${friendly(code)}.`}];
}

export function stageErrors(errors: AuthoringError[], path: string, id: string) {
  return errors.filter(e => e.path && (e.path === id || e.path === path || e.path.startsWith(path + '.') && !e.path.slice(path.length + 1).includes('body.')));
}

export function blankStage(kind: string, id: string, agent: string, builder: string, reviewer: string, nested: boolean): WorkflowStage {
  const common = {id,kind,depends_on:[],inputs:{}};
  const step = (alias: string) => ({agent:alias,brief:'Describe the work to do.',skills:[]});
  if (kind === 'agent') return {...common,...step(agent),access:'read',output:'text'};
  if (kind === 'repair') return {...common,build:step(builder),review:step(reviewer),fix:step(builder),check_ids:[],max_fixes:3};
  if (kind === 'check') return {...common,check_ids:[],snapshot:{literal:null}};
  if (kind === 'gate') return {...common,prompt:'Review the work. Approve completion?'};
  if (kind === 'foreach' && !nested) return {...common,source:{literal:null},max_units:16,width:1,body:[]};
  return {id,kind:'report',depends_on:[]};
}

export function workflowDrafts(content: string): {definition: WorkflowDefinition; definition_ref?: {id: string; revision: WorkflowRecord['revision']}}[] {
  const drafts: ReturnType<typeof workflowDrafts> = [];
  for (const match of content.matchAll(/```(?:json|workflow-definition)?\s*\n([\s\S]*?)```/g)) {
    try { const value = JSON.parse(match[1]), definition = value.definition ?? value;
      if (editableDefinition(definition) && (!value.definition_ref || typeof value.definition_ref.id === 'string' && workflowInteger(value.definition_ref.revision))) drafts.push({definition, ...(value.definition_ref ? {definition_ref:value.definition_ref} : {})});
    } catch { /* Ordinary prose and incomplete JSON are not definitions. */ }
  }
  return drafts;
}

/** Presentation shape guard only. Bob still validates all schema/graph/authority rules. */
export function editableDefinition(value: unknown): value is WorkflowDefinition {
  const object = (v: any) => !!v && typeof v === 'object' && !Array.isArray(v);
  const strings = (v: any) => Array.isArray(v) && v.every(x=>typeof x==='string');
  const reference = (v: any) => object(v) && (typeof v.input === 'string' || typeof v.stage === 'string' && typeof v.output === 'string' || v.item === true || 'literal' in v);
  const step = (v: any) => object(v) && typeof v.agent==='string' && typeof v.brief==='string' && Array.isArray(v.skills);
  const stages = (rows: any): boolean => Array.isArray(rows) && rows.every((s: any)=>object(s) && typeof s.id==='string' && strings(s.depends_on) && (!s.inputs || object(s.inputs) && Object.values(s.inputs).every(reference)) && (
    s.kind==='agent' ? step(s) && typeof s.access==='string' && typeof s.output==='string' :
    s.kind==='repair' ? step(s.build) && step(s.review) && step(s.fix) && strings(s.check_ids) && Number.isInteger(s.max_fixes) :
    s.kind==='foreach' ? reference(s.source) && Number.isInteger(s.max_units) && Number.isInteger(s.width) && stages(s.body) :
    s.kind==='check' ? strings(s.check_ids) && reference(s.snapshot) : s.kind==='gate' ? typeof s.prompt==='string' : s.kind==='report'));
  const d=value as any;
  return object(d) && d.schema==='workflow/v1' && typeof d.name==='string' && typeof d.summary==='string' && object(d.agents) && Object.values(d.agents).every((a:any)=>object(a) && typeof a.face_id==='string' && (a.binding_id===null || typeof a.binding_id==='string')) && object(d.inputs) && Object.values(d.inputs).every((i:any)=>object(i) && ['string','evidence_ids'].includes(i.type) && typeof i.required==='boolean') && object(d.workspace) && typeof d.workspace.required==='boolean' && object(d.authority) && strings(d.authority.workspace_ops) && Array.isArray(d.authority.checks) && d.authority.checks.every((c:any)=>object(c) && typeof c.id==='string' && typeof c.command==='string' && typeof c.cwd==='string' && Number.isInteger(c.timeout_seconds)) && object(d.budget) && ['calls','active_seconds','job_seconds','max_job_attempts'].every(k=>Number.isInteger(d.budget[k])) && typeof d.review_policy==='string' && stages(d.stages);
}
