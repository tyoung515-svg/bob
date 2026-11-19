import { WorkflowProposalCard } from './WorkflowViews';
import { workflowInteger } from './workflow-types';
import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { newEntry, parseYmd, REPEAT_LABEL, type CalendarStore, type Repeat } from './calendar';
import type { Face } from './portal-protocol';
import type { ReactNode } from 'react';

export type BobAction =
  | { type: 'workflow'; title: string; proposal_id: string; proposal_revision: number | string; proposal_digest: string }
  | { type: 'calendar'; title: string; date: string; start?: string; minutes?: number; repeat?: Repeat; note?: string }
  | { type: 'handoff'; agent: 'builder' | 'fixer' | 'orchestrator'; title: string; brief: string; manage?: boolean; session?: string };
export type ActionContext = { conversationId: string; messageId: string; streaming?: boolean; proposerName?: string; faces: Face[]; store: CalendarStore;
  onTasks: () => void; onHandoff: (action: Extract<BobAction, { type: 'handoff' }>, id: string, managed: boolean) => Promise<void>;
  managedRun?: (id: string) => ReactNode };
export const ActionCardsContext = createContext<ActionContext | null>(null);

export function validateAction(text: string, faces: Face[]): BobAction | null {
  try {
    const a = JSON.parse(text);
    if (!a || typeof a !== 'object' || Array.isArray(a)) return null;
    const string = (v: unknown, max: number) => typeof v === 'string' && v.trim().length > 0 && Array.from(v).length <= max;
    if (!string(a.title, 80)) return null;
    if (a.type === 'workflow') {
      if (Object.keys(a).some(k => !['type','title','proposal_id','proposal_revision','proposal_digest'].includes(k)) || !string(a.proposal_id,128) || /[\r\n]/.test(a.proposal_id) || !workflowInteger(a.proposal_revision) || BigInt(a.proposal_revision) < 1n || typeof a.proposal_digest !== 'string' || !/^[a-f0-9]{64}$/.test(a.proposal_digest)) return null;
      return a;
    }
    if (a.type === 'calendar') {
      if (Object.keys(a).some(k => !['type','title','date','start','minutes','repeat','note'].includes(k))) return null;
      if (typeof a.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(a.date)) return null;
      const [y,m,d] = a.date.split('-').map(Number), date = new Date(`${a.date}T12:00:00Z`);
      if (y < 1 || date.getUTCFullYear() !== y || date.getUTCMonth() !== m-1 || date.getUTCDate() !== d) return null;
      if ('start' in a && (typeof a.start !== 'string' || !/^([01]\d|2[0-3]):[0-5]\d$/.test(a.start))) return null;
      if ('minutes' in a && (!Number.isInteger(a.minutes) || a.minutes < 5 || a.minutes > 1440)) return null;
      if ('repeat' in a && !['none','daily','weekdays','weekly','monthly'].includes(a.repeat)) return null;
      if ('note' in a && (typeof a.note !== 'string' || Array.from(a.note).length > 500)) return null;
      return a;
    }
    if (a.type === 'handoff' && Object.keys(a).every(k => ['type','agent','title','brief','manage','session'].includes(k)) &&
      !/[\r\n]/.test(a.title) && (!('manage' in a) || typeof a.manage === 'boolean') &&
      (!('session' in a) || typeof a.session === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(a.session)) &&
      ['builder','fixer','orchestrator'].includes(a.agent) && string(a.brief, 4000) && faces.some(f => f.id === a.agent && f.allowed !== false)) return a;
  } catch { /* Untrusted assistant text is never executable. */ }
  return null;
}

export function ActionCard({ action, index }: { action: BobAction; index: number }) {
  const context = useContext(ActionCardsContext)!;
  if (action.type === 'workflow') return context.streaming ? <p>A workflow proposal is being prepared.</p> : <WorkflowProposalCard id={action.proposal_id} source={{conversationId:context.conversationId,name:context.proposerName ?? 'the Assistant',revision:action.proposal_revision,digest:action.proposal_digest}} />;
  return <OrdinaryActionCard action={action} index={index} />;
}
function OrdinaryActionCard({ action, index }: { action: Exclude<BobAction, {type: 'workflow'}>; index: number }) {
  const context = useContext(ActionCardsContext)!;
  // The entry itself is the durable deduplication receipt, including after reload.
  const id = 'bob-action:' + JSON.stringify([context.conversationId, context.messageId, index]);
  const receiptKey = `bob:added-task:v1:${id}`;
  const [added, setAdded] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const gate = useRef(false);
  useEffect(() => {
    let active = true;
    let remembered = false;
    try { remembered = localStorage.getItem(receiptKey) === 'added'; } catch { /* Entry id also deduplicates. */ }
    setAdded(remembered);
    if (action.type === 'calendar' && !remembered) void context.store.load().then(rows => { if (active) setAdded(rows.some(r => r.id === id)); }).catch(() => {});
    return () => { active = false; };
  }, [context.store, id, receiptKey, action.type]);
  async function run(managed = true) {
    if (gate.current || added || context.streaming) return;
    gate.current = true; setBusy(true); setError('');
    try {
      if (action.type === 'calendar') {
        await context.store.addOnce({ ...newEntry(action.date, 'person'), id, title: action.title, start: action.start ?? null,
          minutes: action.minutes ?? 30, repeat: action.repeat ?? 'none', notes: action.note ?? '' });
        setAdded(true);
        try { localStorage.setItem(receiptKey, 'added'); } catch { /* The saved entry still carries the stable id. */ }
      } else await context.onHandoff(action, id, managed);
    } catch { setError('Could not complete this action. Try again.'); }
    finally { gate.current = false; setBusy(false); }
  }
  const agent = action.type === 'handoff' ? context.faces.find(f => f.id === action.agent)?.name ?? action.agent : '';
  const managedRun = action.type === 'handoff' ? context.managedRun?.(id) : null;
  return <section className="bob-action-card" aria-label={action.type === 'calendar' ? 'Suggested task' : 'Suggested hand-off'}>
    {agent && <p>{agent}</p>}<h3>{action.title}</h3>
    {action.type === 'calendar' ? <>
      <p>{parseYmd(action.date).toLocaleDateString()} · {action.start ?? 'All day'} · {REPEAT_LABEL[action.repeat ?? 'none']}</p>
      {action.note && <p className="action-text">{action.note}</p>}
      {added ? <p>Added to Tasks · <button type="button" onClick={context.onTasks}>Open Tasks</button></p> :
        <button type="button" disabled={busy || context.streaming} onClick={() => void run()}>Add to Tasks</button>}
    </> : <><details><summary>Show brief</summary><p className="action-text">{action.brief}</p></details>
      {managedRun || (action.manage === false ?
        <button type="button" disabled={busy || context.streaming} onClick={() => void run(false)}>Start in {agent}</button> : <>
        <button type="button" disabled={busy || context.streaming} onClick={() => void run()}>Start {agent}</button>
        <button type="button" className="link" disabled={busy || context.streaming} onClick={() => void run(false)}>Open in {agent} instead</button>
      </>)}</>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
