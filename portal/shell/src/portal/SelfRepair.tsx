import { useEffect, useRef, useState } from 'react';
import type { NativeInvoke } from '../workbench/workbench-client';
export type Observation = { state: 'healthy' | 'degraded' | 'unreachable' | 'wedged' | 'stopped'; observed: string };
type Backup = { id: string; time: number; path: string };
export type Repair = { observation: Observation; repairable?: boolean; needs?: string; diagnostics?: string; backup?: Backup; receipts?: { time: number; step: string; exit: number | null; verification: Observation }[] };
/** The runtime needs attention (degraded, unreachable or wedged): the repair card is offered. */
export function isStuck(status: Repair | null) { return !!status && !['healthy', 'stopped'].includes(status.observation.state); }
/** Only a wedged runtime is "stuck". A healthy runtime whose desktop link dropped is merely offline. */
export function isWedged(status: Repair | null) { return status?.observation.state === 'wedged'; }
export function useRepairStatus(invoke: NativeInvoke, enabled = true) {
  const [status, setStatus] = useState<Repair | null>(null);
  const [statusError, setStatusError] = useState('');
  const refresh = useRef<() => Promise<void>>(async () => {});
  useEffect(() => {
    let alive = true, timer: ReturnType<typeof setTimeout>;
    setStatus(null); setStatusError('');
    const read = async () => {
      try {
        const next = await invoke<Repair>('runtime_repair_status');
        if (!next?.observation) throw new Error('Runtime status unavailable: invalid receipt.');
        if (alive) { setStatusError(''); setStatus(old => old?.needs ? { ...old, ...next, needs: old.needs, backup: old.backup } : next); }
      } catch (reason) { if (alive) { setStatus(null); setStatusError(String(reason)); } }
    };
    refresh.current = read;
    const poll = async () => { await read(); if (alive) timer = setTimeout(() => void poll(), 30000); };
    if (enabled) void poll();
    return () => { alive = false; clearTimeout(timer); };
  }, [invoke, enabled]);
  return { status, statusError, setStatus, refresh: () => refresh.current() };
}
/** A compact card inside Activity. Native IPC is independent of chat content. */
export function SelfRepair({ invoke, settings = false, onRepaired, control }: { invoke: NativeInvoke; settings?: boolean; onRepaired?: () => void; control?: ReturnType<typeof useRepairStatus> }) {
  const local = useRepairStatus(invoke, !control), { status, statusError, setStatus, refresh } = control ?? local;
  const [busy, setBusy] = useState(false), [didRepair, setDidRepair] = useState(false);
  const [error, setError] = useState(''), [confirm, setConfirm] = useState<'shutdown' | 'restore' | null>(null);
  const active = useRef(false);
  // Older native Windows receipts predate repairable. Require the native Windows host
  // as well as a resolved receipt; missing capability on any other host stays unknown.
  const legacyWindows = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
    && /Windows/i.test(window.navigator.userAgent);
  const supported = !!status && (status.repairable === true || (status.repairable == null && legacyWindows));
  useEffect(() => { if (!busy) return; const timer = setInterval(() => void refresh(), 1000); return () => clearInterval(timer); }, [busy]);
  async function run(shutdown = false, restore = false) {
    if (active.current || !supported) return;
    active.current = true; setDidRepair(true); setBusy(true); setError(''); setConfirm(null);
    try {
      if (restore) {
        const result = await invoke<{ verification: Observation }>('runtime_repair_restore', { backupId: status?.backup?.id });
        setStatus({ observation: result.verification });
        if (result.verification.state === 'healthy') onRepaired?.();
      } else {
        const result = await invoke<Repair>('runtime_repair_start', { shutdownConfirmed: shutdown });
        setStatus(result);if (result.observation.state === 'healthy') onRepaired?.();
      }
    } catch (e) { setError(String(e)); }
    finally { active.current = false; setBusy(false); }
  }
  if (!settings && !busy && !isStuck(status) && !didRepair) return null;
  return <section className="repair-card" aria-label="Repair BoB" id="repair-card" tabIndex={-1}>
    <p>{status?.observation.observed ?? (statusError ? 'Runtime status unavailable.' : 'Checking runtime status…')}</p>
    {supported ? <button disabled={busy} onClick={() => void run()}>Repair</button>
      : <p>{status?.repairable === false ? 'No automatic repair is available for this runtime. Use the reported status above.' : 'Automatic repair capability is unavailable until supported by runtime status.'}</p>}
    {statusError && <p role="alert">{statusError}</p>}
    {busy && <p role="status">Repairing and checking the result…</p>}
    {supported && status?.needs === 'shutdown_confirmation' && <><p>Service and BoB restarts did not restore health. The next step stops every WSL distro, including Docker and Ubuntu. Save your work first.</p>
      <button disabled={busy} onClick={() => setConfirm('shutdown')}>Review WSL shutdown</button></>}
    {supported && status?.needs === 'restore_confirmation' && status.backup && <button disabled={busy} onClick={() => setConfirm('restore')}>Review backup restore</button>}
    {supported && confirm && <div className="owner-modal-backdrop"><div className="set-card owner-modal" role="dialog" aria-modal="true" aria-label={confirm === 'shutdown' ? 'Confirm all WSL shutdown' : 'Confirm backup restore'}>
      {confirm === 'shutdown' ? <p>This stops all WSL distros, including Docker and Ubuntu, then starts BoB. Continue?</p> :
        <p>Restore backup {status?.backup?.id}, saved {new Date((status?.backup?.time ?? 0) * 1000).toLocaleString()} at {status?.backup?.path}. This replaces the current BoB space. All projects, messages, memory, model setup and settings changed since that backup will be lost.</p>}
      <button onClick={() => setConfirm(null)}>Cancel</button><button onClick={() => void run(confirm === 'shutdown', confirm === 'restore')}>{confirm === 'shutdown' ? 'Stop all WSL and continue' : 'Restore this named backup and discard later changes'}</button>
    </div></div>}
    {!!status?.receipts?.length && <details open={busy}><summary>Repair steps ({status.receipts.length})</summary><ol>{status.receipts.map((r, i) => <li key={i}>{r.step} · exit {r.exit ?? 'unknown'} · {r.verification.state}</li>)}</ol></details>}
    {status?.diagnostics && <p role="status">Sanitized diagnostics saved locally: {status.diagnostics}</p>}
    {status?.needs === 'manual_repair' && <p role="alert">Automatic repair could not establish health. No registered backup is available. Keep the diagnostics for support.</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
