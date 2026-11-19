// Tasks: one calendar for the person and the agent systems (owner request
// 2026-11-02). Month view → click a day for the hour view, where entries are
// seen, set and changed. Team runs already recorded by the portal show on the
// days they ran. Agent entries wait for "Run now"; nothing here spends by itself.
import { useCallback, useContext, useEffect, useState } from 'react';
import { WorkflowContext, WorkflowProposalCard } from './WorkflowViews';
import { WorkflowRunForm } from './WorkflowLibrary';
import { CalendarWorkflowPicker } from './WorkflowCalendar';
import type { WorkflowPropose, WorkflowRecord } from './workflow-types';
import { authoringErrors } from './workflow-library';
import { isDefiniteRefusal, toPortalError } from './portal-protocol';
import type { Catalog, LedgerRun, SavedTeam } from './portal-protocol';
import type { PortalClient } from './portal-client';
import { allTeams, supportsV2, UPGRADE_REASON, executionRefused, mutationDone, mutationKey, pendingMutation } from './durable-runs';
import { RunBudgetPicker } from './RunBudgetPicker';
import {
  REPEAT_LABEL,
  isDue,
  monthGrid,
  newEntry,
  occurrencesOn,
  parseYmd,
  ymd,
} from './calendar';
import type { AgentPlan, CalEntry, CalendarStore, Occurrence, Repeat, Who } from './calendar';

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const HOURS = Array.from({ length: 24 }, (_, h) => h);

function runDay(run: LedgerRun): string {
  const at = new Date(run.started_at);
  return Number.isFinite(at.getTime()) ? ymd(at) : '';
}

function runHour(run: LedgerRun): number {
  const at = new Date(run.started_at);
  return Number.isFinite(at.getTime()) ? at.getHours() : 0;
}

