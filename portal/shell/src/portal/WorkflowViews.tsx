import { createContext, useContext, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import type { Catalog } from './portal-protocol';
import { isDefiniteRefusal, isRateLimited, RATE_LIMIT_WRITE_LINE, toPortalError } from './portal-protocol';
import { executionRefused, mutationDone, mutationKey, pendingMutation } from './durable-runs';
import { BuilderApprovalCard } from './BuilderApprovalCard';
import { useOwnerAction } from './OwnerControls';
import { useExperience } from './experience';
import type { WorkflowProposal, WorkflowRun, WorkflowStage, WorkflowStart, WorkflowWait } from './workflow-types';
import { workflowEnded, workflowTerminal } from './workflow-types';
import { WorkflowStore, type ProposalSource } from './workflow-store';

type WorkflowContextValue = { store: WorkflowStore; catalog: Catalog | null; onOpen: (id: string) => void; onFresh: (id: string | null, text: string) => void; pc: (id: string | null) => ReactNode; onAnswered?: (conversationId: string | null, questionId: string) => void };
export const WorkflowContext = createContext<WorkflowContextValue | null>(null);
export const useWorkflows = (store: WorkflowStore) => useSyncExternalStore(store.subscribe, store.snapshot, store.snapshot);
const REASONS: Record<string, string> = {
  review_policy_requires_explicit_same_model_choice: 'Only one model is set up, so the review would be done by the same model that did the work',
  canonical_model_identity_unavailable: 'Bob cannot confirm that these are different models',
  workflow_turn_adapter_unavailable: 'This model cannot run workflow stages in this build',
  execution_storage_unavailable: "Bob's run storage is not available right now",
  'workflow runner not installed in this build': 'This build cannot run workflows yet',
  owner_stopped: 'You stopped the run', owner_rejected: 'You did not approve it', unit_blocked: 'A planned item could not be finished',
};
const plain = (s: string) => REASONS[s] ?? s.replaceAll('_', ' ').replace(/\.$/, '');
const duration = (seconds: number | string) => { const n = Number(seconds); if (!Number.isFinite(n)) return `${seconds} seconds`; const h = Math.floor(n / 3600), m = Math.round((n % 3600) / 60); return n < 60 ? `${n} seconds` : [h ? `${h} hour${h === 1 ? '' : 's'}` : '', m ? `${m} minute${m === 1 ? '' : 's'}` : ''].filter(Boolean).join(' '); };
const agentName = (s: string) => ({ assistant: 'Assistant', planner: 'Planner', orchestrator: 'Orchestrator', builder: 'Builder', reviewer: 'Reviewer', fixer: 'Fixer' }[s] ?? plain(s));
const problem = (e: unknown) => { const t = toPortalError(e); return isRateLimited(t) ? RATE_LIMIT_WRITE_LINE : t.code === 'proposal_stale' ? 'This proposal is out of date. Ask the agent for a fresh one.' : executionRefused(t.code) || isDefiniteRefusal(t) ? 'This action was not applied. Review the current details and try again.' : 'The reply was lost, so the outcome is unknown. Reconnect to inspect it, or retry the same saved request.'; };
const kindName = (kind: string) => ({agent: 'Agent work', repair: 'Build, review and fix', foreach: 'Work on each planned item', check: 'Run checks', gate: 'Your review', report: 'Report'}[kind] ?? plain(kind));
export function orderedStages(stages: WorkflowStage[]): WorkflowStage[] {
  const result: WorkflowStage[] = [], done = new Set<string>();
  while (result.length < stages.length) { const next = stages.filter(s => !done.has(s.id) && s.depends_on.every(id => done.has(id))).sort((a,b) => a.id.localeCompare(b.id))[0]; if (!next) break; result.push(next); done.add(next.id); }
  return result;
}
function ProposalStages({ stages, proposal }: { stages: WorkflowStage[]; proposal: WorkflowProposal }) {
  const c = proposal.coverage;
  const binding = (alias: string) => { const b = c.bindings[alias]; return `${agentName(c.definition.agents[alias]?.face_id ?? alias)} using ${b?.model ?? 'a model that has not been reported'}`; };
  return <ol>{orderedStages(stages).map((s, i) => <li key={s.id}>
    <strong>{i + 1}. {kindName(s.kind)}.</strong>{s.agent && <span> {binding(s.agent)}.</span>}
    {s.kind === 'repair' && <p>Build: {s.build ? binding(s.build.agent) : 'Not reported'}. Review: {s.review ? binding(s.review.agent) : 'Not reported'}. Fix: {s.fix ? binding(s.fix.agent) : 'Not reported'}.</p>}
    {s.kind === 'gate' && <p>{s.prompt}</p>}{s.body && <ProposalStages stages={s.body} proposal={proposal} />}
  </li>)}</ol>;
}

export function WorkflowProposalCard({ id, source, taskShown = false }: { id: string; source: ProposalSource; taskShown?: boolean }) {
  const context = useContext(WorkflowContext);
  if (!context) return <p>Open this proposal in a connected conversation to review it.</p>;
  return <ProposalCard {...context} id={id} source={source} taskShown={taskShown} />;
}
function ProposalCard({ store, catalog, onFresh, id, source, taskShown }: WorkflowContextValue & { id: string; source: ProposalSource; taskShown?: boolean }) {
  useWorkflows(store);
  const owner = useOwnerAction(store.client, catalog, false, `${store.scope}:${id}`);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [stale, setStale] = useState(false), [replacement, setReplacement] = useState<string | null>(null);
  const lock = useRef(false), live = useRef(true);
  const entry = store.proposals.get(replacement ?? id), p = entry?.proposal;
  const actualSource = entry?.source ?? source;
  const simple = useExperience() === 'simple';
  useEffect(() => { live.current = true; void Promise.all([store.loadCatalog(), store.proposal(id, source)]).catch(e => { if (live.current) setError(problem(e)); }); return () => { live.current = false; }; }, [store, id]);
  useEffect(() => { if (!p || p.state !== 'proposed') return; const timer = setTimeout(() => setStale(true), Math.max(0,Math.min(2147483647,Date.parse(p.expires_at)-Date.now()))); return () => clearTimeout(timer); },[p?.id,p?.expires_at,p?.state]);
  const outdated = stale || !!p && (p.state === 'expired' || Date.parse(p.expires_at) <= Date.now() || BigInt(p.revision) !== BigInt(actualSource.revision) || p.digest !== actualSource.digest);
  const operation = `workflow-start:${p?.id ?? id}`;
  const saved = pendingMutation<Omit<WorkflowStart, 'request_key'>>(store.scope, operation);
  async function changeMode(mode: 'auto' | 'approve') {
    if (!p || lock.current || saved) return; lock.current = true; setBusy(true); setError('');
    const op = `workflow-mode:${p.id}`;
    try {
      const c = p.coverage;
      const payload = { ...(p.definition_ref ? {definition_ref: p.definition_ref} : {definition: c.definition}), inputs: c.inputs,
        bindings: Object.fromEntries(Object.entries(c.bindings).filter(([alias]) => !c.definition.agents[alias]?.binding_id).map(([alias,b]) => [alias,b.binding_id])),
        project_id: c.project_id, origin_conversation_id: actualSource.conversationId, expires_at: p.expires_at, run_mode: mode,
        ...(c.workspace?.create ? {workspace: {create: c.workspace.create}} : {}) };
      const {proposal} = await store.client.workflowPropose({...payload, request_key: mutationKey(store.scope,op,payload)});
      mutationDone(store.scope,op);
      store.proposals.delete(p.id); store.proposals.set(proposal.id, {proposal, source: {...actualSource, revision: proposal.revision, digest: proposal.digest}}); store.changed(); setReplacement(proposal.id);
    } catch (e) { if (executionRefused(toPortalError(e).code)) mutationDone(store.scope,op); if (live.current) setError(problem(e)); }
    finally { lock.current = false; if (live.current) setBusy(false); }
  }
  async function start(allow: boolean) {
    if (!p || lock.current || outdated || !store.catalog?.available) return;
    lock.current = true; setBusy(true); setError('');
    try {
      const current = (await store.client.workflowProposal(p.id)).proposal;
      if (current.run_id) { await store.readRun(current.run_id); store.proposals.set(p.id,{proposal: current,source:actualSource}); store.changed(); mutationDone(store.scope,operation); return; }
      if (BigInt(current.revision) !== BigInt(p.revision) || current.digest !== p.digest || current.state !== 'proposed' || Date.parse(current.expires_at) <= Date.now()) { setStale(true); return; }
      const payload = saved ?? {proposal_id: p.id, proposal_revision: p.revision, proposal_digest: p.digest, allow_same_model_review: allow};
      const request_key = mutationKey(store.scope, operation, payload);
      const result = await owner.run('workflows.grant_start', {proposal_id: p.id}, () => store.client.workflowStart({...payload, request_key}), undefined, 'Confirm Start');
      store.putRun(result.run); store.proposals.set(p.id,{proposal:{...p,state:'granted',run_id:result.run.id},source:actualSource}); store.changed(); mutationDone(store.scope,operation);
    } catch (e) {
      const t = toPortalError(e); if (t.code === 'proposal_stale') setStale(true);
      if (executionRefused(t.code) || isDefiniteRefusal(t) || t.code === 'owner_cancelled') mutationDone(store.scope,operation);
      if (live.current) setError(problem(e));
    } finally { lock.current = false; if (live.current) setBusy(false); }
  }
  if (p?.run_id) return <WorkflowRunView id={p.run_id} />;
  const c = p?.coverage, choice = c?.review.same_model_choice_required;
  const blocked = !p || !store.catalog || !store.catalog.available || (!p.runnable && (!choice || p.reasons.some(r => r !== 'review_policy_requires_explicit_same_model_choice' && r !== 'Files from the earlier run are still in this workspace'))) || outdated || busy;
  return <section className="bob-action-card workflow-proposal" aria-label="Workflow proposal">
    {owner.dialog}<h3>{c?.definition.name ?? 'Workflow proposal'}</h3><span className="preview-tag" title="Workflows are a preview in 0.99. A run may stop before it finishes. Chat, agents and Builder are not affected.">Preview</span><p>Proposed by {actualSource.name}.</p>
    {!p && !error && <p>Reading the proposal for review.</p>}
    {p && c && <>
      {!taskShown && <p className="action-text">{String(c.inputs.task ?? '')}</p>}<p>{c.definition.summary}</p>
      <p>{c.workspace?.create ? `A new workspace named ${c.workspace.create} will be created when you start.` : c.workspace?.path_label ? `Work will take place in the existing workspace: ${c.workspace.path_label}.` : 'The workspace has not been reported.'}</p>
      <ProposalStages stages={c.definition.stages} proposal={p} />
      <p>Review policy: {({any:'Any model may review the work',different_model:'A different model must review the work',different_family:'A different model family must review the work'}[c.review.policy] ?? plain(c.review.policy))}. {c.review.policy_met ? 'This policy can be met.' : 'This policy cannot be met. Review would use the same model.'}</p>
      {c.checks.length ? <><p>The following checks will run.</p><ul>{c.checks.map(check => <li key={check.id}>{check.command} (in {check.cwd || 'the workspace root'}, up to {check.timeout_seconds} seconds).</li>)}</ul></> : <p>No deterministic checks will run.</p>}
      <p>The budget is {c.budget.calls} model calls, up to {duration(c.budget.active_seconds)} of active work, up to {duration(c.budget.job_seconds)} of check time and {c.budget.max_job_attempts} check attempts.</p>
      <p>{agentName((c.assistant_binding as {face_id?: string} | undefined)?.face_id ?? 'assistant')}&apos;s summary: {c.assistant_summary_calls} {c.assistant_summary_calls === 1 ? 'call' : 'calls'}. {c.assistant_summary_calls ? 'This is included in the budget and will be skipped if the budget is spent.' : 'This run does not include a summary call.'}</p>
      <p>You will still be asked about questions, approvals required by this mode, owner review gates and recovery after an interruption.</p>
      <label>Run mode <select aria-label="Workflow run mode" disabled={busy || !!saved || outdated} value={c.run_mode} onChange={e => void changeMode(e.target.value as 'auto'|'approve')}>
        {c.run_modes.map(mode => <option key={mode.id} value={mode.id}>{mode.label}</option>)}
      </select></label>
      <p>{c.run_mode === 'auto' ? 'Work can proceed without asking inside this workspace. Actions that still require approval will ask.' : 'Each change will ask for your approval.'}</p>
      {!store.catalog?.available && <p role="status">Workflows are unavailable. {store.catalog?.reasons.map(plain).join('. ') || 'Availability has not been confirmed.'}</p>}
      {p.reasons.length > 0 && <p>{p.reasons.map(plain).join('. ')}.</p>}
      {outdated ? <><p>This proposal is out of date. Nothing has started.</p><button onClick={() => onFresh(actualSource.conversationId, 'Please make a fresh workflow proposal for this task: ' + String(c.inputs.task ?? ''))}>Ask the agent for a fresh proposal</button></> :
        <button disabled={blocked} onClick={() => void start(!!choice)}>{saved ? 'Retry Start with the saved request' : choice ? 'Start anyway with same-model review' : 'Start'}</button>}
      {!simple && <details><summary>Details</summary><pre>{JSON.stringify(p,null,2)}</pre></details>}
    </>}{error && <p role="alert">{error}</p>}
  </section>;
}

const runState = (s: string) => ({accepted:'Accepted. No work has been reported yet.', queued:'Waiting to begin.', running:'Work is in progress.', paused:'Paused.', pausing:'Pausing at a safe boundary.', stopping:'Stopping. Cleanup has not yet been confirmed.', completed:'Completed.', stopped:'Stopped.', failed:'Failed.', over_budget:'The budget was exceeded.', interrupted:'Interrupted. The unfinished stage needs your decision.', expired:'The permission or budget has expired.', pending:'Waiting to begin.', ready:'Ready to begin.', succeeded:'Finished successfully.', recovering:'Recovering the unfinished stage.', blocked:'Blocked.', unknown:'The outcome is unknown.', skipped:'This stage was skipped.', waiting:'Waiting for your response.', needs_you:'Waiting for your response.'}[s] ?? `${plain(s)}.`);
function WorkflowWaitControl({ wait, run }: { wait: WorkflowWait; run: WorkflowRun }) {
  const context = useContext(WorkflowContext)!; const {store, catalog, onOpen, pc} = context;
  const owner = useOwnerAction(store.client,catalog,false,`${store.scope}:${run.id}:${wait.wait_id}`);
  const [answer,setAnswer] = useState(''), [busy,setBusy] = useState(false), [error,setError] = useState(''), [settled,setSettled] = useState(false);
  const lock = useRef(false);
  useEffect(() => { setSettled(false); },[wait.state,wait.wait_id]);
  async function decide(decision?: 'approve'|'reject') {
    if (lock.current) return; lock.current = true; setBusy(true); setError(''); const op = `workflow-wait:${wait.wait_id}`;
    try {
      if (wait.kind === 'question') {
        const result = await store.client.answerQuestion(wait.record_id,answer);
        if (!result.resolved) { setError('The turn has moved on. This answer was not applied.'); return; }
        setSettled(true); store.settleRecord('question',wait.record_id,'answered'); context.onAnswered?.(wait.conversation_id,wait.record_id);
      }
      else if (wait.kind === 'gate') {
        const saved = pendingMutation<{run_id:string;wait_id:string;expected_revision:WorkflowRun['revision'];decision:'approve'|'reject'}>(store.scope,op);
        if (saved && saved.decision !== decision) { setError('The earlier decision has an unknown outcome. Retry that same decision or reconnect before changing it.'); return; }
        const payload = saved ?? {run_id:run.id,wait_id:wait.wait_id,expected_revision:run.revision,decision:decision!};
        const result = await owner.run('workflows.gate',{run_id:run.id,wait_id:wait.wait_id},() => store.client.workflowGate({...payload,request_key:mutationKey(store.scope,op,payload)})); store.putRun(result.run);
      } else if (wait.kind === 'recovery') {
        const payload = pendingMutation<{run_id:string;wait_id:string;expected_revision:WorkflowRun['revision']}>(store.scope,op) ?? {run_id:run.id,wait_id:wait.wait_id,expected_revision:run.revision};
        const result = await owner.run('workflows.resume',{run_id:run.id},() => store.client.workflowResume({...payload,request_key:mutationKey(store.scope,op,payload)})); store.putRun(result.run);
      }
      mutationDone(store.scope,op);
    } catch (e) { if (executionRefused(toPortalError(e).code)) mutationDone(store.scope,op); setError(problem(e)); }
    finally { lock.current = false; setBusy(false); }
  }
  if (wait.state !== 'open') return null;
  return <section className="workflow-wait" aria-label="Workflow needs you"><p>{wait.prompt}</p>{owner.dialog}
    {wait.kind === 'question' && (settled ? <p>Your answer was sent. Waiting for the run to continue.</p> : <><label>Your answer <input aria-label="Workflow answer" value={answer} onChange={e => setAnswer(e.target.value)} /></label><button disabled={busy || !answer.trim()} onClick={() => void decide()}>Send answer</button></>)}
    {wait.kind === 'gate' && <><button disabled={busy} onClick={() => void decide('approve')}>Approve this stage</button><button disabled={busy} onClick={() => void decide('reject')}>Reject this stage</button></>}
    {wait.kind === 'recovery' && <button disabled={busy} onClick={() => void decide()}>Resume this stage</button>}
    {wait.kind === 'builder_approval' && wait.conversation_id && <BuilderApprovalCard client={store.client} catalog={catalog} conversationId={wait.conversation_id} approvalId={wait.record_id} scope={store.scope} />}
    {wait.kind === 'pc' && pc(wait.conversation_id)}
    {wait.conversation_id && <button onClick={() => onOpen(wait.conversation_id!)}>Open the agent conversation</button>}
    {wait.expires_at && <p>This request expires at {new Date(wait.expires_at).toLocaleString()}.</p>}{error && <p role="alert">{error}</p>}
  </section>;
}

export function RunAgainControl({store, runId}: {store: WorkflowStore; runId: string}) {
  useWorkflows(store);
  const lock = useRef(false), live = useRef(true);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [fresh, setFresh] = useState<string | null>(null);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, [store, runId]);
  async function again() {
    if (lock.current || !store.proposeAgainAdvertised) return;
    lock.current = true; setBusy(true); setError('');
    try { const id = await store.proposeEndedRun(runId); if (live.current) setFresh(id); }
    catch (e) {
      const t = toPortalError(e), reason = t.detail;
      const sentences: Record<string, string> = {
        proposal_not_expired: 'The server only allows expired proposals to be asked for again. This run has already used its proposal.',
        proposal_not_found: 'The original proposal is no longer on record.',
        workflow_deleted: 'This workflow has been deleted.',
        proposal_again_not_permitted: 'Only the signed-in owner can ask for this proposal again.',
        proposal_recheck_failed: 'The fresh check did not pass, so no new proposal was made.',
        workspace_exists: 'A workspace with that name already exists. The server must reuse the workspace from the first run.',
      };
      if (live.current) setError(sentences[reason] ?? sentences[t.code] ?? reason.replaceAll('_', ' '));
    } finally { lock.current = false; if (live.current) setBusy(false); }
  }
  const entry = fresh ? store.proposals.get(fresh) : null;
  return <div className="workflow-run-again">
    {!entry && <button type="button" disabled={busy || !store.proposeAgainAdvertised} onClick={() => void again()}>Run it again</button>}
    {!store.proposeAgainAdvertised && <p>This server does not offer workflow re-proposal.</p>}
    {error && <p role="status">{error}</p>}
    {entry && <><p>Review this fresh proposal, then press Start when you are ready.</p><WorkflowProposalCard id={entry.proposal.id} source={entry.source} /></>}
  </div>;
}

