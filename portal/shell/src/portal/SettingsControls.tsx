import type { NativeInvoke } from '../workbench/workbench-client';
import { useEffect, useRef, useState } from 'react';
import type { PortalClient } from './portal-client';
import type { ApprovalPolicy, Catalog, PolicyProjection, SecretReceipt } from './portal-protocol';
import { isRateLimited, toPortalError, txt } from './portal-protocol';
import { canonical, executionRefused, mutationDone, mutationKey, operationReason, pendingMutation, validInteger } from './durable-runs';
import { useOwnerAction } from './OwnerControls';
import { expectProviderRestart } from './provider-restart';
import { MODEL_APPLYING, retryModelRead } from './model-reload';

function validReceipt(value: SecretReceipt, provider: string): boolean {
  return value?.stored === true && value.provider === provider && typeof value.fingerprint === 'string' && /^[0-9a-f]{16}$/.test(value.fingerprint) && typeof value.operation_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value.operation_id);
}

function receiptProviders(scope: string): string[] {
  const prefix = `bob:settings-receipt:${scope}:`, providers: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const name = localStorage.key(i) ?? '', provider = name.slice(prefix.length);
      if (name.startsWith(prefix) && /^[a-z0-9_-]{1,64}$/.test(provider) && /^[A-Za-z0-9_-]{1,128}$/.test(localStorage.getItem(name) ?? '')) providers.push(provider);
    }
  } catch { /* safe receipt storage is optional */ }
  return providers;
}

export function ProviderKeys({ client, catalog, owner, scope, invoke }: { client: PortalClient; catalog: Catalog | null; owner: boolean; scope: string; invoke?: NativeInvoke }) {
  const [saved, setSaved] = useState<Record<string, boolean>>({});
  useEffect(() => {
    let alive = true;
    setSaved({});
    const refresh = () => {
      if (owner && invoke) void retryModelRead(invoke, () => invoke<{ providers: Record<string, boolean> }>('runtime_model_status'))
        .then(status => { if (alive) setSaved(status.providers); }, () => { /* receipt remains available */ });
    };
    refresh(); window.addEventListener('bob-model-changed', refresh);
    return () => { alive = false; window.removeEventListener('bob-model-changed', refresh); };
  }, [invoke, owner, scope]);
  if (!owner) return null;
  const reason = operationReason(catalog, 'secrets.set');
  const cap = catalog?.capabilities.operations?.['secrets.set'];
  const providers = (cap?.storage_only === true || cap?.enrolls_backend === true) && Array.isArray(cap.providers) ? [...new Set(cap.providers.filter(p => typeof p === 'string' && /^[a-z0-9_-]{1,64}$/.test(p)))] : [];
  const labels = new Map((Array.isArray(cap?.provider_display) ? cap.provider_display : [])
    .filter(row => row && typeof row.id === 'string' && typeof row.label === 'string' && row.label)
    .map(row => [row.id, row.label]));
  const pendingProviders = receiptProviders(scope).filter(provider => !providers.includes(provider));
  return <section className="set-card"><h3>Provider keys</h3>
    <p>{cap?.enrolls_backend === true ? 'Keys are stored only on the server. Saving a key turns that provider’s model on for this lane. It does not change your default if you already have one or enable paid fallbacks.' : 'Keys are stored only on the server. Storage does not enroll a provider, change defaults or enable paid fallbacks.'}</p>
    {reason && <p role="status">{reason}</p>}
    {!providers.length && <p>No storage-capable providers advertised by this server.</p>}
    {providers.map(provider => <ProviderKey key={`${scope}:${provider}`} client={client} catalog={catalog} provider={provider} label={labels.get(provider) || provider} saved={saved[`${provider}_key`] === true} scope={scope} disabled={!!reason} />)}
    {!!pendingProviders.length && <><h4>Pending storage receipts</h4><p>Safe lookup remains available when storage is no longer advertised.</p>
      {pendingProviders.map(provider => <ProviderKey key={`${scope}:${provider}`} client={client} catalog={catalog} provider={provider} scope={scope} disabled receiptOnly />)}</>}
  </section>;
}

