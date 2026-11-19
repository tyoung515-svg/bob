import { ReadFaultText } from './provider-restart';
import { useEffect, useRef, useState } from 'react';
import type { PortalClient, GrantIssue } from './portal-client';
import type { Catalog, JobGrantPolicy, GrantProposal, OwnerChallenge, OwnerSession, RunDetail } from './portal-protocol';
import { isRateLimited, toPortalError, txt } from './portal-protocol';
import { canonical, executionRefused, greater, integerInput, mutationDone, mutationKey, operationReason, pendingMutation, validInteger, jobGrantReason, jobTimeReason } from './durable-runs';
import type { RunDraft } from './durable-runs';
import { PermissionSummary } from './PermissionSummary';

type Policy = Pick<GrantIssue, 'coverage_kind' | 'auto_escalate' | 'call_ceiling' | 'expires_at'> & JobGrantPolicy;
type Review = { operation: string; catalog?: Catalog | null; jobPolicy?: JobGrantPolicy; challenge: OwnerChallenge; needsProof: boolean; proposal?: GrantProposal; label: string; scope?: string; refreshChallenge: () => Promise<OwnerChallenge>; resolve: (policy?: Policy) => Promise<void>; reject: (error: unknown) => void };
const cancelled = () => JSON.stringify({ code: 'owner_cancelled', detail: 'Owner action cancelled. Nothing further was sent.' });

