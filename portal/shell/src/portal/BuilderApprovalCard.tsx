import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { BuilderApproval, BuilderDecision, Catalog } from './portal-protocol';
import { builderTime, isRateLimited, toPortalError } from './portal-protocol';
import { useOwnerAction } from './OwnerControls';
import { canonical, mutationDone, mutationKey } from './durable-runs';
import { PermissionSummary, permissionDisplay } from './PermissionSummary';
import { validBuilderApproval } from './builder-read-validation';
import { useBuilderRead } from './builder-read-store';
import { ReadFaultText } from './provider-restart';
import type { LibraryStore } from './library';
import { useExperience } from './experience';

const id = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);

// Model text supplies lookup identifiers only. Never use its summary/detail/mode.
export function builderApprovalIds(content: string, conversationId: string): string[] {
  const ids = new Set<string>();
  for (const match of content.matchAll(/```bob-builder-approval[^\S\n]*\n([\s\S]*?)```/g)) {
    if (match[1].length > 512 * 1024) continue;
    try {
      const value = JSON.parse(match[1]);
      if (value?.schema_version === 1 && value.conversation_id === conversationId && id(value.approval_id)) ids.add(value.approval_id);
    } catch { /* incomplete or malformed blocks are inert */ }
  }
  return [...ids];
}

export { validBuilderApproval } from './builder-read-validation';

export function BuilderApprovalCards(props: { content: string; client: PortalClient; catalog: Catalog | null; conversationId: string; scope: string; refreshKey?: string; libraryStore?: LibraryStore; includePending?: boolean }) {
  const contentIds = builderApprovalIds(props.content, props.conversationId);
  const store = useBuilderRead(props.client, props.conversationId, props.scope, props.refreshKey, undefined, props.includePending || contentIds.length > 0);
  const ids = [...new Set([...contentIds, ...(props.includePending ? (store.mode?.approvals ?? []).filter(a => a.conversation_id === props.conversationId && id(a.approval_id)).map(a => a.approval_id) : [])])];
  const decided = ids.filter(id => { const row = store.records.get(id); return row && row.state !== 'pending'; });
  const pending = ids.filter(id => !decided.includes(id));
  const card = (approvalId: string) => <BuilderApprovalCard key={`${props.conversationId}:${approvalId}`} {...props} approvalId={approvalId} />;
  return <>{pending.map(card)}{decided.length > 1 ? <details className="builder-steps"><summary>{builderStepsSummary(decided.map(id => store.records.get(id)!))}</summary>{decided.map(card)}</details> : decided.map(card)}</>;
}

