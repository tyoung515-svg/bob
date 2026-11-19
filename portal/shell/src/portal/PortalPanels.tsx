import { ReadFaultText } from './provider-restart';
import { useCallback, useEffect, useState } from 'react';
import { CONTRACT_GAPS, gapText, toPortalError, txt } from './portal-protocol';
import type {
  Approval,
  Hello,
  LedgerRun,
  MemoryHit,
  MemoryStatus,
  PortalError,
  ProfilesPayload,
  Project,
  Run,
} from './portal-protocol';
import type { PortalClient } from './portal-client';

function Fault({ error, onRetry }: { error: PortalError; onRetry?: () => void }) {
  return (
    <div role="alert" className="portal-fault">
      <ReadFaultText error={error} fallback={<><p><code>{error.code}</code> {error.detail}</p>
        {error.hint && <p className="hint">{error.hint}</p>}</>} />
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Reload this panel
        </button>
      )}
    </div>
  );
}

function detailsText(details: unknown): string {
  try {
    const text = typeof details === 'string' ? details : JSON.stringify(details);
    return text.length > 400 ? `${text.slice(0, 400)}…` : text;
  } catch {
    return 'not renderable';
  }
}

/** Full details for an expanded row: pretty JSON, nothing clipped. */
function fullDetails(details: unknown): string {
  try {
    return typeof details === 'string' ? details : JSON.stringify(details, null, 2);
  } catch {
    return 'not renderable';
  }
}

/** Something in Needs-you the owner wants an agent to fix or plan; nothing is sent until they press Send. */
export type WorkItem = { kind: 'approval' | 'gap'; title: string; body: string; conversation_id?: string };
export type WorkMode = 'fix' | 'plan';

export function WorkOnIt({ item, onWorkOn }: { item: WorkItem; onWorkOn?: (item: WorkItem, mode: WorkMode) => void }) {
  if (!onWorkOn) return null;
  return (
    <div className="work-on-it">
      <span className="subtle">Work on it with an agent:</span>
      <button type="button" onClick={() => onWorkOn(item, 'fix')}>
        Ask to fix
      </button>
      <button type="button" onClick={() => onWorkOn(item, 'plan')}>
        Ask for a plan
      </button>
    </div>
  );
}

type DecisionView = { decision: string; agent_resume: string; status: string; refreshed: boolean };

// R9-3: the panel is mounted only while Approvals is the open tab, so component
// state died on one click on Memory/Runs — the owner lost the verdict, its
// agent_resume, the re-read status and the caveat (the exact loss R7-2 was filed
// against). The maps now live one level up, on this app instance, and the hook
// writes through to them so a remounted panel still shows what the server said.
export type ApprovalSessionStore = {
  decided: Record<string, DecisionView>;
  refusals: Record<string, PortalError>;
  conversations?: Record<string, string>;
};

function useSessionMap<T>(store: Record<string, T>) {
  const [state, setState] = useState<Record<string, T>>(() => ({ ...store }));
  const set = useCallback(
    (next: Record<string, T> | ((prior: Record<string, T>) => Record<string, T>)) => {
      // R9b-N3: mutate the store NOW rather than inside the setState updater. React
      // may never run an updater queued just before unmount, and a verdict recorded
      // while the owner clicks away would then be lost — the R7-2 loss again.
      const prior = { ...store };
      const value =
        typeof next === 'function' ? (next as (p: Record<string, T>) => Record<string, T>)(prior) : next;
      for (const key of Object.keys(store)) delete store[key];
      Object.assign(store, value);
      setState({ ...store });
    },
    [store],
  );
  return [state, set] as const;
}

/** One recorded verdict or typed refusal, with its caveat. Rendered the same way
 *  whether the row is still in the pending page or only in this session's list,
 *  so a verdict cannot lose its meaning by moving (R7-2). */
function Verdict({ decision, refusal }: { decision?: DecisionView; refusal?: PortalError }) {
  return (
    <>
      {refusal && (
        <p role="alert" className="typed-refusal">
          <ReadFaultText error={refusal} fallback={<><code>{refusal.code}</code> {refusal.detail}</>} />
          <br />
          <span className="hint">{refusal.hint}</span>
        </p>
      )}
      {decision && (
        <div className="decision">
          <p>
            decision: <strong>{decision.decision}</strong> · agent_resume: <strong>{decision.agent_resume}</strong> · row status:{' '}
            <strong>{decision.status}</strong> {decision.refreshed ? '(re-read from the server)' : '(re-read failed)'}
          </p>
          <p className="caveat">
            A recorded decision is not proof of effect. This app has no effect readback; the approval row was re-read and the resumed
            output (if any) must be inspected in the conversation.
          </p>
        </div>
      )}
    </>
  );
}

