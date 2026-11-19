import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { BuilderNetworkState, Catalog } from './portal-protocol';
import { toPortalError } from './portal-protocol';
import { builderHostname, builderRevision, validBuilderNetwork } from './builder-network';
import { canonical, executionRefused, mutationDone, mutationKey, operationReason, pendingMutation } from './durable-runs';
import { useOwnerAction } from './OwnerControls';
import { useBuilderRead } from './builder-read-store';
import { ReadFaultText } from './provider-restart';

export function BuilderNetworkSettings({ client, catalog, scope, projectId, conversationId, owner, refreshKey }: {
  client: PortalClient; catalog?: Catalog | null; scope: string; projectId: string; conversationId?: string; owner: boolean; refreshKey?: string;
}) {
  const ownerAction = useOwnerAction(client, catalog, true, JSON.stringify([scope, projectId, conversationId, owner]));
  const [state, setState] = useState<BuilderNetworkState | null>(null);
  const [domain, setDomain] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const epoch = useRef(0), writing = useRef(false), sequence = useRef(0);
  const reads = useBuilderRead(client, conversationId ?? '', scope, refreshKey, undefined, owner && !!conversationId);
  const operation = `builder-network:${projectId}`;
  async function read(generation: number) {
    const started = ++sequence.current;
    if (!conversationId) throw new Error('Open a conversation in this project to read its network settings.');
    const value = await client.builderModeGet(conversationId);
    if (value.conversation_id !== conversationId || value.project_id !== projectId || !validBuilderNetwork(value.network) || !builderRevision(value.network.revision)) throw new Error('Invalid project network record.');
    const result: BuilderNetworkState = { project_id: projectId, ...value.network, revision: value.network.revision! };
    if (generation === epoch.current && started === sequence.current) {
      const pending = pendingMutation<{ expected_revision: string | number }>(scope, operation);
      if (pending && BigInt(result.revision) > BigInt(pending.expected_revision)) mutationDone(scope, operation);
      setState(result);
    }
    return result;
  }
  useEffect(() => {
    epoch.current++;
    writing.current = false; setState(null); setError(''); setBusy(false); setDomain('');
    return () => { epoch.current++; };
  }, [client, scope, projectId, conversationId, owner]);
  useEffect(() => {
    const value = reads.mode;
    if (!owner || writing.current || !value || value.project_id !== projectId || !validBuilderNetwork(value.network) || !builderRevision(value.network.revision)) return;
    const result: BuilderNetworkState = { project_id: projectId, ...value.network, revision: value.network.revision! };
    const pending = pendingMutation<{ expected_revision: string | number }>(scope, operation);
    if (pending && BigInt(result.revision) > BigInt(pending.expected_revision)) mutationDone(scope, operation);
    setState(result); setError('');
  }, [reads.mode, owner, projectId, scope, operation]);
  async function refresh() {
    const generation = epoch.current, started = sequence.current + 1;
    try { await read(generation); if (generation === epoch.current && started === sequence.current) setError(''); }
    catch (e) { if (generation === epoch.current && started === sequence.current) { setState(null); setError(JSON.stringify(toPortalError(e))); } }
  }
  async function change(add: string[], remove: string[]) {
    if (!owner || !state || busy || writing.current || [...add, ...remove].some(d => !builderHostname(d))) return;
    const generation = epoch.current, reviewed = state;
    writing.current = true; sequence.current++; setBusy(true); setError('');
    let sent = false;
    try {
      await ownerAction.run('builder.network.set', { project_id: projectId }, async () => {
        const current = await read(generation);
        if (generation !== epoch.current) throw new Error('Project changed.');
        if (canonical(current) !== canonical(reviewed)) throw new Error('Network settings changed. Review them again.');
        const payload = { project_id: projectId, ...(add.length ? { add } : {}), ...(remove.length ? { remove } : {}), expected_revision: current.revision };
        const request_key = mutationKey(scope, operation, payload);
        sent = true;
        // The mutation receipt is not a read oracle: always reconcile mode.get.
        await client.builderNetworkSet({ ...payload, request_key });
        if (generation !== epoch.current) return;
        setState(null);
        const updated = await read(generation);
        if (BigInt(updated.revision) <= BigInt(current.revision) || add.some(d => !updated.project_extra.includes(d)) || remove.some(d => updated.project_extra.includes(d))) throw new Error('Network change not confirmed by server state.');
        mutationDone(scope, operation); setDomain('');
      });
    } catch (e) {
      if (sent && executionRefused(toPortalError(e).code)) mutationDone(scope, operation);
      if (generation === epoch.current && toPortalError(e).code !== 'owner_cancelled') { setState(null); setError(`${toPortalError(e).detail || toPortalError(e).code} Refresh server state before retrying.`); }
    } finally { if (generation === epoch.current) { writing.current = false; setBusy(false); } }
  }
  if (!owner) return null;
  const reason = operationReason(catalog, 'builder.network.set');
  return <section className="set-card" aria-label="Project network settings">
    <h4>Project network allowlist</h4>
    {!conversationId && <p>Open a conversation in this project to read its network settings.</p>}
    {state && <><p>Default domains: {state.allowlist.join(', ') || 'None'}</p>
      <p>Project extra domains:</p><ul>{state.project_extra.map(d => <li key={d}>{d} <button type="button" disabled={busy || !!reason} onClick={() => void change([], [d])}>Remove {d}</button></li>)}</ul></>}
    <form onSubmit={e => { e.preventDefault(); void change([domain], []); }}>
      <label>Extra domain <input aria-label="Extra domain" value={domain} disabled={busy} onChange={e => setDomain(e.target.value)} placeholder="example.com" /></label>
      <button disabled={busy || !state || !!reason || !builderHostname(domain) || state.project_extra.includes(domain) || state.allowlist.includes(domain)}>Add domain</button>
    </form>
    <p>Use a lowercase hostname, without a URL, port, wildcard or IP address. Only project extra domains can be removed.</p>
    <button type="button" disabled={busy || !conversationId} onClick={() => void refresh()}>Refresh network settings</button>
    {reads.error && <small role="status"><ReadFaultText client={client} error={reads.fault} fallback={reads.error} /></small>}
    {reason && <p>{reason}</p>}{error && <p role="alert"><ReadFaultText client={client} error={error} /></p>}{ownerAction.dialog}
  </section>;
}