export function WorkflowRunView({id}: {id: string}) {
  const context = useContext(WorkflowContext)!; const {store,catalog,onOpen} = context;
  useWorkflows(store); const r = store.runs.get(id);
  const owner = useOwnerAction(store.client,catalog,false,`${store.scope}:${id}`);
  const simple = useExperience() === 'simple';
  const [error,setError] = useState(''), [busy,setBusy] = useState(false), [text,setText] = useState(''), [stage,setStage] = useState(''), [artifact,setArtifact] = useState('');
  const lock = useRef(false);
  useEffect(() => { const release = store.view(id); if (!store.runs.has(id)) void store.readRun(id).catch(e => setError(problem(e))); return release; },[store,id]);
  async function action(kind: 'pause'|'resume'|'stop'|'message') {
    if (!r || lock.current) return; lock.current = true; setBusy(true); setError(''); const op = `workflow-${kind}:${id}`;
    try {
      const payload = kind === 'stop' ? {run_id:id} : kind === 'message' ? {run_id:id,stage_key:stage || r.stages[0]?.key,text} : {run_id:id,expected_revision:r.revision};
      const saved = pendingMutation<typeof payload>(store.scope,op) ?? payload;
      const request_key = mutationKey(store.scope,op,saved);
      if (kind === 'message') { await store.client.workflowMessage({...saved,request_key} as Parameters<typeof store.client.workflowMessage>[0]); setText(''); }
      else {
        const result = kind === 'stop' ? await store.client.workflowStop(id,request_key) : kind === 'pause' ? await store.client.workflowPause({...saved,request_key} as Parameters<typeof store.client.workflowPause>[0]) : await owner.run('workflows.resume',{run_id:id},() => store.client.workflowResume({...saved,request_key} as Parameters<typeof store.client.workflowResume>[0]));
        store.putRun(result.run);
      } mutationDone(store.scope,op);
    } catch (e) { if (executionRefused(toPortalError(e).code) || isDefiniteRefusal(toPortalError(e))) mutationDone(store.scope,op); setError(problem(e)); }
    finally {lock.current = false;setBusy(false);}
  }
  async function fullReport() {
    if (!r?.report || lock.current) return; lock.current = true; setBusy(true);
    try { let offset: number | null = 0, content = ''; const seen = new Set<number>();
      while (offset !== null) { if (seen.has(offset)) throw new Error('Repeated artifact page.'); seen.add(offset); const a = await store.client.workflowArtifact(id,r.report.artifact_id,offset); if (a.encoding !== 'utf8' || a.digest !== r.report.digest) throw new Error('The report could not be verified.'); content += a.content; offset = a.next_offset; } setArtifact(content);
    } catch (e) {setError(problem(e));} finally {lock.current = false;setBusy(false);}
  }
  if (!r) return <section className="workflow-run"><p>Reading the workflow run.</p>{error && <p role="alert">{error}</p>}</section>;
  const stale = store.stale.has(id), terminal = workflowTerminal(r.status) && r.status !== 'interrupted';
  const proposal = [...store.proposals.values()].find(p => p.proposal.run_id === id)?.proposal;
  return <section className="workflow-run bob-action-card" aria-label="Workflow run"><h3>{r.name}</h3><span className="preview-tag" title="Workflows are a preview in 0.99. A run may stop before it finishes. Chat, agents and Builder are not affected.">Preview</span>{owner.dialog}
    {stale && <p role="alert">This view is out of date. Observation ended; this does not mean the run stopped. <button disabled={busy} onClick={() => void store.readRun(id,true).catch(e => setError(problem(e)))}>Reconnect to this run</button></p>}
    <p role="status">{runState(r.status)}</p>{r.status === 'accepted' && r.reason === 'workflow runner not installed in this build' && <p>This run was accepted, but no work was executed.</p>}{r.reason && <p>{plain(r.reason)}.</p>}
    <p>Last reported usage: {r.budget.consumed_calls} of {r.budget.limit.calls} model calls used{r.budget.estimated ? ' (estimated)' : ''}; {r.budget.reserved_calls} reserved and {r.budget.unknown_calls} with unknown usage. {r.budget.active_seconds} of {r.budget.limit.active_seconds} active seconds used. {r.budget.job_seconds} of {r.budget.limit.job_seconds} check seconds used.</p>
    {r.review?.downgraded && <p>You chose review by the same model.</p>}
    <ol>{[...r.stages].sort((a,b) => a.ordinal-b.ordinal).map((s,i) => <li key={s.key}>
      <strong>Stage {i+1}: {kindName(s.kind)}.</strong> {runState(s.state)}
      <p>{s.actual_models.length ? s.actual_models.map(b => `${agentName(s.agent_alias ? proposal?.coverage.definition.agents[s.agent_alias]?.face_id ?? s.agent_alias : b.face_ids[0] ?? 'Agent')} using ${b.model}`).join('; ') + '.' : 'The executing agent and model have not been reported yet.'}</p>
      {s.reason && <p>{plain(s.reason)}</p>}{s.conversation_id && <button onClick={() => onOpen(s.conversation_id!)}>Open in Agents</button>}
    </li>)}</ol>
    {r.needs_you.map(w => <WorkflowWaitControl key={w.wait_id} wait={w} run={r} />)}
    {!terminal && <div className="workflow-controls"><button disabled={busy} onClick={() => void action('stop')}>Stop run</button><button disabled={busy || r.status === 'stopping'} onClick={() => void action(r.status === 'paused' || r.status === 'interrupted' ? 'resume' : 'pause')}>{r.status === 'paused' || r.status === 'interrupted' ? 'Resume run' : 'Pause run'}</button>
      <label>Message for a stage <select aria-label="Workflow message stage" value={stage || r.stages[0]?.key || ''} onChange={e => setStage(e.target.value)}>{r.stages.map((s,i) => <option key={s.key} value={s.key}>Stage {i+1}: {kindName(s.kind)}</option>)}</select><textarea aria-label="Workflow message" maxLength={12000} value={text} onChange={e => setText(e.target.value)} /></label><button disabled={busy || !text.trim()} onClick={() => void action('message')}>Send message to stage</button></div>}
    {workflowEnded(r, r) && <RunAgainControl store={store} runId={id} />}
    {error && <p role="alert">{error}</p>}
    {!simple && <details><summary>Details</summary><pre>{JSON.stringify(r,null,2)}</pre></details>}
    {r.report && <article className="workflow-report" aria-label="Workflow report"><h4>Workflow report</h4><p>This report was recorded by the workflow service.</p><p className="action-text">{r.report.summary}</p>
      {r.report.units.map((u,i) => <p key={u.id}>Item {i+1}: {plain(u.verdict)}. {u.reason}</p>)}
      <p>{r.report.checks.length ? `Checks recorded: ${r.report.checks.length}.` : 'No deterministic checks ran.'}</p>{r.report.caveats.map((c,i) => <p key={i}>{c}</p>)}
      <details><summary>Details</summary><button disabled={busy} onClick={() => void fullReport()}>Read the full report</button>{artifact && <pre>{artifact}</pre>}{!simple && <pre>{JSON.stringify(r.report,null,2)}</pre>}</details>
    </article>}
  </section>;
}