/** The broker still returns the server's run when its own ledger write fails; the
 *  run is real but history is not recorded, so it must never look normal. */
function ledgerWarning(warnings?: string[]): string {
  if (!warnings || warnings.length === 0) return '';
  return ` Not recorded in the portal ledger: ${warnings.join('; ')} — this run will be missing from recorded runs until it is read again.`;
}

export function ApprovalsPanel({
  client,
  onOpenConversation,
  onBusy,
  session,
  onWorkOn,
  conversationId,
  conversationLabel,
}: {
  client: PortalClient;
  onOpenConversation: (id: string) => void;
  onBusy?: (busy: boolean, conversationId?: string) => void;
  session: ApprovalSessionStore;
  conversationId?: string;
  conversationLabel?: (id: string) => string;
  onWorkOn?: (item: WorkItem, mode: WorkMode) => void;
}) {
  const [items, setItems] = useState<Approval[]>([]);
  const [excluded, setExcluded] = useState(0);
  const [error, setError] = useState<PortalError | null>(null);
  const [busy, setBusy] = useState(false);
  const [decided, setDecided] = useSessionMap(session.decided);
  const [refusals, setRefusals] = useSessionMap(session.refusals);
  const [notice, setNotice] = useState('');
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const page = await client.approvals('pending', 50);
      session.conversations ??= {};
      for (const row of page.items) session.conversations[row.id] = row.conversation_id;
      setItems(page.items);
      setExcluded(page.excluded ?? 0);
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(approval: Approval, decision: 'approve' | 'reject') {
    setBusy(true);
    onBusy?.(true, approval.conversation_id);
    setError(null);
    setNotice('');
    setRefusals((prior) => {
      const next = { ...prior };
      delete next[approval.id];
      return next;
    });
    try {
      const result = await client.approvalDecide(approval.id, decision);
      let status = result.approval?.status ?? 'unknown';
      let refreshed = false;
      try {
        const reread = await client.approvalGet(approval.id);
        status = reread.approval.status;
        refreshed = true;
      } catch {
        // The decision itself was recorded; a failed re-read is reported, not hidden.
      }
      setDecided((prior) => ({
        ...prior,
        [approval.id]: { decision: result.decision, agent_resume: result.agent_resume, status, refreshed },
      }));
      // The recorded verdict is repeated in the panel's own notice: the pending
      // page reload below drops the row from `items` (R7-2).
      setNotice(
        `Decision recorded for ${approval.id}: ${result.decision} · agent_resume: ${result.agent_resume} · row status: ${status}` +
          `${refreshed ? ' (re-read from the server)' : ' (re-read failed)'}. A recorded decision is not proof of effect.`,
      );
      await load();
    } catch (e) {
      setRefusals((prior) => ({ ...prior, [approval.id]: toPortalError(e) }));
    } finally {
      setBusy(false);
      onBusy?.(false, approval.conversation_id);
    }
  }

  // Verdicts for rows the pending page no longer lists (R7-2). `load()` above
  // replaces `items` with the still-pending page, so a decided row would
  // otherwise vanish together with what the server said about it.
  const visible = conversationId ? items.filter(row => row.conversation_id === conversationId) : items;
  const listed = new Set(visible.map((row) => row.id));
  const sessionVerdicts = [...new Set([...Object.keys(decided), ...Object.keys(refusals)])].filter((id) => !listed.has(id) && (!conversationId || session.conversations?.[id] === conversationId));

  return (
    <section aria-label="Approvals" tabIndex={-1} className="portal-panel-body">
      <header className="panel-header">
        <h2>Approvals</h2>
        <button type="button" onClick={() => void load()} disabled={busy}>
          Reload
        </button>
      </header>
      <p className="subtle">
        Click an item to read all of it, or hand it to an agent to fix or plan.
        {excluded > 0 ? ` ${excluded} row(s) belonging to another user are hidden.` : ''}
      </p>
      <details className="aside-note">
        <summary>How approvals work here</summary>
        <p>
          Pending approvals, filtered row by row on the row&apos;s own <code>user_id</code> against this identity. Only owned, pending
          rows are decidable; a refusal is a typed state (<code>approval_foreign</code>, <code>approval_stale</code>,
          <code>approval_not_found</code>, <code>approval_not_permitted</code> when the broker runs without approval support) and
          not an app bug. A recorded decision is not proof of effect.
        </p>
      </details>
      {error && <Fault error={error} onRetry={() => void load()} />}
      {notice && <p className="notice">{notice}</p>}
      {!visible.length && !error && <p className="subtle">No pending approvals.</p>}
      {visible.map((approval) => {
        const decision = decided[approval.id];
        const refusal = refusals[approval.id];
        return (
          <article id={'approval-'+approval.id} tabIndex={-1} key={approval.id} className={`approval-row${open === approval.id ? ' open' : ''}`}>
            <button
              type="button"
              className="row-toggle"
              aria-expanded={open === approval.id}
              onClick={() => setOpen(open === approval.id ? null : approval.id)}
            >
              <span className="twisty">{open === approval.id ? '▾' : '▸'}</span>
              <strong>{txt(approval.action_type)}</strong>
              {conversationLabel && <span>{conversationLabel(approval.conversation_id)}</span>}
              <small>{String(approval.created_at).slice(0, 16).replace('T', ' ')}</small>
              {open !== approval.id && (
                <span className="summary">
                  <code>{approval.id}</code> · {detailsText(approval.details)}
                </span>
              )}
            </button>
            {open === approval.id && (
              <>
                <p className="subtle">
                  <code>{approval.id}</code> · conversation{' '}
                  <button type="button" className="link" onClick={() => onOpenConversation(approval.conversation_id)}>
                    {approval.conversation_id}
                  </button>
                </p>
                <pre className="details full">{fullDetails(approval.details)}</pre>
                <WorkOnIt
                  item={{
                    kind: 'approval',
                    title: txt(approval.action_type),
                    body: `Approval ${approval.id} (${approval.created_at}), from conversation ${approval.conversation_id}:\n${fullDetails(approval.details)}`,
                    conversation_id: approval.conversation_id,
                  }}
                  onWorkOn={onWorkOn}
                />
              </>
            )}
            {!decision && (
              <div className="row-actions">
                <button type="button" className="primary" disabled={busy} onClick={() => void decide(approval, 'approve')}>
                  Approve
                </button>
                <button type="button" disabled={busy} onClick={() => void decide(approval, 'reject')}>
                  Reject
                </button>
              </div>
            )}
            {(decision || refusal) && <Verdict decision={decision} refusal={refusal} />}
          </article>
        );
      })}
      {sessionVerdicts.length > 0 && (
        <section aria-label="Decided in this session" className="decided-session">
          <h3>Decided in this session</h3>
          <p className="subtle">
            The list above is the server&apos;s <em>pending</em> page, so a row leaves it once it is decided. These verdicts are what the
            server returned to this window and are kept here; they are not re-read on Reload.
          </p>
          {sessionVerdicts.map((id) => (
            <article key={id} className="approval-row decided">
              <h4>
                <code>{id}</code>
              </h4>
              <Verdict decision={decided[id]} refusal={refusals[id]} />
            </article>
          ))}
        </section>
      )}
    </section>
  );
}