function ProviderKey({ client, catalog, provider, label = provider, saved = false, scope, disabled, receiptOnly = false }: { client: PortalClient; catalog: Catalog | null; provider: string; label?: string; saved?: boolean; scope: string; disabled: boolean; receiptOnly?: boolean }) {
  const field = useRef<HTMLInputElement>(null);
  const action = useOwnerAction(client, catalog);
  const storageId = `bob:settings-receipt:${scope}:${provider}`;
  const [lookup, setLookup] = useState(() => {
    try { const key = localStorage.getItem(storageId); return key && /^[A-Za-z0-9_-]{1,128}$/.test(key) ? key : ''; } catch { return ''; }
  });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [savedHere, setSavedHere] = useState(false);
  const [receiptDetails, setReceiptDetails] = useState<SecretReceipt | null>(null);
  const [retryMessage, setRetryMessage] = useState('');
  async function applied(confirmation: string) {
    setMessage(MODEL_APPLYING); setRetryMessage('');
    try { await client.modelsChanged(); setMessage(confirmation); }
    catch (error) { setMessage(toPortalError(error).detail); setRetryMessage(confirmation); }
  }
  const clear = () => { if (field.current) field.current.value = ''; };
  useEffect(() => () => clear(), []);
  const forget = () => { setLookup(''); try { localStorage.removeItem(storageId); } catch { /* optional safe receipt persistence */ } };
  const stored = async (receipt: SecretReceipt, wasSaved?: boolean) => {
    if (!validReceipt(receipt, provider)) throw JSON.stringify({ code: 'invalid_receipt' });
    const enrolls = catalog?.capabilities.operations?.['secrets.set']?.enrolls_backend === true;
    const starting = enrolls && wasSaved === false;
    if (enrolls) expectProviderRestart(client, label);
    setSavedHere(true);
    setReceiptDetails(receipt);
    forget();
    const confirmation = starting ? `Saved. ${label} is turned on.` : enrolls && wasSaved === true ? `Saved. The new ${label} key is in use.` : 'Saved.';
    if (enrolls) await applied(confirmation); else setMessage(confirmation);
  };
  async function submit() {
    const wasSaved = saved || savedHere;
    let key = field.current?.value ?? '';
    clear(); setMessage(''); setReceiptDetails(null); setBusy(true);
    try {
      if (!key || new TextEncoder().encode(key).length > 16384 || /\s|[\x00-\x1f\x7f]/.test(key)) { setMessage('Enter a key without spaces or line breaks, up to 16384 bytes.'); return; }
      const reply = await action.run('secrets.set', { provider }, async () => {
        try { return await client.secretSet(provider, key); } finally { key = ''; }
      });
      await stored(reply, wasSaved);
    } catch (e) {
      const error = toPortalError(e);
      if (error.request_key) {
        setLookup(error.request_key);
        try { localStorage.setItem(storageId, error.request_key); } catch { /* memory still retains the safe lookup */ }
      }
      const refused = ['unsupported_provider', 'invalid_key', 'invalid_args', 'bad_request', 'not_dispatched', 'owner_cancelled', 'owner_session_invalid', 'owner_session_expired', 'owner_confirmation_required', 'operation_unavailable', 'client_upgrade_required'].includes(error.code);
      if (isRateLimited(error) || refused) { forget(); setMessage(isRateLimited(error) ? error.detail : `Storage refused (${error.code}).`); }
      else setMessage('Bob did not confirm the save. Check whether it was stored');
    } finally { key = ''; clear(); setBusy(false); }
  }
  async function status() {
    clear(); setBusy(true);
    try {
      const reply = await client.settingsStatus({ request_key: lookup });
      if (reply.phase === 'ready' && reply.result) await stored(reply.result);
      else if (reply.phase === 'failed') { forget(); setMessage(`Storage failed · receipt ${reply.operation_id}.`); }
      else setMessage(`Storage ${reply.phase} · receipt ${reply.operation_id}. Outcome not yet confirmed.`);
    } catch (e) { setMessage(`Safe receipt unavailable (${toPortalError(e).code}). Credentials were not retried.`); }
    finally { clear(); setBusy(false); }
  }
  const statusReason = operationReason(catalog, 'settings.status', 1);
  return <div className="key-row">{action.dialog}<strong>{label}</strong>{(saved || savedHere) && <span> · Saved</span>}
    {!receiptOnly && <form className="key-entry" onSubmit={e => { e.preventDefault(); if (!busy && !disabled && !lookup) void submit(); else clear(); }}>
      <input ref={field} aria-label={`New key for ${provider}`} type="password" autoComplete="off" spellCheck={false} disabled={busy || disabled || !!lookup} />
      <button className="primary" disabled={busy || disabled || !!lookup}>{busy ? 'Storing…' : 'Store key'}</button>
    </form>}
    {message && <p role="status">{message}</p>}
    {retryMessage && <button onClick={() => void applied(retryMessage)}>Try loading models again</button>}
    {receiptDetails && <details><summary>Details</summary><small>Server fingerprint {receiptDetails.fingerprint} · receipt {receiptDetails.operation_id}</small></details>}
    {lookup && <><p>Pending safe receipt: {lookup}</p><button type="button" disabled={busy || !!statusReason} onClick={() => void status()}>Check storage receipt</button>
      {statusReason && <p>{statusReason}</p>}</>}
  </div>;
}

const emptyPolicy = (): ApprovalPolicy => ({ posture: 'scoped', scope: { auto_actions: [], escalate_actions: [], may_touch: [], may_not_touch: [] } });
export function ApprovalPolicyEditor({ client, catalog, owner, scope }: { client: PortalClient; catalog: Catalog | null; owner: boolean; scope: string }) {
  return owner ? <OwnerApprovalPolicyEditor key={scope} client={client} catalog={catalog} scope={scope} /> : null;
}

