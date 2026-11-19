import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { BuilderMode, BuilderModeSet, Catalog, WorkspaceReceipt } from './portal-protocol';
import { builderModes, builderTime, isRateLimited, toPortalError } from './portal-protocol';
import { useOwnerAction } from './OwnerControls';
import { executionRefused, mutationDone, mutationKey, pendingMutation } from './durable-runs';
import { YoloConfirm } from './YoloConfirm';
import { BuilderPreview } from './BuilderPreview';
import { BuilderApprovalCard } from './BuilderApprovalCard';
import { validBuilderMode } from './builder-read-validation';
import { useBuilderRead } from './builder-read-store';
import { ReadFaultText } from './provider-restart';
import { BuilderNetworkLog } from './BuilderNetworkLog';
import { BuilderNetworkSettings } from './BuilderNetworkSettings';
import { WorkspaceBrowser } from './WorkspaceBrowser';
import { SaveToLibrary } from './SaveToLibrary';
import type { LibraryStore } from './library';

export { validBuilderMode } from './builder-read-validation';

export function BuilderModeBar({ client, catalog, conversationId, scope, refreshKey, onWorkspace, libraryStore }: {
  client: PortalClient; catalog: Catalog | null; conversationId: string; scope: string; refreshKey?: string;
  onWorkspace?: (projectId: string | null, receipt?: WorkspaceReceipt | null) => void;
  libraryStore?: LibraryStore;
}) {
  const owner = useOwnerAction(client, catalog, false, JSON.stringify([scope, conversationId, refreshKey]));
  const yoloOwner = useOwnerAction(client, catalog, true, JSON.stringify([scope, conversationId, refreshKey]));
  const store = useBuilderRead(client, conversationId, scope, refreshKey);
  const mode = store.mode;
  const setMode = store.setMode;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [now, setNow] = useState(Date.now());
  const [yolo, setYolo] = useState(false);
  const [preview, setPreview] = useState(false);
  const [file, setFile] = useState<{ project: string; path: string } | null>(null);
  const [options, setOptions] = useState(false);
  const binding = useRef(onWorkspace);
  binding.current = onWorkspace;
  const epoch = useRef(0);
  const mutating = useRef(false);
  const sequence = useRef(0);
  const yoloChoice = useRef<((allow: boolean) => void) | null>(null);
  useEffect(() => {
    epoch.current++;
    mutating.current = false;
    setError(''); setBusy(false); setYolo(false); setPreview(false);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => { epoch.current++; clearInterval(tick); yoloChoice.current?.(false); yoloChoice.current = null; };
  }, [client, conversationId, refreshKey, scope]);
  useEffect(() => {
    if (!mode) return;
    const operation = `builder-mode:${conversationId}`;
    const pending = pendingMutation<BuilderModeSet>(scope, operation);
    if (pending && BigInt(mode.revision) > BigInt(pending.expected_revision)) mutationDone(scope, operation);
    binding.current?.(mode.project_id, mode.workspace_receipt);
  }, [mode, conversationId, scope]);
  useEffect(() => {
    const cancel = () => yoloChoice.current?.(false);
    window.addEventListener('bob-owner-invalidated', cancel);
    return () => window.removeEventListener('bob-owner-invalidated', cancel);
  }, []);
  async function change(next: BuilderMode) {
    if (!mode || busy || next === mode.mode) return;
    const generation = epoch.current;
    const operation = `builder-mode:${conversationId}`;
    let sent = false;
    mutating.current = true; sequence.current++; setBusy(true); setError('');
    try {
      // Owner confirmation finishes and closes before the secondary dialog mounts.
      await (next === 'yolo' ? yoloOwner : owner).run('builder.mode.set', { conversation_id: conversationId, requested_mode: next }, async () => undefined);
      if (generation !== epoch.current) return;
      if (next === 'yolo') {
        const acknowledged = await new Promise<boolean>(resolve => { yoloChoice.current = resolve; setYolo(true); });
        yoloChoice.current = null; setYolo(false);
        if (!acknowledged || generation !== epoch.current) return;
      }
      const payload = { conversation_id: conversationId, mode: next, expected_revision: mode.revision, ...(next === 'yolo' ? { yolo_acknowledged: true as const } : {}) };
      const request_key = mutationKey(scope, operation, payload);
      sent = true;
      const value = await client.builderModeSet({ ...payload, request_key });
      if (!validBuilderMode(value, conversationId)) throw new Error('Invalid mode receipt. Refresh server state.');
      mutationDone(scope, operation);
      if (generation === epoch.current) {
        // Re-read canonical state, including server expiry/network restrictions.
        setMode(value);
        const current = await client.builderModeGet(conversationId);
        if (!validBuilderMode(current, conversationId)) throw new Error('Invalid server mode record.');
        if (generation === epoch.current) setMode(current);
      }
    } catch (e) {
      if (sent && executionRefused(toPortalError(e).code)) mutationDone(scope, operation);
      if (generation === epoch.current && toPortalError(e).code !== 'owner_cancelled') setError(isRateLimited(toPortalError(e)) ? toPortalError(e).detail : `${toPortalError(e).detail || toPortalError(e).code} Refresh server state before retrying an uncertain change.`);
    } finally { if (generation === epoch.current) { mutating.current = false; setBusy(false); } }
  }
  const expired = mode?.mode === 'yolo' && builderTime(mode.expires_at) <= now;
  const disabled = busy || !mode || expired;
  return <section className="builder-mode-bar" aria-label="Builder permissions">
    <label>Mode <select aria-label="Builder mode" value={mode?.mode ?? ''} disabled={disabled} onChange={e => void change(e.target.value as BuilderMode)}>
      {!mode && <option value="">Reading server mode…</option>}
      {builderModes.map(m => <option key={m} value={m}>{m[0].toUpperCase() + m.slice(1)}</option>)}
    </select></label>
    {mode?.project_id && mode.result_path && <button type="button" onClick={() => {
      setOptions(true);setPreview(true);setFile({project:mode.project_id!,path:mode.result_path!});
    }}>Open result</button>}
    {mode?.project_id && mode.result_path && libraryStore && <SaveToLibrary client={client} store={libraryStore} projectId={mode.project_id} />}
    {mode && <span className="builder-network-indicator" title="Project network policy">Network {expired ? 'awaiting server refresh' : mode.mode === 'yolo' ? 'unrestricted (logged)' : 'bounded'}{mode.mode === 'yolo' && (expired ? ' · Expired; refreshing Auto from server' : ` · ${Math.max(0, Math.ceil((builderTime(mode.expires_at) - now) / 60000))} min left`)}</span>}
    <details className="builder-options" open={options} onToggle={e => setOptions(e.currentTarget.open)}><summary aria-label={`Workspace and network options${mode?.approvals?.length ? `, ${mode.approvals.length} pending approvals` : ''}`}>···{mode?.approvals?.length ? ` (${mode.approvals.length})` : ''}</summary>
    {options && <div className="builder-options-content">
    <button type="button" disabled={busy || !mode} onClick={() => void (async () => {
      setBusy(true);setError('');
      try { await client.builderTaskEnd(conversationId); window.dispatchEvent(new CustomEvent('bob-builder-changed', { detail: { conversation_id: conversationId } })); }
      catch (e) { setError(toPortalError(e).detail); }
      finally { setBusy(false); }
    })()}>End task approval</button>
    {mode && <>
      <details><summary>Network domains</summary><p>Allowlist: {mode.network.allowlist.join(', ') || 'None'}</p><p>Project extras: {mode.network.project_extra.join(', ') || 'None'}</p></details></>}
    <details><summary>View network log</summary><BuilderNetworkLog client={client} conversationId={conversationId} refreshKey={refreshKey} /></details>
    {mode?.project_id && <details><summary>Edit allowlist</summary><BuilderNetworkSettings client={client} catalog={catalog} scope={scope} projectId={mode.project_id} conversationId={conversationId} refreshKey={refreshKey} owner /></details>}
    <button type="button" onClick={() => setPreview(p => !p)}>Preview workspace file</button>
    {preview && mode?.project_id && <>
      <WorkspaceBrowser key={mode.project_id} client={client} projectId={mode.project_id} onFile={path => setFile({ project: mode.project_id!, path })} />
      {file?.project === mode.project_id && <BuilderPreview key={`${conversationId}:${mode.project_id}:${file.path}`} client={client} conversationId={conversationId} initialPath={file.path} autoLoad readOnlyPath />}
    </>}
    {Array.isArray(mode?.approvals) && mode.approvals.length > 0 && <details open><summary>Pending Builder actions</summary><div style={{ maxHeight: '30vh', overflow: 'auto' }}>
      {mode.approvals.slice(0, 100).filter(a => a.conversation_id === conversationId && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(a.approval_id)).map(a =>
        <BuilderApprovalCard key={a.approval_id} approvalId={a.approval_id} client={client} catalog={catalog} conversationId={conversationId} scope={scope} refreshKey={refreshKey} />)}
    </div></details>}
    </div>}</details>
    {store.error && <small role="status"><ReadFaultText client={client} error={store.fault} fallback={store.error} /></small>}
    {error && <p role="alert">{error}</p>}{owner.dialog}{yoloOwner.dialog}
    {yolo && <YoloConfirm onCancel={() => yoloChoice.current?.(false)} onConfirm={() => yoloChoice.current?.(true)} />}
  </section>;
}
