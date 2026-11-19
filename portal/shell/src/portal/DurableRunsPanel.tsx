import { ReadFaultText } from './provider-restart';
import { CheckedRunEvidence } from './CheckedEditsDetail';
import { validCheckedManifest } from './checked-edits';
import { WorkspacePanel } from './WorkspacePanel';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { AcceptanceContract, Catalog, Identity, PortalError, Run, RunBudget, RunDetail, SavedTeam, JobCoverage, JobGrantPolicy } from './portal-protocol';
import { toPortalError, txt } from './portal-protocol';
import { LegacyRunsPanel } from './PortalPanels';
import { acceptanceReason, allTeams, checkedAcceptance, compatibilityError, executionReason, executionRefused, featureReason, ownerEligible, greater, operationReason, LOOP, mutationDone, mutationKey, PAUSE_REASONS, pendingMutation, supportsV2, UPGRADE_REASON } from './durable-runs';
import type { RunDraft } from './durable-runs';
import { RunBudgetPicker } from './RunBudgetPicker';
import { AcceptanceEditor } from './AcceptanceEditor';
import { RunResume } from './RunResume';
import { approvalDone, useOwnerAction } from './OwnerControls';
import { JobTimePicker } from './JobTimePicker';
import { RunJobs, RunFlags, FlagBadge } from './RunFlags';
import { checkedJobBudget, jobGrantReason, jobPolicy, validInteger, customJobLimit } from './durable-runs';
import type { JobTimeProposal } from './durable-runs';
import { ProjectBudget } from './ProjectBudget';

export type RunSeed = { task: string; at: number; team?: string; budget?: RunBudget; evidence_ids?: string[]; acceptance?: AcceptanceContract | string; project_id?: string; conversation_id?: string; job_coverage?: JobCoverage; job_time?: JobTimeProposal };
type Props = { client: PortalClient; catalog?: Catalog | null; scope: string; runs: Run[]; onRuns: (next: Run[]) => void; seed?: RunSeed | null; identity?: Identity | null };
export function RunsPanel(props: Props) {
  if (!supportsV2(props.catalog)) return <><p role="status">{UPGRADE_REASON}</p><LegacyRunsPanel {...props} /></>;
  return <DurableRunsPanel {...props} />;
}