export function WorkflowConversation({conversationId}: {conversationId: string}) {
  const c = useContext(WorkflowContext)!; useWorkflows(c.store);
  const cardRunIds = new Set([...c.store.proposals.values()].filter(p => p.source.conversationId === conversationId).map(p => p.proposal.run_id));
  return <>{[...c.store.summaries.values()].filter(r => r.origin_conversation_id === conversationId && !cardRunIds.has(r.id)).map(r => <WorkflowRunView key={r.id} id={r.id} />)}</>;
}
export function WorkflowNeeds() {
  const c = useContext(WorkflowContext)!; useWorkflows(c.store);
  return <section aria-label="Workflows needing you"><h2>Workflows needing you</h2>
    {c.store.proposalsAdvertised && <button onClick={() => void c.store.refreshProposals()}>Refresh workflow proposals</button>}
    {c.store.error && <p role="alert">Pending proposals could not be refreshed. Try refreshing again.</p>}
    {[...c.store.proposals].filter(([,p]) => p.proposal.state === 'proposed').map(([id,p]) => <WorkflowProposalCard key={id} id={id} source={p.source} />)}
    {[...c.store.runs.values()].filter(r => r.needs_you.some(w => w.state === 'open')).map(r => <section key={r.id}><h3>{r.name}</h3><button onClick={() => c.onOpen(r.origin_conversation_id ?? '')}>Open the run conversation</button><WorkflowRunView id={r.id} /></section>)}
  </section>;
}
export function WorkflowRuns() {
  const c = useContext(WorkflowContext)!; useWorkflows(c.store); const [selected,setSelected] = useState<string | null>(null), [error,setError] = useState('');
  useEffect(() => { void c.store.discover().catch(e => setError(problem(e))); },[c.store]);
  return <section aria-label="Workflow runs"><h2>Workflow runs</h2><button onClick={() => void c.store.discover().catch(e => setError(problem(e)))}>Refresh workflow runs</button>
    {[...c.store.summaries.values()].map(r => <button className="workflow-run-row" key={r.id} onClick={() => setSelected(r.id)}>{r.name}: {runState(r.status)}</button>)}
    {selected && <WorkflowRunView key={selected} id={selected} />}{error && <p role="alert">{error}</p>}
  </section>;
}