export function MemoryPanel({ client }: { client: PortalClient }) {
  const [status, setStatus] = useState<MemoryStatus | null>(null);
  const [statusError, setStatusError] = useState<PortalError | null>(null);
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<MemoryHit[]>([]);
  const [read, setRead] = useState<{ source_id: string; content: string } | null>(null);
  const [draft, setDraft] = useState('');
  const [category, setCategory] = useState('note');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<PortalError | null>(null);
  const [notice, setNotice] = useState('');

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await client.memoryStatus());
      setStatusError(null);
    } catch (e) {
      setStatusError(toPortalError(e));
    }
  }, [client]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  async function search() {
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    setRead(null);
    try {
      const page = await client.memorySearch(query.trim(), 20);
      setHits(page.items);
      setNotice(`${page.items.length} result(s) in your scope.`);
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  async function openHit(hit: MemoryHit) {
    setBusy(true);
    setError(null);
    try {
      const body = await client.memoryRead(hit.source_id, 8000);
      setRead({ source_id: body.source_id, content: body.content });
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!draft.trim()) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      await client.memorySave(draft, category);
      setDraft('');
      setNotice(`Saved to central memory as a ${category}. The server payload is authoritative; counts may lag.`);
      await loadStatus();
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Memory" className="portal-panel-body">
      <header className="panel-header">
        <h2>Memory (your scope)</h2>
        <button type="button" onClick={() => void loadStatus()} disabled={busy}>
          Reload status
        </button>
      </header>
      {statusError && <Fault error={statusError} onRetry={() => void loadStatus()} />}
      {status && (
        <div className="memory-status">
          <p>
            authority: <strong>{String(status.authority ?? 'not reported')}</strong> · complete_corpus:{' '}
            <strong>{String(status.complete_corpus ?? 'not reported')}</strong>
          </p>
          <p className="caveat">{String(status.coverage_note ?? 'No coverage note was returned; coverage is not a completeness claim.')}</p>
        </div>
      )}
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          void search();
        }}
      >
        <label htmlFor="memory-query">Search central memory</label>
        <input id="memory-query" value={query} onChange={(e) => setQuery(e.target.value)} disabled={busy} />
        <button disabled={busy || !query.trim()}>Search</button>
      </form>
      {error && <Fault error={error} />}
      {notice && <p className="notice">{notice}</p>}
      {hits.map((hit) => (
        <article key={hit.source_id} className="memory-hit">
          <h3>
            {txt(hit.kind)} · {txt(hit.owner)}
          </h3>
          <p className="subtle">
            <code>{txt(hit.source_id)}</code> · provenance {txt(hit.provenance)}
            {hit.truncated ? ' · excerpt truncated' : ''}
          </p>
          <p>{txt(hit.excerpt, 'no excerpt')}</p>
          <button type="button" disabled={busy} onClick={() => void openHit(hit)}>
            Read source
          </button>
        </article>
      ))}
      {read && (
        <div className="memory-read">
          <h3>Source {txt(read.source_id)}</h3>
          <pre>{read.content}</pre>
        </div>
      )}
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <label htmlFor="memory-draft">Save a durable note (max 12000 characters)</label>
        <textarea id="memory-draft" value={draft} rows={4} maxLength={12000} onChange={(e) => setDraft(e.target.value)} disabled={busy} />
        <label htmlFor="memory-category">Category</label>
        <select id="memory-category" value={category} onChange={(e) => setCategory(e.target.value)} disabled={busy}>
          <option value="fact">fact</option>
          <option value="preference">preference</option>
          <option value="project">project</option>
          <option value="note">note</option>
        </select>
        <button disabled={busy || !draft.trim()}>Save to central memory</button>
      </form>
      <p className="subtle">Work memory (`/work-memory/*`) is out of scope and is never called from this panel.</p>
    </section>
  );
}

