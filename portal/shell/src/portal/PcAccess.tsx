import { retryModelRead } from './model-reload';
import { ActivityTrail } from './ActivityTrail';
import { useEffect, useRef, useState } from 'react';
import type { NativeInvoke } from '../workbench/workbench-client';
export type Mode = 'suggest' | 'approve' | 'auto' | 'yolo';
type Request = { id: string; task_id: string; agent: string; conversation_id: string; operation: string; args: Record<string, unknown>; created_at?: number; expires_at: number };
export type Card = { request: Request; mode: Mode; root: string | null; until: number | null; protected_reason: string | null; running: boolean };
export type Receipt = { status: string; agent?: string; conversation_id?: string; task_id?: string; time?: number; operation?: string; reason?: string; exit_code?: number; stdout?: string; stderr?: string; receipt_path?: string; request_args?: Record<string, unknown>; elapsed_ms?: number };
export type PcQueue = { requests: Card[]; outcomes: Record<string, Receipt>; tasks?: Card[] };
export const EMPTY_PC_QUEUE: PcQueue = { requests: [], outcomes: {}, tasks: [] };

/** Wake only at a displayed minute boundary or expiry; native polling stays unchanged. */
export function usePcApprovalClock(queue: PcQueue) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const current = Date.now();
    const waits = queue.requests.filter(c => !c.running && c.request.expires_at * 1000 > current)
      .map(c => (c.request.expires_at * 1000 - current) % 60000 || 60000);
    if (!waits.length) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.max(1, Math.ceil(Math.min(...waits))));
    return () => clearTimeout(timer);
  }, [queue, now]);
  return Date.now();
}