// No credentials in React state, journals, summaries or error messages. The
// uncontrolled DOM inputs are erased synchronously before native IPC starts.
function ConfirmOwner({ review, client, onClose }: { review: Review; client: PortalClient; onClose: () => void }) {
  const password = useRef<HTMLInputElement>(null);
  const totp = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [proof, setProof] = useState(review.needsProof && review.challenge.confirmation_mode !== 'signed_in_user');
  const [challenge, setChallenge] = useState(review.challenge);
  const saved = review.proposal && review.scope ? pendingMutation<GrantIssue>(review.scope, `issue:${review.proposal.proposal_id}`) ?? issuedPolicy(review.scope, review.proposal.proposal_id) : null;
  const [kind, setKind] = useState<'task' | 'project'>(saved?.coverage_kind ?? 'task');
  const [escalate, setEscalate] = useState(saved?.auto_escalate ?? false);
  const [ceiling, setCeiling] = useState(saved?.call_ceiling == null ? '' : String(saved.call_ceiling));
  const [expiry, setExpiry] = useState(saved?.expires_at ? new Date(Date.parse(saved.expires_at) - new Date(saved.expires_at).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '');
  const initialJob = saved ?? review.jobPolicy;
  const [jobSeconds, setJobSeconds] = useState(initialJob?.job_override_seconds == null ? '' : String(initialJob.job_override_seconds));
  const [jobCustom, setJobCustom] = useState(initialJob?.allow_custom_job_limit ?? false);
  const [jobExtended, setJobExtended] = useState(initialJob?.allow_extended_jobs ?? false);
  const [jobAuto, setJobAuto] = useState(initialJob?.job_auto_escalate ?? true);
  const [jobCeiling, setJobCeiling] = useState(initialJob?.job_seconds_ceiling == null ? '' : String(initialJob.job_seconds_ceiling));
  const jobAvailable = !jobTimeReason(review.catalog);
  const jobReason = jobGrantReason(review.catalog);
  const jobCoverage = review.proposal?.coverage.request.job_coverage;
  const [retryAt, setRetryAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const live = useRef(true);
  const clear = () => { if (password.current) password.current.value = ''; if (totp.current) totp.current.value = ''; };
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => { live.current = false; clearInterval(timer); clear(); };
  }, []);
  const budget = review.proposal?.coverage.request.budget;
  const invalidJob = (jobSeconds !== '' && !validInteger(jobSeconds)) || (jobCeiling !== '' && (!validInteger(jobCeiling) || (validInteger(jobSeconds) && greater(jobSeconds, jobCeiling)))) || (validInteger(jobSeconds) && greater(jobSeconds, 3600) && !jobExtended) || ((jobSeconds !== '' || jobCustom || jobExtended || jobCeiling !== '') && (!jobCoverage || !!jobReason));
  const invalid = invalidJob || !!ceiling && (!validInteger(ceiling) || (budget && greater(budget.start_calls, ceiling)));
  async function submit() {
    let pw = password.current?.value ?? '', code = totp.current?.value ?? '';
    clear(); setError('');
    if (proof && (!pw || new TextEncoder().encode(pw).length > 1024 || !/^[0-9]{6}$/.test(code))) { pw = code = ''; setError('Enter your password and a six-digit authenticator code.'); return; }
    setBusy(true);
    let actionStarted = false;
    try {
      if (review.needsProof) {
        try {
          if (proof) await client.ownerConfirm(challenge.challenge_id, pw, code);
          else await client.ownerConfirm(challenge.challenge_id);
        }
        finally { pw = code = ''; }
      }
      if (!live.current) throw cancelled();
      actionStarted = true;
      await review.resolve(review.proposal ? { coverage_kind: kind, auto_escalate: escalate, call_ceiling: ceiling ? integerInput(ceiling) : null, expires_at: expiry ? new Date(expiry).toISOString() : null, ...(jobAvailable && jobCoverage && !jobReason ? { allow_extended_jobs: jobExtended, allow_custom_job_limit: jobCustom, job_override_seconds: jobSeconds ? integerInput(jobSeconds) : null, job_seconds_ceiling: jobCeiling ? integerInput(jobCeiling) : null, job_auto_escalate: jobAuto } : {}) } : undefined);
      if (live.current) onClose();
    } catch (e) {
      pw = code = ''; clear();
      if (!live.current) return;
      const typed = toPortalError(e);
      // A failed/uncertain proof is never resent. A fresh explicit submit needs
      // a fresh challenge. Preserve mutation keys for unknown action outcomes.
      if (typed.retry_after) setRetryAt(Date.now() + typed.retry_after * 1000);
      setError(isRateLimited(typed) ? typed.detail : `${typed.code}${typed.retry_after ? ` · Retry-After: ${typed.retry_after} seconds` : ''}. ${typed.code === 'timeout_no_reply' || typed.code === 'transport_unavailable' ? 'Outcome unknown. Close and reconcile the action before retrying.' : proof ? 'Confirm again with a fresh authenticator code.' : 'Review the action and confirm again.'}`);
      if (!actionStarted) {
        try { const next = await review.refreshChallenge(); setChallenge(next); setProof(next.confirmation_mode !== 'signed_in_user'); } catch { review.reject(e); onClose(); }
      } else { review.reject(e); onClose(); }
    } finally { clear(); if (live.current) setBusy(false); }
  }
  return <div className="owner-modal-backdrop"><section className="set-card owner-modal" role="dialog" aria-modal="true" aria-label="Confirm it's you">
    <h2>{review.operation.startsWith('builder.') && review.needsProof ? 'Confirm owner session' : challenge.confirmation_mode === 'signed_in_user' ? 'Review permission' : review.proposal ? 'Allow long run' : "Confirm it's you"}</h2>
    {review.operation.startsWith('builder.') && review.needsProof && <p>Confirm your identity to authorize this action.</p>}
    {challenge.confirmation_mode === 'signed_in_user' && <p>Approve this action using your current signed-in session.</p>}
    <PermissionSummary value={challenge.summary} />
    {review.proposal && <>
      <label>Approval coverage <select aria-label="Approval coverage" value={kind} disabled={busy || !!saved} onChange={e => setKind(e.target.value as 'task' | 'project')}>
        <option value="task">This task</option><option value="project" disabled={!review.proposal.coverage.request.project_id}>Future matching tasks in this project</option>
      </select></label>
      <p>Team: {review.proposal.coverage.request.team} · Project: {review.proposal.coverage.request.project_id ?? 'none'}</p>
      <p>Models and initiating scope: {txt(review.proposal.coverage.snapshot)} · {txt(review.proposal.coverage.initiator)}</p>
      <p>Starting allowance: {budget?.start_calls} calls · {budget?.wall_mode === 'none' ? 'No time limit, call budget only' : `${budget?.wall_seconds} seconds (${budget?.wall_mode})`}</p>
      <label><input type="checkbox" aria-label="Approval auto escalation" checked={escalate} disabled={busy || !!saved} onChange={e => setEscalate(e.target.checked)} /> Automatic escalation {escalate ? 'on' : 'off'}</label>
      <label>Per-run call ceiling <input aria-label="Approval call ceiling" inputMode="numeric" value={ceiling} disabled={busy || !!saved} onChange={e => setCeiling(e.target.value)} placeholder="No ceiling" /></label>
      <p>{ceiling ? `Per-run ceiling: ${ceiling}` : 'No ceiling'}</p>
      <label>Approval expiry <input aria-label="Approval expiry" type="datetime-local" value={expiry} disabled={busy || !!saved} onChange={e => setExpiry(e.target.value)} /></label><p>{expiry || 'No expiry'}</p>
      {!jobAvailable ? <fieldset disabled><legend>Job-time options</legend>Unavailable · coming with build jobs</fieldset> : <fieldset disabled={busy || !!saved || !!jobReason || !jobCoverage}><legend>Job-time options</legend>
        <p>Reviewed job coverage: {JSON.stringify(jobCoverage ?? null, null, 2)}</p>
        {!jobCoverage && <p>No job coverage in this proposal. A call-only grant cannot authorize job overrides.</p>}
        {jobReason && <p role="status">{jobReason}</p>}
        <label>Job time <select aria-label="Approval job tier" value={jobCustom || (jobSeconds && !['900','1800','3600'].includes(jobSeconds)) ? 'custom' : jobSeconds} onChange={e => { setJobCustom(e.target.value === 'custom'); if (e.target.value !== 'custom') setJobSeconds(e.target.value); }}>
          <option value="">Agent selects per job</option>{review.catalog?.capabilities.execution_v2?.job_time?.presets_seconds.map(n => <option key={n} value={n}>{n / 60} min</option>)}<option value="custom">Custom / above 60 min</option>
        </select></label>
        {(jobCustom || (jobSeconds && !['900','1800','3600'].includes(jobSeconds))) && <label>Custom seconds <input aria-label="Approval custom job seconds" inputMode="numeric" value={jobSeconds} onChange={e => setJobSeconds(e.target.value)} /></label>}
        <label><input aria-label="Allow extended jobs" type="checkbox" checked={jobExtended} onChange={e => setJobExtended(e.target.checked)} /> Allow jobs above 60 minutes (long-run approval required)</label>
        <label><input aria-label="Allow custom job limit" type="checkbox" checked={jobCustom} onChange={e => setJobCustom(e.target.checked)} /> Allow custom job limits</label>
        <label>Per-job seconds ceiling <input aria-label="Approval job ceiling" inputMode="numeric" value={jobCeiling} onChange={e => setJobCeiling(e.target.value)} placeholder="No ceiling" /></label>
        <label><input aria-label="Approval job escalation" type="checkbox" checked={jobAuto} onChange={e => setJobAuto(e.target.checked)} /> Automatic job continuation after reconciliation</label>
      </fieldset>}
    </>}
    {proof && <><label>Password <input ref={password} aria-label="Owner password" type="password" autoComplete="off" maxLength={1024} disabled={busy} autoFocus /></label>
      <label>Authenticator code <input ref={totp} aria-label="Owner authenticator code" type="password" inputMode="numeric" autoComplete="off" maxLength={6} disabled={busy} /></label></>}
    {error && <p role="alert">{error}</p>}{invalid && <p role="alert">Use a canonical integer ceiling at least as large as the starting allowance.</p>}
    <button type="button" disabled={busy || invalid || now < retryAt} onClick={() => void submit()}>{review.label}</button>
    <button type="button" disabled={busy} onClick={() => { clear(); review.reject(cancelled()); onClose(); }}>Cancel</button>
  </section></div>;
}

