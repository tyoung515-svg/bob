import { useEffect, useState } from 'react';
import type { BudgetSetV2, PortalClient } from './portal-client';
import type { Catalog, ProjectCallBudget, WireInteger } from './portal-protocol';
import { toPortalError } from './portal-protocol';
import { compatibilityError, executionRefused, integerInput, mutationDone, mutationKey, operationReason, pendingMutation, validInteger } from './durable-runs';
import { useOwnerAction } from './OwnerControls';

type Draft = Omit<BudgetSetV2, 'request_key'>;
type Removal = { project_id: string; expected_revision: WireInteger };
export function ProjectBudget({ client, catalog, scope, projectId, owner }: {
  client: PortalClient; catalog?: Catalog | null; scope: string; projectId: string; owner: boolean;
}) {
  const operation = `budget:${projectId}`, removeOp = `remove-budget:${projectId}`;
  const ownerAction = useOwnerAction(client, catalog);
  const [pending, setPending] = useState(() => pendingMutation<Draft>(scope, operation));
  const [removal, setRemoval] = useState(() => pendingMutation<Removal>(scope, removeOp));
  const [receipt, setReceipt] = useState<ProjectCallBudget | null>(null);
  const [limit, setLimit] = useState<WireInteger | null>(pending?.call_limit ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [blocked, setBlocked] = useState<Record<string, string>>({});
  const reason = (op: string) => blocked[op] || operationReason(catalog, op);
  const readReason = reason('budgets.get'), setReason = reason('budgets.set'), removeReason = reason('budgets.remove');
  const invalid = limit !== null && !validInteger(limit);
  function received(result: ProjectCallBudget) { setReceipt(result); if (!pending) setLimit(result.call_limit); }
  // Opening/reconnecting never prompts for factors. Read under a live session.
  useEffect(() => {
    let live = true; setReceipt(null);
    if (owner && !readReason && !operationReason(catalog, 'owner.status')) void client.ownerStatus().then(async r => {
      if (!r.owner_session.active) return;
      const value = await client.budgetGet(projectId); if (live) received(value);
    }).catch(e => { if (live) setError(toPortalError(e).code); });
    return () => { live = false; };
  }, [client, projectId, catalog, owner]);
  async function perform(kind: 'read' | 'set' | 'remove') {
    const op = `budgets.${kind === 'read' ? 'get' : kind}`;
    if (reason(op)) return;
    setBusy(true); setError('');
    try {
      await ownerAction.run(op, { project_id: projectId }, async () => {
        if (kind === 'read') { received(await client.budgetGet(projectId)); return; }
        let current = receipt;
        if (!pending && !removal && !readReason) { current = await client.budgetGet(projectId); setReceipt(current); }
        if (kind === 'remove') {
          const payload = removal ?? { project_id: projectId, expected_revision: current?.revision! };
          if (!payload.expected_revision) throw new Error('Read the project budget before removing its limit.');
          const key = mutationKey(scope, removeOp, payload); setRemoval(payload);
          const result = await client.budgetRemove(projectId, payload.expected_revision, key);
          mutationDone(scope, removeOp); setRemoval(null); received(result); setLimit(null);
        } else {
          if (!pending && !validInteger(limit)) throw new Error('Use a positive integer call limit.');
          const payload: Draft = pending ?? { contract_version: 2, project_id: projectId, call_limit: limit!, expected_revision: current?.revision ?? null };
          const key = mutationKey(scope, operation, payload); setPending(payload);
          const result = await client.budgetSetV2({ ...payload, request_key: key });
          mutationDone(scope, operation); setPending(null); setReceipt(result); setLimit(result.call_limit);
        }
      });
    } catch (e) {
      const typed = toPortalError(e);
      setError(typed.code === 'budget_in_use' ? 'budget_in_use: Calls are held by active runs. Pause or cancel them and retry after settlement.' :
        typed.code === 'below_committed_budget' ? 'Cannot set a limit below calls already used and held (below_committed_budget).' : `${typed.code}: ${typed.detail}${typed.retry_after ? ` · Retry-After: ${typed.retry_after} seconds` : ''}`);
      if (compatibilityError(typed.code, typed.detail)) setBlocked(prior => ({ ...prior, [op]: `Update Bob on the server to use ${op}.` }));
      if (executionRefused(typed.code)) {
        mutationDone(scope, kind === 'remove' ? removeOp : operation); setPending(null); setRemoval(null);
        if (typed.code === 'revision_conflict' && !readReason) try { received(await client.budgetGet(projectId)); } catch { /* explicit read remains available */ }
      }
    } finally { setBusy(false); }
  }
  if (!owner) return null;
  return <section aria-label="Project call budget"><h4>Project call budget · {projectId}</h4>
    {ownerAction.dialog}
    <p>Used: {receipt?.consumed ?? 'not reported'} · held: {receipt?.reserved ?? 'not reported'} · limit: {receipt ? receipt.call_limit ?? 'No limit' : 'not reported'}</p>
    {receipt && <p>Counter coverage: {receipt.counter_coverage ?? 'not reported'} · observed {receipt.observed_at ?? 'not reported'}</p>}
    {!receipt && <p>Existing server limits and usage are unknown. Read the project budget to inspect them.</p>}
    <button disabled={busy || !!readReason} onClick={() => void perform('read')}>Read project budget</button>
    <fieldset disabled={busy || !!pending || !!removal || !!setReason}>
      <label>Optional project budget <select aria-label="Project budget mode" value={limit === null ? 'none' : 'limit'} onChange={e => setLimit(e.target.value === 'none' ? null : 200)}>
        <option value="none">No new limit</option><option value="limit">Set call limit</option>
      </select></label>
      {limit !== null && <label>Total project calls <input aria-label="Project call limit" inputMode="numeric" value={limit} onChange={e => setLimit(integerInput(e.target.value))} /></label>}
    </fieldset>
    {[readReason, setReason, removeReason].filter(Boolean).map((r, i) => <p key={i} role="status">{r}</p>)}
    {invalid && <p role="status">Use a positive integer call limit.</p>}
    {(pending || removal) && <p role="status">Project budget outcome unknown. Retry keeps the original request key.</p>}
    {error && <p role="alert">{error}</p>}
    <button disabled={busy || !!setReason || !!removal || (!pending && (limit === null || invalid))} onClick={() => void perform('set')}>{pending ? 'Reconcile project budget' : 'Set project budget'}</button>
    <button disabled={busy || !!removeReason || !!pending || (!removal && !receipt?.configured)} onClick={() => void perform('remove')}>{removal ? 'Reconcile removal' : 'Remove limit'}</button>
    <button disabled>Sprint budgets</button><p>Needs sprints (coming soon).</p>
  </section>;
}
