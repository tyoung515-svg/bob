import { useEffect, useState } from 'react';
import type { PortalClient, FlagResolve } from './portal-client';
import type { Catalog, FlagDetail, FlagSummary, Run, RunEvent, WireInteger } from './portal-protocol';
import { toPortalError } from './portal-protocol';
import { compatibilityError, executionRefused, mutationDone, mutationKey, openFlags, operationReason, pendingMutation, validInteger } from './durable-runs';
import { useOwnerAction } from './OwnerControls';

function Evidence({ label, value }: { label: string; value: unknown }) {
  return <details><summary>{label}</summary><pre>{value == null ? 'not reported' : JSON.stringify(value, null, 2)}</pre></details>;
}
export function FlagBadge({ run }: { run: Pick<Run, 'flags' | 'owner_flag'> }) {
  const count = openFlags(run.flags).length;
  return count || run.owner_flag ? <strong className="badge" aria-label="Open owner flags">{count ? count + ' open flag' + (count === 1 ? '' : 's') : 'Owner flag'}</strong> : null;
}
export function RunJobs({ run, events }: { run: Run; events: RunEvent[] }) {
  const account = run.job_account;
  return <section aria-label="Job account"><h4>Job account</h4>
    {account ? <><p>Jobs used: {account.dispatched} / {account.max_job_attempts} · held {account.held} · completed {account.completed} · unknown {account.unknown}</p>
      <p>Job time used: {account.seconds_used} seconds · held {account.seconds_held} · cleanup {account.cleanup_seconds} · budget {account.job_seconds_budget ?? 'not set'}</p>
      <p>CLI model usage: {account.cli_usage_basis} · estimated or unknown; separate from exact model calls.</p></> : <p>Job account not reported by this server.</p>}
    {!run.job_waits?.length && <p>Current job tier: not reported by this server.</p>}
    {run.job_waits?.map(job => <article key={job.execution_id}><p>Current tier: {job.selected_tier ?? 'not reported'} seconds · effective {job.effective_seconds ?? 'not reported'} · {job.limiting_reason}</p>
      <p>Proposed {job.proposed_seconds} · {job.rationale} · owner override {job.owner_override ?? 'none'}</p>
      <p>{job.state} · timeout episode {job.timeout_episode} · reconciliation {job.reconciliation} · continuation of {job.parent_execution_id ?? 'none'}</p><Evidence label="Job receipt" value={job} /></article>)}
    {run.failure_recovery && <Evidence label="Failure recovery episodes" value={run.failure_recovery} />}
    {run.failure_recovery_truncated && <p>Recovery summary truncated. Page through run events for the full trail.</p>}
    <h4>Timeout continuations</h4><p>Continuation requires trusted workspace/effect reconciliation and fresh authority checks. Unknown outcomes stay paused.</p>
    {events.filter(e => /timeout|continuation|job_|worker/.test(e.kind)).map(e => <Evidence key={e.seq} label={e.kind + ' · ' + e.at} value={e.detail} />)}
  </section>;
}
export function RunFlags({ client, catalog, run, scope, owner, onResolved }: { client: PortalClient; catalog?: Catalog | null; run: Run; scope: string; owner: boolean; onResolved?: () => void }) {
  const action = useOwnerAction(client, catalog);
  const [items, setItems] = useState<FlagSummary[]>(() => [...new Map((run.flags ?? []).map(f => [f.id, f])).values()]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [detail, setDetail] = useState<FlagDetail | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState<Record<string, string>>({});
  const [decision, setDecision] = useState<FlagResolve['decision']>('inspected');
  const [guidance, setGuidance] = useState('');
  const [pending, setPending] = useState<Omit<FlagResolve, 'request_key'> | null>(null);
  const reason = (op: string) => blocked[op] || operationReason(catalog, op);
  function fault(op: string, e: unknown) {
    const t = toPortalError(e); setError(t.code + ': ' + t.detail);
    if (compatibilityError(t.code, t.detail)) setBlocked(prior => ({ ...prior, [op]: 'Update Bob on the server to use ' + op + '.' }));
  }
  async function load(next?: string) {
    setBusy(true); setError('');
    try { const page = await client.flagsList(run.id, next, 'all');
      if (page.source !== 'server') throw new Error('Flags did not come from the server.');
      setItems(prior => [...new Map([...(next ? prior : []), ...page.items].map(f => [f.id, f])).values()]);
      if (next && next === page.next_cursor) throw new Error('Repeated flags cursor.');
      setCursor(page.next_cursor);
    } catch (e) { fault('flags.list', e); } finally { setBusy(false); }
  }
  useEffect(() => { if (!reason('flags.list')) void load(); }, [client, run.id, run.revision, catalog]);
  async function inspect(id: string, after: WireInteger = 0, jobCursor?: string) {
    setBusy(true); setError('');
    try {
      const result = await client.flagGet(id, after, jobCursor);
      setDetail(prior => prior?.flag.id === id && (after || jobCursor) ? { ...result,
        events: [...new Map([...prior.events, ...result.events].map(e => [e.seq, e])).values()],
        flag: { ...result.flag, live_job_cleanup: [...new Map([...prior.flag.live_job_cleanup, ...result.flag.live_job_cleanup].map(j => [j.execution_id, j])).values()] },
        next_cursor: jobCursor ? prior.next_cursor : result.next_cursor,
        next_job_cursor: after ? prior.next_job_cursor : result.next_job_cursor } : result);
      const saved = pendingMutation<Omit<FlagResolve, 'request_key'>>(scope, 'resolve:' + id); setPending(saved);
      if (saved) { setDecision(saved.decision); setGuidance(saved.guidance); }
    } catch (e) { fault('flags.get', e); } finally { setBusy(false); }
  }
  async function resolve() {
    if (!detail || !owner || reason('flags.resolve')) return;
    setBusy(true); setError(''); const op = 'resolve:' + detail.flag.id;
    try {
      const payload = pending ?? { contract_version: 2 as const, flag_id: detail.flag.id, expected_revision: detail.flag.revision, decision, guidance };
      const result = await action.run('flags.resolve', { flag_id: detail.flag.id }, async () => {
        const key = mutationKey(scope, op, payload); setPending(payload);
        return client.flagResolve({ ...payload, request_key: key });
      });
      if (result.resolved !== true || result.run_resumed !== false) throw new Error('Unexpected flag resolution receipt. Inspect the run before continuing.');
      mutationDone(scope, op); setPending(null);
      setItems(rows => rows.map(f => f.id === result.flag_id ? { ...f, status: 'resolved' } : f));
      setDetail(prior => prior && { ...prior, flag: { ...prior.flag, status: 'resolved' } });
      onResolved?.();
      setNotice('Flag resolved. The run was not resumed. Resume, budget authorization and cancellation remain separate actions.');
    } catch (e) {
      const t = toPortalError(e); fault('flags.resolve', e);
      if (executionRefused(t.code)) { mutationDone(scope, op); setPending(null); }
    } finally { setBusy(false); }
  }
  const flag = detail?.flag;
  return <section aria-label="Run flags"><h4>Owner flags</h4>{action.dialog}<FlagBadge run={{ flags: items, owner_flag: run.owner_flag && !items.length }} />
    <button disabled={busy || !!reason('flags.list')} onClick={() => void load()}>Refresh flags</button>
    {['flags.list','flags.get', ...(owner ? ['flags.resolve'] : [])].map(op => reason(op) && <p key={op} role="status">{reason(op)}</p>)}
    {items.map(f => <article key={f.id}><p>{f.status} · {f.cause} · {f.phase} · {f.id}</p><button disabled={busy || !!reason('flags.get')} onClick={() => void inspect(f.id)}>Inspect flag {f.id}</button></article>)}
    {cursor && <button disabled={busy || !!reason('flags.list')} onClick={() => void load(cursor)}>More flags</button>}
    {flag && <article aria-label="Flag detail"><h4>{flag.cause} · {flag.status}</h4>
      <p>Flag {flag.id} · run {flag.run_id} · project {flag.project_id ?? 'none'} · thread {flag.conversation_id ?? 'none'}</p>
      <p>Stopped phase {flag.phase} · step {flag.ordinal ?? 'not reported'} · created {flag.created_at} · updated {flag.updated_at}</p>
      <p>Effects: {flag.effects} · cleanup pending: {String(flag.cleanup_pending)} · source {flag.cleanup_source}</p>
      <Evidence label="Original scope and team policy" value={{ scope: flag.scope, policy: flag.policy }} />
      <Evidence label="Budgets, calls and job accounting" value={flag.budget} /><Evidence label="Stopped budget and authority" value={flag.stopped_context} />
      <Evidence label="Attempt trail" value={flag.attempts} /><Evidence label="Audit and failure episode" value={flag.episode} />
      <Evidence label="Usage estimates (not billing accurate)" value={flag.usage_estimates} /><Evidence label="Outputs and check receipts" value={flag.output_links} />
      <Evidence label="Live job cleanup" value={flag.live_job_cleanup} /><Evidence label="Safe next actions" value={{ actions: flag.safe_next_actions, contract: flag.action_contract }} />
      <p>Model audit is advisory. Inspect check receipts before claiming effects or verification.</p>
      {detail.events.map(e => <Evidence key={e.seq} label={e.kind + ' · ' + e.at} value={e.detail} />)}
      {detail.next_cursor !== null && <button disabled={busy || !!reason('flags.get')} onClick={() => void inspect(flag.id, detail.next_cursor!)}>More flag events</button>}
      {detail.next_job_cursor && <button disabled={busy || !!reason('flags.get')} onClick={() => void inspect(flag.id, 0, detail.next_job_cursor!)}>More job cleanup</button>}
      {owner && (flag.status === 'open' || pending) && <fieldset disabled={busy || !!reason('flags.resolve')}><legend>Resolve flag</legend>
        <p>Records your decision only. Does not resume, cancel or reconcile an unknown effect.</p>
        <label>Decision <select aria-label="Flag decision" value={decision} disabled={!!pending} onChange={e => setDecision(e.target.value as FlagResolve['decision'])}><option value="inspected">Inspected</option><option value="resume_with_guidance">Resume with guidance (separate action)</option><option value="authorize_budget">Authorize budget (separate approval)</option><option value="cancel">Cancel (separate action)</option></select></label>
        <label>Guidance <textarea aria-label="Flag guidance" value={guidance} maxLength={4000} disabled={!!pending} onChange={e => setGuidance(e.target.value)} /></label>
        {pending && <p>Resolution outcome unknown. Reconcile with the saved revision, decision and request key.</p>}
        <button disabled={!pending && (!guidance.trim() || guidance.length > 4000 || !validInteger(flag.revision))} onClick={() => void resolve()}>{pending ? 'Reconcile resolution' : 'Resolve flag'}</button>
      </fieldset>}
    </article>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </section>;
}