function OwnerApprovalPolicyEditor({ client, catalog, scope }: { client: PortalClient; catalog: Catalog | null; scope: string }) {
  const action = useOwnerAction(client, catalog);
  const [current, setCurrent] = useState<PolicyProjection | null>(null);
  const [draft, setDraft] = useState(() => JSON.stringify(pendingMutation<{ policy: ApprovalPolicy }>(scope, 'policy.set')?.policy ?? emptyPolicy(), null, 2));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const readReason = operationReason(catalog, 'policy.get'), writeReason = operationReason(catalog, 'policy.set');
  async function read() {
    setBusy(true); setMessage('');
    try { setCurrent(await action.run('policy.get', {}, () => client.policyGet())); }
    catch (e) { setMessage(`Policy read failed (${toPortalError(e).code}).`); }
    finally { setBusy(false); }
  }
  async function save() {
    setBusy(true); setMessage('');
    try {
      const policy = JSON.parse(draft) as ApprovalPolicy;
      const pending = pendingMutation<{ policy: ApprovalPolicy; expected_revision: PolicyProjection['revision'] }>(scope, 'policy.set');
      if (pending && canonical(pending.policy) !== canonical(policy)) { setMessage('An earlier policy change is unresolved. Restore that draft and reconcile it first.'); return; }
      if (!current && !pending) return;
      const proposal = pending ?? { policy, expected_revision: current!.revision };
      const key = mutationKey(scope, 'policy.set', proposal);
      const result = await action.run('policy.set', {}, () => client.policySet(proposal.policy, proposal.expected_revision, key));
      if (!validInteger(result.revision) || !result.policy || typeof result.operation_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(result.operation_id) || !Array.isArray(result.paused_runs) || result.paused_runs.some(id => typeof id !== 'string')) throw JSON.stringify({ code: 'invalid_receipt' });
      mutationDone(scope, 'policy.set'); setCurrent(result);
      setMessage(`Policy saved · receipt ${result.operation_id} · paused runs: ${result.paused_runs.join(', ') || 'none'}`);
    } catch (e) {
      const error = toPortalError(e);
      if (error.code === 'revision_conflict') {
        mutationDone(scope, 'policy.set');
        setMessage('Revision conflict: policy changed on the server. Read the current policy, review your draft, and save again.');
        setCurrent(null);
      } else if (e instanceof SyntaxError) setMessage('Enter valid policy JSON.');
      else {
        if (executionRefused(error.code) || ['invalid_policy', 'invalid_policy_action', 'invalid_policy_path', 'resource_not_found', 'owner_cancelled'].includes(error.code)) mutationDone(scope, 'policy.set');
        setMessage(isRateLimited(error) ? error.detail : `Policy change ${executionRefused(error.code) ? 'refused' : 'unconfirmed'} (${error.code}). Reconcile the same draft before making another change.`);
      }
    } finally { setBusy(false); }
  }
  return <section className="set-card">{action.dialog}<h3>Approval policy</h3>
    <p>Policy adds a ceiling to existing permissions. It cannot expand signed scope, grant owner controls, enroll providers or bypass isolation and always-human actions.</p>
    <p>Narrowing can pause conflicting runs and cancel workers. Already dispatched effects remain charged or unknown. Widening never resumes work. Scoped and confirm postures refuse opaque shell, Python and CLI worker execution.</p>
    <button type="button" disabled={busy || !!readReason} onClick={() => void read()}>Read approval policy</button>{readReason && <p>{readReason}</p>}
    {current && <><p>Revision {current.revision} · {current.policy?.posture ?? 'No additional policy installed'}</p><h4>Effective policy</h4><pre>{JSON.stringify(current.policy, null, 2)}</pre>
      <h4>Immutable restrictions</h4><pre>{txt(current.immutable_restrictions)}</pre></>}
    <p>This draft replaces the complete policy. Edit posture: inherit (empty scope), confirm (human review), or scoped. Actions use installed action IDs. Path rules use <code>{'{"attachment_id":"registered-id","path":"src/**"}'}</code>. Review and enter all intended rules; effective absolute paths above are never copied into this draft.</p>
    <label>Policy draft<textarea aria-label="Policy draft" rows={16} value={draft} disabled={busy} onChange={e => setDraft(e.target.value)} /></label>
    <button type="button" disabled={busy || !!writeReason || (!current && !pendingMutation(scope, 'policy.set'))} onClick={() => void save()}>{pendingMutation(scope, 'policy.set') ? 'Reconcile policy change' : 'Save approval policy'}</button>{writeReason && <p>{writeReason}</p>}
    {message && <p role="status">{message}</p>}
  </section>;
}