function DurableRunsPanel({ client, catalog, scope, runs, onRuns, seed, identity }: Props) {
  const ownerAction = useOwnerAction(client, catalog);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [teams, setTeams] = useState<SavedTeam[]>([]);
  const [team, setTeam] = useState(seed?.team ?? '');
  const [task, setTask] = useState(seed?.task ?? '');
  const [budget, setBudget] = useState<RunBudget>(seed?.budget ?? { start_calls: 50, wall_mode: 'follow_budget', wall_seconds: 18000 });
  const [acceptance, setAcceptance] = useState<AcceptanceContract>(typeof seed?.acceptance === 'object' ? seed.acceptance : { criteria: [] });
  const proposedJobPolicy = () => seed?.job_time && validInteger(seed.job_time.proposed_seconds) && (greater(seed.job_time.proposed_seconds, 3600) || !catalog?.capabilities.execution_v2?.job_time?.presets_seconds.some(n => String(n) === String(seed.job_time!.proposed_seconds)))
    ? jobPolicy(seed.job_time.proposed_seconds, customJobLimit(seed.job_time.proposed_seconds), true) : undefined;
  const [jobOverride, setJobOverride] = useState<JobGrantPolicy | undefined>(proposedJobPolicy);
  const [pending, setPending] = useState<RunDraft | null>(null);
  const [items, setItems] = useState<Run[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [coverage, setCoverage] = useState('');
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<PortalError | null>(null);
  const [blocked, setBlocked] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  function fault(e: unknown) {
    const typed = toPortalError(e);
    setError(typed);
    if (compatibilityError(typed.code, typed.detail)) setBlocked(UPGRADE_REASON);
  }
  useEffect(() => {
    if (pending) return;
    setJobOverride(proposedJobPolicy()); setTeam(seed?.team ?? ''); setTask(seed?.task ?? '');
    if (seed?.budget) setBudget(seed.budget);
    setAcceptance(typeof seed?.acceptance === 'object' ? seed.acceptance : { criteria: [] });
  }, [seed?.at, seed?.task, seed?.team, seed?.budget]);
  useEffect(() => {
    try {
      const saved = pendingMutation<RunDraft>(scope, 'start');
      if (saved) { setPending(saved); setTeam(saved.team); setTask(saved.task); setBudget(saved.budget); if (typeof saved.acceptance === 'object') setAcceptance(saved.acceptance); }
    } catch (e) { setError(toPortalError(e)); }
  }, [scope]);
  const load = useCallback(async (next?: string) => {
    setBusy(true); setError(null);
    try {
      const page = await client.runsListV2(next);
      if (page.source !== 'server') throw new Error('The broker did not return the server v2 run list.');
      setItems(prior => next ? [...prior, ...page.items.filter(row => !prior.some(p => p.id === row.id))] : page.items);
      setCursor(page.next_cursor); setCoverage(page.coverage);
    } catch (e) { const typed = toPortalError(e); setError(typed); if (compatibilityError(typed.code, typed.detail)) setBlocked(UPGRADE_REASON); }
    finally { setBusy(false); }
  }, [client]);
  useEffect(() => {
    void load();
    void allTeams(client).then(rows => { setTeams(rows); setTeam(prior => prior || rows.find(t => t.runnable)?.id || rows[0]?.id || ''); })
      .catch(e => { const typed = toPortalError(e); setError(typed); if (compatibilityError(typed.code, typed.detail)) setBlocked(UPGRADE_REASON); });
  }, [client, load]);
  function show(result: RunDetail) {
    if (!mounted.current) return;
    setDetail(result);
    onRuns([result.run, ...runs.filter(r => r.id !== result.run.id)]);
    setItems(prior => prior.map(r => r.id === result.run.id ? { ...r, ...result.run } : r));
  }
  async function read(id: string, after = 0, receiptCursor = 0) {
    setBusy(true); setError(null);
    try {
      const result = await client.runGetV2(id, after, receiptCursor);
      if ((after || receiptCursor) && detail?.run.id === id) {
        result.events = [...new Map([...detail.events, ...result.events].map(e => [e.seq, e])).values()];
        result.receipts = [...new Map([...detail.receipts, ...result.receipts].map(r => [JSON.stringify(r), r])).values()];
        result.outputs = [...new Map([...(detail.outputs ?? []), ...(result.outputs ?? [])].map(o => [o.id, o])).values()];
        if (after) result.next_receipt_cursor = detail.next_receipt_cursor;
        if (receiptCursor) result.next_cursor = detail.next_cursor;
      }
      show(result);
    } catch (e) { fault(e); } finally { setBusy(false); }
  }
  async function start() {
    setBusy(true); setError(null);
    try {
      const selected = teams.find(t => t.id === team);
      if (!selected && !pending) throw new Error('Choose a saved team.');
      const payload: RunDraft = pending ?? { contract_version: 2, team, team_revision: selected!.revision, task, budget: checkedJobBudget(budget),
        ...(seed?.job_coverage ? { job_coverage: seed.job_coverage } : {}),
        ...(seed?.project_id ? { project_id: seed.project_id } : {}),
        ...(seed?.conversation_id ? { conversation_id: seed.conversation_id } : {}),
        ...(seed?.evidence_ids ? { evidence_ids: seed.evidence_ids } : {}),
        ...(selected?.topology === LOOP ? { acceptance: checkedAcceptance(acceptance) } : typeof seed?.acceptance === 'string' ? { acceptance: seed.acceptance } : {}) };
      const dispatch = async (draft: RunDraft) => {
        const request_key = mutationKey(scope, 'start', draft); setPending(draft);
        return client.runsStartV2({ ...draft, request_key });
      };
      let result: RunDetail;
      try { result = !pending && (greater(payload.budget.start_calls, 200) || !!jobOverride) && ownerEligible(identity, catalog) ? await ownerAction.approveStart(payload, scope, 'start', dispatch, jobOverride ?? (seed?.job_time && seed.job_coverage ? jobPolicy(seed.job_time.proposed_seconds, customJobLimit(seed.job_time.proposed_seconds), true) : undefined)) : await dispatch(payload); }
      catch (e) {
        if (toPortalError(e).code !== 'confirmation_required' || !ownerEligible(identity, catalog)) throw e;
        mutationDone(scope, 'start'); setPending(null);
        result = await ownerAction.approveStart(payload, scope, 'start', dispatch, jobOverride ?? (seed?.job_time && seed.job_coverage ? jobPolicy(seed.job_time.proposed_seconds, customJobLimit(seed.job_time.proposed_seconds), true) : undefined));
      }
      mutationDone(scope, 'start'); approvalDone(scope, 'start'); setPending(null); show(result);
      setNotice(`Run ${result.run.id}: ${result.run.status}. Completion requires a terminal server receipt.`);
      await load();
    } catch (e) {
      const typed = toPortalError(e);
      // These are pre-admission refusals. Unknown transport/HTTP errors retain
      // the frozen request; an explicit retry sends its original key only.
      if (executionRefused(typed.code)) {
        mutationDone(scope, 'start'); setPending(null);
      }
      fault(e);
    } finally { setBusy(false); }
  }
  async function cancel(id: string) {
    setBusy(true); setError(null);
    try {
      const op = `cancel:${id}`;
      const result = await client.runCancelV2(id, mutationKey(scope, op, { run_id: id }));
      mutationDone(scope, op); show(result);
      setNotice(`Cancellation: ${result.run.status}.`);
    } catch (e) { fault(e); } finally { setBusy(false); }
  }
  const selected = teams.find(t => t.id === team);
  const reason = blocked || executionReason(catalog);
  const startReason = (selected?.topology === LOOP ? featureReason(catalog, 'loop') || acceptanceReason(acceptance) : '') ||
    (!pending && ownerEligible(identity, catalog) && greater(budget.start_calls, 200) ? operationReason(catalog, 'grants.prepare') || operationReason(catalog, 'grants.issue') : '');
  const jobStartReason = jobOverride ? jobGrantReason(catalog) || (!seed?.job_coverage ? 'Job override requires agent-proposed job coverage.' : '') || (jobOverride.job_override_seconds != null && !validInteger(jobOverride.job_override_seconds) ? 'Use a positive integral job limit.' : '') : '';
  const current = detail?.run;
  return <section aria-label="Runs" className="portal-panel-body">
    {ownerAction.dialog}
    <h2>Server team runs</h2><p className="subtle">{coverage || 'Authorized durable runs from any PC. Legacy portal history is separate.'}</p>
    <button disabled={busy} onClick={() => void load()}>Reload history</button>
    {error && <div role="alert"><ReadFaultText client={client} error={error} fallback={<><code>{error.code}</code> {error.detail} {error.hint}</>} /></div>}
    {reason && <p role="status">{reason}</p>}
    {items.map(row => <article className="run-row" key={row.id}><strong>{row.id} · {row.status} · {row.phase}</strong><p>{row.team} · {row.task_excerpt}</p><p>{row.calls_used} / {row.effective_call_limit} calls</p><FlagBadge run={row} /><button disabled={busy} onClick={() => void read(row.id)}>Read from server</button></article>)}
    {cursor && <button disabled={busy} onClick={() => void load(cursor)}>More runs</button>}
    <h3>Start a run</h3>
    <form onSubmit={e => { e.preventDefault(); void start(); }}>
      <label>Team <select aria-label="Run team" value={team} disabled={busy || !!pending} onChange={e => setTeam(e.target.value)}><option value="">Choose saved team</option>{teams.map(t => <option key={t.id} value={t.id} disabled={t.topology === LOOP && !!featureReason(catalog, 'loop')}>{t.id} · {t.topology} · saved · {t.runnable ? 'runnable' : 'blocked'}</option>)}</select></label>
      {selected && <p>Saved revision {selected.revision} · {selected.eligibility} {selected.blocked_reasons.join(', ')}</p>}
      <label>Task <textarea aria-label="Run task" value={task} disabled={busy || !!pending} onChange={e => setTask(e.target.value)} /></label>
      {selected?.topology === LOOP && <AcceptanceEditor value={acceptance} onChange={setAcceptance} disabled={busy || !!pending} />}
      {typeof (pending?.acceptance ?? seed?.acceptance) === 'string' && <p>Acceptance: {String(pending?.acceptance ?? seed?.acceptance)}</p>}
      {startReason && <p role="status">{startReason}</p>}
      <RunBudgetPicker budget={budget} onChange={setBudget} disabled={busy || !!pending} growthReason={selected?.topology === LOOP ? featureReason(catalog, 'growth') : undefined} />
      <JobTimePicker catalog={catalog} proposal={seed?.job_time} policy={jobOverride} onPolicy={setJobOverride} budget={budget} onBudget={setBudget} disabled={busy || !!pending} owner={ownerEligible(identity, catalog)} />
      {jobStartReason && <p role="status">{jobStartReason}</p>}
      {greater(budget.start_calls, catalog?.capabilities.execution_v2?.confirmation_threshold ?? 200) && <p role="status">Above {catalog?.capabilities.execution_v2?.confirmation_threshold ?? 200} needs your OK. Server reason: {catalog?.capabilities.execution_v2?.above_threshold_reason || 'confirmation_required'}. Review the approval coverage before starting.</p>}
      {pending && <p role="status">Previous start outcome unknown. Retry uses the saved request key and original details.</p>}
      <button disabled={busy || !!reason || (!pending && (!!startReason || !!jobStartReason || !selected?.runnable || !task.trim()))}>{pending ? 'Reconcile start' : 'Start run'}</button>
    </form>
    {notice && <p role="status">{notice}</p>}
    {current && <article className="run-row" aria-label="Run detail">
      <h3>{current.id} · {current.status} · {current.phase}</h3><p>{current.task}</p>
      <p>Requested {current.requested_start_calls} calls · effective {current.effective_call_limit} · used {current.calls_used} / {current.effective_call_limit} · reserved {current.reserved_calls} · unknown {current.unknown_calls}</p>
      <p>Requested wall: {current.budget?.wall_mode} · {current.budget?.wall_seconds ?? 'No time limit'} · effective deadline: {current.deadline ?? 'No time limit'}</p>
      <p>Rough estimate: {current.estimated_remaining_calls ?? 'not reported'} remaining calls · basis: {current.remaining_calls_basis === 'unmet_required_criteria_proxy_excludes_planning_may_share_one_call' ? 'unmet required criteria; excludes planning; several criteria may share one call' : current.remaining_calls_basis === 'four_step_topology' ? 'remaining four-step calls' : current.remaining_calls_basis ?? 'not reported'} · review: {current.review_independence}</p>
      {(current.pause_reason || current.reason) && <p>Pause / waiting reason: {PAUSE_REASONS[current.pause_reason ?? current.reason ?? ''] ?? current.pause_reason ?? current.reason}</p>}
      <RunJobs run={current} events={detail.events} />
      <CheckedRunEvidence detail={detail} />
      <WorkspacePanel key={JSON.stringify([scope,current.id,identity?.token_type,identity?.actor,identity?.owner])} client={client.jobs} portalClient={client} catalog={catalog} identity={identity} scope={scope} runId={current.id} acceptedProposalDigest={current.accepted_outputs?.map(o => o.check_receipts).find(validCheckedManifest)?.proposal_digest} />
      <RunFlags key={current.id} client={client} catalog={catalog} run={current} scope={scope} owner={ownerEligible(identity, catalog)} onResolved={() => void read(current.id)} />
      <h4>Accepted outputs</h4>
      {!current.accepted_outputs?.length && <p>No accepted output yet.</p>}
      {current.accepted_outputs?.map(output => <details key={output.output_id}><summary>{output.output_id} · {output.criteria_ids.join(', ')}</summary>
        <p>Digest: {output.digest} · step {output.step} · lineage: {output.step_lineage?.join(' → ')}</p>
        <pre>{detail.outputs?.find(o => o.id === output.output_id)?.content ?? 'Content not in this receipt page; load more receipts.'}</pre>
        <p>Check receipts: {txt(output.check_receipts)}</p></details>)}
      <h4>Growth history</h4>
      {current.growth_events?.map(event => <p key={event.seq}>{event.detail.old_limit} → {event.detail.new_limit}: {event.detail.reason === 'no_accepted_output' ? 'no accepted output yet' : event.detail.reason?.replaceAll('_', ' ')}</p>)}
      <h4>Pool holds</h4>{current.pool_holds?.map(hold => <p key={`${hold.kind}:${hold.pool_id}`}>{hold.kind} {hold.pool_id}: {hold.remaining} calls held</p>)}
      <p>Receipts observed; not independently verified.</p>
      {detail.receipts.map((r, i) => <details key={i}><summary>{r.kind ?? r.role} · {r.model_face ?? 'worker (no model seat)'}</summary><pre>{r.output}</pre><p>Estimated usage (server report): {txt(r.usage)}</p>{r.check_receipts !== undefined && <p>Check receipts: {txt(r.check_receipts)}</p>}</details>)}
      <h4>Events and interruptions</h4>{detail.events.map(e => <p key={e.seq}>{e.kind} · {e.at} · {txt(e.detail)}</p>)}
      {detail.next_cursor !== null && <button disabled={busy} onClick={() => void read(current.id, detail.next_cursor!)}>More events</button>}
      {detail.next_receipt_cursor != null && <button disabled={busy} onClick={() => void read(current.id, 0, detail.next_receipt_cursor!)}>More receipts</button>}
      <RunResume key={`${scope}:${current.id}`} client={client} catalog={catalog} run={current} scope={scope} owner={ownerEligible(identity, catalog)} onResult={show} onRefresh={() => void read(current.id)} />
      {current.project_id && <ProjectBudget key={`${scope}:${current.project_id}`} client={client} catalog={catalog} scope={scope} projectId={current.project_id} owner={ownerEligible(identity, catalog)} />}
      <button disabled={busy} onClick={() => void read(current.id)}>Poll run</button>
      <button disabled={busy || ['completed', 'failed', 'cancelled', 'cancelling'].includes(current.status)} onClick={() => void cancel(current.id)}>Request cancellation</button>
    </article>}
  </section>;
}