export function LegacyRunsPanel({
  client,
  runs,
  onRuns,
  seed,
}: {
  client: PortalClient;
  runs: Run[];
  onRuns: (next: Run[]) => void;
  seed?: { task: string; at: number } | null;
}) {
  const [team, setTeam] = useState('');
  const [task, setTask] = useState('');
  // R12: "Make this a task" in the transcript hands the request over here. It
  // only fills the box — a run still starts when Start is pressed, because a
  // team run spends real model calls.
  const seedAt = seed?.at ?? 0;
  const seedTask = seed?.task ?? '';
  useEffect(() => {
    if (seedTask) setTask(seedTask);
  }, [seedAt, seedTask]);
  const [maxCalls, setMaxCalls] = useState(4);
  const [maxSeconds, setMaxSeconds] = useState(900);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<PortalError | null>(null);
  const [notice, setNotice] = useState('');
  // Recorded runs come from the broker's own ledger (contract addendum): the
  // server persists run rows but exposes no run-list route, so this is the
  // durable view of what THIS portal dispatched - never a claim about other clients.
  const [ledger, setLedger] = useState<LedgerRun[] | null>(null);
  const [ledgerTotal, setLedgerTotal] = useState(0);
  const [ledgerCaveats, setLedgerCaveats] = useState<string[]>([]);
  const [ledgerBusy, setLedgerBusy] = useState(false);
  const [ledgerError, setLedgerError] = useState<PortalError | null>(null);

  const loadLedger = useCallback(async () => {
    setLedgerBusy(true);
    setLedgerError(null);
    try {
      const result = await client.runsList(20, 0);
      const items = Array.isArray(result.items) ? result.items : [];
      setLedger(items);
      setLedgerTotal(typeof result.total === 'number' ? result.total : items.length);
      setLedgerCaveats(Array.isArray(result.caveats) ? result.caveats : []);
    } catch (e) {
      setLedgerError(toPortalError(e));
    } finally {
      setLedgerBusy(false);
    }
  }, [client]);

  useEffect(() => {
    void loadLedger();
  }, [loadLedger]);

  function upsert(run: Run) {
    const without = runs.filter((r) => r.id !== run.id);
    onRuns([run, ...without]);
  }

  async function start() {
    if (!Number.isInteger(maxCalls) || maxCalls < 1 || maxCalls > 4 || maxSeconds < 30 || maxSeconds > 900) {
      setError({ code: 'unsupported_run_contract', detail: 'Legacy runs require 1–4 calls and 30–900 seconds. Update Bob on the server for v2 budgets.', hint: '' });
      return;
    }
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      const result = await client.runsStart({ team: team.trim(), task: task.trim(), max_model_calls: maxCalls, max_seconds: maxSeconds });
      upsert(result.run);
      setTask('');
      const warning = ledgerWarning(result.warnings);
      setNotice(
        `Run ${result.run.id} accepted (${result.run.status}). Receipts are observed, not independently verified.${warning}`,
      );
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  async function poll(runId: string) {
    setBusy(true);
    setError(null);
    try {
      const result = await client.runGet(runId);
      upsert(result.run);
      setNotice(`Read ${runId} from the server.${ledgerWarning(result.warnings)}`);
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  async function cancel(runId: string) {
    setBusy(true);
    setError(null);
    try {
      const result = await client.runCancel(runId);
      upsert(result.run);
      setNotice(
        `Cancellation requested for ${runId}; the returned status is ${result.run.status}.${ledgerWarning(result.warnings)}`,
      );
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Runs" className="portal-panel-body">
      <header className="panel-header">
        <h2>Read-only team runs</h2>
      </header>
      <p className="subtle">
        Runs execute on the server. The server exposes no run list, so this panel shows only (a) runs this window started and
        (b) this portal&apos;s own ledger of the runs it dispatched — kept across restarts, and never a claim about runs started
        by another client.
      </p>
      <header className="panel-header">
        <h3>Recorded runs (portal ledger)</h3>
        <button type="button" disabled={ledgerBusy} onClick={() => void loadLedger()}>
          {ledgerBusy ? 'Reloading…' : 'Reload history'}
        </button>
      </header>
      {ledgerError && <Fault error={ledgerError} onRetry={() => void loadLedger()} />}
      {!ledgerError && ledger === null && <p className="subtle">Reading the portal ledger…</p>}
      {!ledgerError && ledger !== null && ledger.length === 0 && (
        <p className="subtle">No runs recorded yet. A run appears here after this portal dispatches one.</p>
      )}
      {!ledgerError && ledger !== null && ledger.length > 0 && (
        <>
          <p className="subtle">
            showing {ledger.length} of {ledgerTotal} recorded · newest first · source: the portal&apos;s own ledger on the server.
          </p>
          {ledger.map((row) => (
            <article key={`ledger:${row.run_id}`} className="run-row">
              <h3>
                <code>{txt(row.run_id)}</code> · {txt(row.last_status)}
              </h3>
              <p className="subtle">
                started {txt(row.started_at)} · last seen {txt(row.last_checked_at)} · team {txt(row.team)}
                {row.worker ? ` · worker ${txt(row.worker)}` : ''} · {row.receipts?.length ?? 0} receipts
              </p>
              {row.task_excerpt ? <p>{txt(row.task_excerpt)}</p> : null}
              <div className="row-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void poll(row.run_id)}
                  title="Read this run from the server now and show it above"
                >
                  Read from server
                </button>
              </div>
            </article>
          ))}
        </>
      )}
      {ledgerCaveats.length > 0 && (
        <ul className="caveat">
          {ledgerCaveats.map((caveat) => (
            <li key={caveat}>{caveat}</li>
          ))}
        </ul>
      )}
      <h3>Start a run</h3>
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          void start();
        }}
      >
        <label htmlFor="run-team">Team</label>
        <input id="run-team" value={team} onChange={(e) => setTeam(e.target.value)} disabled={busy} />
        <label htmlFor="run-task">Task (read-only)</label>
        <textarea id="run-task" value={task} rows={3} maxLength={16000} onChange={(e) => setTask(e.target.value)} disabled={busy} />
        <label htmlFor="run-calls">Max model calls</label>
        <input
          id="run-calls"
          type="number"
          min={1}
          max={4}
          value={maxCalls}
          onChange={(e) => setMaxCalls(Math.max(1, Math.floor(Number(e.target.value) || 1)))}
          disabled={busy}
        />
        <p className="caveat">
          Legacy execution supports 1–4 calls and 30–900 seconds. Update Bob on the server for v2 budgets; a larger request is never reduced and resent.
        </p>
        <label htmlFor="run-seconds">Max seconds (30–900)</label>
        <input
          id="run-seconds"
          type="number"
          min={30}
          max={900}
          value={maxSeconds}
          onChange={(e) => setMaxSeconds(Number(e.target.value))}
          disabled={busy}
        />
        <button disabled={busy || !team.trim() || !task.trim()}>Start run</button>
      </form>
      {error && <Fault error={error} />}
      {notice && <p className="notice">{notice}</p>}
      <h3>Runs started in this window</h3>
      {!runs.length && <p className="subtle">No runs started from this window.</p>}
      {runs.map((run) => {
        const terminal = ['completed', 'failed', 'cancelled', 'interrupted'].includes(run.status);
        return (
          <article key={run.id} className="run-row">
            <h3>
              <code>{txt(run.id)}</code> · {txt(run.status)}
            </h3>
            <p className="subtle">
              team {txt(run.team)} · {txt(run.max_model_calls, '?')} model calls · {txt(run.max_seconds, '?')} s
            </p>
            {run.task ? <p>{txt(run.task)}</p> : null}
            {run.receipts && run.receipts.length > 0 && (
              <table className="receipts">
                <caption>Receipts — observed; not independently verified</caption>
                <thead>
                  <tr>
                    <th scope="col">role</th>
                    <th scope="col">seat</th>
                    <th scope="col">elapsed</th>
                  </tr>
                </thead>
                <tbody>
                  {run.receipts.map((receipt, index) => (
                    <tr key={`${run.id}:${index}`}>
                      <td>{txt(receipt.role)}</td>
                      <td>{txt(receipt.model_face ?? receipt.seat)}</td>
                      <td>{typeof receipt.elapsed_seconds === 'number' ? `${receipt.elapsed_seconds}s` : 'not reported'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="caveat">receipt observed; not independently verified</p>
            <div className="row-actions">
              <button type="button" disabled={busy} onClick={() => void poll(run.id)}>
                Poll run
              </button>
              <button type="button" disabled={busy || terminal} onClick={() => void cancel(run.id)}>
                Request cancellation
              </button>
            </div>
          </article>
        );
      })}
    </section>
  );
}

export function ProjectsPanel({
  client,
  conversationId,
  onAssigned,
}: {
  client: PortalClient;
  conversationId: string | null;
  onAssigned: () => void;
}) {
  const [items, setItems] = useState<Project[]>([]);
  const [error, setError] = useState<PortalError | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [edits, setEdits] = useState<Record<string, { name: string; description: string; instructions: string }>>({});
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [instructions, setInstructions] = useState('');
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const page = await client.projects();
      setItems(page.items);
      const next: Record<string, { name: string; description: string; instructions: string }> = {};
      for (const project of page.items) {
        next[project.id] = {
          name: txt(project.name, ''),
          description: txt(project.description, ''),
          instructions: txt(project.instructions, ''),
        };
      }
      setEdits(next);
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }, [client]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create() {
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      const result = await client.projectSave({ name: name.trim(), description, instructions });
      setNotice(`Project ${result.project?.id ?? ''} created.`);
      setName('');
      setDescription('');
      setInstructions('');
      await load();
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  async function update(project: Project) {
    const edit = edits[project.id];
    if (!edit) return;
    setBusy(true);
    setError(null);
    try {
      await client.projectSave({ id: project.id, name: edit.name, description: edit.description, instructions: edit.instructions });
      setNotice(`Project ${project.id} updated; the server row is authoritative.`);
      await load();
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(project: Project) {
    setBusy(true);
    setError(null);
    try {
      await client.projectSave({ id: project.id, delete: true });
      setConfirmDelete(null);
      setNotice(`Project ${project.id} archived on the server.`);
      await load();
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  async function assign(project: Project) {
    if (!conversationId) return;
    setBusy(true);
    setError(null);
    try {
      await client.conversationAssignProject(conversationId, project.id);
      setNotice(`Conversation ${conversationId} now uses project ${project.id}.`);
      onAssigned();
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Projects" className="portal-panel-body">
      <header className="panel-header">
        <h2>Projects</h2>
        <button type="button" onClick={() => void load()} disabled={busy}>
          Reload
        </button>
      </header>
      <p className="subtle">
        Server conversation projects and instructions. This is organizational context, not a family project approval and not a repo
        access grant.
      </p>
      {error && <Fault error={error} onRetry={() => void load()} />}
      {notice && <p className="notice">{notice}</p>}
      {items.map((project) => {
        const edit = edits[project.id] ?? { name: txt(project.name, ''), description: '', instructions: '' };
        return (
          <article key={project.id} className="project-row">
            <h3>
              {txt(project.name)} <small>{txt(project.conversation_count, '0')} conversation(s)</small>
            </h3>
            <p className="subtle">
              <code>{project.id}</code> · updated {project.updated_at || 'not reported'}
            </p>
            <label htmlFor={`p-name-${project.id}`}>Name</label>
            <input
              id={`p-name-${project.id}`}
              value={txt(edit.name, '')}
              disabled={busy}
              onChange={(e) => setEdits((prior) => ({ ...prior, [project.id]: { ...edit, name: e.target.value } }))}
            />
            <label htmlFor={`p-desc-${project.id}`}>Description</label>
            <input
              id={`p-desc-${project.id}`}
              value={txt(edit.description, '')}
              disabled={busy}
              onChange={(e) => setEdits((prior) => ({ ...prior, [project.id]: { ...edit, description: e.target.value } }))}
            />
            <label htmlFor={`p-inst-${project.id}`}>Instructions</label>
            <textarea
              id={`p-inst-${project.id}`}
              rows={3}
              value={txt(edit.instructions, '')}
              disabled={busy}
              onChange={(e) => setEdits((prior) => ({ ...prior, [project.id]: { ...edit, instructions: e.target.value } }))}
            />
            <div className="row-actions">
              <button type="button" disabled={busy || !edit.name.trim()} onClick={() => void update(project)}>
                Save project
              </button>
              <button type="button" disabled={busy || !conversationId} onClick={() => void assign(project)}>
                Assign to current conversation
              </button>
              {confirmDelete === project.id ? (
                <>
                  <button type="button" className="danger" disabled={busy} onClick={() => void remove(project)}>
                    Confirm delete
                  </button>
                  <button type="button" onClick={() => setConfirmDelete(null)}>Keep</button>
                </>
              ) : (
                <button type="button" disabled={busy} onClick={() => setConfirmDelete(project.id)}>
                  Delete…
                </button>
              )}
            </div>
          </article>
        );
      })}
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          void create();
        }}
      >
        <h3>New project</h3>
        <label htmlFor="new-project-name">Name</label>
        <input id="new-project-name" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
        <label htmlFor="new-project-desc">Description</label>
        <input id="new-project-desc" value={description} onChange={(e) => setDescription(e.target.value)} disabled={busy} />
        <label htmlFor="new-project-inst">Instructions</label>
        <textarea id="new-project-inst" rows={3} value={instructions} onChange={(e) => setInstructions(e.target.value)} disabled={busy} />
        <button disabled={busy || !name.trim()}>Create project</button>
      </form>
    </section>
  );
}

export function GapsPanel({
  serverGaps,
  onWorkOn,
}: {
  serverGaps: unknown[];
  onWorkOn?: (item: WorkItem, mode: WorkMode) => void;
}) {
  const rows = [
    ...CONTRACT_GAPS.map((gap) => ({ title: gap.title, detail: gap.detail, source: 'app contract' })),
    ...serverGaps.map((gap) => {
      const text = gapText(gap);
      return { title: text.length > 90 ? `${text.slice(0, 90)}…` : text, detail: text, source: 'reported by the server' };
    }),
  ];
  return (
    <section aria-label="Capability gaps" className="portal-panel-body">
      <header className="panel-header">
        <h2>Gaps · what Bob can&apos;t do yet</h2>
      </header>
      <p className="subtle">Missing capabilities, not loading states. Open one to read it, or ask an agent to fix it or plan the fix.</p>
      <ul className="gap-list">
        {rows.map((gap, index) => (
          <li key={`${gap.source}:${index}`}>
            <details>
              <summary>{gap.title}</summary>
              <p>{gap.detail}</p>
              <p className="subtle">Source: {gap.source}</p>
              <WorkOnIt item={{ kind: 'gap', title: gap.title, body: `${gap.detail}\n(source: ${gap.source})` }} onWorkOn={onWorkOn} />
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function SettingsPanel({
  client,
  profiles,
  hello,
  onProfiles,
  onClose,
}: {
  client: PortalClient;
  profiles: ProfilesPayload | null;
  hello: Hello | null;
  onProfiles: (payload: ProfilesPayload) => void;
  onClose: () => void;
}) {
  const [choice, setChoice] = useState(profiles?.active ?? 'bob_server');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<PortalError | null>(null);
  const [notice, setNotice] = useState('');

  async function apply() {
    setBusy(true);
    setError(null);
    try {
      const payload = await client.selectProfile(choice);
      onProfiles(payload);
      setNotice(`Runtime profile is now ${payload.active}.`);
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Portal settings" className="portal-panel-body">
      <header className="panel-header">
        <h2>Settings</h2>
        <button type="button" onClick={onClose}>
          Close settings
        </button>
      </header>
      <p className="subtle">
        The runtime profile decides where chat and effects execute. Selecting a local profile leaves the server Bob and returns to the
        WSL local runtime; nothing is migrated between them.
      </p>
      <label htmlFor="portal-profile">Runtime profile</label>
      <select id="portal-profile" value={choice} onChange={(e) => setChoice(e.target.value)} disabled={busy}>
        {(profiles?.profiles ?? []).map((profile) => (
          <option key={profile.id} value={profile.id} disabled={!profile.available}>
            {profile.label}
            {profile.available ? '' : ` · unavailable (${profile.reason ?? 'no reason reported'})`}
          </option>
        ))}
      </select>
      <button type="button" disabled={busy || !profiles || choice === profiles.active} onClick={() => void apply()}>
        Apply runtime profile
      </button>
      {error && <Fault error={error} />}
      {notice && <p className="notice">{notice}</p>}
      {profiles && (
        <dl className="facts">
          <dt>Active profile</dt>
          <dd>{profiles.active}</dd>
          <dt>Data root</dt>
          <dd><code>{profiles.data_root}</code></dd>
          <dt>Settings file</dt>
          <dd>
            <code>{profiles.settings.path}</code> · hardened (user-only ACL): {profiles.settings.hardened ? 'yes' : 'no'}
          </dd>
          <dt>Transport</dt>
          <dd>
            {profiles.transport.kind} · targets {profiles.transport.targets.join(' → ')} · <code>{profiles.transport.ssh}</code> · broker{' '}
            <code>{profiles.transport.broker}</code>
          </dd>
        </dl>
      )}
      {hello && (
        <dl className="facts">
          <dt>Identity</dt>
          <dd>
            {txt(hello.identity.sub)} · gateway {txt(hello.identity.gateway)} · token expires {txt(hello.identity.expires_at, 'not reported')}
          </dd>
          <dt>Server</dt>
          <dd>
            boot {txt(hello.server?.boot, 'not reported by this gateway')} · version{' '}
            {txt(hello.server?.version, 'not reported by this gateway')}
          </dd>
          <dt>May touch</dt>
          <dd>{(hello.identity.may_touch ?? []).join(', ') || 'not reported'}</dd>
          <dt>May not touch</dt>
          <dd>{(hello.identity.may_not_touch ?? []).join(', ') || 'not reported'}</dd>
        </dl>
      )}
      <p className="subtle">
        The renderer never holds the bearer token and cannot execute tools. Token contents are never displayed, logged or stored here.
      </p>
    </section>
  );
}
