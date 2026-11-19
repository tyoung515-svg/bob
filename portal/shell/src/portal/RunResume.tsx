import { useState } from 'react';
import type { PortalClient, RunResumeV2 } from './portal-client';
import type { Catalog, Run, RunBudget, RunDetail, WireInteger } from './portal-protocol';
import { toPortalError } from './portal-protocol';
import { checkedBudget, compatibilityError, executionReason, executionRefused, featureReason, mutationDone, mutationKey, pendingMutation, resumable, validInteger, integerInput, greater, operationReason } from './durable-runs';

import { approvalDone, rememberApproval, issueApproval, useOwnerAction } from './OwnerControls';

type Draft = Omit<RunResumeV2, 'request_key'>;
export function RunResume({ client, catalog, run, scope, owner, onResult, onRefresh }: {
  client: PortalClient; catalog?: Catalog | null; run: Run; scope: string; owner: boolean;
  onResult: (result: RunDetail) => void; onRefresh: () => void;
}) {
  const ownerAction = useOwnerAction(client, catalog);
  const operation = `resume:${run.id}`;
  const [pending, setPending] = useState(() => pendingMutation<Draft>(scope, operation));
  const [calls, setCalls] = useState<WireInteger>(pending?.budget?.start_calls ?? run.effective_call_limit ?? 50);
  const [changeWall, setChangeWall] = useState(!!pending?.budget?.wall_mode);
  const [mode, setMode] = useState<RunBudget['wall_mode']>(pending?.budget?.wall_mode ?? 'fixed');
  const [seconds, setSeconds] = useState<WireInteger>(pending?.budget?.wall_seconds ?? 3600);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [blocked, setBlocked] = useState('');
  const reason = blocked || featureReason(catalog, 'runs.resume') || executionReason(catalog) ||
    (greater(calls, 200) && !run.selected_grant_id ? operationReason(catalog, 'grants.prepare') || operationReason(catalog, 'grants.issue') : '');
  const invalid = !validInteger(calls) || greater(run.effective_call_limit ?? 1, calls) || !greater(calls, run.calls_used ?? 0) ? 'Raise the allowance above calls used; it cannot be reduced.' :
    changeWall && mode !== 'none' && (!validInteger(seconds)) ? 'Use a positive integral time limit.' :
    !run.revision ? 'Refresh this run to get its revision.' : '';
  if (!owner || (!pending && (run.status !== 'paused' || !resumable(run.pause_reason ?? run.reason)))) return null;
  async function resume() {
    if ((!pending && invalid) || reason) return;
    setBusy(true); setError('');
    try {
      let payload: Draft = pending ?? { contract_version: 2, run_id: run.id, expected_revision: run.revision!,
        budget: changeWall ? checkedBudget(calls, mode, mode === 'none' ? null : seconds) : { start_calls: calls } };
      const dispatch = async () => {
        const request_key = mutationKey(scope, operation, payload); setPending(payload);
        return client.runResumeV2({ ...payload, request_key });
      };
      const approve = async () => {
        const why = operationReason(catalog, 'grants.prepare'); if (why) throw new Error(why);
        const prepare = { contract_version: 2 as const, run_id: run.id, expected_revision: payload.expected_revision };
        const proposal = await client.grantsPrepare({ ...prepare, request_key: mutationKey(scope, `prepare:${operation}`, prepare) });
        rememberApproval(scope, operation, proposal.proposal_id);

        // The server snapshot is original coverage; show the requested Resume
        // allowance/time policy alongside it without changing its digests.
        const review = { ...proposal, coverage: { ...proposal.coverage, request: { ...proposal.coverage.request, budget: { ...proposal.coverage.request.budget, ...payload.budget } } } };
        const result = await ownerAction.run('grants.issue', { proposal_id: proposal.proposal_id }, async policy => {
          await issueApproval(client, scope, proposal, policy!);
          const latest = await client.runGetV2(run.id);
          if (!latest.run.revision || BigInt(latest.run.revision) !== BigInt(payload.expected_revision) + BigInt(1)) throw JSON.stringify({ code: 'revision_conflict', detail: 'Run changed during approval. Refresh before resuming.' });
          payload = { ...payload, expected_revision: latest.run.revision };
          return dispatch();
        }, review, 'Allow and resume', scope);
        approvalDone(scope, operation); return result;
      };
      let result: RunDetail;
      try { result = greater(payload.budget?.start_calls ?? 0, 200) && !pending && !run.selected_grant_id ? await approve() : await ownerAction.run('runs.resume', { run_id: run.id }, dispatch); }
      catch (e) {
        if (toPortalError(e).code !== 'confirmation_required') throw e;
        mutationDone(scope, operation); setPending(null); result = await approve();
      }
      mutationDone(scope, operation); approvalDone(scope, operation); setPending(null); onResult(result);
    } catch (e) {
      const typed = toPortalError(e);
      setError(typed.code === 'revision_conflict' ? 'This run changed, refresh.' : `${typed.code}: ${typed.detail}`);
      if (compatibilityError(typed.code, typed.detail)) setBlocked('Update Bob on the server to resume runs.');
      if (executionRefused(typed.code)) { mutationDone(scope, operation); setPending(null); }
    } finally { setBusy(false); }
  }
  return <section aria-label="Resume run"><h4>Resume from accepted work</h4>
    {ownerAction.dialog}
    <fieldset disabled={busy || !!pending || !!reason}>
      <label>Call allowance <input aria-label="Resume call allowance" type="text" inputMode="numeric" value={Number.isNaN(calls) ? '' : calls} onChange={e => setCalls(integerInput(e.target.value))} /></label>
      <label><input aria-label="Change resume time limit" type="checkbox" checked={changeWall} onChange={e => setChangeWall(e.target.checked)} /> Extend or remove time limit</label>
      {changeWall && <><label>Time limit <select aria-label="Resume time mode" value={mode} onChange={e => setMode(e.target.value as RunBudget['wall_mode'])}><option value="fixed">Extend from now</option><option value="follow_budget">Follow budget</option><option value="none">No time limit</option></select></label>
        {mode !== 'none' && <label>Seconds from resume <input aria-label="Resume seconds" type="text" inputMode="numeric" value={Number.isNaN(seconds) ? '' : seconds} onChange={e => setSeconds(integerInput(e.target.value))} /></label>}</>}
    </fieldset>
    {(reason || invalid) && <p role="status">{reason || invalid}</p>}
    {pending && <p role="status">Resume outcome unknown. Retry keeps the original revision, budget and request key across reconnects.</p>}
    {error && <p role="alert">{error}</p>}
    <button disabled={busy || !!reason || (!pending && !!invalid)} onClick={() => void resume()}>{pending ? 'Reconcile resume' : 'Resume run'}</button>
    <button disabled={busy} onClick={onRefresh}>Refresh run</button>
  </section>;
}