export function useOwnerAction(client: PortalClient, catalog?: Catalog | null, requireConfirmation = false, context?: string) {
  const [review, setReview] = useState<Review | null>(null);
  const pending = useRef<Review | null>(null);
  const live = useRef(true);
  const generation = useRef(0);
  useEffect(() => { live.current = true; setReview(null); return () => { live.current = false; generation.current++; pending.current?.reject(cancelled()); pending.current = null; }; }, [client, context]);
  useEffect(() => {
    const clear = () => { generation.current++; pending.current?.reject(cancelled()); pending.current = null; setReview(null); };
    window.addEventListener('bob-owner-invalidated', clear);
    return () => window.removeEventListener('bob-owner-invalidated', clear);
  }, []);
  async function run<T>(operation: string, resource: Record<string, string>, action: (policy?: Policy) => Promise<T>, proposal?: GrantProposal, label = 'Confirm and continue', scope?: string, initialJobPolicy?: JobGrantPolicy): Promise<T> {
    const started = generation.current;
    const checkCurrent = () => { if (!live.current || started !== generation.current) throw cancelled(); };
    checkCurrent();
    for (const op of [operation, 'owner.status']) { const reason = operationReason(catalog, op, op.startsWith('workflows.') ? 1 : 2); if (reason) throw new Error(reason); }
    const session = (await client.ownerStatus()).owner_session;
    checkCurrent();
    const active = session.active && (session.expires_at ?? 0) * 1000 > Date.now() && session.allowed_operations?.includes(operation);
    if (active && !proposal && !requireConfirmation) return action();
    for (const op of ['owner.begin', ...(!active || requireConfirmation ? ['owner.confirm'] : [])]) { const reason = operationReason(catalog, op, op.startsWith('workflows.') ? 1 : 2); if (reason) throw new Error(reason); }
    const challenge = await client.ownerBegin(operation, resource, proposal?.revision);
    checkCurrent();
    if (pending.current) throw new Error('Finish the open owner confirmation first.');
    return new Promise<T>((resolve, reject) => {
      const next: Review = { operation, catalog, jobPolicy: initialJobPolicy, challenge, needsProof: !active || requireConfirmation, proposal, label, scope, refreshChallenge: () => client.ownerBegin(operation, resource, proposal?.revision), reject, resolve: async policy => { checkCurrent(); const result = await action(policy); resolve(result); } };
      pending.current = next; setReview(next);
    });
  }
  async function approveStart(draft: RunDraft, scope: string, operation: string, dispatch: (draft: RunDraft) => Promise<RunDetail>, initialJobPolicy?: JobGrantPolicy) {
    const reason = operationReason(catalog, 'grants.prepare'); if (reason) throw new Error(reason);
    const key = mutationKey(scope, `prepare:${operation}`, { request: draft });
    const prepare = { contract_version: 2 as const, request: { ...draft, request_key: key }, request_key: key };
    const proposal = await client.grantsPrepare(prepare);
    rememberApproval(scope, operation, proposal.proposal_id);
    let result: RunDetail;
    try { result = await run('grants.issue', { proposal_id: proposal.proposal_id }, async policy => {
      await issueApproval(client, scope, proposal, policy!);
      return dispatch({ ...draft, intent_id: proposal.intent_id ?? proposal.proposal_id });
    }, proposal, 'Allow and start', scope, initialJobPolicy); }
    catch (e) { if (toPortalError(e).code === 'owner_cancelled' && !pendingMutation(scope, `issue:${proposal.proposal_id}`) && !issuedPolicy(scope, proposal.proposal_id)) approvalDone(scope, operation); throw e; }
    approvalDone(scope, operation);
    return result;
  }
  return { run, approveStart, dialog: review && <ConfirmOwner key={review.challenge.challenge_id} review={review} client={client} onClose={() => { pending.current = null; setReview(null); }} /> };
}