function clock(entry: CalEntry): string {
  if (!entry.start) return 'All day';
  const [h, m] = entry.start.split(':').map(Number);
  const end = h * 60 + m + entry.minutes;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${entry.start}–${pad(Math.floor(end / 60) % 24)}:${pad(end % 60)}`;
}

export function TasksCalendar({
  store,
  ledger,
  onRunNow,
  onTeamSetup,
  onShowRuns,
  client,
  catalog,
}: {
  store: CalendarStore;
  ledger: LedgerRun[];
  /** Starts the entry's team run now; resolves to the server run id, or null when it did not start. */
  onRunNow: (entry: CalEntry, date: string) => Promise<string | null>;
  onTeamSetup: () => void;
  onShowRuns: () => void;
  client?: PortalClient;
  catalog?: Catalog | null;
}) {
  const today = ymd(new Date());
  const [entries, setEntries] = useState<CalEntry[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [month, setMonth] = useState(() => {
    const now = new Date();
    return { year: now.getFullYear(), month: now.getMonth() };
  });
  const [day, setDay] = useState<string | null>(null);
  const [draft, setDraft] = useState<CalEntry | null>(null);
  const [show, setShow] = useState<Record<Who, boolean>>({ person: true, agent: true });
  const [message, setMessage] = useState('');
  const [running, setRunning] = useState<string | null>(null);
  const [teams, setTeams] = useState<SavedTeam[]>([]);
  const workflowContext=useContext(WorkflowContext);
  const [workflowReview,setWorkflowReview]=useState<{record:WorkflowRecord;inputs:Record<string,unknown>}|null>(null);
  const [workflowConfiguration,setWorkflowConfiguration]=useState<{record:WorkflowRecord;plan:AgentPlan}|null>(null);
  const [workflowProposal,setWorkflowProposal]=useState<string|null>(null);
  useEffect(() => {
    if (client && supportsV2(catalog)) void allTeams(client).then(setTeams).catch(e => setMessage(`Teams could not be read: ${String(e)}`));
  }, [client, catalog]);

  useEffect(() => {
    store
      .load()
      .then((rows) => {
        setEntries(rows);
        setLoaded(true);
      })
      .catch((error: unknown) => {
        setMessage(`The calendar could not be read: ${String(error)}`);
        setLoaded(true);
      });
  }, [store]);

  const persist = useCallback(
    async (next: CalEntry[], note: string) => {
      setEntries(next);
      try {
        await store.save(next);
        setMessage(note);
      } catch (error) {
        setMessage(`Not saved: ${String(error)}`);
      }
    },
    [store],
  );

  const visible = entries.filter((entry) => show[entry.who]);
  const runsByDay = new Map<string, LedgerRun[]>();
  for (const run of ledger) {
    const key = runDay(run);
    if (key) runsByDay.set(key, [...(runsByDay.get(key) ?? []), run]);
  }

  function shiftMonth(by: number) {
    setMonth(({ year, month: m }) => {
      const next = new Date(year, m + by, 1);
      return { year: next.getFullYear(), month: next.getMonth() };
    });
  }

  function openDay(date: string) {
    setDay(date);
    setDraft(null);
    const at = parseYmd(date);
    setMonth({ year: at.getFullYear(), month: at.getMonth() });
  }

  function shiftDay(by: number) {
    if (!day) return;
    const at = parseYmd(day);
    openDay(ymd(new Date(at.getFullYear(), at.getMonth(), at.getDate() + by)));
  }

  async function saveDraft() {
    if (!draft) return;
    if (!draft.title.trim()) {
      setMessage('Give it a title first.');
      return;
    }
    if (draft.who === 'agent' && !draft.agent?.task.trim()) {
      setMessage('An agent task needs the task text the team will work on.');
      return;
    }
    const exists = entries.some((entry) => entry.id === draft.id);
    const next = exists ? entries.map((entry) => (entry.id === draft.id ? draft : entry)) : [...entries, draft];
    await persist(next, exists ? `Updated “${draft.title}”.` : `Added “${draft.title}”.`);
    setDraft(null);
  }

  async function remove(entry: CalEntry) {
    await persist(
      entries.filter((row) => row.id !== entry.id),
      `Removed “${entry.title}”${entry.repeat !== 'none' ? ' and all its repeats' : ''}.`,
    );
    setDraft(null);
  }

  async function runNow(occurrence: Occurrence) {
    setRunning(occurrence.entry.id);
    const operation=`calendar-workflow:${occurrence.entry.id}:${occurrence.date}`;let proposing=false;
    try {
      const plan=occurrence.entry.agent;
      if(plan?.workflow_ref) {
        if(!workflowContext)throw new Error('Connect to Bob before reviewing this workflow.');
        if(plan.workflow_ref.scope !== workflowContext.store.scope)throw new Error('This workflow was saved for another connection. Select it again before reviewing.');
        const {definition}=await workflowContext.store.client.workflowGet(plan.workflow_ref.id,plan.workflow_ref.revision);
        if(definition.digest!==plan.workflow_ref.digest)throw new Error('The saved workflow digest could not be verified. Select the workflow again.');
        if(plan.workflow_choices){
          const {store}=workflowContext;
          const payload=pendingMutation<Omit<WorkflowPropose,'request_key'>>(store.scope,operation) ?? {definition_ref:{id:definition.id,revision:definition.revision},inputs:{...plan.workflow_inputs,task:plan.task},...plan.workflow_choices,origin_conversation_id:null,expires_at:new Date(Date.now()+3600_000).toISOString()};
          proposing=true;const {proposal}=await store.client.workflowPropose({...payload,request_key:mutationKey(store.scope,operation,payload)});mutationDone(store.scope,operation);
          store.proposals.set(proposal.id,{proposal,source:{conversationId:null,name:'you',revision:proposal.revision,digest:proposal.digest}});store.changed();setWorkflowProposal(proposal.id);return;
        }
        setWorkflowReview({record:definition,inputs:{...plan.workflow_inputs,task:plan.task}});return;
      }
      const runId = await onRunNow(occurrence.entry, occurrence.date);
      if (runId) {
        const next = entries.map((entry) =>
          entry.id === occurrence.entry.id
            ? { ...entry, runs: [...entry.runs, { date: occurrence.date, run_id: runId, at: new Date().toISOString() }] }
            : entry,
        );
        await persist(next, `Started run ${runId} for “${occurrence.entry.title}”. Follow it in Run history.`);
      }
    } catch(error) {
      const typed=toPortalError(error);if(proposing && workflowContext && (executionRefused(typed.code)||isDefiniteRefusal(typed)))mutationDone(workflowContext.store.scope,operation);
      setMessage(authoringErrors(error).map(e=>e.message).join(' '));
    } finally {
      setRunning(null);
    }
  }

  function chip(occurrence: Occurrence) {
    const due = isDue(occurrence);
    const ran = occurrence.entry.runs.some((run) => run.date === occurrence.date);
    return (
      <span key={occurrence.entry.id} className={`cal-chip ${occurrence.entry.who}${due ? ' due' : ''}${ran ? ' ran' : ''}`}>
        {occurrence.entry.start ? `${occurrence.entry.start} ` : ''}
        {occurrence.entry.title}
      </span>
    );
  }

  const monthName = new Date(month.year, month.month, 1).toLocaleString(undefined, { month: 'long', year: 'numeric' });
  const dueNow = loaded ? visible.flatMap((entry) => occurrencesOn([entry], today)).filter((occurrence) => isDue(occurrence)) : [];

  function editor() {
    if (!draft) return null;
    const agent = draft.agent;
    const update = (change: Partial<CalEntry>) => setDraft({ ...draft, ...change });
    return (
      <form
        className="cal-editor"
        onSubmit={(event) => {
          event.preventDefault();
          void saveDraft();
        }}
      >
        <h3>{entries.some((entry) => entry.id === draft.id) ? 'Edit' : 'New'} {draft.who === 'agent' ? 'agent task' : 'item for me'}</h3>
        <label htmlFor="cal-title">Title</label>
        <input id="cal-title" value={draft.title} onChange={(e) => update({ title: e.target.value })} autoFocus />
        <div className="cal-row">
          <label className="check">
            <input type="checkbox" checked={draft.start === null} onChange={(e) => update({ start: e.target.checked ? null : '09:00' })} /> All day
          </label>
          {draft.start !== null && (
            <>
              <input aria-label="Start time" type="time" value={draft.start} onChange={(e) => update({ start: e.target.value || '09:00' })} />
              <select aria-label="Duration" value={draft.minutes} onChange={(e) => update({ minutes: Number(e.target.value) })}>
                {[15, 30, 45, 60, 90, 120, 180, 240].map((n) => (
                  <option key={n} value={n}>
                    {n < 60 ? `${n} min` : `${n / 60} h`}
                  </option>
                ))}
              </select>
            </>
          )}
        </div>
        <label htmlFor="cal-date">Date</label>
        <input id="cal-date" type="date" value={draft.date} onChange={(e) => e.target.value && update({ date: e.target.value })} />
        <label htmlFor="cal-repeat">Repeats</label>
        <select id="cal-repeat" value={draft.repeat} onChange={(e) => update({ repeat: e.target.value as Repeat })}>
          {(Object.keys(REPEAT_LABEL) as Repeat[]).map((key) => (
            <option key={key} value={key}>
              {REPEAT_LABEL[key]}
            </option>
          ))}
        </select>
        {agent && (
          <>
            <CalendarWorkflowPicker plan={agent} onChange={agent=>update({agent})} onConfigure={plan=>{
              void (async()=>{try{
                if(!workflowContext || !plan.workflow_ref)throw new Error('Connect to Bob and choose a workflow first.');
                if(plan.workflow_ref.scope!==workflowContext.store.scope)throw new Error('Select this workflow again for the current connection.');
                const {definition}=await workflowContext.store.client.workflowGet(plan.workflow_ref.id,plan.workflow_ref.revision);
                if(definition.digest!==plan.workflow_ref.digest)throw new Error('The saved workflow digest could not be verified. Select it again.');
                setWorkflowConfiguration({record:definition,plan});
              }catch(e){setMessage(authoringErrors(e).map(e=>e.message).join(' '));}})();
            }} />
            {!agent.workflow_ref && <>
            <label htmlFor="cal-team">Team</label>
            {supportsV2(catalog) ? <select id="cal-team" value={agent.team} onChange={(e) => update({ agent: { ...agent, team: e.target.value } })}>
              <option value="">Choose a saved team</option>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>
                  {team.id} · {team.runnable ? 'runnable' : `saved/blocked: ${team.blocked_reasons.join(', ')}`}
                </option>
              ))}
            </select> : <><input id="cal-team" value={agent.team} onChange={e => update({ agent: { ...agent, team: e.target.value } })} /><p>{UPGRADE_REASON}</p></>}
            </>}
            <label htmlFor="cal-task">{agent.workflow_ref ? 'What the workflow should do' : 'What the team should do'}</label>
            <textarea id="cal-task" rows={3} value={agent.task} onChange={(e) => update({ agent: { ...agent, task: e.target.value } })} />
            {!agent.workflow_ref && <RunBudgetPicker budget={agent.budget ?? { start_calls: agent.max_calls, wall_mode: 'fixed', wall_seconds: 900 }} onChange={budget => update({ agent: { ...agent, max_calls: budget.start_calls, budget } })} />}
            <p className="caveat">
              When it is due it shows as due with a Run button; it does not start by itself (the server has no scheduler yet). The server
              determines runnable eligibility and admission. Saved teams do not start automatically.
            </p>
          </>
        )}
        <label htmlFor="cal-notes">Notes</label>
        <textarea id="cal-notes" rows={2} value={draft.notes} onChange={(e) => update({ notes: e.target.value })} />
        <div className="row-actions">
          <button className="primary">Save</button>
          <button type="button" onClick={() => setDraft(null)}>
            Cancel
          </button>
          {entries.some((entry) => entry.id === draft.id) && (
            <button type="button" className="danger" onClick={() => void remove(draft)}>
              Delete{draft.repeat !== 'none' ? ' series' : ''}
            </button>
          )}
        </div>
      </form>
    );
  }

  function dayView(date: string) {
    const occurrences = occurrencesOn(visible, date);
    const allDay = occurrences.filter((occurrence) => !occurrence.entry.start);
    const timed = occurrences.filter((occurrence) => occurrence.entry.start);
    const ran = runsByDay.get(date) ?? [];
    const title = parseYmd(date).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
    const item = (occurrence: Occurrence) => {
      const due = isDue(occurrence);
      const done = occurrence.entry.runs.find((run) => run.date === occurrence.date);
      return (
        <div key={occurrence.entry.id} className={`cal-item ${occurrence.entry.who}${due ? ' due' : ''}`}>
          <button type="button" className="cal-item-main" onClick={() => setDraft(occurrence.entry)}>
            <strong>{occurrence.entry.title}</strong>
            <small>
              {clock(occurrence.entry)} · {occurrence.entry.who === 'agent' ? `agent · ${occurrence.entry.agent?.team}` : 'me'}
              {occurrence.entry.repeat !== 'none' ? ` · ${REPEAT_LABEL[occurrence.entry.repeat].toLowerCase()}` : ''}
              {due ? ' · due' : ''}
              {done ? ` · ran ${done.run_id.slice(0, 8)}` : ''}
            </small>
          </button>
          {occurrence.entry.who === 'agent' && occurrence.entry.agent && (
            <button
              type="button"
              className={due ? 'primary' : undefined}
              disabled={running !== null || !!done}
              onClick={() => void runNow(occurrence)}
              title={occurrence.entry.agent.workflow_ref ? 'Opens the saved workflow for proposal review.' : `Starts ${occurrence.entry.agent.team} now; spends up to ${occurrence.entry.agent.max_calls} model calls`}
            >
              {running === occurrence.entry.id ? 'Starting…' : 'Run now'}
            </button>
          )}
        </div>
      );
    };
    return (
      <div className="cal-day">
        <header className="cal-toolbar">
          <button type="button" onClick={() => setDay(null)}>
            ← Month
          </button>
          <button type="button" aria-label="Previous day" onClick={() => shiftDay(-1)}>
            ◂
          </button>
          <button type="button" aria-label="Next day" onClick={() => shiftDay(1)}>
            ▸
          </button>
          <h2 className="cal-title">{title}</h2>
          <span className="spacer" />
          <button type="button" onClick={() => setDraft(newEntry(date, 'person'))}>
            + For me
          </button>
          <button type="button" onClick={() => setDraft(newEntry(date, 'agent'))}>
            + Agent task
          </button>
        </header>
        <div className="cal-day-body">
          <div className="cal-timeline">
            <div className="cal-allday">
              <span className="hour">all day</span>
              <div>{allDay.length ? allDay.map(item) : <small className="subtle">—</small>}</div>
            </div>
            {HOURS.map((hour) => {
              const here = timed.filter((occurrence) => Number(occurrence.entry.start?.slice(0, 2)) === hour);
              const runs = ran.filter((run) => runHour(run) === hour);
              return (
                <div key={hour} className="cal-hour" onDoubleClick={() => setDraft({ ...newEntry(date, 'person'), start: `${String(hour).padStart(2, '0')}:00` })}>
                  <span className="hour">{String(hour).padStart(2, '0')}:00</span>
                  <div>
                    {here.map(item)}
                    {runs.map((run) => (
                      <div key={run.run_id} className="cal-item ran">
                        <span className="cal-item-main">
                          <strong>Team run · {run.team ?? 'team'}</strong>
                          <small>
                            {run.last_status} · {run.task_excerpt ?? 'no task text'} · {run.run_id.slice(0, 8)}
                          </small>
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
          <aside className="cal-side">
            {draft ? (
              editor()
            ) : (
              <div className="cal-hint">
                <p>Click an item to change it, or add one for you or for an agent team.</p>
                <p className="subtle">Double-click an hour to add something at that time.</p>
              </div>
            )}
          </aside>
        </div>
      </div>
    );
  }

  if(workflowConfiguration)return <section className="cal" aria-label="Calendar workflow choices"><WorkflowRunForm record={workflowConfiguration.record} initialInputs={{...workflowConfiguration.plan.workflow_inputs,task:workflowConfiguration.plan.task}} initialChoices={workflowConfiguration.plan.workflow_choices} onPrepare={(choices,inputs)=>{if(draft)setDraft({...draft,agent:{...workflowConfiguration.plan,task:String(inputs.task ?? ''),workflow_inputs:inputs,workflow_choices:choices}});setWorkflowConfiguration(null);}} onClose={()=>setWorkflowConfiguration(null)} /></section>;
  if(workflowProposal && workflowContext){const entry=workflowContext.store.proposals.get(workflowProposal);return <section className="cal" aria-label="Calendar workflow review">{entry && <WorkflowProposalCard id={workflowProposal} source={entry.source} />}<button onClick={()=>setWorkflowProposal(null)}>Close proposal review</button></section>;}
  if (workflowReview) return <section className="cal" aria-label="Calendar workflow review"><WorkflowRunForm record={workflowReview.record} initialInputs={workflowReview.inputs} onClose={()=>setWorkflowReview(null)} /></section>;
  if (day) return <section className="cal" aria-label="Tasks calendar">{dayView(day)}{message && <p className="cal-message">{message}</p>}</section>;

  const grid = monthGrid(month.year, month.month);
  return (
    <section className="cal" aria-label="Tasks calendar">
      <header className="cal-toolbar">
        <button type="button" aria-label="Previous month" onClick={() => shiftMonth(-1)}>
          ◂
        </button>
        <button
          type="button"
          onClick={() => {
            const now = new Date();
            setMonth({ year: now.getFullYear(), month: now.getMonth() });
          }}
        >
          Today
        </button>
        <button type="button" aria-label="Next month" onClick={() => shiftMonth(1)}>
          ▸
        </button>
        <h2 className="cal-title">{monthName}</h2>
        <span className="spacer" />
        <label className={`cal-filter person${show.person ? ' on' : ''}`}>
          <input type="checkbox" checked={show.person} onChange={(e) => setShow({ ...show, person: e.target.checked })} /> Me
        </label>
        <label className={`cal-filter agent${show.agent ? ' on' : ''}`}>
          <input type="checkbox" checked={show.agent} onChange={(e) => setShow({ ...show, agent: e.target.checked })} /> Agents
        </label>
        <button type="button" onClick={onShowRuns}>
          Run history
        </button>
        <button type="button" className="primary" disabled={!supportsV2(catalog)} title={!supportsV2(catalog) ? UPGRADE_REASON : undefined} onClick={onTeamSetup}>
          Set up a team…
        </button>
      </header>
      {dueNow.length > 0 && (
        <p className="cal-due">
          {dueNow.length} agent task{dueNow.length === 1 ? ' is' : 's are'} due today:{' '}
          <button type="button" className="link" onClick={() => openDay(today)}>
            open today
          </button>
        </p>
      )}
      <div className="cal-month">
        {WEEKDAYS.map((name) => (
          <div key={name} className="cal-weekday">
            {name}
          </div>
        ))}
        {grid.map((date) => {
          const occurrences = occurrencesOn(visible, date);
          const ran = runsByDay.get(date) ?? [];
          const inMonth = parseYmd(date).getMonth() === month.month;
          return (
            <button
              key={date}
              type="button"
              className={`cal-cell${inMonth ? '' : ' other'}${date === today ? ' today' : ''}`}
              onClick={() => openDay(date)}
            >
              <span className="cal-date">{parseYmd(date).getDate()}</span>
              {occurrences.slice(0, 3).map(chip)}
              {occurrences.length > 3 && <small className="cal-more">+{occurrences.length - 3} more</small>}
              {ran.length > 0 && <small className="cal-runs">⚙ {ran.length} team run{ran.length === 1 ? '' : 's'}</small>}
            </button>
          );
        })}
      </div>
      <p className="cal-message">
        {message || 'Click a day to see it hour by hour and set things for you or your agents. Saved on this PC until the server has a calendar.'}
        {!supportsV2(catalog) && ` ${UPGRADE_REASON}`}
      </p>
    </section>
  );
}
