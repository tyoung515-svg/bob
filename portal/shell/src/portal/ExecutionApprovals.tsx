import { useState } from 'react';
import type { PortalClient } from './portal-client';
import type { Catalog, ExecutionGrant } from './portal-protocol';
import { toPortalError, txt } from './portal-protocol';
import { executionRefused, mutationDone, mutationKey, operationReason } from './durable-runs';
import { useOwnerAction } from './OwnerControls';

export function ExecutionApprovals({ client, catalog, scope, owner }: { client: PortalClient; catalog?: Catalog | null; scope: string; owner: boolean }) {
  const action = useOwnerAction(client, catalog);
  const [items, setItems] = useState<ExecutionGrant[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState<ExecutionGrant | null>(null);
  const listReason = operationReason(catalog, 'grants.list'), revokeReason = operationReason(catalog, 'grants.revoke');
  async function load() {
    setBusy(true); setError('');
    try {
      await action.run('grants.list', {}, async () => {
        const rows: ExecutionGrant[] = [], seen = new Set<string>(); let cursor: string | undefined;
        do { const page = await client.grantsList(cursor); rows.push(...page.items); cursor = page.next_cursor || undefined;
          if (cursor && seen.has(cursor)) throw new Error('Repeated approvals cursor.'); if (cursor) seen.add(cursor);
        } while (cursor);
        setItems(rows.filter(r => !r.revoked && (!r.expires_at || Date.parse(r.expires_at) > Date.now())));
      });
    } catch (e) { const t = toPortalError(e); setError(`${t.code}: ${t.detail}`); } finally { setBusy(false); }
  }
  async function revoke(grant: ExecutionGrant) {
    setBusy(true); setError(''); const op = `revoke:${grant.id}`;
    try {
      const result = await action.run('grants.revoke', { grant_id: grant.id }, () => client.grantRevoke(grant.id, grant.revision, mutationKey(scope, op, { grant_id: grant.id, expected_revision: grant.revision })));
      mutationDone(scope, op); setItems(rows => rows.filter(r => r.id !== grant.id)); setReview(null);
      setNotice(`Revoked. Affected runs: ${txt(result.affected_runs)}. Already dispatched calls remain charged; cleanup may still be pending.`);
    } catch (e) { const t = toPortalError(e); if (executionRefused(t.code)) mutationDone(scope, op); setError(`${t.code}: ${t.detail}`); } finally { setBusy(false); }
  }
  if (!owner) return null;
  return <section aria-label="Long-run approvals"><h3>Long-run approvals</h3>{action.dialog}
    <button disabled={busy || !!listReason} onClick={() => void load()}>Read active approvals</button>
    {listReason && <p role="status">{listReason}</p>}{revokeReason && <p role="status">{revokeReason}</p>}
    {items.map(grant => <article key={grant.id}><h4>{grant.coverage_kind === 'project' ? 'Future matching tasks in project' : 'This task'} · {grant.project_id ?? grant.run_id}</h4>
      <p>Ceiling: {grant.call_ceiling ?? 'No ceiling'} · escalation {grant.auto_escalate ? 'on' : 'off'} · expiry {grant.expires_at ?? 'No expiry'}</p><p>Job permissions: extended {String(grant.allow_extended_jobs ?? false)} · custom {String(grant.allow_custom_job_limit ?? false)} · override {grant.job_override_seconds ?? 'agent selects'} seconds · ceiling {grant.job_seconds_ceiling ?? 'none'} · automatic continuation {String(grant.job_auto_escalate ?? false)}</p><details><summary>Coverage</summary><pre>{JSON.stringify(grant, null, 2)}</pre></details>
      <button disabled={busy || !!revokeReason} onClick={() => setReview(grant)}>Revoke</button>
    </article>)}
    {review && <section aria-label="Revoke approval"><p>Revoke {review.id}? This prevents further dispatch for bound runs{review.coverage_kind === 'project' ? ' and future matching tasks in this project' : ' for this task'}. Already dispatched calls remain charged; transport cleanup may be pending.</p><button disabled={busy} onClick={() => void revoke(review)}>Revoke approval</button><button disabled={busy} onClick={() => setReview(null)}>Keep approval</button></section>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </section>;
}