export function BuilderApprovalCard({ client, catalog, conversationId, approvalId, scope, refreshKey }: {
  client: PortalClient; catalog: Catalog | null; conversationId: string; approvalId: string; scope: string; refreshKey?: string; libraryStore?: LibraryStore;
}) {
  const simple = useExperience() === 'simple';
  const owner = useOwnerAction(client, catalog, false, JSON.stringify([scope, conversationId, approvalId, refreshKey]));
  const store = useBuilderRead(client, conversationId, scope, refreshKey, approvalId);
  const record = store.records.get(approvalId) ?? null;
  const setRecord = (value: BuilderApproval | null) => store.setRecord(approvalId, value);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());
  const epoch = useRef(0);
  const mutating = useRef(false);
  const readSequence = useRef(0);
  const read = async (_generation: number) => { if (!mutating.current) await store.refresh(); };
  useEffect(() => {
    epoch.current++; mutating.current = false; setError(''); setBusy(false);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => { epoch.current++; clearInterval(tick); };
  }, [client, conversationId, approvalId, refreshKey, scope]);
  useEffect(() => { if (record && record.state !== 'pending') mutationDone(scope, `builder-approval:${approvalId}`); }, [record, scope, approvalId]);
  async function decide(decision: BuilderDecision) {
    if (!record || busy || record.state !== 'pending' || builderTime(record.expires_at) <= Date.now()) return;
    const generation = epoch.current;
    mutating.current = true; readSequence.current++; setBusy(true); setError('');
    try {
      await owner.run('builder.approve', { approval_id: approvalId }, async () => {
        const current = await client.builderApprovalGet(approvalId);
        if (generation !== epoch.current) throw new Error('Conversation changed.');
        if (!validBuilderApproval(current, conversationId, approvalId) || canonical(current) !== canonical(record) || builderTime(current.expires_at) <= Date.now()) {
          setRecord(null); throw new Error('Approval changed or expired. Refresh and review it again.');
        }
        const payload = { approval_id: approvalId, decision };
        const operation = `builder-approval:${approvalId}`;
        const result = await client.builderApprove(approvalId, decision, mutationKey(scope, operation, payload));
        if (result.approval_id !== approvalId || !['allowed', 'denied', 'expired', 'stopped'].includes(result.state)) throw new Error('Invalid decision receipt. Refresh server state.');
        mutationDone(scope, operation);
        if (generation === epoch.current) {
          setRecord({ ...current, state: result.state, execution_state: result.execution_state, result: result.result });
          window.dispatchEvent(new CustomEvent('bob-builder-changed', { detail: { conversation_id: conversationId } }));
        }
      });
    } catch (e) {
      if (generation === epoch.current && toPortalError(e).code !== 'owner_cancelled') {
        const typed = toPortalError(e);
        if (isRateLimited(typed)) {
          mutationDone(scope, `builder-approval:${approvalId}`);
          setError(typed.detail);
        } else {
          setRecord(null); setError(`${typed.detail || typed.code} Read server state before retrying an uncertain decision.`);
        }
      }
    } finally { if (generation === epoch.current) { mutating.current = false; setBusy(false); } }
  }
  const expired = record && (record.state === 'expired' || builderTime(record.expires_at) <= now && record.state === 'pending');
  const disabled = busy || !record || record.state !== 'pending' || !!expired;
  const readButton = <button type="button" disabled={busy} onClick={() => void read(epoch.current)}>Read server state</button>;
  const detail = record && <>
      <PermissionSummary value={record} fallback={record.summary} fullRecord={record.state !== 'pending'} /><p>Authorization: {expired ? 'expired' : record.state} · Mode: {record.mode}</p>
      <p role="status">{builderOutcome(record, !!expired)}</p>
      {record.session_until && <p>Task scope ends by {new Date(record.session_until * 1000).toLocaleTimeString()}, or when this task ends, you stop, change workspace or mode, or log out.</p>}
      {record.state === 'pending' && <p>This approval expires at {new Date(builderTime(record.expires_at)).toLocaleTimeString()}.</p>}
      {record.kind === 'network' && <><p>Domain: {record.detail.domain}</p><p>Method: {record.detail.method ?? 'not visible (HTTPS CONNECT)'}</p></>}
      {record.kind === 'workspace' && <><p>{record.detail.action === 'create' ? 'Create and mount' : 'Mount'}: {record.detail.root_label}</p>
        <p>Permissions reset to Approve. Existing workspace files are kept.</p>
        {record.result?.receipt?.kind === 'workspace' && record.result.status === 'completed' && <p role="status">{record.result.receipt.text}</p>}
      </>}
      {record.detail.path && <p>Path: {record.detail.path}</p>}
      {record.detail.cwd && <p>Working directory: {record.detail.cwd}</p>}
      {record.kind === 'command' && <p>Network: bounded by the project allowlist; new domains need approval.</p>}
  </>;
  const decided = record && record.state !== 'pending';
  return <section className={decided ? 'builder-step' : 'set-card builder-pending'} aria-label="Builder approval" id={'approval-'+approvalId} tabIndex={-1} style={{ minWidth: 0 }}>
    {record ? decided ? <div className="builder-step-line"><span>{builderActionLine(record)}</span><details><summary>Details</summary>{detail}{readButton}</details></div> : <>
      <h3>Builder {record.kind === 'workspace' ? 'workspace approval' : record.kind === 'network' ? 'network approval' : record.mode === 'suggest' ? 'proposal' : 'approval'}</h3>
      {simple ? <>
        <p>{builderActionLabel(record, false)}</p>
        <PermissionSummary value={record} fallback={record.summary} collapseAction={record.kind === 'write' || record.kind === 'edit' ? 'Show change' : 'Show command'} />
        <p>Authorization: {expired ? 'expired' : record.state} · Mode: {record.mode}</p>
        <p role="status">{builderOutcome(record, !!expired)}</p>
        {record.session_until && <p>Task scope ends by {new Date(record.session_until * 1000).toLocaleTimeString()}, or when this task ends, you stop, change workspace or mode, or log out.</p>}
        <p>This approval expires at {new Date(builderTime(record.expires_at)).toLocaleTimeString()}.</p>
        {record.kind === 'command' && <p>Network: bounded by the project allowlist; new domains need approval.</p>}
        {record.kind === 'workspace' && <p>Permissions reset to Approve. Existing workspace files are kept.</p>}
      </> : detail}
      <button type="button" disabled={disabled} onClick={() => void decide('allow_once')}>{record.mode === 'suggest' && record.kind !== 'network' ? 'Apply' : 'Allow once'}</button>
      {record.summary !== 'fs_delete' && record.summary !== 'checkpoint_restore' && <button type="button" disabled={disabled} onClick={() => void decide('allow_session')}>{record.kind === 'workspace' ? 'Allow for task' : 'Allow for session'}</button>}
      {record.kind === 'network' ? <><button type="button" disabled={disabled} onClick={() => void decide('allow_project')}>Always for this project</button><p>Project allowance saves this domain. HTTPS CONNECT hides the request method; method limits are best effort.</p></> : record.kind !== 'workspace' && !permissionDisplay(record) && <p>Session scope is unavailable from this server. Review Details before allowing.</p>}
      <button type="button" disabled={disabled} onClick={() => void decide('deny')}>Deny</button>
      {readButton}
    </> : <><p>Waiting for a verified server approval record.</p>{readButton}</>}
    {(store.error || store.recordErrors.get(approvalId)) && <small role="status"><ReadFaultText client={client} error={store.fault} fallback={store.error || store.recordErrors.get(approvalId)} /></small>}
    {error && <p role="alert">{error}</p>}{owner.dialog}
  </section>;
}