export async function issueApproval(client: PortalClient, scope: string, proposal: GrantProposal, policy: Policy) {
  const payload = { contract_version: 2 as const, proposal_id: proposal.proposal_id, proposal_revision: proposal.revision, expected_revision: null, ...policy, allow_long_runs: true, allow_extended_jobs: policy.allow_extended_jobs ?? false, allow_custom_job_limit: policy.allow_custom_job_limit ?? false, job_seconds_ceiling: policy.job_seconds_ceiling ?? null };
  const op = `issue:${proposal.proposal_id}`;
  const issued = issuedPolicy(scope, proposal.proposal_id);
  if (issued) {
    if (canonical(issued) !== canonical(payload)) throw new Error('This approval was already issued with different policy. Read it in Settings.');
    return;
  }
  try {
    await client.grantsIssue({ ...payload, request_key: mutationKey(scope, op, payload) });
    localStorage.setItem(`bob.execution-issued:${scope}:${proposal.proposal_id}`, JSON.stringify(payload));
  } catch (e) {
    if (executionRefused(toPortalError(e).code)) mutationDone(scope, op);
    throw e;
  }
}

export function OwnerSessionIndicator({ client, catalog, eligible }: { client: PortalClient; catalog?: Catalog | null; eligible: boolean }) {
  const [session, setSession] = useState<OwnerSession>({ active: false });
  const [error, setError] = useState('');
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    setSession({ active: false });
    if (!eligible || operationReason(catalog, 'owner.status')) return;
    let live = true;
    let reading = false, next = 0, failures = 0;
    const read = async () => {
      if (!live || reading || document.visibilityState === 'hidden' || Date.now() < next) return;
      reading = true;
      try {
        const r = await client.ownerStatus();
        if (live) { setSession(r.owner_session); setNow(Date.now()); setError(''); failures = 0; next = Date.now() + 30000; }
      } catch (e) {
        if (live) {
          const error = toPortalError(e);
          setError(JSON.stringify(error));
          next = Date.now() + Math.max((error.retry_after ?? 30) * 1000, Math.min(120000, 30000 * 2 ** failures++)) + Math.random() * 1000;
        }
      } finally { reading = false; }
    };
    void read(); const timer = setInterval(() => { setNow(Date.now()); void read(); }, 1000);
    document.addEventListener('visibilitychange', read);
    return () => { live = false; clearInterval(timer); document.removeEventListener('visibilitychange', read); };
  }, [client, catalog, eligible]);
  useEffect(() => {
    const clear = () => setSession({ active: false });
    window.addEventListener('bob-owner-invalidated', clear);
    return () => window.removeEventListener('bob-owner-invalidated', clear);
  }, []);
  if (!eligible) return null;
  if (!session.active || (session.expires_at ?? 0) * 1000 <= now) return error ? <aside className="owner-session" role="alert">Owner session: <ReadFaultText client={client} error={error} fallback={error.startsWith('{') ? 'Reconnecting' : error} /></aside> : null;
  async function lock(logout = false) {
    try { if (logout) await client.ownerLogout(); else await client.ownerLock(); }
    catch (e) { setError(toPortalError(e).code); }
    finally { setSession({ active: false }); }
  }
  return <aside className="owner-session">Confirmed as owner · {Math.ceil(((session.expires_at ?? 0) * 1000 - now) / 60000)} min left · <button title={operationReason(catalog, 'owner.lock')} disabled={!!operationReason(catalog, 'owner.lock')} onClick={() => void lock()}>Lock</button><button title={operationReason(catalog, 'owner.logout')} disabled={!!operationReason(catalog, 'owner.logout')} onClick={() => void lock(true)}>Log out</button>{error && <span role="alert"><ReadFaultText client={client} error={error} fallback={error.startsWith('{') ? 'Reconnecting' : error} /></span>}</aside>;
}

// Associates nonsecret approval receipts with the mutation they authorize so a
// successful reconciliation after reload also releases the original journals.
export function rememberApproval(scope: string, operation: string, proposalId: string) {
  localStorage.setItem(`bob.execution-approval:${scope}:${operation}`, proposalId);
}
export function approvalDone(scope: string, operation: string) {
  const key = `bob.execution-approval:${scope}:${operation}`;
  const id = localStorage.getItem(key);
  if (id) { mutationDone(scope, `issue:${id}`); localStorage.removeItem(`bob.execution-issued:${scope}:${id}`); }
  mutationDone(scope, `prepare:${operation}`); localStorage.removeItem(key);
}

function issuedPolicy(scope: string, id: string): GrantIssue | null {
  return JSON.parse(localStorage.getItem(`bob.execution-issued:${scope}:${id}`) || 'null') as GrantIssue | null;
}