/** The only source of PC cards/modes/receipts is native IPC, never chat content. */
export function usePcQueue(invoke: NativeInvoke, scope: string, enabled = true) {
  const [queue, setQueue] = useState<PcQueue>(EMPTY_PC_QUEUE);
  const [error, setError] = useState('');
  const [checkedAt, setCheckedAt] = useState<number | null>(null);
  const refresh = useRef<() => Promise<void>>(async () => {});
  useEffect(() => {
    let active = true, timer: ReturnType<typeof setTimeout>, reading: Promise<void> | null = null, waiting = false;
    setQueue(EMPTY_PC_QUEUE);
    const read = (): Promise<void> => {
      if (reading) return reading;
      reading = (async () => {
        try { const next = await retryModelRead(invoke, () => invoke<PcQueue>('pc_poll')); if (active && next.requests && next.outcomes) {
          waiting = next.requests.some(c => c.running || c.request.expires_at * 1000 > Date.now() && !next.outcomes[c.request.id]);
          setQueue(next); setError(''); setCheckedAt(Date.now());
        } else if (active) { setError('Actions on this PC could not be checked.'); } }
        catch { if (active) setError('Actions on this PC could not be checked.'); }
        finally { reading = null; }
      })();
      return reading;
    };
    refresh.current = enabled ? read : async () => {};
    const tick = async () => { clearTimeout(timer); await read(); if (active) timer = setTimeout(() => void tick(), waiting ? 5000 : 15000); };
    const wake = () => { if (enabled) void tick(); };
    window.addEventListener('bob-pc-changed', wake);
    window.addEventListener('focus', wake);
    if (enabled) void tick();
    return () => { active = false; clearTimeout(timer); window.removeEventListener('bob-pc-changed', wake); window.removeEventListener('focus', wake); };
  }, [invoke, scope, enabled]);
  return { queue, error, checkedAt, refresh: () => refresh.current() };
}
export function PcAccess({ invoke, scope, conversationId, queue: supplied, refresh: suppliedRefresh }: {
  invoke: NativeInvoke; scope: string; conversationId?: string | null; queue?: PcQueue; refresh?: () => Promise<void>;
}) {
  const local = usePcQueue(invoke, scope, !supplied);
  const queue = supplied ?? local.queue, refresh = suppliedRefresh ?? local.refresh;
  return <div aria-label="Native PC access" className="pc-approvals">
    {queue.requests.filter(c => c.request.conversation_id === conversationId).map(card =>
      <PcCard key={card.request.id} card={card} invoke={invoke} refresh={refresh} />)}
  </div>;
}
export function PcModeRow({ queue, conversationId, invoke, refresh }: { queue: PcQueue; conversationId?: string | null; invoke: NativeInvoke; refresh: () => Promise<void> }) {
  const tasks = queue.tasks ?? queue.requests;
  return <div className="pc-mode-row" aria-label="PC task modes">
    {tasks.filter(c => c.request.conversation_id === conversationId).map(card =>
      <details key={card.request.task_id}><summary>PC · {card.request.agent} · {card.mode}{card.until ? ` · ${Math.max(0, Math.ceil((card.until * 1000 - Date.now()) / 60000))} min left` : ''}</summary>
        <PcCard key={`${card.mode}-${card.until}`} card={card} invoke={invoke} refresh={refresh} modeOnly />
      </details>)}
    {conversationId && <button className="link" onClick={() => void invoke('pc_revoke', { conversationId }).then(refresh)}>Revoke PC access in this conversation</button>}
  </div>;
}
/** List items join the existing Activity tool trail, with full receipts on demand. */
export function PcReceipts({ queue, conversationId }: { queue: PcQueue; conversationId?: string | null }) {
  return <ActivityTrail activity={[]} queue={queue} conversationId={conversationId} />;
}
export function PcNeeds({ queue, onOpen }: { queue: PcQueue; onOpen: (id: string) => void }) {
  return <section aria-label="PC actions needing you"><h2>PC actions needing you</h2>
    {queue.requests.filter(c => !c.running).map(c => <button key={c.request.id} onClick={() => onOpen(c.request.conversation_id)}>
      {c.request.agent} · {c.request.operation} · open conversation
    </button>)}
  </section>;
}
const backslash = String.fromCharCode(92);
function localDrive(path: string): boolean {
  return /^[A-Za-z]$/.test(path[0] ?? '') && path[1] === ':' && (path[2] === backslash || path[2] === '/');
}
function suggestedRoot(request: Request): string {
  const path = String(request.args.cwd ?? request.args.path ?? '');
  if (request.operation === 'pc.list' || request.operation === 'pc.run') return path;
  if (!localDrive(path)) return 'C:' + backslash;
  const end = Math.max(path.lastIndexOf(backslash), path.lastIndexOf('/'));
  return path.slice(0, end === 2 ? 3 : end);
}
function editableFolder(root: string): string {
  // Native canonicalization returns the Win32 verbatim prefix. The executor
  // deliberately rejects device aliases from UI/model input, so display its
  // already-validated local drive scope in ordinary editable Windows syntax.
  const plain = root.startsWith(backslash + backslash + '?' + backslash) ? root.slice(4) : root;
  return localDrive(plain) ? plain : root;
}
export function PcCard({ card, invoke, refresh, modeOnly = false, approvalMinutes }: { card: Card; invoke: NativeInvoke; refresh: () => Promise<void>; modeOnly?: boolean; approvalMinutes?: number }) {
  const { request } = card;
  const info = request.operation === 'pc.info';
  const [root, setRoot] = useState(card.root ? editableFolder(card.root) : suggestedRoot(request)), [mode, setMode] = useState<Mode>(card.mode);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [risk, setRisk] = useState(false), [yolo, setYolo] = useState(false);
  async function act(decision: 'allow_once' | 'allow_task' | 'deny') {
    if (busy) return; setBusy(true); setError('');
    try { await invoke('pc_decide', { requestId: request.id, decision, taskFolder: info ? '' : root }); await refresh(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  async function setNativeMode(ack = false) {
    if (mode === 'yolo' && !ack) { setYolo(true); return; }
    setBusy(true); setError('');
    try { await invoke('pc_set_mode', { requestId: request.id, mode, taskFolder: info ? '' : root, yoloAcknowledged: ack }); setYolo(false); setRisk(false); await refresh(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  return <section className="set-card" aria-label={modeOnly ? "Native PC mode" : "Native PC approval"}>
    {!modeOnly && <><h3>{info ? `Share this PC's hardware summary (processor, graphics, memory, disk space, Windows version) with ${request.agent}?` : `${request.agent} asks for ${request.operation}`}</h3>
    {approvalMinutes !== undefined && <p className="approval-countdown" aria-live="off">{approvalMinutes} minute{approvalMinutes === 1 ? '' : 's'} left to decide</p>}
    <p>Task: Windows work in this conversation. Current PC mode: {card.mode}.</p>
    <details><summary>Agent and task identity</summary><p>Agent: {request.agent}. Task: {request.task_id}. Conversation: {request.conversation_id}.</p></details>
    <p>{info ? 'Effect: share only processor, graphics, memory, fixed disk space and Windows version.' : request.operation === 'pc.write' ? 'Effect: create or replace this Windows file atomically after checking its preimage.' : request.operation === 'pc.run' ? 'Effect: run this command as your Windows account, with its stated timeout.' : request.operation === 'pc.open' ? 'Effect: ask Windows to open this file or URL in its default app.' : 'Effect: read this Windows file or directory.'}</p>
    <pre style={{ maxHeight: '16rem', overflow: 'auto', whiteSpace: 'pre-wrap' }}>{JSON.stringify(request.args, null, 2)}</pre>
    </>}
    {!info && <label>Task folder on this PC<input aria-label="PC task folder" value={root} onChange={e => setRoot(e.target.value)} disabled={busy || card.running} /></label>}
    {!modeOnly && <>{card.protected_reason && <p role="alert">Always ask: {card.protected_reason}</p>}
    <div>{info ? <><button disabled={busy || card.running} onClick={() => void act('allow_once')}>{card.mode === 'suggest' ? 'Apply once on this PC' : 'Allow once'}</button>
      {card.mode !== 'suggest' && <button disabled={busy || card.running} onClick={() => void act('allow_task')}>Allow for this task</button>}</> :
      <button disabled={busy || card.running} onClick={() => void act(card.mode === 'auto' && !card.protected_reason ? 'allow_task' : 'allow_once')}>{card.mode === 'auto' && !card.protected_reason ? 'Allow this operation for this task folder' : card.mode === 'suggest' ? 'Apply once on this PC' : 'Allow once on this PC'}</button>}
      <button disabled={busy || card.running} onClick={() => void act('deny')}>{info ? 'Deny' : 'Deny PC action'}</button></div>
    </>}
    <label>PC mode for {request.agent} and this task<select value={mode} onChange={e => setMode(e.target.value as Mode)}>{(['suggest', 'approve', 'auto', 'yolo'] as Mode[]).map(m => <option key={m} value={m}>{m}</option>)}</select></label>
    <button disabled={busy || card.running} onClick={() => void setNativeMode()}>Set PC mode for this agent and task</button>
    {card.until && <p>Native grant ends by {new Date(card.until * 1000).toLocaleTimeString()}, or on Stop, task end, mode change, logout or revoke. Reconnecting does not renew it.</p>}
    <button onClick={() => void invoke('pc_revoke', { conversationId: request.conversation_id }).then(refresh)}>Revoke PC task</button>
    {yolo && <div className="owner-modal-backdrop"><div className="set-card owner-modal" role="dialog" aria-modal="true" aria-label="Confirm PC yolo">
      <p>PC yolo can do anything your Windows account can. It applies only to {request.agent} and this task, for at most 30 minutes. Protected actions still ask. Command screening is best-effort static inspection, not a guarantee. {info ? 'Hardware summary only; no task folder is granted.' : `Task folder: ${root}.`}</p>
      <label><input type="checkbox" checked={risk} onChange={e => setRisk(e.target.checked)} />I understand this Windows account risk.</label>
      <button onClick={() => setYolo(false)}>Cancel</button><button disabled={!risk || busy} onClick={() => void setNativeMode(true)}>Enable PC yolo for this agent and task</button>
    </div></div>}
    {card.running && <p role="status">Running on this PC; waiting for the native execution receipt.</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