export function builderOutcome(record: BuilderApproval, expired = false): string {
  if (record.state === 'stopped' || record.execution_state === 'stopped') return 'Stopped · nothing will be replayed.';
  if (record.state === 'denied') return 'Denied · this action was not run.';
  if (expired || record.state === 'expired') return 'Expired · request a fresh proposal; this action will not run.';
  if (record.execution_state === 'unknown' || record.result?.status === 'unknown') return 'Outcome unknown · inspect the receipt before requesting any new action. Nothing is replayed.';
  if (record.state === 'pending') return 'Waiting for you';
  if (record.execution_state === 'failed' || record.result?.error || (record.result?.exit_code !== undefined && record.result.exit_code !== 0)) {
    const reason = record.result?.error?.code ?? `exit code ${record.result?.exit_code ?? 'unknown'}`;
    return `${record.kind === 'write' || record.kind === 'edit' ? 'Write failed' : 'A check or action did not pass'}${record.detail.path ? ': ' + record.detail.path : ''} (${reason}). The Builder can look at why — ask it to continue.`;
  }
  if (record.execution_state === 'completed' || record.result?.status === 'completed' || record.result?.exit_code === 0) return 'Done';
  return 'Running · waiting for the execution receipt.';
}

export function builderActionLabel(record: BuilderApproval, past = true): string {
  const path = record.detail.path || 'project files';
  if (record.kind === 'write') return `${past ? 'Wrote' : 'Write'} ${path}`;
  if (record.kind === 'edit') return `${past ? 'Updated' : 'Change'} ${path}`;
  if (record.kind === 'network') return `${past ? 'Connected' : 'Connect'} to ${record.detail.domain || 'a network domain'}`;
  if (record.kind === 'workspace') return `${past ? 'Opened' : 'Open'} workspace ${record.detail.root_label || record.detail.name || ''}`.trim();
  const command = record.detail.command || record.summary;
  if (/^(?:pwd|ls|find|cat|head|tail|rg|tree)\b/.test(command.trim())) return past ? 'Looked at project files' : 'Look at project files';
  const check = /\b(test|check|pytest|unittest|assert|chk)\b/i.test(command);
  return `${past ? 'Ran' : 'Run'} a ${check ? 'check' : 'command'}`;
}

export function builderActionLine(record: BuilderApproval): string {
  const outcome = builderOutcome(record);
  if (['denied', 'stopped', 'expired'].includes(record.state)) return outcome.split(' · ')[0];
  const label = builderActionLabel(record);
  if (outcome === 'Done') return label + (record.kind === 'command' ? ' · passed' : '');
  if (outcome.startsWith('Outcome unknown')) return label + ' · outcome unknown — nothing will be replayed';
  if (outcome.startsWith('Stopped')) return label + ' · stopped — nothing will be replayed';
  if (record.execution_state === 'failed' || record.result?.error || record.result?.exit_code !== undefined && record.result.exit_code !== 0) return label + ' · did not pass';
  return builderActionLabel(record, false) + ' · running';
}

export function builderStepsSummary(records: BuilderApproval[]): string {
  const failed = records.filter(row => builderOutcome(row).includes('ask it to continue')).length;
  return `${records.length} steps · ${failed ? `${failed} did not pass` : records.every(row => builderOutcome(row) === 'Done') ? 'all passed' : 'review outcomes'}`;
}
