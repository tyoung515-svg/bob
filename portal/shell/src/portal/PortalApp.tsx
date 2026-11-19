import { ModelPicker, modelName } from './ModelPicker';
import { CatalogReload, retryModelRead, modelRestarting, type ModelChangedEvent } from './model-reload';
import type { ModelStatus } from './ModelSetup';
import { WorkflowContext, WorkflowConversation, WorkflowProposalCard, WorkflowRuns, WorkflowRunView, useWorkflows } from './WorkflowViews';
import { WorkflowStore } from './workflow-store';
import { NeedsYouPage, type NeedItem } from './NeedsYouPage';
import { workflowEnded } from './workflow-types';
import { builderReadStore } from './builder-read-store';
import { WorkflowDraftReview } from './WorkflowLibrary';
import { Component, Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { listen } from '@tauri-apps/api/event';
import type { NativeInvoke } from '../workbench/workbench-client';
import { createPortalClient } from './portal-client';
import type { PortalClient, SendArgs } from './portal-client';
import { PORTAL_EVENT, isRateLimited, RATE_LIMIT_SEND_LINE, isTransportFault, messageProvenance, toPortalError, txt } from './portal-protocol';
import type { Approval, Catalog, Conversation, Hello, LedgerRun, Message, PortalError, ProfilesPayload, Project, Run } from './portal-protocol';
import {
  COMPLETE_UNREAD_LINE,
  composerLocked,
  inFlight,
  inspectUnknownSend,
  liveStream,
  pickFromServer,
  pinLine,
  sendPinArgs,
  terminalProvenance,
  turnReducer,
} from './portal-state';
import type { SendMark, TurnEvent } from './portal-state';
import type { ApprovalSessionStore, WorkItem, WorkMode } from './PortalPanels';
import { ApprovalsPanel, GapsPanel, WorkOnIt } from './PortalPanels';
import { RunsPanel } from './DurableRunsPanel';
import { FlagBadge } from './RunFlags';
import type { RunSeed } from './DurableRunsPanel';
import { TeamProposalCard } from './TeamProposalCard';
import { WorkActionCard } from './WorkActionCard';
import { BuilderModeBar } from './BuilderModeBar';
import { useAgentConversations, usePanelCollapse } from './panel-collapse';
import { localMessageTime, localSessionTime } from './local-time';
import { useActivityScroll } from './activity-scroll';
import type { WorkspaceReceipt } from './portal-protocol';
import { BuilderApprovalCards, builderActionLabel } from './BuilderApprovalCard';
import { ConversationApprovals } from './ConversationApprovals';
import { BuilderFinishStrip } from './SaveToLibrary';
import { ExperienceContext } from './experience';
import { WorkspaceFileProvider, WorkspaceFilePreview } from './WorkspaceFileLinks';
import { hasTurnRow, turnRowId, pendingReadbackRows, singleRenderRows } from './transcript-copies';
import { allTeams, checkedBudget, executionReason, executionRefused, LOOP, mutationDone, mutationKey, pendingMutation, supportsV2 } from './durable-runs';
import type { RunDraft } from './durable-runs';
import { PortalMarkdown } from './PortalMarkdown';
import { ActionCardsContext, type ActionContext, type BobAction } from './ActionCard';
import { ActivityTrail, toolLabel } from './ActivityTrail';
import { managedOrigin, originName, managedKey, readManaged, managedOutcome, managedReport, managedStats, managedFinal, reportAgent, type ManagedRun } from './managed-handoff';
import { displaySessionTitle, firstMessageTitle, rememberDefaultTitle } from './session-names';
import { WorkspacePanel } from './WorkspacePanel';
import { createIdeClient } from './ide-client';
import { createCalendarStore } from './calendar';
import type { CalEntry } from './calendar';
import { TasksCalendar } from './TasksCalendar';
import { createBoardStore } from './board';
import { ProjectsBoard } from './ProjectsBoard';
import { MemoryExplorer } from './MemoryExplorer';
import type { MemoryAsk, MemoryCache } from './MemoryExplorer';
import type { MemItem } from './memory';
import { LibraryView } from './LibraryView';
import type { AppAsk } from './LibraryView';
import { createLibraryStore, sketchText } from './library';
import type { LibraryApp, Sketch } from './library';
import { RecoveryActions } from './RecoveryActions';
import { FirstRun } from './FirstRun';
import { RuntimeUpgrade } from './RuntimeUpgrade';
import { SelfRepair, useRepairStatus, isStuck, isWedged } from './SelfRepair';
import { PcAccess, PcCard, PcModeRow, usePcApprovalClock, usePcQueue } from './PcAccess';
import { plainTurnEnd, ReplyStatus, UserAnswer } from './turn-presentation';
import { EmbedderWarning } from './EmbedderWarning';
import { approvalDone, OwnerSessionIndicator, useOwnerAction } from './OwnerControls';
import { SecurityNotice } from './SecurityNotice';
import { ownerEligible, greater, budgetWallSeconds } from './durable-runs';
import { DEFAULT_APPEARANCE, SettingsView, connectionMode } from './SettingsView';
import type { Appearance, ConnectionPayload } from './SettingsView';
import { useProviderRestartMessage, ReadFaultText, observeRestartLink, ProviderRestartClient } from './provider-restart';

const APPEARANCE_KEY = 'bobportal.appearance';

function readAppearance(): Appearance {
  try {
    const saved = JSON.parse(window.localStorage.getItem(APPEARANCE_KEY) ?? 'null') as Partial<Appearance> | null;
    return { ...DEFAULT_APPEARANCE, ...(saved ?? {}) };
  } catch {
    return DEFAULT_APPEARANCE;
  }
}
import type { IdeContext, IdeHandle } from './IdeEditor';
import { extractOptions, stripOptionBlock } from './quick-replies';
import { STATUS_LABEL, agentKey, ago, buildAgents, hue, initials } from './agents';
import type { AgentThread } from './agents';
import { useConversationStates, frameConversation, conversationLimit, CONVERSATION_LIMIT_LINE } from './conversation-state';
import type { ToolActivity } from './conversation-state';
import './portal.css';

type Panel = 'runs' | null;
type View = 'chat' | 'workspace' | 'tasks' | 'projects' | 'memory' | 'library' | 'settings' | 'needs';

const RAIL_ICONS: Record<string, string> = {
  agents: 'M4 5h16v10H9l-5 4z',
  workspace: 'M3 5h18v14H3zM3 9h18M8 9v10',
  needs: 'M12 3l9 16H3zM12 10v4M12 16.5v.5',
  runs: 'M5 6h14M5 12h14M5 18h9M3 6h0M3 12h0M3 18h0',
  memory: 'M12 4a3 3 0 100 6 3 3 0 000-6zM5 16a3 3 0 100 6 3 3 0 000-6zM19 16a3 3 0 100 6 3 3 0 000-6zM12 10l-6 6M12 10l6 6',
  projects: 'M3 6h7l2 2h9v11H3z',
  library: 'M4 4h4v16H4zM10 4h4v16h-4zM16 5l4 1-3 14-4-1z',
  settings: 'M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2',
};

// Monaco and xterm load only when the Workspace is first opened.
const IdeEditor = lazy(() => import('./IdeEditor'));

/** The editor failing to load must not take the agents column down with it. */
class IdeBoundary extends Component<{ children: ReactNode; fallback?: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(error: unknown) {
    return { error: txt(error, 'the editor could not load') };
  }
  render() {
    if (this.state.error)
      return (
        <div role="alert" className="portal-fault banner">
          <p>The editor and terminal could not load. Chat with the agent still works.</p>
          <p className="hint">{this.state.error}</p>
          {this.props.fallback}
        </div>
      );
    return this.props.children;
  }
}

const CONTEXT_LIMIT = 16000;

/** The message plus the open file (or selection), fenced so the agent can tell them apart. */
function withContext(message: string, context: IdeContext): string {
  const body = context.selection || context.content;
  const label = context.selection ? 'Selected text from' : 'Open file';
  const clipped = body.length > CONTEXT_LIMIT ? `${body.slice(0, CONTEXT_LIMIT)}\n… [clipped at ${CONTEXT_LIMIT} characters]` : body;
  return `${message}\n\n---\n${label}: ${context.path}\n\`\`\`\n${clipped}\n\`\`\``;
}

function RailIcon({ name }: { name: string }) {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d={RAIL_ICONS[name]} />
    </svg>
  );
}

function Avatar({ agent, size = 36 }: { agent: Pick<AgentThread, 'key' | 'name'>; size?: number }) {
  return (
    <span className="avatar" aria-hidden="true" style={{ width: size, height: size, background: `hsl(${hue(agent.key)} 32% 34%)` }}>
      {initials(agent.name)}
    </span>
  );
}


/** One agent turn as a mini terminal in the Workspace: what it called, what it said. */
type AgentRun = {
  id: string;
  agent: string;
  session: string;
  started: number;
  ended: number | null;
  state: 'working' | 'needs_you' | 'done' | 'stopped' | 'error';
  rows: ToolActivity[];
  tail: string;
};

const RUN_LABEL: Record<AgentRun['state'], string> = {
  working: 'working',
  needs_you: 'needs you',
  done: 'done',
  stopped: 'stopped',
  error: 'error',
};

function tileLines(run: AgentRun, liveText: string | null): string[] {
  const lines: string[] = [];
  for (const row of run.rows) {
    if (row.phase === 'start') lines.push(`$ ${row.tool}${row.args ? ` ${row.args}` : ''}`);
    else {
      lines.push(`  ${row.ok === false ? '✗' : '✓'} ${row.tool}${typeof row.elapsed_ms === 'number' ? ` · ${row.elapsed_ms} ms` : ''}`);
      if (row.preview) for (const line of row.preview.split('\n').slice(0, 3)) lines.push(`    ${line}`);
    }
  }
  const text = (liveText ?? run.tail).trim();
  if (text) for (const line of text.split('\n').slice(-8)) lines.push(`› ${line}`);
  if (!lines.length) lines.push(run.state === 'working' ? '… waiting for the first output' : '(no output recorded)');
  return lines;
}

function AgentTiles({ runs, live }: { runs: AgentRun[]; live: Map<string, string> }) {
  if (!runs.length)
    return <p className="subtle ide-pad">When an agent works, a live mini terminal for it appears here: each tool call, its result, and the reply as it streams.</p>;
  return (
    <div className="agent-tiles">
      {runs.map((run) => {
        const seconds = Math.max(0, Math.round(((run.ended ?? Date.now()) - run.started) / 1000));
        return (
          <article key={run.id} className={`agent-tile ${run.state}`}>
            <header>
              <span className="pulse" aria-hidden="true" />
              <strong>{run.agent}</strong>
              <small>{run.session}</small>
              <em>
                {RUN_LABEL[run.state]} · {seconds}s
              </em>
            </header>
            <div className="tile-log">
              <pre>{tileLines(run, live.get(run.id) ?? null).join('\n')}</pre>
            </div>
          </article>
        );
      })}
    </div>
  );
}


function asStr(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function AssistantBody({
  content,
  onChoose,
  onTask,
  actions,
}: {
  content: string;
  onChoose: (choice: string) => void;
  onTask: () => void;
  actions?: ActionContext;
}) {
  const choices = extractOptions(content);
  return (
    <>
      <ActionCardsContext.Provider value={actions ?? null}><PortalMarkdown text={stripOptionBlock(content)} role="assistant" /></ActionCardsContext.Provider>
      {choices.length > 0 && (
        <div className="quick-replies" role="group" aria-label="Choices Bob offered">
          {choices.map((choice) => (
            <button
              key={choice}
              type="button"
              className="quick-reply"
              title={choice}
              onClick={() => onChoose(choice)}
            >
              {choice.length > 96 ? `${choice.slice(0, 93)}…` : choice}
            </button>
          ))}
        </div>
      )}
      <div className="msg-actions">
        <button type="button" className="link" onClick={onTask}>
          Make this a task
        </button>
      </div>
    </>
  );
}

function sortAsc(items: Message[]): Message[] {
  return singleRenderRows([...items].sort((a, b) => {
    const byTime = String(a.created_at ?? '').localeCompare(String(b.created_at ?? ''));
    return byTime !== 0 ? byTime : String(a.id).localeCompare(String(b.id));
  }));
}

function mergeMessages(prior: Message[], incoming: Message[]): Message[] {
  const seen = new Set<string>();
  const merged: Message[] = [];
  for (const message of [...incoming, ...prior]) {
    if (seen.has(message.id)) continue;
    seen.add(message.id);
    merged.push(message);
  }
  return sortAsc(merged);
}

/**
 * Shown only while the server's stored row for a completed turn is genuinely
 * not in the readback yet (R4-1). One re-read is scheduled so it does not stay
 * on screen after the row lands.
 */
const PENDING_READBACK_NOTICE =
  'The answer has finished and is shown here while we check that it was saved.';
const MISSING_READBACK_NOTICE = 'The answer is shown here, but it is still missing from saved history after checking again.';
const READBACK_RETRY_MS = 1500;

function ConnectedPortalApp({
  invoke,
  profiles,
  onProfiles,
  mode = null,
  onConnection,
}: {
  invoke: NativeInvoke;
  profiles: ProfilesPayload | null;
  onProfiles: (payload: ProfilesPayload) => void;
  /** The effective connection mode, when the native connection payload reported one. */
  mode?: 'server' | 'this_pc' | null;
  onConnection?: (payload: ConnectionPayload) => void;
}) {
  // Wording must stay true for This PC mode: there is no "server Bob" on a local runtime.
  const bobName = mode === 'this_pc' ? 'Bob on this PC' : 'the server Bob';
  const bobNameRef = useRef(bobName);
  bobNameRef.current = bobName;
  const client = useMemo<PortalClient>(() => createPortalClient(invoke), [invoke]);
  const workflowRef = useRef<WorkflowStore | null>(null);
  const workspaceClient = client.jobs;
  const ideClient = useMemo(() => createIdeClient(invoke), [invoke]);
  const calendarStore = useMemo(() => createCalendarStore(invoke), [invoke]);
  const boardStore = useMemo(() => createBoardStore(invoke), [invoke]);
  const memoryCache = useRef<MemoryCache>({ items: null, status: null, calls: 0, at: 0 });
  const libraryStore = useMemo(() => createLibraryStore(invoke), [invoke]);
  // A folder the Library asked the Workspace to open; the counter makes a repeat request count.
  const [folderRequest, setFolderRequest] = useState<{ path: string; n: number } | null>(null);
  // Appearance is a per-window convenience, so the window's own storage is enough.
  const [appearance, setAppearance] = useState<Appearance>(readAppearance);
  useEffect(() => {
    try {
      window.localStorage.setItem(APPEARANCE_KEY, JSON.stringify(appearance));
    } catch {
      // Settings still apply for this session without storage.
    }
  }, [appearance]);
  // Team runs the portal has recorded, shown on the calendar on the days they ran.
  const [ledger, setLedger] = useState<LedgerRun[]>([]);
  const [calendarError, setCalendarError] = useState<PortalError | null>(null);
  const ide = useRef<IdeHandle>(null);
  // Workspace: the open file the agent can see, and whether to send it along.
  const [idePath, setIdePath] = useState<string | null>(null);
  const [attachFile, setAttachFile] = useState(true);
  // Mini terminals: the latest agent turns, newest first; bumping the ping brings them into view.
  const [agentRuns, setAgentRuns] = useState<AgentRun[]>([]);
  const [agentsPing, setAgentsPing] = useState(0);
  const updateRun = useCallback((id: string, change: (run: AgentRun) => AgentRun) => {
    setAgentRuns((runs) => runs.map((run) => (run.id === id ? change(run) : run)));
  }, []);
  // Bumped whenever the link is replaced, so Workspace revalidates its open job
  // instead of keeping a schedule that died with the old link.
  const [linkGeneration, setLinkGeneration] = useState(0);
  // Chat stays mounted (hidden) while the Workspace is shown, so a live turn,
  // draft or pending approval is never dropped by switching tabs.
  const [view, setView] = useState<View>('chat');
  const [workspaceOpened, setWorkspaceOpened] = useState(false);
  const [hello, setHello] = useState<Hello | null>(null);
  const ownerIdentity = useRef('');
  const [managedRuns, setManagedRuns] = useState<ManagedRun[]>([]);
  const managedRef = useRef<ManagedRun[]>([]);
  // Mount-local eligibility: reopening the app never sends a report by itself.
  const autoReports = useRef(new Set<string>());
  const managedDispatchBusy = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const saveManaged = useCallback((change: (rows: ManagedRun[]) => ManagedRun[]) => {
    const rows = change(managedRef.current);
    managedRef.current = rows; setManagedRuns(rows);
    try { localStorage.setItem(managedKey(ownerIdentity.current), JSON.stringify(rows)); } catch { /* Session state still works. */ }
  }, []);
  const resetCatalog = useRef(() => {});
  const resetOwnerReads = useCallback(() => {
    resetCatalog.current();
    memoryCache.current = { items: null, status: null, calls: 0, at: 0 };
    setLedger([]);
    setCalendarError(null);
    setRuns([]);
    setLinkGeneration((n) => n + 1);
  }, []);
  const acceptHello = useCallback((next: Hello) => {
    const identity = JSON.stringify([next.identity.sub, next.identity.gateway, next.identity.may_touch, next.identity.may_not_touch]);
    if (ownerIdentity.current && ownerIdentity.current !== identity) resetOwnerReads();
    if (ownerIdentity.current !== identity) {
      localSessions.current.clear();
      autoReports.current.clear();
      managedRef.current = readManaged(identity); setManagedRuns(managedRef.current);
    }
    ownerIdentity.current = identity;
    setHello(next);
  }, [resetOwnerReads]);
  const [rawCatalog, setCatalog] = useState<Catalog | null>(null);
  const catalog = useMemo(() => rawCatalog ? { ...rawCatalog, capabilities: { ...rawCatalog.capabilities, operations: hello?.operations } } : null, [rawCatalog, hello]);
  const ownerAction = useOwnerAction(client, catalog);
  const [modelSetupRequest, setModelSetupRequest] = useState(0);
  function connectModel() { setModelSetupRequest((n) => n + 1); setView('settings'); }
  const [catalogError, setCatalogError] = useState<PortalError | null>(null);
  const catalogLoader = useMemo(() => new CatalogReload(async () => {
    const next = await client.catalog();
    // hello carries the operation capabilities; its broker suppresses a failed
    // capabilities read, so read it again once the model service is responding.
    return { catalog: next, hello: modelRestarting(invoke) ? await client.hello() : null };
  }, next => {
    if (next.hello) { acceptHello(next.hello); setLink('ready'); }
    setCatalog(next.catalog);
  }, reason => setCatalogError(reason === null ? null : toPortalError(reason))), [client, invoke, acceptHello]);
  const reloadCatalog = catalogLoader.reload;
  resetCatalog.current = () => { catalogLoader.invalidate(); setCatalog(null); setCatalogError(null); };
  useEffect(() => {
    const changed = (event: ModelChangedEvent) => {
      if (event.catalogRefreshed) return;
      const read = reloadCatalog(); event.waitUntil?.(read); void read.catch(() => {});
    };
    window.addEventListener('bob-model-changed', changed);
    return () => { catalogLoader.invalidate(); window.removeEventListener('bob-model-changed', changed); };
  }, [catalogLoader, reloadCatalog]);
  const [link, setLink] = useState<'connecting' | 'ready' | 'dead'>('connecting');
  const previousLink = useRef(link);
  useEffect(() => {
    if (link === 'ready' && previousLink.current === 'dead') void reloadCatalog().catch(() => {});
    if (link !== 'connecting') previousLink.current = link;
  }, [link, reloadCatalog]);
  // Successful (re)connects in this window: the update card retires its finished note on the next one.
  const [connectedCount, setConnectedCount] = useState(0);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const localSessions = useRef(new Set<string>());
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const state = useConversationStates();
  function bind(id: string) {
    const scoped = state.scope(id);
    return { ...scoped, setQuestion: (next: typeof scoped.question) => {
      scoped.setQuestion(next);
      if (next) scoped.setQuestions(prior => prior.some(q => q.question_id === next.question_id) ? prior.map(q => q.question_id === next.question_id ? next : q) : [...prior, next]);
      else { scoped.setQuestions([]); scoped.setQuestionDrafts({}); }
    }, apply: (event: TurnEvent) => applyTo(id, event) };
  }
  const {
    messages, hasMore, pins, face, pin, sentPin, research,
    input, sending, stopping, stopCleared, approvalBusy, fault, error,
    notice, workspaceReceipt, activity, setMessages, setHasMore, setFace, setPin,
    setResearch, setInput, setFault, setError, setNotice, setWorkspaceReceipt,
    turn, question, sendMark
  } = bind(conversation?.id ?? '');
  const [windowNotices, setWindowNotices] = useState<string[]>([]);
  const [windowError, setWindowError] = useState<PortalError | null>(null);
  const windowNotice = (text: string) => setWindowNotices(rows => rows.includes(text) ? rows : [...rows, text]);
  const openSequence = useRef(0);
  const earlyFrames = useRef(new Map<string, string[]>());
  // A model pin is only valid with its backend (the broker refuses
  // model-without-backend with `invalid_pin`), so the picker holds the pair.
  const [routingOpen, setRoutingOpen] = useState(false);
  const [modelStatus, setModelStatus] = useState<ModelStatus | null>(null);
  const [projectDefault, setProjectDefault] = useState<{id: string; backend: string | null} | null>(null);
  useEffect(() => {
    const id = conversation?.project_id; let active = true;
    setProjectDefault(null);
    if (id) void client.projects().then(page => {
      const project = page.items.find(row => row.id === id);
      if (active && project) setProjectDefault({id, backend: project.default_backend});
    }, () => { /* Keep the default unnamed when project settings cannot be read. */ });
    return () => { active = false; };
  }, [client, conversation?.project_id]);
  useEffect(() => {
    let active = true;
    let statusSequence = 0;
    const read = () => {
      if (mode !== 'this_pc') { setModelStatus(null); return; }
      const sequence = ++statusSequence;
      void retryModelRead(invoke, () => invoke<ModelStatus>('runtime_model_status')).then(value => { if (active && sequence === statusSequence) setModelStatus(value); }, () => { /* Keep the last good status; Settings offers a refresh. */ });
    };
    read(); window.addEventListener('bob-model-changed', read);
    return () => { active = false; window.removeEventListener('bob-model-changed', read); };
  }, [invoke, mode]);
  /** Request id the server reported as not generating; Stop stops claiming it (R7-3). */
  const restarting = useProviderRestartMessage(client, error ?? fault);
  const [busyLabel, setBusyLabel] = useState('');
  const [panel, setPanel] = useState<Panel>(null);
  // R12: "Make this a task" hands the text to the Runs panel instead of trying
  // to dispatch from the transcript (nothing is spent until Start is pressed).
  const [runsSeed, setRunsSeed] = useState<RunSeed | null>(null);
  // S1 (2026-10-27): what the turn is doing while it does it — the tool trail the
  // core now publishes. Kept per conversation until its next send.
  // S2 (2026-10-27): a question the running turn is parked on. The turn cannot
  // continue until this is answered, so it is rendered above the composer.
  const [runs, setRuns] = useState<Run[]>([]);
  const [search, setSearch] = useState('');
  // The agent whose thread is open; '' is the server's default face.
  const [agentSel, setAgentSel] = useState<string | null>(null);
  // Pending approvals by conversation id, so a thread that needs the owner can rise.
  const [approvalCounts, setApprovalCounts] = useState<Record<string, number>>({});
  const [approvalItems, setApprovalItems] = useState<Approval[]>([]);
  const [approvalReadError, setApprovalReadError] = useState('');
  const [approvalCheckedAt, setApprovalCheckedAt] = useState<number | null>(null);
  const [needsCheckedAt, setNeedsCheckedAt] = useState<number | null>(null);
  const [needsChecking, setNeedsChecking] = useState(false);
  const [needsReadError, setNeedsReadError] = useState('');
  const [questionsReadError, setQuestionsReadError] = useState('');
  const [builderReadErrors,setBuilderReadErrors] = useState<Record<string,string>>({});
  const needsCheckLock = useRef(false);
  const [needTarget,setNeedTarget] = useState<{id:string;card?:string} | null>(null);
  const [needsNow, setNeedsNow] = useState(Date.now);
  const firstSeen = useRef(new Map<string,number>());
  const observedAt = (key: string) => { if (!firstSeen.current.has(key)) firstSeen.current.set(key,Date.now()); return firstSeen.current.get(key)!; };
  const [titleDraft, setTitleDraft] = useState('');
  const [renaming, setRenaming] = useState(false);

  const scroller = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const composing = useRef(false);
  const conversationRef = useRef<Conversation | null>(null);
  // Each turn's own mark, keyed by request id (R3-1): a reconcile that lands
  // after a newer send must still anchor on the turn it belongs to, not on the
  // newer send's prompt.
  const marksByRequest = useRef<Map<string, SendMark>>(new Map());
  // R9-3: approval verdicts belong to this window's run, not to the panel mount.
  const approvalSession = useRef<ApprovalSessionStore>({ decided: {}, refusals: {} });
  // Request ids whose answer was staged from the completion frame's chunks and
  // has not been confirmed by a history row yet (R4-1).
  const stagedReadback = useRef<Set<string>>(new Set());
  const reconcileRef = useRef<
    | ((
        conversationId: string,
        requestId: string,
        messageId: string | null,
        content: string,
        mark: SendMark | null,
        allowRetry?: boolean,
      ) => Promise<void>)
    | null
  >(null);

  // The reducer is the only writer of the turn slice. Keeping a mirror ref lets
  // the Tauri event callback read the same state synchronously, so a chunk can
  // never be matched against a render that has not committed yet.
  const applyTo = useCallback((id: string, event: TurnEvent) => {
    if (event.type === 'accepted' && (!state.scope(id).turn.pending || state.scope(id).turn.stream)) return;
    const { turnRef, questionRef, setQuestion } = bind(id);
    const stream = turnRef.current.stream;
    const managed = stream && managedRef.current.find(r => r.request === stream.request_id && r.session.id === stream.conversation_id);
    if (managed && (event.type === 'chunk' && event.request_id === stream.request_id || event.type === 'terminal' && event.request_id === stream.request_id || event.type === 'transport_fault')) {
      if (event.type === 'chunk') {
        const content = event.content;
        saveManaged(rows => rows.map(r => r.id === managed.id ? { ...r, content: r.content + content } : r));
      }
      else {
        if (event.type !== 'transport_fault' && event.type !== 'terminal') return;
        const state = event.type === 'transport_fault' ? 'interrupted' : managedOutcome(event.kind, event.code, !!questionRef.current);
        const interrupted = event.type === 'transport_fault';
        if (!interrupted) autoReports.current.add(managed.id);
        saveManaged(rows => rows.map(r => r.id === managed.id ? { ...r, content: stream.content,
          state } : r));
      }
    }
    if (event.type === 'terminal' || event.type === 'transport_fault') {
      if (event.type === 'terminal' && questionRef.current && turnRef.current.stream && event.kind !== 'complete') {
        event = { ...event, unanswered_question: true };
      }
      setQuestion(null);
    }
    state.scope(id).setTurn(turnReducer(turnRef.current, event));
    if (event.type === 'accepted') {
      state.requests.set(event.request_id, id);
      const queued = earlyFrames.current.get(event.request_id) ?? [];
      earlyFrames.current.delete(event.request_id);
      queueMicrotask(() => queued.forEach(line => handlerRef.current(line)));
    }
    if (event.type === 'terminal' || event.type === 'transport_fault') {
      state.scope(id).setBuilderWaiting(false);
      state.scope(id).setFinishedUnseen(conversationRef.current?.id !== id);
    }
  }, []);
  const endLiveTurns = useCallback((code: string, detail: string) => {
    autoReports.current.clear();
    for (const [id, record] of state.records) {
      if (!record.turn.stream && !record.turn.pending) continue;
      if ([...(workflowRef.current?.runs.values() ?? [])].some(r => r.stages.some(s => s.conversation_id === id && !!s.request_id && s.request_id === record.turn.stream?.request_id))) continue;
      const scoped = bind(id), request = record.turn.stream?.request_id;
      scoped.setError(null);
      if (record.turn.pending && !record.turn.stream) scoped.setFault({ code, detail, hint: 'Inspect history before sending again.', sent: true, text: record.turn.pending.text, conversation_id: id });
      scoped.apply({ type: 'transport_fault', code, detail, conversation_id: id });
      saveManaged(rows => rows.map(r => r.session.id === id && ['working', 'needs you'].includes(r.state) ? { ...r, state: 'interrupted' } : r));
      if (request) updateRun(request, run => ({ ...run, state: 'error', ended: Date.now(), tail: record.turn.terminal?.partial ?? run.tail }));
    }
  }, []);
  useEffect(() => {
    conversationRef.current = conversation;
  }, [conversation]);

  const refreshList = useCallback(async () => {
    // `page.excluded` is not rendered: the broker's rows carry no owner field, so
    // a count of rows "excluded by ownership" cannot be substantiated here.
    try {
      const page = await client.conversations(50, 0);
      // Keep just-created sessions while the list projection catches up.
      setConversations(prior => [...page.items, ...prior.filter(row => !page.items.some(c => c.id === row.id) && localSessions.current.has(row.id))]);
      setNeedsReadError('');
      return page.items;
    } catch (e) {
      setNeedsReadError('Conversation titles could not be checked.');
      throw e;
    }
  }, [client]);

  // A read only: the badge is a pointer to the Needs-you inbox, never a decision.
  const refreshApprovals = useCallback(async () => {
    try {
      const page = await client.approvals('pending', 50);
      const counts: Record<string, number> = {};
      for (const row of page.items) counts[row.conversation_id] = (counts[row.conversation_id] ?? 0) + 1;
      setApprovalCounts(counts); setApprovalItems(page.items); setApprovalReadError(''); setApprovalCheckedAt(Date.now());
    } catch {
      setApprovalReadError('Approvals could not be checked.');
    }
  }, [client]);

  const openConversation = useCallback(
    async (id: string) => {
      const { setError, setPins, setMessages, setHasMore, setNotice, setFace, setPin, setSentPin, setFault } = bind(id);
      const sequence = ++openSequence.current;
      const historyVersion = state.scope(id).historyVersion;
      try {
        const opened = await client.conversationOpen(id, 50);
        if (sequence !== openSequence.current) return;
        conversationRef.current = opened.conversation;
        setConversation(opened.conversation);
        setAgentSel(agentKey(opened.conversation.face_id));
        state.scope(id).setFinishedUnseen(false);
        setMessages(prior => state.scope(id).historyVersion === historyVersion ? sortAsc(opened.messages) : mergeMessages(prior, opened.messages));
        setHasMore(!!opened.has_more);
        setTitleDraft(opened.conversation.title);
        setRenaming(false);
        try {
          const serverPins = await client.pins(id);
          const hadPins = !!state.scope(id).pins;
          setPins(serverPins);
          setNotice(previous => previous.startsWith('Pins could not be read:') ? '' : previous);
          if (!hadPins) { setFace(serverPins.face_id ?? ''); setPin(pickFromServer(serverPins.model, serverPins.backend)); setSentPin(null); }
        } catch (pinError) {
          setNotice(`Pins could not be read: ${toPortalError(pinError).detail}. The server pin still applies to the next turn.`);
        }
      } catch (e) {
        const typed = toPortalError(e);
        setError(typed);
        if (isTransportFault(typed)) {
          setLink('dead'); endLiveTurns(typed.code, typed.detail);
          setFault({ ...typed, sent: false });
        }
      }
    },
    [client],
  );

  const connect = useCallback(async () => {
    setWindowError(null);
    setLink('connecting');
    setBusyLabel(`Opening the portal to ${bobNameRef.current}…`);
    try {
      // Establish the owner before publishing a catalog for this connection.
      acceptHello(await client.hello());
      let catalogueNote = '';
      try { await reloadCatalog(); }
      catch (reason) {
        if (isTransportFault(toPortalError(reason))) throw reason;
        catalogueNote = 'The server answered, but its face and model catalogue could not be refreshed. Use Refresh models to try again.';
        windowNotice(catalogueNote);
      }
      setLink('ready');
      setConnectedCount((n) => n + 1);
      // Refresh must never clear an unknown send outcome: only a successful
      // history re-read may (CONTRACT §5.5, no duplicate send).
      setFault((prior) => (prior && prior.sent ? prior : null));
      const items = await refreshList();
      void refreshApprovals();
      if (!items.length) {
        windowNotice(
          [catalogueNote, 'No conversations yet on the server for this identity. Create one to start.'].filter(Boolean).join(' '),
        );
      }
    } catch (e) {
      const typed = toPortalError(e);
      setWindowError(typed);
      setLink(isTransportFault(typed) ? 'dead' : 'ready');
      if (isTransportFault(typed)) endLiveTurns(typed.code, typed.detail);
      if (isTransportFault(typed)) setFault((prior) => (prior && prior.sent ? prior : { ...typed, sent: false }));
    } finally {
      setBusyLabel('');
    }
  }, [client, refreshList, refreshApprovals, acceptHello, reloadCatalog]);

  useEffect(() => {
    void connect();
  }, [connect]);

  const reconcile = useCallback(
    async (
      conversationId: string,
      requestId: string,
      messageId: string | null,
      content: string,
      mark: SendMark | null,
      allowRetry = true,
    ) => {
      const { setNotice, setMessages, setHasMore, setError, setFault, apply, turnRef, readbackRetry } = bind(conversationId);
      try {
        const opened = await client.conversationOpen(conversationId, 100);
        const items = sortAsc(opened.messages);
        const managed = managedRef.current.find(r => r.request === requestId && r.session.id === conversationId);
        if (managed) saveManaged(rows => rows.map(r => r.id === managed.id ? { ...r, rows: items } : r));
        if (turnRef.current.stream && turnRef.current.stream.request_id !== requestId || turnRef.current.terminal?.request_id !== requestId) return;
        // R4-1/R3-1: match this turn's answer by the turn itself — the mark this
        // send took and its own prompt row — never by the completion id the frame
        // named (an identifier for the same answer) and never by where the
        // transcript happened to end when the answer is read.
        // Did this turn ever stage a copy from the completion frame's chunks?
        // Only then can the pending notice be this window's to retire.
        const hadPendingCopy = stagedReadback.current.has(requestId);
        const storedRowId = turnRowId(items, conversationId, requestId, messageId, content, mark);
        let inTranscript = storedRowId !== null;
        if (messageId && !inTranscript) {
          items.push({
            id: messageId,
            role: 'assistant',
            content,
            created_at: new Date().toISOString(),
            metadata: { request_id: requestId, history_readback: 'pending' },
          });
          // Shown under the server's own message id, so still exactly one copy.
          inTranscript = true;
          stagedReadback.current.add(requestId);
          setNotice(allowRetry ? PENDING_READBACK_NOTICE : MISSING_READBACK_NOTICE);
          // The server stores the row after it reports completion, so one more
          // read is what resolves this: without it the honest article — and the
          // copy under the completion id — stayed on screen for good.
          if (allowRetry) {
            if (readbackRetry.current !== null) window.clearTimeout(readbackRetry.current);
            readbackRetry.current = window.setTimeout(() => {
              readbackRetry.current = null;
              void reconcileRef.current?.(conversationId, requestId, messageId, content, mark, false);
            }, READBACK_RETRY_MS);
          }
        } else if (hadPendingCopy) {
          // The row is readable now, so the copy staged from this turn's chunks
          // is dropped (this readback is what the transcript is built from) and
          // the "not yet readable" notice is no longer true of it.
          stagedReadback.current.delete(requestId);
          setNotice('');
        }
        setMessages(items);
        setHasMore(!!opened.has_more);
        // R2-1: the reconcile is what makes the streamed copy redundant. Drop
        // it before the note so the answer is never rendered twice under a
        // provenance line that would then be false.
        if (inTranscript) {
          const rowId = storedRowId ?? messageId;
          const terminal = turnRef.current.terminal;
          if (rowId && terminal && (terminal.stopped_reason || terminal.step_extensions !== undefined)) {
            state.scope(conversationId).setReplyOutcomes(prior => ({ ...prior, [rowId]: { stopped_reason: terminal.stopped_reason, step_extensions: terminal.step_extensions } }));
          }
          apply({ type: 'turn_in_transcript', message_id: rowId });
        }
        // R3-4: nothing is re-noted here. The reducer already set the honest
        // `complete` line, and it is only rendered when the streamed copy is kept
        // (the readback did not confirm it). A confirmed turn shows the answer
        // and no extra status article.
        void refreshList().catch(() => undefined);
      } catch (e) {
        const typed = toPortalError(e);
        setError(typed);
        // The server said `complete`, so the send's outcome is known — this must
        // not become an unknown-send lock. The streamed text stays on screen as a
        // local note because history could not confirm it.
        apply({ type: 'note', line: `${COMPLETE_UNREAD_LINE} (${typed.code}).` });
        if (isTransportFault(typed)) {
          setLink('dead'); endLiveTurns(typed.code, typed.detail);
          setFault({ ...typed, sent: false });
        }
      }
    },
    [client, refreshList],
  );

  const handleEvent = useCallback(
    (line: string) => {
      let frame: { event?: unknown; data?: Record<string, unknown> };
      try {
        frame = JSON.parse(line) as { event?: unknown; data?: Record<string, unknown> };
      } catch {
        return;
      }
      const name = asStr(frame.event);
      const data = frame.data ?? {};
      if (!name) return;
      if (workflowRef.current?.observe(name,data)) return;
      if (name === 'transport_fault') {
        const code = asStr(data.code) ?? 'transport_unavailable', detail = asStr(data.detail) ?? 'the portal link closed';
        observeRestartLink(client, false, { code, detail }); setLink('dead');
        if (modelRestarting(invoke)) void reloadCatalog().catch(() => {});
        endLiveTurns(code, detail);
        if (!modelRestarting(invoke)) setWindowError({ code, detail: `The link to ${bobNameRef.current} is down. Nothing was retried. ${detail}`,
          hint: 'Inspect history before sending again.' });
        return;
      }
      if (data.source === 'workflow') {
        if (!workflowRef.current?.compatibility(data)) return;
        const workflowId = asStr(data.conversation_id)!, request = asStr(data.request_id)!;
        const scoped = bind(workflowId);
        if (!['complete','stopped','error'].includes(name) && !scoped.turnRef.current.stream && !scoped.turnRef.current.pending) {
          state.requests.set(request,workflowId);
          scoped.apply({type:'observe',conversation_id:workflowId,request_id:request});
        }
      }
      const requestTag = asStr(data.request_id);
      if (requestTag && !state.requests.has(requestTag) && [...state.records.values()].some(s => s.sending && s.turn.pending && !s.turn.stream)) {
        earlyFrames.current.set(requestTag, [...(earlyFrames.current.get(requestTag) ?? []), line]);
        return;
      }
      const id = frameConversation(state.records, state.requests, data);
      if (!id) {
        if (name === 'approval_request' || name === 'error') {
          windowNotice(name === 'approval_request'
            ? `The server asked for approval (${String(data.action ?? 'unknown action')} · ${String(data.approval_id ?? 'no id')}). Open Needs you to inspect it. Its conversation is unknown.`
            : `The server reported an error (${String(data.code ?? 'gateway_error')}): ${String(data.message ?? data.detail ?? 'No detail supplied')}. Its conversation is unknown.`);
          if (name === 'approval_request') void refreshApprovals();
        }
        return;
      }
      const { setPins, setQuestion, turnRef, setActivity, setNotice, setError, apply, stopRequested } = bind(id);
      if (name === 'pins') {
        setPins({
          face_id: asStr(data.face_id),
          backend: asStr(data.backend),
          model: asStr(data.model),
          profile: asStr(data.profile),
          locale: asStr(data.locale),
        });
        return;
      }
      if (name === 'question') {
        const questionId = asStr(data.question_id);
        if (!questionId) return;
        if (asStr(data.request_id) && asStr(data.request_id) !== turnRef.current.stream?.request_id) return;
        saveManaged(rows => rows.map(r => r.request === turnRef.current.stream?.request_id ? { ...r, state: 'needs you' } : r));
        setQuestion({
          question_id: questionId,
          question: asStr(data.question) ?? 'Bob is asking you something.',
          choices: Array.isArray(data.choices) ? data.choices.filter((c): c is string => typeof c === 'string') : [],
          origin: asStr(data.origin) ?? undefined,
          created_at: asStr(data.created_at) ?? new Date().toISOString(),
        });
        const asking = turnRef.current.stream?.request_id;
        if (asking) updateRun(asking, (run) => ({ ...run, state: 'needs_you' }));
        return;
      }
      if (name === 'builder_wait') {
        state.scope(id).setBuilderWaiting(data.waiting === true);
        window.dispatchEvent(new CustomEvent('bob-builder-changed', { detail: { conversation_id: id } }));
        // PC requests announce themselves with the same wait frame: read the native queue now
        // instead of waiting for the next quiet poll, so the approval card appears at once.
        window.dispatchEvent(new Event('bob-pc-changed'));
        const requestId = asStr(data.request_id);
        if (requestId && requestId === turnRef.current.stream?.request_id) {
          saveManaged(rows => rows.map(r => r.request === requestId ? { ...r, state: data.waiting === true ? 'needs you' : 'working' } : r));
          updateRun(requestId, run => ({ ...run, state: data.waiting === true ? 'needs_you' : 'working' }));
        }
        return;
      }
      if (name === 'tool_activity') {
        if ((asStr(data.tool) ?? '').startsWith('pc_')) window.dispatchEvent(new Event('bob-pc-changed'));
        const requestId = asStr(data.request_id) ?? '';
        const current = turnRef.current.stream;
        const expected = current?.request_id ?? turnRef.current.pending?.request_id ?? '';
        if (requestId && expected && requestId !== expected) return; // another turn's trail
        const frame: ToolActivity = {
          observed_ms: Date.now(),
          request_id: requestId,
          phase: asStr(data.phase) ?? 'result',
          tool: asStr(data.tool) ?? 'tool',
          args: asStr(data.args) ?? undefined,
          preview: asStr(data.preview) ?? undefined,
          ok: typeof data.ok === 'boolean' ? data.ok : undefined,
          elapsed_ms: typeof data.elapsed_ms === 'number' ? data.elapsed_ms : undefined,
        };
        setActivity((rows) => [...rows.slice(-49), frame]);
        saveManaged(rows => rows.map(r => r.request === requestId ? { ...r, activity: [...r.activity, frame] } : r));
        if (['workspace_create', 'workspace_mount'].includes(frame.tool) && frame.phase !== 'start') {
          // Activity supplies only an invalidation hint; re-read the server's
          // canonical binding and receipt before changing the header/browser.
          window.dispatchEvent(new CustomEvent('bob-builder-changed', { detail: { conversation_id: id } }));
        }
        if (requestId) updateRun(requestId, (run) => ({ ...run, state: run.state === 'needs_you' ? run.state : 'working', rows: [...run.rows.slice(-39), frame] }));
        return;
      }
      if (name === 'chunk') {
        const requestId = asStr(data.request_id) ?? '';
        const content = typeof data.content === 'string' ? data.content : '';
        const current = turnRef.current.stream;
        if (!current || current.done || current.request_id !== requestId) {
          setNotice(`Ignored a chunk for request ${requestId || '(none)'} that this window did not start.`);
          return;
        }
        apply({
          type: 'chunk',
          request_id: requestId,
          content,
          model: asStr(data.model),
          backend: asStr(data.backend),
        });
        return;
      }
      if (name === 'approval_request') {
        setNotice(
          `The server asked for approval (${String(data.action ?? 'unknown action')} · ${String(data.approval_id ?? 'no id')}). It is in Needs you.`,
        );
        state.scope(id).setApprovalFrames(prior => ({ ...prior, [String(data.approval_id ?? data.request_id ?? 'approval')]: String(data.action ?? 'Approval') }));
        void refreshApprovals();
        return;
      }
      if (name === 'error') {
        if (asStr(data.request_id) && asStr(data.request_id) !== turnRef.current.stream?.request_id) return;
        const code = asStr(data.code);
        const typed: PortalError = toPortalError({
          ...data, code: code ?? 'gateway_error',
          detail: asStr(data.message) ?? asStr(data.detail) ?? 'the server reported an error for this turn',
          hint: 'The server ended this turn. Nothing is retried automatically.',
        });
        const requestId = asStr(data.request_id) ?? turnRef.current.stream?.request_id ?? '';
        if (isRateLimited(typed) || conversationLimit(typed)) {
          const draft = state.scope(id).sendMark.current?.draft ?? state.scope(id).sendMark.current?.text ?? turnRef.current.pending?.text ?? '';
          state.scope(id).setInput(prior => prior || draft);
          setError(isRateLimited(typed) ? null : { ...typed, detail: CONVERSATION_LIMIT_LINE });
          if (isRateLimited(typed)) { state.scope(id).setFault(null); setNotice(RATE_LIMIT_SEND_LINE); }
          apply({ type: 'refused' });
          updateRun(requestId, run => ({ ...run, state: 'error', ended: Date.now(), tail: isRateLimited(typed) ? RATE_LIMIT_SEND_LINE : CONVERSATION_LIMIT_LINE }));
          return;
        }
        // A model refusal may arrive after the send acknowledgement. Keep the
        // submitted draft here too, without overwriting anything typed since.
        if (code === 'invalid_pin' || /\bnot offered\b/i.test(typed.detail)) {
          const draft = state.scope(id).sendMark.current?.draft ?? turnRef.current.pending?.text ?? '';
          state.scope(id).setInput(prior => prior || draft);
        }
        marksByRequest.current.delete(requestId);
        setError(typed);
        apply({ type: 'terminal', request_id: requestId, kind: 'error', code: typed.code, detail: typed.detail });
        updateRun(requestId, (run) => ({ ...run, state: 'error', ended: Date.now(), tail: `${turnRef.current.stream?.content ?? run.tail}\n[error] ${typed.detail}` }));
        return;
      }
      if (name === 'complete') {
        const requestId = asStr(data.request_id) ?? '';
        const current = turnRef.current.stream;
        if (!current || current.request_id !== requestId) return;
        const messageId = asStr(data.message_id);
        const content = current.content;
        // The mark this turn took at send time, not whatever the last send left
        // behind: two sends can overlap while the first reconcile is in flight.
        const mark = marksByRequest.current.get(requestId) ?? null;
        marksByRequest.current.delete(requestId);
        apply({ type: 'terminal', request_id: requestId, message_id: messageId, kind: 'complete', code: null, detail: null,
          stopped_reason: asStr(data.stopped_reason), step_extensions: typeof data.step_extensions === 'number' ? data.step_extensions : undefined });
        updateRun(requestId, (run) => ({ ...run, state: 'done', ended: Date.now(), tail: content }));
        void reconcile(current.conversation_id, requestId, messageId, content, mark);
        return;
      }
      if (name === 'stopped') {
        if (asStr(data.request_id) && asStr(data.request_id) !== turnRef.current.stream?.request_id) return;
        // Every non-complete turn ends here (broker `_terminal`), including a
        // broker-side close. The lock must be released either way.
        const requestId = asStr(data.request_id) ?? turnRef.current.stream?.request_id ?? '';
        const code = asStr(data.code) ?? 'stopped';
        marksByRequest.current.delete(requestId);
        const byUser = stopRequested.current !== null && stopRequested.current === turnRef.current.stream?.conversation_id;
        stopRequested.current = null;
        const partial = turnRef.current.stream?.content ?? '';
        apply({ type: 'terminal', request_id: requestId, kind: 'stopped', code, detail: null, by_user: byUser });
        updateRun(requestId, (run) => ({ ...run, state: 'stopped', ended: Date.now(), tail: `${partial}\n[stopped: ${code}]` }));
        // The outcome is explained once, after the answer in the transcript.
        setNotice('');
      }
    },
    [applyTo, reconcile, refreshApprovals, updateRun, client, invoke, reloadCatalog],
  );

  const handlerRef = useRef<(line: string) => void>(() => undefined);
  useEffect(() => {
    handlerRef.current = handleEvent;
  }, [handleEvent]);
  useEffect(() => {
    reconcileRef.current = reconcile;
  }, [reconcile]);
  useEffect(
    () => () => {
      // A retry outliving the mount would set state on a dead tree.
      for (const record of state.records.values()) if (record.readbackRetry !== null) window.clearTimeout(record.readbackRetry);
    },
    [],
  );
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    void listen<string>(PORTAL_EVENT, (event) => handlerRef.current(event.payload))
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch((e) => setWindowError(toPortalError(e)));
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    if (!following.current || !scroller.current) return;
    scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [messages.length, turn.stream?.content, turn.pending, turn.terminal]);

  const live = liveStream(turn, conversation?.id);
  const pendingHere = turn.pending && turn.pending.conversation_id === conversation?.id ? turn.pending : null;
  const terminalHere = turn.terminal && turn.terminal.conversation_id === conversation?.id ? turn.terminal : null;
  const liveHasRow = live && hasTurnRow(messages, live.conversation_id, live.request_id, null, live.content, sendMark.current);
  const terminalHasRow = terminalHere && hasTurnRow(messages, terminalHere.conversation_id, terminalHere.request_id, terminalHere.message_id, terminalHere.partial, sendMark.current);
  // R7-3: a turn this window dispatched whose stream died (a transport fault)
  // may still be generating on the server. Stop must stay reachable for it, and
  // stop claiming to be available once the server says nothing is running.
  const orphanedHere =
    terminalHere &&
    terminalHere.kind === 'transport' &&
    terminalHere.request_id !== '' &&
    stopCleared !== terminalHere.request_id
      ? terminalHere
      : null;
  const carrierInFlight = inFlight(turn);
  const unknownOutcome = !!fault?.sent;
  const locked = composerLocked({ sending, stopping, approvalBusy, inFlight: carrierInFlight, unknownOutcome });
  const navLocked = !!busyLabel;

  // A choice loads the composer and focuses it; the operator still presses Send,
  // so a suggested option can never spend a model call on its own.
  function chooseOption(choice: string) {
    setInput(choice);
    requestAnimationFrame(() => {
      (document.getElementById('composer') as HTMLTextAreaElement | null)?.focus();
    });
  }

  async function answerQuestion(text: string, sessionId: string, questionId?: string) {
    const { question, answering, setAnswering, setError, setNotice, turnRef, setMessages } = bind(sessionId);
    const pending = state.scope(sessionId).questions.find(q => q.question_id === questionId) ?? question;
    if (!pending || !text.trim() || answering) return;
    setAnswering(true);
    setError(null);
    try {
      const result = await client.answerQuestion(pending.question_id, text.trim());
      if (result.resolved) {
        workflowRef.current?.settleRecord('question',pending.question_id,'answered');
        const remaining = state.scope(sessionId).questions.filter(q => q.question_id !== pending.question_id);
        state.scope(sessionId).setQuestions(remaining);
        state.scope(sessionId).setQuestion(remaining[0] ?? null);
        if (result.delivered_to_turn===false) setNotice('Answer saved to history. The running turn had already moved on.');
        const session = sessionId;
        saveManaged(rows => rows.map(r => r.request === turnRef.current.stream?.request_id ? { ...r, state: 'working' } : r));
        if (session) {
          try {
            const page=await client.messagesMore(session,null);
            saveManaged(rows => rows.map(r => r.session.id === session ? { ...r, rows: mergeMessages(r.rows, page.items) } : r));
            setMessages(prior=>mergeMessages(prior,page.items));
          } catch { setNotice('Answer saved. Reopen the conversation to refresh its history.'); }
        }
      } else {
        setNotice('The turn had already moved on — that answer was not applied.');
      }
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setAnswering(false);
    }
  }

  // "Make this a task" seeds the Runs panel with the request that produced this
  // answer (falling back to the answer itself). Nothing is dispatched here.
  function makeTask(message: Message) {
    const index = messages.findIndex((row) => row.id === message.id);
    const before = index > 0 ? messages.slice(0, index) : [];
    const prompt = [...before].reverse().find((row) => row.role === 'user');
    const task = (prompt?.content ?? message.content ?? '').trim();
    if (!task) return;
    setRunsSeed({ task, at: Date.now() });
    setView('tasks');
    setPanel('runs');
  }

  async function submit(draftOverride?: string) {
    const { messages, hasMore, face, pin, research, input, sending, stopping, approvalBusy, setSentPin, setInput, setSending, setStopCleared, setFault, setError, setNotice, setActivity, turnRef, setQuestion, sendMark, apply } = bind(conversationRef.current?.id ?? '');
    const current = conversationRef.current;
    const draft = draftOverride ?? input;
    if (!current || !draft.trim()) return;
    // In the Workspace the agent sees the open file (or just the selection) with the message.
    const context = draftOverride === undefined && view === 'workspace' && attachFile ? ide.current?.context() ?? null : null;
    const text = context ? withContext(draft, context) : draft;
    if (composerLocked({ sending, stopping, approvalBusy, inFlight: inFlight(turnRef.current), unknownOutcome: !!state.scope(current.id).fault?.sent })) return;
    setSending(true);
    setError(null);
    setFault(null);
    setStopCleared(null);
    setNotice('');
    // R2-7: remember where the transcript ended, so a later inspection reads
    // only rows this send could have produced — an older identical message can
    // never be mistaken for it. R3-1: the prompt travels with the mark, because
    // the answer is matched to this turn's own prompt row.
    const mark: SendMark = {
      conversation_id: current.id,
      count: messages.length,
      last_id: messages.length ? messages[messages.length - 1].id : null,
      text, draft,
    };
    sendMark.current = mark;
    setActivity([]);
    setQuestion(null);
    apply({ type: 'send', conversation_id: current.id, text, pin });
    setInput('');
    const args: SendArgs = { conversation_id: current.id, content: text };
    if (face) args.face_id = face;
    const pinArgs = sendPinArgs(pin);
    if (pinArgs.backend) args.backend = pinArgs.backend;
    if (pinArgs.model) args.model = pinArgs.model;
    if (research) args.research = true;
    setSentPin(pinArgs.backend && pinArgs.model ? { backend: pinArgs.backend, model: pinArgs.model } : null);
    try {
      const accepted = await client.send(args);
      const creationTime = current.created_at ? new Date(current.created_at) : null;
      const creationDefault = creationTime && Number.isFinite(creationTime.getTime()) ? `${agentName} · ${localSessionTime(creationTime)}` : undefined;
      const autoTitle = !messages.some(m => m.role === 'user') && !hasMore ? firstMessageTitle(current.id, current.title, draft, creationDefault) : null;
      if (autoTitle) void client.conversationRename(current.id, autoTitle).then(result => {
        rememberDefaultTitle(current.id, null);
        setConversation(prior => prior?.id === current.id ? result.conversation : prior);
        setConversations(prior => prior.map(row => row.id === current.id ? result.conversation : row));
      }).catch(() => {});
      marksByRequest.current.set(accepted.request_id, mark);
      if (turnRef.current.pending) apply({ type: 'accepted', request_id: accepted.request_id });
      const running = turnRef.current.stream?.request_id === accepted.request_id;
      setAgentRuns((runs) => [
        {
          id: accepted.request_id,
          agent: agentName,
          session: txt(current.title, 'Untitled session'),
          started: Date.now(),
          ended: running ? null : Date.now(),
          state: running ? 'working' as const : 'error' as const,
          rows: [],
          tail: '',
        },
        ...runs.filter((run) => run.id !== accepted.request_id),
      ].slice(0, 6));
      if (view === 'workspace') setAgentsPing((n) => n + 1);
    } catch (e) {
      const typed = toPortalError(e);
      setError(conversationLimit(typed) ? { ...typed, detail: CONVERSATION_LIMIT_LINE } : typed);
      // Restore only for the submitted text, and only if nothing newer is typed.
      setInput((prior) => (prior.trim().length > 0 ? prior : draft));
      if (isRateLimited(typed)) {
        apply({ type: 'refused' });
        setFault(null); setError(null); setNotice(RATE_LIMIT_SEND_LINE);
      } else if (typed.code === 'not_dispatched') {
        // Nothing reached the link, so this is a clean refusal: the draft comes
        // back and no unknown-outcome lock is raised (R7-4).
        apply({ type: 'refused' });
        setNotice(`The send was refused before dispatch (${typed.detail}). Nothing was sent; the draft is back in the box.`);
      } else if (isTransportFault(typed)) {
        setLink('dead'); endLiveTurns(typed.code, typed.detail);
        // Unknown outcome: the only escape is reading the server history.
        setFault({ ...typed, sent: true, text, conversation_id: current.id });
      } else {
        apply({ type: 'refused' });
      }
    } finally {
      setSending(false);
    }
  }

  async function stop(sessionId?: string) {
    const { stopping, setStopping, setStopCleared, setFault, setError, setNotice, turnRef, stopRequested, apply } = bind(sessionId ?? conversationRef.current?.id ?? '');
    const streamSession = sessionId ?? turnRef.current.stream?.conversation_id;
    const current = streamSession ? { id: streamSession } : conversationRef.current;
    if (!current || stopping) return;
    // R7-3: the turn may belong to a stream this window already lost. The stop
    // still goes to the OPEN conversation, and a `stopped:false` reply is
    // reported as such — never dressed up as a success.
    const orphan = !turnRef.current.stream && turnRef.current.terminal?.kind === 'transport' ? turnRef.current.terminal : null;
    const dispatchedId = turnRef.current.stream?.request_id ?? orphan?.request_id ?? '';
    setStopping(true);
    setError(null);
    setNotice('');
    stopRequested.current = current.id;
    try {
      const result = await client.stop(current.id);
      if (sessionId && !result.stopped && !turnRef.current.stream) {
        saveManaged(rows => rows.map(r => r.session.id === sessionId && r.state === 'interrupted' ? { ...r, state: 'stopped' } : r));
      }
      if (result.stopped) {
        setNotice(
          orphan
            ? `Stop sent for ${current.id}; the server reported ${result.code ?? 'stopped'}. Nothing local was streaming, so inspect the history to confirm the turn ended.`
            : `Stop sent for ${current.id}; the server reported ${result.code ?? 'stopped'}. The turn ends when the server confirms, and the composer unlocks then.`,
        );
      } else {
        setNotice(
          orphan
            ? `The server reported no active generation for ${current.id}: there was nothing to stop server-side. No turn was ended by this window.`
            : 'The server reported no active generation for this conversation.',
        );
      }
      if (orphan) {
        // No `stopped` frame can arrive for a dead link, so nothing local will
        // confirm this stop: drop the pending marker and, when the server said
        // nothing was generating, retire the stop offer.
        stopRequested.current = null;
        if (!result.stopped) setStopCleared(dispatchedId);
      } else if (!result.stopped) {
        // Nothing is generating server-side, so no `stopped` frame is coming:
        // release the local turn instead of leaving the composer locked.
        stopRequested.current = null;
        if (inFlight(turnRef.current)) {
          apply({ type: 'terminal', request_id: turnRef.current.stream?.request_id ?? '', kind: 'stopped', code: null, detail: 'no active generation server-side', by_user: true });
        }
      }
    } catch (e) {
      const typed = toPortalError(e);
      setError(typed);
      if (isRateLimited(typed)) stopRequested.current = null;
      if (isTransportFault(typed)) {
        // The link died, so no terminal frame can arrive for this turn.
        stopRequested.current = null;
        setLink('dead'); endLiveTurns(typed.code, typed.detail);
        // endLiveTurns preserved the ids and partial text of every affected turn.
        setFault({ ...typed, sent: false });
      }
    } finally {
      setStopping(false);
    }
  }

  async function reconnectAndInspect() {
    endLiveTurns('transport_replaced', 'The portal link was replaced. Nothing was replayed; inspect history before sending again.');
    const inspectId = conversationRef.current?.id ?? '';
    const { fault, setError, setFault, setNotice, setMessages, setHasMore, setSentPin, setInput, sendMark } = bind(inspectId);
    resetOwnerReads();
    setHello(null);
    setBusyLabel(`Reconnecting to ${bobName}…`);
    setError(null);
    const unknown = fault && fault.sent ? fault : null;
    try {
      const opened = await client.reconnect();
      setWindowError(null);
      setLink('ready');
      setConnectedCount((n) => n + 1);
      acceptHello(opened.hello);
      if (!unknown) {
        // Nothing is in doubt: an ordinary reconnect may clear the banner.
        setFault((prior) => (prior && prior.sent ? prior : null));
      }
      const targetId = unknown?.conversation_id ?? (inspectId || null);
      if (!targetId) {
        windowNotice(`Reconnected to ${opened.target}. No conversation was open, so no history was re-read and nothing was replayed.`);
        return;
      }
      // The escape from an unknown send outcome: re-read conversations.get +
      // messages.page and decide from the server's own rows.
      const result = await client.conversationOpen(targetId, 100);
      const items = sortAsc(result.messages);
      setMessages(items);
      setHasMore(!!result.has_more);
      void refreshList().catch(() => undefined);
      if (!unknown) {
        setNotice(`Reconnected to ${opened.target}. History re-read: ${items.length} message(s). Nothing was replayed.`);
        return;
      }
      const text = unknown.text ?? '';
      // Only rows after the pre-send mark count (R2-7). If the mark cannot be
      // anchored in this readback the gate stays: a wrong release would invite
      // a second send of the same text.
      const outcome = inspectUnknownSend(items, sendMark.current, text, targetId);
      setSentPin(null);
      if (outcome.state === 'answered') {
        setFault(null);
        setNotice(
          `Reconnected to ${opened.target}. History re-read: the unconfirmed message is stored at message ${outcome.message_id} and has a reply after it, so the outcome is known. Nothing was replayed.`,
        );
      } else if (outcome.state === 'not_stored') {
        setFault(null);
        setInput((prior) => (prior.trim().length > 0 ? prior : text));
        setNotice(
          `Reconnected to ${opened.target}. History re-read: the unconfirmed text is not stored on the server after the pre-send transcript, so the send was not accepted. It is back in the composer; nothing was replayed.`,
        );
      } else {
        // Stored but unanswered, or the readback cannot be tied to this send:
        // outcome still unknown. Keep the gate and offer inspection only —
        // never a resend.
        const reason =
          outcome.state === 'stored_unanswered'
            ? `the unconfirmed text is stored at message ${outcome.message_id} with no assistant reply after it`
            : `this readback cannot be tied to the send in doubt (${outcome.reason})`;
        setFault({
          ...unknown,
          detail: `${unknown.detail} History re-read: ${reason}.`,
          hint: 'The outcome is still unknown. Read the conversation on the server (Refresh reloads the list; it does not unlock Send) or start a new conversation — this window will not send the same text again.',
        });
        setNotice(
          `Reconnected to ${opened.target}. History re-read: ${reason}. Send stays locked (outcome unknown) and nothing is replayed.`,
        );
      }
    } catch (e) {
      const typed = toPortalError(e);
      setError(typed);
      setLink('dead');
      // Keep an unknown-send lock across a failed inspection: only a successful
      // history read may release it.
      if (unknown) setFault({ ...unknown, ...typed, sent: true });
      else setFault({ ...typed, sent: false });
    } finally {
      setBusyLabel('');
    }
  }

  // A new session lives inside the agent's thread: it is a server conversation
  // pinned to that agent's face, so it never appears as a separate contact.
  async function newSession(agent: AgentThread, named?: string, projectId?: string): Promise<boolean> {
    const title = named ?? `${agent.name} · ${localSessionTime()}`;
    setBusyLabel(`Starting a new session with ${agent.name}…`);
    setWindowError(null);
    try {
      const created = await client.conversationNew({
        title,
        ...(agent.face_id ? { face_id: agent.face_id } : {}),
        ...(projectId ? { project_id: projectId } : {}),
      });
      rememberDefaultTitle(created.conversation.id, named === undefined ? title : null);
      const session = created.conversation;
      localSessions.current.add(session.id);
      state.scope(session.id);
      setConversations(rows => [session, ...rows.filter(c => c.id !== session.id)]);
      conversationRef.current = session; setConversation(session); setAgentSel(agentKey(session.face_id));
      await openConversation(session.id);
      state.scope(created.conversation.id).setNotice(`New session started with ${agent.name}.`);
      return true;
    } catch (e) {
      const typed = toPortalError(e);
      setWindowError(typed);
      if (isTransportFault(typed)) { setLink('dead'); endLiveTurns(typed.code, typed.detail); }
      return false;
    } finally {
      setBusyLabel('');
    }
  }

  const refreshLedger = useCallback(async () => {
    const readIdentity = ownerIdentity.current;
    setCalendarError(null);
    try {
      if (supportsV2(catalog)) {
        const rows: Run[] = [], seen = new Set<string>(); let cursor: string | undefined;
        do {
          const page = await client.runsListV2(cursor);
          if (page.source !== 'server') throw new Error('Expected authorized server run summaries.');
          rows.push(...page.items); cursor = page.next_cursor || undefined;
          if (cursor && seen.has(cursor)) throw new Error('The server repeated a runs cursor.');
          if (cursor) seen.add(cursor);
        } while (cursor);
        if (readIdentity !== ownerIdentity.current) return;
        setRuns(prior => [...new Map([...prior, ...rows].map(row => [row.id, row])).values()]);
        setLedger(rows.map(row => ({ run_id: row.id, team: row.team, task_excerpt: row.task_excerpt,
          started_at: row.created_at ?? '', last_status: row.status, last_checked_at: '', receipts: [], source: 'server' as const })));
      } else setLedger((await client.runsList(50, 0)).items);
    } catch (e) {
      if (readIdentity === ownerIdentity.current) setCalendarError(toPortalError(e));
    }
  }, [client, catalog]);

  useEffect(() => { if (conversation?.id && supportsV2(catalog)) void refreshLedger(); }, [conversation?.id, catalog, refreshLedger]);

  const runScope = JSON.stringify([hello?.identity.gateway, hello?.identity.sub, hello?.identity.may_touch, hello?.identity.may_not_touch]);
  const workflows = useMemo(() => new WorkflowStore(client,runScope),[client,runScope]);
  workflowRef.current = workflows;
  const workflowOperations = hello?.operations ?? catalog?.capabilities.operations ?? {};
  const canReadEarlier = workflowOperations['workflows.proposals']?.include_expired === true;
  const againCapability = workflowOperations['workflows.propose_again'];
  workflows.proposeAgainAdvertised = !!againCapability && againCapability.available !== false && againCapability.implemented !== false;
  workflows.includeExpired = canReadEarlier || workflows.proposeAgainAdvertised;
  workflows.proposalsAdvertised = !!workflowOperations['workflows.proposals'];
  workflows.refreshHistory = async run => {
    const ids = new Set([...(run.origin_conversation_id ? [run.origin_conversation_id] : []), ...run.stages.flatMap(s => s.conversation_id && state.records.has(s.conversation_id) ? [s.conversation_id] : [])]);
    for (const id of ids) {
      const page = await client.messagesMore(id,null);
      if (workflowRef.current !== workflows) return;
      const scoped = bind(id); scoped.setMessages(prior => mergeMessages(prior,page.items));
      const stage = run.stages.find(s => s.conversation_id === id && s.request_id === scoped.turnRef.current.stream?.request_id);
      if (stage && ['succeeded','failed','blocked','stopped','expired','unknown'].includes(stage.state) && scoped.turnRef.current.stream) {
        scoped.apply({type:'terminal',request_id:stage.request_id!,kind:stage.state === 'succeeded' ? 'complete' : 'error',code:stage.state === 'succeeded' ? null : stage.state,detail:null});
        if (page.items.some(m => m.role === 'assistant' && m.content.trim() === scoped.turnRef.current.terminal?.partial.trim())) scoped.apply({type:'turn_in_transcript'});
      }
    }
  };
  useWorkflows(workflows);
  const workflowEnabled = Object.keys(hello?.operations ?? catalog?.capabilities.operations ?? {}).some(op => op.startsWith('workflows.')) || workflows.proposals.size > 0 || workflows.summaries.size > 0;
  useEffect(() => { workflows.activate(); return () => workflows.dispose(); },[workflows]);
  useEffect(() => { if (workflowEnabled && link === 'ready') void workflows.discover().catch(() => {}); },[workflows,workflowEnabled,link,workflows.proposalsAdvertised,workflows.includeExpired]);
  useEffect(() => { if (link !== 'ready') return; const focus = () => { void workflows.refreshProposals(); }; window.addEventListener('focus',focus); return () => window.removeEventListener('focus',focus); },[workflows,link]);

  const builderPending = useCallback((id: string, ids: string[], error = '') => {
    setBuilderReadErrors(prior => prior[id] === error ? prior : {...prior,[id]:error});
    const scoped = state.scope(id);
    if (!error && JSON.stringify(scoped.builderApprovals) !== JSON.stringify(ids)) scoped.setBuilderApprovals(ids);
    if (!error) scoped.setBuilderWaiting(ids.length > 0);
  }, []);

  const { collapsed, toggle: togglePanel } = usePanelCollapse(JSON.stringify([hello?.identity.gateway, hello?.identity.sub]));
  const { opened: openedAgents, toggle: toggleAgent } = useAgentConversations(JSON.stringify([hello?.identity.gateway, hello?.identity.sub]));
  const activityVisible = view !== 'chat' || !collapsed.right || !!panel;
  // An effective server/project pin wins. Runtime status identifies the lane default;
  // the catalog alone does not declare which backend is the default.
  const projectBackend = conversation?.project_id
    ? projectDefault?.id === conversation.project_id ? projectDefault.backend ?? modelStatus?.default_backend : null
    : modelStatus?.default_backend;
  const defaultBackend = pins?.backend ?? conversation?.backend ?? projectBackend;
  const defaultModel = pins?.model ?? conversation?.model
    ?? (defaultBackend === 'openai_codex' ? modelStatus?.chatgpt?.selected_model : null)
    ?? catalog?.backends.find(row => row.id === defaultBackend)?.model
    ?? (() => { const rows = catalog?.models.filter(row => row.backend === defaultBackend && row.model) ?? []; return rows.length === 1 ? rows[0].model : null; })();
  const defaultModelName = defaultModel ? (defaultBackend ? modelName({backend: defaultBackend, model: defaultModel}, catalog) : defaultModel) : null;
  const modelEntryLabel = pin ? modelName(pin, catalog) : defaultModelName ? defaultModelName + ' (default)' : catalog && !catalog.models.length && !catalog.backends.length ? 'No model set up' : 'server/project default';
  const pcScope = JSON.stringify(hello?.identity);
  const { queue: pcQueue, refresh: refreshPc, error: pcReadError, checkedAt: pcCheckedAt } = usePcQueue(invoke, pcScope);
  const pcNow = usePcApprovalClock(pcQueue);
  const repair = useRepairStatus(invoke);
  // Attention never changes the owner's panel visibility choice.
  const stuck = isStuck(repair.status);
  const wedged = isWedged(repair.status);
  const pendingPc = pcQueue.requests.filter(c => !c.running && c.request.expires_at * 1000 > pcNow && !pcQueue.outcomes[c.request.id]);
  const pcHere = pendingPc.filter(c => c.request.conversation_id === conversation?.id);
  // A terminal closes this turn's inline prompts even before the next native poll.
  const inlinePc = terminalHere && !live && !pendingHere ? [] : pcHere;
  const inlinePcKey = JSON.stringify(inlinePc.map(c => c.request.id));
  useEffect(() => {
    if (following.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [inlinePcKey]);
  function approvalCount(id: string) {
    const record = state.records.get(id);
    const builder = record?.builderApprovals ?? [];
    const generic = Object.keys(record?.approvalFrames ?? {}).filter(id => !builder.includes(id));
    return Math.max(approvalCounts[id] ?? 0, generic.length) + (builder.length || (record?.builderWaiting ? 1 : 0));
  }
  const attention = pendingPc.length + [...state.records.values()].reduce((sum, r) => sum + r.questions.length, 0) + [...new Set([...conversations.map(c => c.id), ...state.records.keys(), ...Object.keys(approvalCounts)])].reduce((sum, id) => sum + approvalCount(id), 0) + (stuck ? 1 : 0);
  const pcTrail = JSON.stringify(Object.entries(pcQueue.outcomes).filter(([, r]) => r.conversation_id === conversation?.id));
  const activityScroll = useActivityScroll(activityVisible && !panel, conversation?.id, activity, pcTrail);
  const workspaceBinding = useCallback((projectId: string | null, receipt?: WorkspaceReceipt | null) => {
    const id = conversation?.id;
    if (!id) return;
    setConversation(prior => prior?.id === id && prior.project_id !== projectId ? { ...prior, project_id: projectId } : prior);
    setConversations(prior => prior.some(c => c.id === id && c.project_id !== projectId) ? prior.map(c => c.id === id ? { ...c, project_id: projectId } : c) : prior);
    if (receipt?.conversation_id === id && receipt.project_id === projectId && receipt.status === 'completed' && receipt.receipt?.kind === 'workspace') {
      setWorkspaceReceipt(prior => JSON.stringify(prior) === JSON.stringify(receipt) ? prior : receipt);
    }
  }, [conversation?.id]);

  // A calendar agent task started by the owner pressing Run now: a real team run.
  async function runEntry(entry: CalEntry, date: string): Promise<string | null> {
    if (!entry.agent) return null;
    const operation = `calendar:${entry.id}:${date}`;
    try {
      let result: { run: Run };
      if (supportsV2(catalog)) {
        const reason = executionReason(catalog);
        if (reason) throw new Error(reason);
        let payload = pendingMutation<RunDraft>(runScope, operation);
        if (!payload) {
          const team = (await allTeams(client)).find(t => t.id === entry.agent!.team);
          if (!team) throw new Error('This calendar team is not saved on the server.');
          const proposedBudget = checkedBudget(entry.agent.max_calls, entry.agent.budget?.wall_mode ?? 'follow_budget', entry.agent.budget ? entry.agent.budget.wall_seconds : budgetWallSeconds(entry.agent.max_calls));
          if (team.topology === LOOP) {
            setRunsSeed({ team: team.id, task: entry.agent.task, budget: proposedBudget, at: Date.now() });
            setPanel('runs'); windowNotice('Review what done looks like before starting this loop run.');
            return null;
          }
          payload = { contract_version: 2 as const, team: team.id, team_revision: team.revision, task: entry.agent.task,
            budget: proposedBudget };
        }
        const dispatch = (draft: RunDraft) => client.runsStartV2({ ...draft, request_key: mutationKey(runScope, operation, draft) });
        try {
          if (greater(payload.budget.start_calls, 200) && ownerEligible(hello?.identity, catalog)) result = await ownerAction.approveStart(payload, runScope, operation, dispatch);
          else result = await dispatch(payload);
        } catch (e) {
          if (toPortalError(e).code !== 'confirmation_required' || !ownerEligible(hello?.identity, catalog)) throw e;
          mutationDone(runScope, operation);
          result = await ownerAction.approveStart(payload, runScope, operation, dispatch);
        }
      } else {
        if (greater(entry.agent.max_calls, 4)) throw new Error('Update Bob on the server for this budget. It will not be reduced to four calls.');
        result = await client.runsStart({
        team: entry.agent.team,
        task: entry.agent.task,
        max_model_calls: Number(entry.agent.max_calls),
        max_seconds: 900,
        });
      }
      mutationDone(runScope, operation); approvalDone(runScope, operation);
      setRuns((prior) => [result.run, ...prior.filter((row) => row.id !== result.run.id)]);
      void refreshLedger();
      return result.run.id;
    } catch (e) {
      const typed = toPortalError(e);
      if (executionRefused(typed.code)) mutationDone(runScope, operation);
      setCalendarError(typed);
      return null;
    }
  }

  // Teams are set up by talking to an agent, not by filling a form.
  async function teamSetup() {
    if (!supportsV2(catalog)) {
      setNotice('Update Bob on the server to save teams with execution v2. Legacy runs remain available in Run history.');
      return;
    }
    if (navLocked) {
      setNotice('A turn is still running; let it finish before starting team setup.');
      return;
    }
    const agent = selectedAgent ?? agents.find((row) => row.key === '') ?? agents[0];
    if (!agent) return;
    setPanel(null);
    if (!(await newSession(agent, 'Team setup'))) return;
    state.scope(conversationRef.current?.id ?? '').setInput(
      [
        'Help me set up a team of agents. Ask only for missing task or evidence details, one question at a time.',
        'Propose the roles, available bindings, acceptance criteria and fitting starting budget yourself; I can tweak them.',
        'Use the server configured palette; never invent providers or team names. Saving never starts a run.',
        'When ready, include a fenced bob-team-proposal JSON block with definition, expected_revision (null for new), task and budget. Preserve original source IDs in optional evidence_ids and output criteria in acceptance.',
        'definition: {schema_version:1,id:<new slug>,state:"saved",topology:<supported topology>,roles:{manager:<configured binding id>,worker:<binding id>,critic:<binding id>},review_independence:"same_model"}. Missing bindings may be null; these save as blocked.',
        `Available topologies: ${catalog?.capabilities.execution_v2?.topologies?.join(', ') || 'four-step-readonly/v1 (loop unavailable; update Bob on the server)'}. Propose agent-loop/v1 for multi-part work when available, four-step-readonly/v1 for a quick reviewed answer. The user can switch.`,
        'For a loop, draft acceptance:{criteria:[{id:"answer",required:true,output_kind:"answer",checks:{min_length:200,max_length:2000,required_sections:["Conclusion"],require_citations:true}}]}. Tailor rows and bounds to the actual task. At least one required row. Kinds: answer, report, proposal, json. Cite only supplied evidence IDs.',
        `Configured execution binding ids: ${(catalog?.capabilities.execution_models ?? []).map(m => `${m.id} (${m.reason ?? 'server checks eligibility on save'})`).join(', ') || 'unavailable; leave unresolved'}.`,
        'budget: {start_calls:<10/50/100/200 or fitting custom integer>,wall_mode:"fixed"|"follow_budget"|"none",wall_seconds:<positive integer or null only for none>}. Propose a fitting starting level and time; I can tweak before Start.',
        'Budget guidance: 10 for small bounded work, 50 for several deliverables, 100 for moderate work, 200 for larger work. Do not always choose 10. Follow-budget timing uses 360 seconds per call.',
        'Summarize the team and propose calendar entries; schedules stay manual.',
        '',
        'Goal: ',
      ].join('\n'),
    );
    state.scope(conversationRef.current?.id ?? '').setNotice(`Team setup with ${agent.name}: finish the goal line and press Send.`);
    requestAnimationFrame(() => (document.getElementById('composer') as HTMLTextAreaElement | null)?.focus());
  }

  // Projects: the PM is the agent in the right column; agent teams are the workers.
  function pmAgent(): AgentThread | undefined {
    return selectedAgent ?? agents.find((row) => row.key === '') ?? agents[0];
  }

  const PM_ROLE =
    'You are my project manager (PM). Agent teams do the work; you plan it with me, split it into pieces a team can take, and keep the board honest. ' +
    'Use the server teams list and its runnable or blocked reasons. You cannot move cards or start runs yourself: tell me what to move or start.';

  /** The card click is the send; model-supplied actions are otherwise inert. */
  async function startHandoff(action: Extract<BobAction, { type: 'handoff' }>, id: string, managed: boolean) {
    if (managedDispatchBusy.current || busyLabel) throw new Error('A dispatch is already being prepared.');
    const agent = agents.find(row => row.face_id === action.agent && row.allowed);
    const origin = conversationRef.current;
    if (!agent || !origin) throw new Error('Session unavailable.');
    if (!managed) {
      if (!(await newSession(agent, action.title))) throw new Error('Session unavailable.');
      setView('chat'); setPanel(null); state.scope(conversationRef.current?.id ?? '').setInput(action.brief);
      state.scope(conversationRef.current?.id ?? '').setNotice(`Ready for ${agent.name}: check the request and press Send.`);
      return;
    }
    const originFace = managedOrigin(origin.face_id);
    if (managedRef.current.some(r => r.id === id)) return;
    managedDispatchBusy.current = true;
    let targetId: string | null = null;
    let targetReserved = false;
    try {
      const opened = action.session ? await client.conversationOpen(action.session, 100) : null;
      if (opened && opened.conversation.face_id !== action.agent) throw new Error('Session belongs to another agent.');
      const session = opened?.conversation ?? (await client.conversationNew({ title: action.title, face_id: action.agent })).conversation;
      if (!opened) localSessions.current.add(session.id);
      targetId = session.id;
      const { setMessages, setActivity, setQuestion, sendMark, apply } = bind(session.id);
      const target = state.scope(session.id);
      if (target.turn.stream || target.turn.pending || target.sending || target.fault?.sent) throw new Error('This agent session already has a turn or an unknown send outcome.');
      target.setSending(true); targetReserved = true;
      const history = sortAsc(opened?.messages ?? []);
      setMessages(history); target.setFace(action.agent);
      const run: ManagedRun = { id, origin: origin.id, originFace, agent: action.agent, name: agent.name, title: action.title, session,
        request: '', brief: action.brief, before: history.map(m => m.id), state: 'working', content: '', rows: history, activity: [], report: 'pending' };
      saveManaged(rows => [...rows, run]);
      setConversations(rows => [session, ...rows.filter(c => c.id !== session.id)]);
      setView('chat'); setPanel(null); setActivity([]); setQuestion(null);
      const mark: SendMark = { conversation_id: session.id, count: history.length, last_id: history.length ? history[history.length - 1].id : null, text: action.brief };
      sendMark.current = mark;
      apply({ type: 'send', conversation_id: session.id, text: action.brief, pin: null });
      const accepted = await client.send({ conversation_id: session.id, face_id: action.agent, content: action.brief });
      if (!alive.current) return;
      marksByRequest.current.set(accepted.request_id, mark);
      saveManaged(rows => rows.map(r => r.id === id ? { ...r, request: accepted.request_id } : r));
      apply({ type: 'accepted', request_id: accepted.request_id });
      const running = target.turnRef.current.stream?.request_id === accepted.request_id;
      setAgentRuns(rows => [{ id: accepted.request_id, agent: agent.name, session: session.title, started: Date.now(), ended: running ? null : Date.now(), state: running ? 'working' as const : 'error' as const, rows: [], tail: '' }, ...rows].slice(0, 6));
    } catch (e) {
      if (targetId && managedRef.current.some(r => r.id === id)) applyTo(targetId, { type: 'refused' });
      saveManaged(rows => rows.map(r => r.id === id ? { ...r, state: 'interrupted' } : r));
      const typed = toPortalError(e);
      const run = managedRef.current.find(r => r.id === id);
      if (run && (isRateLimited(typed) || conversationLimit(typed))) {
        state.scope(run.session.id).setError(isRateLimited(typed) ? null : { ...typed, detail: CONVERSATION_LIMIT_LINE });
        if (isRateLimited(typed)) state.scope(run.session.id).setNotice(RATE_LIMIT_SEND_LINE);
        state.scope(run.session.id).setInput(prior => prior || action.brief);
      }
      if (run && isTransportFault(typed)) {
        setLink('dead'); endLiveTurns(typed.code, typed.detail);
        state.scope(run.session.id).setFault({ ...typed, sent: true, text: action.brief, conversation_id: run.session.id });
      }
      throw e;
    } finally { managedDispatchBusy.current = false; if (alive.current && targetId && targetReserved) state.scope(targetId).setSending(false); }
  }

  async function sendManagedReport(id: string) {
    const run = managedRef.current.find(r => r.id === id);
    if (!run || run.report === 'sent' || run.report === 'sending' || managedDispatchBusy.current) return;
    const { turnRef, sending, fault, setSending, setMessages, sendMark, apply, setFault, setError } = bind(run.origin);
    if (inFlight(turnRef.current) || turnRef.current.pending || sending || fault?.sent) return;
    autoReports.current.delete(id);
    managedDispatchBusy.current = true;
    // Persist the in-progress receipt before any async work or network send.
    saveManaged(rows => rows.map(r => r.id === id ? { ...r, report: 'sending' } : r));
    setSending(true);
    let dispatched = false;
    try {
      const agentHistory = await client.conversationOpen(run.session.id, 100);
      if (!alive.current) return;
      const rows = sortAsc(agentHistory.messages);
      const completed = { ...run, rows };
      const text = run.reportText ?? managedReport({ ...completed, content: managedFinal(rows, run) });
      const assistant = await client.conversationOpen(run.origin, 100);
      if (!alive.current) return;
      const originFace = managedOrigin(assistant.conversation.face_id);
      if (originFace !== (run.originFace ?? 'assistant')) throw new Error('Report destination agent changed.');
      const receipt = run.report === 'uncertain' ? inspectUnknownSend(sortAsc(assistant.messages), run.reportMark, text, run.origin) : null;
      if (receipt?.state === 'stored_unanswered' || receipt?.state === 'answered') {
        saveManaged(all => all.map(r => r.id === id ? { ...completed, report: 'sent', reportText: text } : r));
        return;
      }
      if (run.report === 'uncertain') throw new Error('Report delivery is uncertain. Inspect the originating conversation history before sending another report.');
      saveManaged(all => all.map(r => r.id === id ? { ...completed, report: 'sending', reportText: text } : r));
      // Deliver on the originating conversation's own carrier without moving the open conversation.
      setMessages(prior => mergeMessages(prior, assistant.messages));
      if (!alive.current) return;
      const ordered = sortAsc(assistant.messages);
      const mark: SendMark = { conversation_id: run.origin, count: ordered.length, last_id: ordered.length ? ordered[ordered.length - 1].id : null, text };
      saveManaged(all => all.map(r => r.id === id ? { ...r, reportMark: mark, reportDispatched: true } : r));
      sendMark.current = mark;
      apply({ type: 'send', conversation_id: run.origin, text, pin: null });
      dispatched = true;
      const accepted = await client.send({ conversation_id: run.origin, face_id: originFace, content: text });
      if (!alive.current) return;
      marksByRequest.current.set(accepted.request_id, mark);
      apply({ type: 'accepted', request_id: accepted.request_id });
      saveManaged(all => all.map(r => r.id === id ? { ...r, report: 'sent' } : r));
    } catch (e) {
      if (!alive.current) return;
      apply({ type: 'refused' });
      saveManaged(rows => rows.map(r => r.id === id ? { ...r, report: isRateLimited(toPortalError(e)) ? 'pending' : dispatched || run.report === 'uncertain' ? 'uncertain' : 'pending' } : r));
      const typed = toPortalError(e);
      if (dispatched && isTransportFault(typed)) {
        setLink('dead'); endLiveTurns(typed.code, typed.detail);
        setFault({ ...typed, sent: true, text: managedRef.current.find(r => r.id === id)?.reportText, conversation_id: run.origin });
      }
      setError(isRateLimited(typed) ? null : typed);
      if (isRateLimited(typed)) { state.scope(run.origin).setNotice(RATE_LIMIT_SEND_LINE); state.scope(run.origin).setInput(prior => prior || managedRef.current.find(r => r.id === id)?.reportText || ''); }
    } finally { managedDispatchBusy.current = false; if (alive.current) setSending(false); }
  }
  const sendManagedReportRef = useRef(sendManagedReport);
  sendManagedReportRef.current = sendManagedReport;
  useEffect(() => {
    if (link !== 'ready') return;
    const id = [...autoReports.current].find(id => {
      const run = managedRef.current.find(r => r.id === id);
      if (!run) return false;
      const target = state.scope(run.origin);
      return !target.turn.stream && !target.turn.pending && !target.sending && !target.fault?.sent;
    });
    if (id) void sendManagedReportRef.current(id);
  });

  async function startSession(title: string, text: string, projectId?: string) {
    if (navLocked) {
      setNotice('A turn is still running; let it finish before starting another session.');
      return;
    }
    const agent = pmAgent();
    if (!agent) return;
    if (!(await newSession(agent, title.slice(0, 120), projectId))) return;
    state.scope(conversationRef.current?.id ?? '').setInput(text.slice(0, 60000));
    state.scope(conversationRef.current?.id ?? '').setNotice(`Ready for ${agent.name}: check the request and press Send.`);
    requestAnimationFrame(() => (document.getElementById('composer') as HTMLTextAreaElement | null)?.focus());
  }

  function startPm(title: string, body: string, projectId?: string) {
    return startSession(title, `${PM_ROLE}\n\n${body}`, projectId);
  }

  // Library: plan a new app with the PM, hand an app to an agent, or send a sketch.
  function planApp(app: LibraryApp, idea: string) {
    void startPm(
      `PM · ${app.name}`,
      [
        `We are planning a new ${app.kind} called “${app.name}”. Ask me questions one at a time to shape it, then propose:`,
        'what it does, its parts, which agent team builds which part, the first milestone, and what you need from me.',
        'If it helps, describe the workflow as numbered steps I can sketch in the Workflow builder.',
        '',
        `Idea: ${idea || '(not written yet — ask me)'}`,
      ].join('\n'),
      app.project_id ?? undefined,
    );
  }

  function askAboutApp(app: LibraryApp, mode: AppAsk) {
    const ask =
      mode === 'drive'
        ? 'Help me use this. Ask what I want to get done with it, then tell me the steps or the team run that would do it.'
        : mode === 'manage'
          ? 'Help me manage this: is it healthy and up to date, what should be documented, improved or retired? Propose concrete next actions.'
          : 'Something is wrong with this. Ask me what I am seeing, then work out the likely cause and a fix or a plan to fix it.';
    const facts = [
      `${app.kind}: ${app.name} (${app.status})`,
      mode === 'fix' && (app.project_id
        ? 'Result opens as an HTML page in the in-app preview (scripts off). Interactive behaviour can only be tried with Open on this PC. Ask where I looked.'
        : 'Result: not an HTML page opened in the in-app preview.'),
      app.summary && `Summary: ${app.summary}`,
      app.command && `Launch command: ${app.command}`,
      app.folder && `Folder: ${app.folder}`,
      `Launched ${app.launches} time(s)${app.last_launched ? `, last ${app.last_launched}` : ''}`,
    ]
      .filter(Boolean)
      .join('\n');
    void startSession(`App · ${app.name}`, `${ask}\n\n${facts}`, app.project_id ?? undefined);
  }

  function sendSketch(sketch: Sketch, app: LibraryApp | null) {
    const text=[
      'Please turn this sketch into a complete workflow/v1 definition for me to review. Ask about anything unclear. Include a changes summary and the full definition in a JSON code block.',
      'If it has a saved user definition, return {"definition_ref":{"id":"the saved id","revision":"the saved revision"},"definition":{...}} so I can review and save a new revision. For a premade, propose a new definition without definition_ref. Do not save, propose a run or start work.',
      ...(app ? [`It is for: ${app.name}${app.summary ? ` — ${app.summary}` : ''}`] : []),sketchText(sketch),
    ].join('\n\n');
    void (async()=>{
      const assistant=agents.find(row=>row.face_id==='assistant');
      if(!assistant){setNotice('Connect an Assistant before sending this sketch.');return;}
      if(conversationRef.current?.face_id !== 'assistant' && !(await newSession(assistant,`Sketch · ${sketch.name}`,app?.project_id ?? undefined)))return;
      const target=state.scope(conversationRef.current?.id ?? '');target.setInput([target.input,text].filter(Boolean).join('\n\n'));setView('chat');setNotice('The sketch is ready in the Assistant draft. Review it and press Send.');
    })();
  }

  function pickUpProject(project: Project) {
    void startPm(
      `PM · ${project.name}`,
      [
        `Let's pick up the project “${project.name}”. Summarise where it stands, propose the next step,`,
        'split it into work an agent team can take, say which team and how many model calls it needs, and tell me what you need from me.',
        '',
        `Summary: ${project.description || '(none yet — propose one)'}`,
        `Instructions for the agents: ${project.instructions || '(none)'}`,
        `Sessions so far: ${project.conversation_count}`,
      ].join('\n'),
      project.id,
    );
  }

  function askPmAboutBoard(summary: string) {
    void startPm(
      'PM · board review',
      `Here is my project board. What should we focus on next? Suggest card moves and which team should take what. Keep it short.\n\n${summary}`,
    );
  }

  // Memory hand-off: picked memories go to the agent on the right to talk about or manage.
  async function askAboutMemory(items: MemItem[], mode: MemoryAsk) {
    if (!items.length) return;
    if (navLocked) {
      setNotice('A turn is still running; let it finish before handing memories over.');
      return;
    }
    const agent = pmAgent();
    if (!agent) return;
    const name = items.length === 1 ? items[0].title : `${items.length} memories`;
    if (!(await newSession(agent, `Memory · ${name}`.slice(0, 120)))) return;
    const ask =
      mode === 'manage'
        ? 'Help me manage these memories. For each: is it still true, a duplicate, test data, or worth merging or rewriting? ' +
          'Propose exact changes. You cannot edit central memory directly yet, so give me the replacement text to save and say which ones to retire.'
        : "Let's talk about these memories. What do they tell you, how do they connect, and what is missing?";
    const body = items
      .map((item) => `### ${item.title}\nsource: ${item.source_id} (${item.kind}${item.flags.length ? `; flags: ${item.flags.join(', ')}` : ''})\n${item.excerpt}`)
      .join('\n\n');
    state.scope(conversationRef.current?.id ?? '').setInput(`${ask}\n\n${body}`.slice(0, 60000));
    state.scope(conversationRef.current?.id ?? '').setNotice(`Ready for ${agent.name}: review the request and press Send.`);
    requestAnimationFrame(() => (document.getElementById('composer') as HTMLTextAreaElement | null)?.focus());
  }

  // Needs-you hand-off: a fresh session with the agent that raised it (or the open
  // one), with the request typed into the composer. Nothing is spent until Send.
  async function workOn(item: WorkItem, mode: WorkMode) {
    if (navLocked) {
      setNotice('A turn is still running; stop it or let it finish before starting another piece of work.');
      return;
    }
    const origin = item.conversation_id ? conversations.find((row) => row.id === item.conversation_id) : undefined;
    const agent =
      (origin && agents.find((row) => row.key === agentKey(origin.face_id))) ?? selectedAgent ?? agents.find((row) => row.key === '') ?? agents[0];
    if (!agent) return;
    const label = item.kind === 'approval' ? 'Approval' : 'Gap';
    const ask =
      mode === 'fix'
        ? 'Please work on this and fix it. Say what you will change before you do anything that cannot be undone.'
        : 'Please read this and write a plan to fix it: the likely cause, the steps, what you need from me, and the risks. Do not change anything yet.';
    setPanel(null);
    setView('chat');
    const started = await newSession(agent, `${mode === 'fix' ? 'Fix' : 'Plan'} · ${item.title}`.slice(0, 120));
    if (!started) return;
    state.scope(conversationRef.current?.id ?? '').setInput(`${ask}\n\n${label}: ${item.title}\n${item.body}`);
    state.scope(conversationRef.current?.id ?? '').setNotice(`Ready for ${agent.name}: review the request and press Send.`);
    requestAnimationFrame(() => (document.getElementById('composer') as HTMLTextAreaElement | null)?.focus());
  }

  async function rename() {
    const current = conversationRef.current;
    if (!current || !titleDraft.trim()) return;
    rememberDefaultTitle(current.id, null);
    setBusyLabel('Renaming on the server…');
    try {
      const result = await client.conversationRename(current.id, titleDraft.trim());
      setConversation(prior => prior?.id === result.conversation.id ? result.conversation : prior);
      setRenaming(false);
      await refreshList();
      setNotice('Title saved by the server.');
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusyLabel('');
    }
  }

  async function loadMore() {
    const current = conversationRef.current;
    if (!current || !messages.length) return;
    setBusyLabel('Loading older messages…');
    try {
      const page = await client.messagesMore(current.id, messages[0].id, 50);
      setMessages((prior) => mergeMessages(prior, page.items));
      setHasMore(!!page.has_more);
    } catch (e) {
      setError(toPortalError(e));
    } finally {
      setBusyLabel('');
    }
  }

  function openAgent(agent: AgentThread) {
    setAgentSel(agent.key);
    if (agent.sessions.length) {
      // Re-reading the open session is deliberate: it is how the owner refreshes a thread.
      void openConversation(agent.sessions[0].id);
      return;
    }
    // A contact with no sessions yet: nothing to open, the thread offers to start one.
    conversationRef.current = null;
    setConversation(null);
    state.scope('').setFace(agent.face_id ?? '');
  }

  const sessionState = (id: string) => {
    const record = state.records.get(id);
    if (record?.questions.length || record?.builderApprovals.length || record?.builderWaiting || Object.keys(record?.approvalFrames ?? {}).length || (approvalCounts[id] ?? 0) || pendingPc.some(c => c.request.conversation_id === id)) return 'needs you';
    if (record?.turn.stream || record?.turn.pending) return 'working';
    return record?.finishedUnseen ? 'finished since you last looked' : '';
  };
  const agents = buildAgents(conversations, catalog?.faces ?? hello?.identity.faces ?? [], {
    working: [...state.records].filter(([, r]) => r.turn.stream || r.turn.pending).map(([id]) => id),
    asking: conversations.filter(c => sessionState(c.id) === 'needs you').map(c => c.id),
    approvals: approvalCounts,
  });
  const questionNeeds = [...state.records].flatMap(([id, r]) => r.questions.map(q => ({ id, q })));
  const approvalNeeds = [...new Set([...conversations.map(c => c.id), ...state.records.keys(), ...Object.keys(approvalCounts)])].flatMap(id => {
    const count = approvalCount(id);
    return count ? [{ id, count }] : [];
  });
  const sessionLabel = (id: string) => {
    const c = conversations.find(c => c.id === id);
    const agent = agents.find(a => a.sessions.some(s => s.id === id));
    return `${agent?.name ?? 'Agent'} · ${c?.title ?? id}`;
  };
  async function openNeed(id: string, card?: string) {
    setPanel(null); setView('chat');
    await openConversation(id);
    setNeedTarget({id,card});
  }
  useEffect(() => {
    if (!needTarget || view !== 'chat' || conversation?.id !== needTarget.id) return;
    const focus = () => {
      const element = needTarget.card ? document.getElementById(needTarget.card) : document.querySelector<HTMLElement>('.ask-card, .inline-pc-approval, [aria-label="Builder approval"], .conversation-approvals [aria-label="Approvals"]');
      if (!element) return false;
      element.scrollIntoView?.({block:'center'}); element.focus(); setNeedTarget(null); return true;
    };
    if (focus()) return;
    // Approval details can arrive after the conversation. Wait for that card to mount.
    const observer = new window.MutationObserver(() => { if (focus()) observer.disconnect(); });
    observer.observe(document.querySelector('.portal-body') ?? document.body,{childList:true,subtree:true});
    return () => observer.disconnect();
  },[needTarget,view,conversation?.id]);
  const needle = search.trim().toLowerCase();
  const sessionTitle = (row: Conversation) => displaySessionTitle(row, agents.find(a => a.key === agentKey(row.face_id))?.name ?? 'Bob');
  const matchesSession = (row: Conversation) => `${sessionTitle(row)} ${row.project_id ?? ''}`.toLowerCase().includes(needle);
  const filtered = agents.filter(
    (agent) =>
      !needle ||
      `${agent.name} ${agent.face_id ?? ''}`.toLowerCase().includes(needle) || agent.sessions.some(matchesSession),
  );
  const selectedAgent = agents.find((agent) => agent.key === agentSel) ?? null;
  const sessionIndex = selectedAgent && conversation ? selectedAgent.sessions.findIndex((row) => row.id === conversation.id) : -1;
  const olderSession = selectedAgent && sessionIndex >= 0 ? (selectedAgent.sessions[sessionIndex + 1] ?? null) : null;
  const newerSession = selectedAgent && sessionIndex > 0 ? selectedAgent.sessions[sessionIndex - 1] : null;
  const workflowProposals = [...workflows.proposals].filter(([,p]) => p.proposal.state === 'proposed' && !p.proposal.run_id && Date.parse(p.proposal.expires_at) > needsNow);
  const workflowWaits = [...workflows.runs.values()].filter(r => !workflowEnded(r,r) && (['paused','interrupted'].includes(r.status) || r.needs_you.some(w => {
    if (w.state !== 'open') return false;
    if (w.kind === 'question' && questionNeeds.some(q => q.q.question_id === w.record_id)) return false;
    if (w.kind === 'builder_approval' && approvalNeeds.some(a => a.id === w.conversation_id)) return false;
    if (w.kind === 'pc' && pendingPc.some(p => p.request.id === w.record_id)) return false;
    return true;
  })));
  const workflowNeedsCount = workflowProposals.length + workflowWaits.length;
  const needsCount = workflowNeedsCount + approvalNeeds.reduce((sum, n) => sum + n.count, 0) + questionNeeds.length + pendingPc.length;
  useEffect(() => {
    const expiry = [...workflows.proposals.values()].filter(p => p.proposal.state === 'proposed').map(p => Date.parse(p.proposal.expires_at)).filter(t => t > Date.now()).sort((a,b) => a-b)[0];
    if (!expiry) return;
    const timer = setTimeout(() => setNeedsNow(Date.now()),Math.min(2147483647,Math.max(0,expiry-Date.now())));
    return () => clearTimeout(timer);
  },[workflows,workflows.snapshot(),needsNow]);
  async function checkNeeds() {
    if (needsCheckLock.current) return;
    needsCheckLock.current = true; setNeedsChecking(true);
    try {
      const results = await Promise.allSettled([
        refreshList(), refreshApprovals(), refreshPc(), client.hello(),
        ...(workflowEnabled ? [workflows.discover(true)] : []),
        ...[...state.records].filter(([,r]) => r.builderWaiting || r.builderApprovals.length).map(([id]) => builderReadStore(client,id,runScope,link).refresh()),
      ]);
      setNeedsReadError(results[0].status === 'rejected' ? 'Conversation titles could not be checked.' : '');
      setQuestionsReadError(results[3].status === 'rejected' ? 'Questions could not be checked because the connection to Bob is unavailable.' : '');
      if (results[3].status === 'fulfilled') acceptHello(results[3].value as Hello);
      setNeedsCheckedAt(Date.now());
    } finally { needsCheckLock.current = false; setNeedsChecking(false); }
  }
  // Questions arrive on the existing connection. There is no pending-question listing operation.
  // Check again verifies that connection using the existing read-only hello operation.
  const approvalBusyChanged = (busy: boolean, id?: string) => {
    if (id) state.scope(id).setApprovalBusy(busy);
    if (!busy) {
      if (id) state.scope(id).setApprovalFrames({});
      void refreshApprovals();
    }
  };
  const agentName = selectedAgent?.name ?? 'Bob';
  const pcWaitingLine = `${agentName} needs your OK to use this PC`;
  const attentionText = [needsCount && (workflowNeedsCount || questionNeeds.length || approvalNeeds.length || pendingPc.some(c => c.request.conversation_id !== conversation?.id)) ? `${needsCount} item${needsCount === 1 ? '' : 's'} need your attention` : '', stuck ? repair.status!.observation.observed : ''].filter(Boolean).join(' · ');
  const toolCalls = activity.filter((row) => row.phase === 'start').length;
  const activityState = question || inlinePc.length ? 'needs_you' : live || pendingHere ? 'working' : activity.length ? 'done' : 'idle';
  // The link is down when the rail says offline or a transport fault is on screen.
  const linkDown = link === 'dead' || (!!error && isTransportFault(error));
  const status = busyLabel
    ? busyLabel
    : link === 'dead'
      ? 'Portal link closed — inspect history before sending'
      : sending
        ? `Sending to ${bobName}…`
        : live
          ? `Streaming from ${bobName}…`
          : link === 'ready'
            ? 'Ready'
            : 'Opening the portal…';

  function renderQuestion(name: string, sessionId = conversation?.id ?? '', onlyQuestion?: string) {
    const { questions, questionDrafts, answering, setQuestionDrafts } = state.scope(sessionId);
    return questions.filter(q => !onlyQuestion || q.question_id === onlyQuestion).map(question => (
            <section key={question.question_id} id={`question-${question.question_id}`} tabIndex={-1} className="ask-card" aria-live="polite">
               {!onlyQuestion && <><h2>{question.origin === 'step_limit' ? 'BoB asks' : `${name} is asking`}</h2>
              <p>{question.question}</p></>}
              <div className="quick-replies">
                {question.choices.map((choice) => (
                  <button
                    key={choice}
                    type="button"
                    className="quick-reply"
                    disabled={answering}
                    onClick={() => void answerQuestion(choice, sessionId, question.question_id)}
                  >
                    {choice}
                  </button>
                ))}
              </div>
              <form
                className="ask-answer"
                onSubmit={(event) => {
                  event.preventDefault();
                  void answerQuestion(questionDrafts[question.question_id] ?? '', sessionId, question.question_id);
                }}
              >
                <input
                  value={questionDrafts[question.question_id] ?? ''}
                  onChange={(event) => { const value = event.target.value; setQuestionDrafts(prior => ({ ...prior, [question.question_id]: value })); }}
                  placeholder="Or type an answer"
                  disabled={answering}
                />
                <button className="primary" disabled={answering || !(questionDrafts[question.question_id] ?? '').trim()}>
                  Answer
                </button>
              </form>
              <p className="subtle">The turn is paused until you answer.</p>
            </section>
    ));
  }
  function renderManagedRun(id: string) {
    const run = managedRuns.find(r => r.id === id);
    if (!run) return null;
    const { turn, question, stopping } = state.scope(run.session.id);
    const active = turn.stream?.request_id === run.request && turn.stream?.conversation_id === run.session.id;
    const pc = active ? pendingPc.filter(c => c.request.conversation_id === run.session.id) : [];
    const stats = managedStats(run);
    const needs = active && (!!question || pc.length > 0 || run.state === 'needs you');
    return <section className="managed-run" aria-label={`${run.name} managed run`}>
      <h4>{run.name} · {run.title}</h4>
      <p role="status">{needs ? 'needs you' : run.state.replace(/_/g, ' ')} · {stats.steps.length} steps ({stats.failed} failed)
        {stats.steps.length > 0 && ` · ${toolLabel(stats.steps[stats.steps.length - 1].tool, true)}`}</p>
      <button type="button" className="link" onClick={() => void openConversation(run.session.id)}>Open {run.name}</button>
      <button type="button" disabled={(!active && run.state !== 'interrupted') || stopping} onClick={() => void stop(run.session.id)}>Stop</button>
      {run.state === 'interrupted' && <p>The run was interrupted. Inspect its session before sending a report. Nothing is sent automatically.</p>}
      {run.content && <PortalMarkdown text={run.content} role="assistant" />}
      <BuilderApprovalCards content={[...run.rows.filter(m => !(run.before ?? []).includes(m.id)).map(m => m.content), run.content].join('\n')} includePending={active && run.agent === 'builder'} client={client} catalog={catalog} conversationId={run.session.id} scope={runScope} refreshKey={link} libraryStore={libraryStore} />
      {pc.map(card => <div className="inline-pc-approval" key={card.request.id}>
        <p role="status" aria-live="polite">{run.name} needs your OK to use this PC</p>
        <PcCard card={card} invoke={invoke} refresh={refreshPc} approvalMinutes={Math.max(0, Math.ceil((card.request.expires_at * 1000 - pcNow) / 60000))} />
      </div>)}
      {active && (approvalCounts[run.session.id] ?? 0) > 0 && <ApprovalsPanel client={client} conversationId={run.session.id} conversationLabel={sessionLabel} onOpenConversation={id => void openNeed(id)} onBusy={approvalBusyChanged} session={approvalSession.current} />}
      {active && renderQuestion(run.name, run.session.id)}
      {autoReports.current.has(run.id) && run.report === 'pending' && <p role="status">report waiting</p>}
      {run.report === 'sent' ? <p>{run.name} report sent to {originName(run.originFace)}</p> : !active &&
        <button type="button" disabled={state.scope(run.origin).sending || !!state.scope(run.origin).turn.stream || run.report === 'sending'} onClick={() => void sendManagedReport(run.id)}>Send report to {originName(run.originFace)}</button>}
    </section>;
  }

  const builderConversation = !!conversation && (face || conversation.face_id) === 'builder';
  const lastAssistant = [...messages].reverse().find(message => message.role === 'assistant');
  const lastUserIndex = messages.reduce((last, message, i) => message.role === 'user' ? i : last, -1);
  const latestAnswerStored = !!lastAssistant && messages.indexOf(lastAssistant) > lastUserIndex;
  const latestTurnContent = messages.slice(lastUserIndex + 1).filter(message => message.role === 'assistant').map(message => message.content).join('\n');
  const completedLatest = !live && !pendingHere && !sending && (terminalHere ? terminalHere.kind === 'complete' : messages[messages.length - 1]?.role === 'assistant');
  const finish = (content: string) => builderConversation && conversation?.project_id && <BuilderFinishStrip client={client} store={libraryStore} projectId={conversation.project_id} conversationId={conversation.id} content={terminalHere && !terminalHasRow ? content : latestTurnContent} scope={runScope} refreshKey={link} />;
  const describeNeed = (id: string) => ({
    agent: agents.find(a => a.sessions.some(c => c.id === id))?.name ?? 'Agent',
    conversation: conversations.find(c => c.id === id)?.title ?? 'The conversation title was not reported',
  });
  const approvalAsk = (given: unknown, details: unknown) => {
    const wording: Record<string,string> = {write_file:'Write a file.',fs_write:'Write a file.',edit_file:'Change a file.',fs_edit:'Change a file.',run_command:'Run a command.',shell_exec:'Run a command.',delete_file:'Delete a file.',network:'Connect to a network service.'};
    // Older servers and some approval frames carry no action name.
    const action = typeof given === 'string' ? given : '';
    const ask = wording[action] ?? (action ? action.replaceAll('_',' ') : 'Review the change waiting for your approval.');
    const detail = details && typeof details === 'object' ? details as Record<string,unknown> : {};
    return ask + (wording[action] && typeof detail.path === 'string' ? ' ' + detail.path : '');
  };
  const openNeedButton = (id: string, card?: string) => <button type="button" onClick={() => void openNeed(id,card)}>Open conversation</button>;
  const needItems: NeedItem[] = [
    ...questionNeeds.map(({id,q}) => ({id:'question:'+q.question_id,kind:q.origin === 'step_limit' ? 'BoB asks' : 'A question',...describeNeed(id),ask:q.question,since:q.created_at ?? observedAt('question:'+q.question_id),
      content:<>{renderQuestion(describeNeed(id).agent,id,q.question_id)}{state.scope(id).error && <p role="alert">{state.scope(id).error?.detail}</p>}{state.scope(id).notice && <p role="status">{state.scope(id).notice}</p>}{openNeedButton(id,'question-'+q.question_id)}</>})),
    ...approvalNeeds.flatMap(({id}) => {
      const record = state.records.get(id), builders = record?.builderApprovals ?? [];
      const generic = approvalItems.filter(a => a.conversation_id === id);
      const ids = [...new Set([...generic.map(a => a.id),...Object.keys(record?.approvalFrames ?? {})])].filter(key => !builders.includes(key));
      return [...ids,...builders,...(!builders.length && record?.builderWaiting ? ['builder-wait'] : [])].map(key => {
        const approval = generic.find(a => a.id === key), builder = builderReadStore(client,id,runScope,link).records.get(key);
        return {id:'approval:'+key,kind:'An approval',...describeNeed(id),ask:approval ? approvalAsk(approval.action_type,approval.details) : builder ? builderActionLabel(builder,false)+'.' : approvalAsk(record?.approvalFrames[key] ?? 'Review the change waiting for your approval.',null),
          since:approval?.created_at ?? observedAt('approval:'+key), observed:!approval?.created_at,
          content:<>{openNeedButton(id,key === 'builder-wait' ? undefined : 'approval-'+key)}{approval && <><details><summary>Read all of it</summary><pre className="details full">{typeof approval.details === 'string' ? approval.details : JSON.stringify(approval.details,null,2)}</pre></details>
            <WorkOnIt item={{kind:'approval',title:approval.action_type,body:'Approval '+approval.id+' ('+approval.created_at+'), from conversation '+id+':\n'+(typeof approval.details === 'string' ? approval.details : JSON.stringify(approval.details,null,2)),conversation_id:id}} onWorkOn={(item,mode) => void workOn(item,mode)} /></>}</>};
      });
    }),
    ...pendingPc.map(({request:r}) => ({id:'pc:'+r.id,kind:'An action on this PC',...describeNeed(r.conversation_id),agent:r.agent,ask:({ 'pc.info':"Share this PC's hardware summary.",'pc.read':'Read a file on this PC.','pc.list':'List a folder on this PC.','pc.write':'Write a file on this PC.','pc.run':'Run a command on this PC.','pc.open':'Open a file or link on this PC.' }[r.operation] ?? 'Review an action on this PC.') + ' ' + String(r.args.command ?? r.args.path ?? r.args.url ?? ''),since:r.created_at ? r.created_at*1000 : observedAt('pc:'+r.id),observed:!r.created_at,content:openNeedButton(r.conversation_id,'pc-'+r.id)})),
    ...workflowProposals.map(([id,p]) => ({id:'proposal:'+id,kind:'A workflow waiting to start',agent:p.source.name,conversation:p.source.conversationId ? describeNeed(p.source.conversationId).conversation : 'From the Library',ask:String(p.proposal.coverage.inputs.task ?? p.proposal.coverage.definition.summary),since:p.proposal.created_at ?? new Date(Date.parse(p.proposal.expires_at)-15*60000).toISOString(),content:<WorkflowProposalCard id={id} source={p.source} taskShown />})),
    ...workflowWaits.map(r => ({id:'workflow:'+r.id,kind:['paused','interrupted'].includes(r.status) ? 'A workflow that is paused' : 'A workflow waiting for your response',...describeNeed(r.origin_conversation_id ?? ''),ask:r.needs_you.filter(w => w.state === 'open').map(w => w.prompt).join(' ') || r.reason?.replaceAll('_',' ') || r.name,since:r.updated_at,content:<WorkflowRunView id={r.id} />})),
  ];
  const needsFailures = [...new Set([approvalReadError,pcReadError,needsReadError,questionsReadError,workflows.error ? 'Workflow proposals could not be checked.' : '',workflows.runsError,
    ...Object.entries(builderReadErrors).filter(([,error]) => error).map(([id]) => 'Approvals in '+describeNeed(id).conversation+' could not be checked.'),
    link !== 'ready' ? 'Questions could not be checked because the connection to Bob is unavailable.' : '',
   ].filter(Boolean))];
  return (
    <ExperienceContext.Provider value={appearance.experience}>
    <WorkflowContext.Provider value={{store:workflows,catalog,onAnswered:(id,questionId) => { if (id) { const scoped=state.scope(id); const remaining=scoped.questions.filter(q => q.question_id !== questionId); scoped.setQuestions(remaining); scoped.setQuestion(remaining[0] ?? null); } },onOpen:id => { if (id) void openNeed(id); },onFresh:(id,text) => { if (id) { bind(id).setInput(text); void openNeed(id); } },pc:id => <PcAccess invoke={invoke} scope={pcScope} queue={pcQueue} refresh={refreshPc} conversationId={id ?? undefined} />}}>
    <ProviderRestartClient.Provider value={client}>
    <WorkspaceFileProvider scope={`${runScope}:${conversation?.id}`} projectId={builderConversation ? conversation?.project_id ?? null : null}
      client={client} online={link === 'ready' && !linkDown}
      completedTurn={completedLatest ? terminalHere?.request_id ? `request:${terminalHere.request_id}` : `history:${messages[lastUserIndex]?.id ?? lastAssistant?.id ?? ''}` : ''}>
    {conversations.filter(c => { const r = state.records.get(c.id); return r && (!!r.turn.stream || !!r.turn.pending || r.builderWaiting || r.builderApprovals.length > 0); }).map(c => <ConversationApprovals key={c.id} client={client} id={c.id} scope={runScope} connection={link} live={!!state.scope(c.id).turn.stream || !!state.scope(c.id).turn.pending} onPending={builderPending} />)}
    <main
      className={`portal${appearance.experience === 'simple' ? ' simple' : ''}${appearance.density === 'compact' ? ' compact' : ''}`}
      style={{ ['--accent' as string]: appearance.accent, zoom: appearance.scale / 100 }}
    >
      {ownerAction.dialog}
      <SecurityNotice />
      <nav aria-label="Bob areas" className="portal-rail">
        <div className="rail-brand" title={hello ? `identity ${hello.identity.sub} · gateway ${hello.identity.gateway}` : undefined}>
          BoB
        </div>
        <div role="tablist" aria-label="Bob surfaces" className="rail-group">
          <button type="button" role="tab" aria-selected={view === 'chat'} onClick={() => setView('chat')}>
            <RailIcon name="agents" />
            <span>Agents</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'workspace'}
            onClick={() => {
              setWorkspaceOpened(true);
              setView('workspace');
            }}
          >
            <RailIcon name="workspace" />
            <span>Workspace</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'tasks'}
            onClick={() => {
              setView('tasks');
              setPanel(null);
              void refreshLedger();
            }}
          >
            <RailIcon name="runs" />
            <span>Tasks</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'projects'}
            onClick={() => {
              setView('projects');
              setPanel(null);
            }}
          >
            <RailIcon name="projects" />
            <span>Projects</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'memory'}
            onClick={() => {
              setView('memory');
              setPanel(null);
            }}
          >
            <RailIcon name="memory" />
            <span>Memory</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === 'library'}
            onClick={() => {
              setView('library');
              setPanel(null);
            }}
          >
            <RailIcon name="library" />
            <span>Library</span>
          </button>
        </div>
        <div className="rail-group" aria-label="Portal panels">
          <button type="button" aria-pressed={view === 'needs'} aria-current={view === 'needs' ? 'page' : undefined}
            aria-label={needsCount ? 'Needs you, ' + needsCount + ' waiting' : 'Needs you'}
            title={needsCount ? 'Needs you · ' + needsCount + ' waiting' : 'Needs you'}
            onClick={() => { setView('needs'); setPanel(null); setNeedsNow(Date.now()); }}>
            <RailIcon name="needs" /><span>Needs you</span>
            {!!needsCount && <b className="rail-badge" data-count={needsCount} aria-hidden="true" />}
          </button>
        </div>
        <div className="rail-group rail-foot">
          <button
            type="button"
            role="tab"
            aria-selected={view === 'settings'}
            onClick={() => {
              setView('settings');
              setPanel(null);
            }}
          >
            <RailIcon name="settings" />
            <span>Settings</span>
          </button>
          <button className={`rail-link ${wedged ? 'stuck' : link}`} title={wedged ? repair.status?.observation.observed : status}
            onClick={() => { setView('chat'); setPanel(null); if (collapsed.right) togglePanel('right'); setTimeout(() => document.getElementById('repair-card')?.focus(), 0); }}>
            {wedged ? 'stuck' : link === 'ready' ? 'online' : link === 'dead' ? 'offline' : '…'}
          </button>
        </div>
      </nav>

      <div className="portal-stage">
        <OwnerSessionIndicator client={client} catalog={catalog} eligible={ownerEligible(hello?.identity, catalog)} />
        {windowNotices.map((text, i) => <p key={i} role="alert" className="notice window-notice">{text}</p>)}
        {catalogError && <p role="alert" className="notice window-notice window-fault-notice">{catalogError.detail} <button onClick={() => void reloadCatalog().catch(() => {})}>Try loading models again</button></p>}
        {windowError && <p role="alert" className="notice window-notice window-fault-notice"><ReadFaultText client={client} error={windowError} /></p>}
      <RuntimeUpgrade invoke={invoke} scope={JSON.stringify(hello?.identity)} onReconnect={() => void reconnectAndInspect()} reconnecting={!!busyLabel} connected={connectedCount} />
      <EmbedderWarning invoke={invoke} onSetup={connectModel} scope={JSON.stringify(hello?.identity)} />
      {fault && (
        <div role="alert" className="portal-fault banner">
          <p>
            {restarting && !fault.sent ? restarting : <><code>{fault.code}</code> {fault.detail}</>}
          </p>
          {(!restarting || fault.sent) && <p className="hint">{fault.hint}</p>}
          {fault.request_id && (
            <p>
              The send that was in flight had request <code>{fault.request_id}</code>; its outcome is unknown.
            </p>
          )}
          {fault.text && (
            <p>
              Unconfirmed text: <q>{fault.text.length > 240 ? `${fault.text.slice(0, 240)}…` : fault.text}</q>
            </p>
          )}
          {fault.sent && (
            <p>
              No send will be replayed and Send stays locked until the server history is read. Refresh reloads the list and cannot clear
              this; only the button below can.
            </p>
          )}
          <RecoveryActions invoke={invoke} onSettings={() => setView('settings')} />
          <button type="button" onClick={() => void reconnectAndInspect()} disabled={!!busyLabel}>
            Reconnect and inspect history
          </button>
        </div>
      )}
      {/* A dead link is not always a `fault` (the native link can report its own death with no call
          in flight), and it must never leave the window without a way back. Reconnect stays an
          explicit press: nothing is retried or replayed. */}
      {linkDown && !fault && !modelRestarting(invoke) && (
        <div role="group" aria-label="Portal link down" className="portal-fault banner" data-testid="link-down">
          {!windowError && <p>{restarting ?? <>The link to {bobName} is down. Nothing is retried or sent again automatically.</>}</p>}
          <button type="button" className="primary" data-testid="link-reconnect" onClick={() => void reconnectAndInspect()} disabled={!!busyLabel}>
            Reconnect
          </button>
        </div>
      )}

      <div
        className={`portal-body${view === 'chat' ? ' panels-resizable' : ''}${view === 'chat' && collapsed.left ? ' left-collapsed' : ''}${view === 'chat' && collapsed.right && !panel ? ' right-collapsed' : ''}${view === 'workspace' ? ' mode-workspace' : ''}${view === 'tasks' || view === 'projects' || view === 'memory' || view === 'library' ? ` mode-split mode-${view}` : ''}${view === 'settings' || view === 'needs' ? ' mode-full' : ''}${panel ? ' with-panel' : ''}`}
      >
        {view === 'needs' && <NeedsYouPage items={needItems} failures={needsFailures} checkedAt={needsCheckedAt ?? (approvalCheckedAt && pcCheckedAt ? Math.min(approvalCheckedAt,pcCheckedAt) : null)}
          checking={needsChecking || link === 'connecting' || (!approvalCheckedAt && !approvalReadError) || (!pcCheckedAt && !pcReadError) || (workflowEnabled && !workflows.discoveredAt && !workflows.runsError && !workflows.error)} onRefresh={() => void checkNeeds()} store={workflows} />}
        {view === 'settings' && (
          <div className="tasks-slot">
            <SettingsView
              client={client}
              invoke={invoke}
              hello={hello}
              catalog={catalog}
              onConfigured={reloadCatalog}
              modelSetupRequest={modelSetupRequest}
              profiles={profiles}
              onProfiles={onProfiles}
              onReconnect={() => void reconnectAndInspect()}
              onConnectionSaved={(payload) => { onConnection?.(payload); resetOwnerReads(); setHello(null); void connect(); }}
              appearance={appearance}
              onAppearance={setAppearance}
              developerNotes={<details className="set-card"><summary>Developer notes: known gaps</summary><GapsPanel serverGaps={hello?.gaps ?? []} onWorkOn={(item,mode) => void workOn(item,mode)} /></details>}
              pendingApprovals={Object.values(approvalCounts).reduce((sum, n) => sum + n, 0)}
            />
          </div>
        )}
        {view === 'library' && (
          <div className="tasks-slot">
            <LibraryView
              client={client}
              store={libraryStore}
              boardStore={boardStore}
              onPlan={planApp}
              onAsk={askAboutApp}
              onSendSketch={sendSketch}
              onOpenFolder={(path) => {
                setWorkspaceOpened(true);
                setView('workspace');
                setFolderRequest((prior) => ({ path, n: (prior?.n ?? 0) + 1 }));
              }}
              onOpenProjects={() => setView('projects')}
            />
          </div>
        )}
        {view === 'memory' && (
          <div className="tasks-slot">
            {hello && <MemoryExplorer key={linkGeneration} client={client} invoke={invoke} cache={memoryCache.current} operations={hello.operations ?? {}} onAsk={(items, mode) => void askAboutMemory(items, mode)} />}
          </div>
        )}
        {view === 'projects' && (
          <div className="tasks-slot">
            <ProjectsBoard
              client={client}
              catalog={catalog} identity={hello?.identity} scope={runScope}
              store={boardStore}
              conversations={conversations}
              openConversationId={conversation?.id ?? null}
              onPickUp={pickUpProject}
              onAskPm={askPmAboutBoard}
              onOpenSession={(id) => void openConversation(id)}
              onAssigned={() => {
                void refreshList().catch(() => undefined);
                if (conversationRef.current) void openConversation(conversationRef.current.id);
              }}
            />
          </div>
        )}
        {view === 'tasks' && (
          <div className="tasks-slot">
            {calendarError && <p role="alert"><ReadFaultText client={client} error={calendarError} fallback={`${calendarError.code} ${calendarError.detail} ${calendarError.hint}`} /></p>}
            <TasksCalendar
              client={client}
              catalog={catalog}
              store={calendarStore}
              ledger={ledger}
              onRunNow={runEntry}
              onTeamSetup={() => void teamSetup()}
              onShowRuns={() => setPanel(panel === 'runs' ? null : 'runs')}
            />
          </div>
        )}
        {workspaceOpened && (
          <div className="ide-slot" hidden={view !== 'workspace'}>
            <IdeBoundary fallback={<WorkspacePanel key={JSON.stringify([runScope, hello?.identity.token_type, hello?.identity.actor, hello?.identity.owner])}
                client={workspaceClient}
                portalClient={client} catalog={catalog} identity={hello?.identity} scope={runScope}
                generation={linkGeneration}
                onOpenDiff={(title, diff) => ide.current?.openDiff(title, diff)}
              />}>
              <Suspense fallback={<p className="subtle ide-pad">Loading the editor…</p>}>
                <IdeEditor
                  ref={ide}
                  client={ideClient}
                  generation={linkGeneration}
                  operations={hello?.operations ?? {}}
                  onContext={setIdePath}
                  openRequest={folderRequest}
                  agentsPing={agentsPing}
                  agents={<AgentTiles runs={agentRuns} live={new Map([...state.records.values()].flatMap(r => r.turn.stream ? [[r.turn.stream.request_id, r.turn.stream.content] as const] : []))} />}
                  jobs={<WorkspacePanel key={JSON.stringify([runScope, hello?.identity.token_type, hello?.identity.actor, hello?.identity.owner])}
                client={workspaceClient}
                portalClient={client} catalog={catalog} identity={hello?.identity} scope={runScope}
                generation={linkGeneration}
                onOpenDiff={(title, diff) => ide.current?.openDiff(title, diff)}
              />} />
              </Suspense>
            </IdeBoundary>
          </div>
        )}
        {view !== 'needs' && <>
        <aside id="thread-panel" aria-label="Agents" className="portal-sidebar" hidden={view === 'chat' && collapsed.left}>
          <button type="button" className="panel-edge" aria-label="Collapse conversation list" aria-controls="thread-panel" aria-expanded="true" aria-keyshortcuts="Control+B" onClick={() => togglePanel('left')}>‹ Hide conversations</button>
          <div className="sidebar-head">
            <h1>Agents</h1>
            <button
              type="button"
              className="icon-button"
              onClick={() => void connect()}
              disabled={!!busyLabel || unknownOutcome}
              title={
                unknownOutcome
                  ? 'A send outcome is unknown. Refresh cannot clear it: use "Reconnect and inspect history".'
                  : 'Re-open the portal link and reload the agents'
              }
            >
              Refresh
            </button>
          </div>
          <label htmlFor="conversation-search" className="sr-only">
            Search agents and sessions
          </label>
          <input id="conversation-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search agents and sessions…" />
          <ul className="conversation-list">
            {filtered.map((agent) => {
              const latest = agent.sessions[0];
              const preview = conversation && agent.sessions.some(row => row.id === conversation.id) ? conversation : latest;
              const expanded = !!needle || openedAgents[agent.key] === true;
              const visibleSessions = needle && !`${agent.name} ${agent.face_id ?? ''}`.toLowerCase().includes(needle)
                ? agent.sessions.filter(matchesSession) : agent.sessions;
              const listId = `agent-conversations-${encodeURIComponent(agent.key || 'default')}`;
              return (
                <li key={agent.key || 'default'}>
                  <div className="agent-contact">
                  <button
                    type="button"
                    aria-current={selectedAgent?.key === agent.key ? 'page' : undefined}
                    className={`agent-row ${agent.status}${selectedAgent?.key === agent.key ? ' selected' : ''}`}
                    onClick={() => openAgent(agent)}
                    disabled={navLocked}
                  >
                    <Avatar agent={agent} />
                    <span className="agent-text">
                      <span className="agent-name">
                        <strong>{agent.name}</strong>
                        <time>{ago(agent.last_at)}</time>
                      </span>
                      <span className="agent-sub">
                        {agent.status !== 'idle' ? (
                          <em className={`chip ${agent.status}`}>{STATUS_LABEL[agent.status]}</em>
                        ) : null}
                        {agent.status !== 'working' && agent.sessions.some(c => state.records.get(c.id)?.turn.stream || state.records.get(c.id)?.turn.pending) && <em className="chip working">working</em>}
                        <span className="agent-preview">{preview ? sessionTitle(preview) : agent.allowed ? 'No conversations yet. Start one.' : 'This agent is not permitted for this account.'}</span>
                      </span>
                    </span>
                  </button>
                  {agent.sessions.length > 1 && <button type="button" className="agent-expander" aria-label={`${expanded ? 'Hide' : 'Show all'} ${agent.sessions.length} conversations with ${agent.name}`} aria-expanded={expanded} aria-controls={listId} disabled={!!needle} onClick={() => toggleAgent(agent.key)}><svg aria-hidden="true" viewBox="0 0 16 16"><path d={expanded ? 'M3 6l5 5 5-5' : 'M6 3l5 5-5 5'} /></svg><small>{agent.sessions.length}</small></button>}
                  </div>
                  {expanded && <ul id={listId} className="agent-sessions">{visibleSessions.map(row => <li key={row.id}><button type="button" aria-current={conversation?.id === row.id ? 'true' : undefined} disabled={navLocked} onClick={() => void openConversation(row.id)}><span>{sessionTitle(row)}</span><time>{ago(String(row.updated_at ?? row.created_at ?? ''))}</time>{sessionState(row.id) && <em>{sessionState(row.id)}</em>}</button></li>)}</ul>}
                </li>
              );
            })}
          </ul>
          {!filtered.length && <p className="subtle">{agents.length ? 'No agent or session matches the search.' : 'No agents on the server yet.'}</p>}
          <small className="identity">
            {hello
              ? `identity ${hello.identity.sub} · gateway ${hello.identity.gateway}`
              : link === 'dead'
                ? mode === 'this_pc' ? 'Bob on this PC unreachable' : 'server unreachable'
                : 'connecting…'}
          </small>
          <details className="aside-note">
            <summary>About this list</summary>
            <p>
              These lists come from the gateway, scoped to your account ({hello?.identity.sub ?? 'unknown'}). The rows carry no owner
              field, so this window cannot show or vouch for per-row ownership here: an account namespace is not proof of household
              ownership, and no row is presented as the owner&apos;s on the strength of it.
            </p>
          </details>
        </aside>

        <section aria-label="Conversation" className="portal-main">
          <div>
          <header className="conversation-header">
            {view === 'chat' && collapsed.left && <button type="button" className="panel-edge" aria-label="Expand conversation list" aria-controls="thread-panel" aria-expanded="false" aria-keyshortcuts="Control+B" onClick={() => togglePanel('left')}>› Conversations</button>}
            {view === 'chat' && collapsed.right && !panel && <button type="button" className={`panel-edge${attention ? ' attention' : ''}`} aria-label={attention ? `Expand Activity, ${attention} item${attention === 1 ? ' needs' : 's need'} your attention` : 'Expand Activity'} aria-controls="activity-panel" aria-expanded="false" aria-keyshortcuts="Control+Alt+B" onClick={() => togglePanel('right')}>Activity {attention ? `(${attention}) ` : ''}‹</button>}
            <div className="thread-title">
              {selectedAgent && <Avatar agent={selectedAgent} size={40} />}
              <div>
                <h1>
                  {selectedAgent ? selectedAgent.name : 'Pick an agent'}
                  {activityState === 'working' && <em className="chip working">working</em>}
                  {activityState === 'needs_you' && <em className="chip needs_you">{inlinePc.length ? pcWaitingLine : 'needs you'}</em>}
                </h1>
                <p className="subtle">
                  {conversation ? (
                    <>
                      {sessionTitle(conversation)} · {modelEntryLabel}
                      {conversation.project_id ? ` · project ${conversation.project_id}` : ''}
                      <FlagBadge run={{ flags: runs.filter(r => r.conversation_id === conversation.id).flatMap(r => r.flags ?? []), owner_flag: runs.some(r => r.conversation_id === conversation.id && r.owner_flag) }} />
                    </>
                  ) : selectedAgent ? (
                    'No session open. Start one to talk to this agent.'
                  ) : (
                    'Your agents are on the left. Each one keeps all its sessions in one thread.'
                  )}
                </p>
              </div>
            </div>
            <div className="row-actions">
              {view === 'projects' && <span className="pm-label">PM</span>}
              {view !== 'chat' && (
                <select
                  aria-label="Agent for this workspace"
                  className="agent-picker"
                  value={selectedAgent?.key ?? ''}
                  disabled={navLocked}
                  onChange={(event) => {
                    const chosen = agents.find((agent) => agent.key === event.target.value);
                    if (chosen) openAgent(chosen);
                  }}
                >
                  {!selectedAgent && <option value="">Pick an agent…</option>}
                  {agents.map((agent) => (
                    <option key={agent.key || 'default'} value={agent.key}>
                      {agent.name}
                    </option>
                  ))}
                </select>
              )}
              {selectedAgent && (
                <button
                  type="button"
                  onClick={() => void newSession(selectedAgent)}
                  disabled={!!busyLabel || link === 'dead' || navLocked || !selectedAgent.allowed}
                  title="Starts a fresh session inside this agent's thread"
                >
                  New session
                </button>
              )}
              {conversation &&
                (renaming ? (
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      void rename();
                    }}
                  >
                    <label htmlFor="rename-title" className="sr-only">
                      Session title
                    </label>
                    <input id="rename-title" value={titleDraft} maxLength={160} onChange={(e) => setTitleDraft(e.target.value)} />
                    <button disabled={!titleDraft.trim() || !!busyLabel}>Save title</button>
                    <button type="button" onClick={() => setRenaming(false)}>
                      Cancel
                    </button>
                  </form>
                ) : (
                  <button type="button" onClick={() => setRenaming(true)} disabled={!!busyLabel}>
                    Rename
                  </button>
                ))}
              {/* Shown while a turn runs, and after a lost link until the server says nothing is generating. */}
              {conversation && (live || terminalHere?.kind === 'transport') && (
                <button
                  type="button"
                  className="danger"
                  onClick={() => void stop()}
                  disabled={!(live || orphanedHere) || stopping}
                  title={
                    live
                      ? 'Asks the server to end this turn; the local stream ends when the server confirms.'
                      : 'The local stream is gone, but the turn may still be generating on the server. This asks the server to stop it.'
                  }
                >
                  {live ? 'Stop turn' : 'Stop server turn'}
                </button>
              )}
            </div>
          </header>

          {workspaceReceipt && workspaceReceipt.conversation_id === conversation?.id && <p className="workspace-divider" role="status">{workspaceReceipt?.receipt.text}</p>}
          </div>
          <div
            ref={scroller}
            className="transcript"
            onScroll={(event) => {
              const element = event.currentTarget;
              following.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
            }}
          >
            {olderSession && !hasMore && (
              <button type="button" className="session-jump" onClick={() => void openConversation(olderSession.id)} disabled={navLocked}>
                ↑ Earlier session · {sessionTitle(olderSession)} · {ago(String(olderSession.updated_at ?? olderSession.created_at ?? ''))}
              </button>
            )}
            {hasMore && conversation && (
              <button type="button" className="load-more" onClick={() => void loadMore()} disabled={!!busyLabel}>
                Load older messages
              </button>
            )}
            {conversation && (
              <div className="session-divider" role="separator">
                <span>
                  Session · {sessionTitle(conversation)}
                  {conversation.created_at ? ` · started ${String(conversation.created_at).slice(0, 16).replace('T', ' ')}` : ''}
                </span>
              </div>
            )}
            {!conversation && selectedAgent && (
              <div className="thread-empty">
                <Avatar agent={selectedAgent} size={64} />
                <p>{selectedAgent.allowed ? `Start a session with ${selectedAgent.name}.` : `${selectedAgent.name} is not permitted for this portal token.`}</p>
                {selectedAgent.allowed && (
                  <button type="button" className="primary" onClick={() => void newSession(selectedAgent)} disabled={!!busyLabel || link === 'dead'}>
                    Start session
                  </button>
                )}
              </div>
            )}
            {conversation && <WorkflowConversation conversationId={conversation.id} />}
            {conversation && !messages.length && !pendingHere && !live && !terminalHere && <p className="empty">Nothing in this session yet. Say hello. To choose which model answers, choose Provider, then Model under the message box.</p>}
            {messages.map((message) => {
              const reportMetadata = message.metadata && typeof message.metadata === 'object' ? message.metadata as Record<string, unknown> : null;
              const observedOutcome = conversation ? state.scope(conversation.id).replyOutcomes[message.id] : undefined;
              if (message.role === 'assistant' && reportMetadata?.kind === 'workflow_report' && reportMetadata.author === 'workflow_controller' && typeof reportMetadata.run_id === 'string') {
                return workflows.summaries.has(reportMetadata.run_id) ? null : <article key={message.id} className="msg assistant"><WorkflowRunView id={reportMetadata.run_id} /></article>;
              }
              const readbackPending =
                typeof message.metadata === 'object' &&
                message.metadata !== null &&
                'history_readback' in (message.metadata as Record<string, unknown>);
              const provenance = conversation
                ? `${messageProvenance(conversation.id, message.id)}${readbackPending ? ' · not yet readable from history' : ''}`
                : `bob:message:${message.id}`;
              return (
                <article key={message.id} className={`msg ${message.role}`}>
                  <h2>
                    {message.role === 'user' ? (reportAgent(message.content) ? 'Report' : 'You') : message.role === 'assistant' ? agentName : txt(message.role)}
                    {message.created_at && <time dateTime={String(message.created_at)}> · {localMessageTime(String(message.created_at))}</time>}
                  </h2>
                  {message.role === 'assistant' ? (
                    <AssistantBody
                      content={txt(message.content, 'no content returned')}
                      actions={conversation ? { conversationId: conversation.id, messageId: message.id, proposerName: `the ${agentName}`, streaming: readbackPending, faces: catalog?.faces ?? [], store: calendarStore,
                        onTasks: () => { setView('tasks'); setPanel(null); }, onHandoff: startHandoff, managedRun: renderManagedRun } : undefined}
                      onChoose={(choice) => chooseOption(choice)}
                      onTask={() => makeTask(message)}
                    />
                  ) : message.role === 'user' ? (
                    reportAgent(message.content) ? <details className="managed-report"><summary>{reportAgent(message.content)} report sent to {agentName}</summary><pre>{message.content}</pre></details> : <UserAnswer content={txt(message.content, 'no content returned')} />
                  ) : (
                    <p className="content">{txt(message.content, 'no content returned')}</p>
                  )}
                  {message.role === 'assistant' && <TeamProposalCard
                    content={message.content} client={client} catalog={catalog} scope={runScope}
                    onRun={seed => { setRunsSeed({ ...seed, ...(conversation?.id ? { conversation_id: conversation.id } : {}), ...(conversation?.project_id ? { project_id: conversation.project_id } : {}) }); setPanel('runs'); }} />}
                  {message.role === 'assistant' && !readbackPending && <WorkflowDraftReview content={message.content} />}
                  {message.role === 'assistant' && <WorkActionCard content={message.content} client={client} catalog={catalog} scope={runScope} onContinue={chooseOption} />}
                  {message.role === 'assistant' && conversation && <BuilderApprovalCards content={message.content} client={client} catalog={catalog} conversationId={conversation.id} scope={runScope} refreshKey={link} libraryStore={libraryStore} />}
                  {completedLatest && latestAnswerStored && message.id === lastAssistant?.id && (!terminalHere?.partial || terminalHasRow) && finish(message.content)}
                  {message.role === 'assistant' && <ReplyStatus
                    stoppedReason={reportMetadata?.stopped_reason ?? observedOutcome?.stopped_reason ?? (terminalHere?.message_id === message.id ? terminalHere.stopped_reason : undefined)}
                    extensions={reportMetadata?.step_extensions ?? observedOutcome?.step_extensions ?? (terminalHere?.message_id === message.id ? terminalHere.step_extensions : undefined)}
                    latest={message.id === messages[messages.length - 1]?.id && !pendingHere && !live} disabled={locked || !conversation} onContinue={() => void submit('continue')} />}
                  {message.role === 'assistant' && typeof reportMetadata?.backend === 'string' && typeof reportMetadata?.model === 'string' && reportMetadata.backend && reportMetadata.model && <p className="model-receipt">Answered by {modelName({backend: reportMetadata.backend, model: reportMetadata.model}, catalog)}</p>}
                  <details className="prov">
                    <summary>source</summary>
                    <small className="provenance">{provenance}</small>
                  </details>
                </article>
              );
            })}
            {pendingHere && (
              <article className="msg user pending">
                <h2>{reportAgent(pendingHere.text) ? 'Report' : 'You · sending'}</h2>
                {reportAgent(pendingHere.text) ? <details className="managed-report"><summary>{reportAgent(pendingHere.text)} report sent to {agentName}</summary><pre>{pendingHere.text}</pre></details> : <UserAnswer content={pendingHere.text} />}
                <details><summary>Details</summary><small className="provenance">
                  {pendingHere.request_id ? `local:pending:${pendingHere.request_id}` : 'local:pending:unsent'} — submitted, not yet in
                  server history
                </small></details>
              </article>
            )}
            {(inlinePc.length > 0 || (live && toolCalls > 0)) && (
              <p className="working-line">
                <span className="pulse" aria-hidden="true" /> {inlinePc.length ? pcWaitingLine : `${agentName} is working · ${toolCalls} tool call${toolCalls === 1 ? '' : 's'} so far — see Activity`}
              </p>
            )}
            {live && !liveHasRow && (
              <article className="msg assistant streaming">
                <h2>{inlinePc.length ? pcWaitingLine : `${agentName} · typing`}</h2>
                <AssistantBody content={live.content || (inlinePc.length ? 'Please review the PC request below.' : appearance.experience === 'simple' ? 'Thinking…' : 'Waiting for the first server chunk…')} onChoose={() => undefined} onTask={() => undefined}
                  actions={{ conversationId: live.conversation_id, messageId: live.request_id, proposerName: `the ${agentName}`, streaming: true, faces: catalog?.faces ?? [], store: calendarStore,
                    onTasks: () => { setView('tasks'); setPanel(null); }, onHandoff: startHandoff, managedRun: renderManagedRun }} />
                <BuilderApprovalCards includePending content={live.content} client={client} catalog={catalog} conversationId={live.conversation_id} scope={runScope} refreshKey={link} libraryStore={libraryStore} />
                <details><summary>Details</summary><small className="provenance">
                  bob:conversation:{live.conversation_id}:stream:{live.request_id} — live chunks, not yet a stored message
                </small></details>
              </article>
            )}
            {inlinePc.map(card => <div className="inline-pc-approval" id={'pc-'+card.request.id} tabIndex={-1} key={card.request.id}>
              <p role="status" aria-live="polite">{pcWaitingLine}</p>
              <PcCard card={card} invoke={invoke} refresh={refreshPc} approvalMinutes={Math.max(0, Math.ceil((card.request.expires_at * 1000 - pcNow) / 60000))} />
            </div>)}
            {terminalHere && ((!terminalHasRow && terminalHere.partial.length > 0) || terminalHere.kind !== 'complete') && (
              <article className={`msg assistant terminal ${terminalHere.kind}`}>
                {!terminalHasRow && terminalHere.partial.length > 0 && <h2>{agentName}</h2>}
                {!terminalHasRow && terminalHere.partial.length > 0 && <AssistantBody content={terminalHere.partial} onChoose={() => undefined} onTask={() => undefined} />}
                {!terminalHasRow && conversation && <BuilderApprovalCards content={terminalHere.partial} client={client} catalog={catalog} conversationId={conversation.id} scope={runScope} refreshKey={link} libraryStore={libraryStore} />}
                {completedLatest && finish(terminalHere.partial)}
                {!terminalHasRow && <ReplyStatus latest={!pendingHere && !live} stoppedReason={terminalHere.stopped_reason} extensions={terminalHere.step_extensions} disabled={locked || !conversation} onContinue={() => void submit('continue')} />}
                <p className="typed-refusal">{plainTurnEnd(terminalHere) ?? terminalHere.line}</p>
                <details><summary>Details</summary>
                  <p>{terminalHere.line}</p>
                  <small className="provenance">{terminalProvenance(terminalHere)}{terminalHere.code ? ` · ${terminalHere.code}` : ''}{terminalHere.detail ? ` · ${terminalHere.detail}` : ''}</small>
                </details>
              </article>
            )}
            {newerSession && (
              <button type="button" className="session-jump" onClick={() => void openConversation(newerSession.id)} disabled={navLocked}>
                ↓ Newer session · {sessionTitle(newerSession)}
              </button>
            )}
          </div>

          {conversation && !live && <BuilderApprovalCards content="" includePending client={client} catalog={catalog} conversationId={conversation.id} scope={runScope} refreshKey={link} libraryStore={libraryStore} />}
          {conversation && ((approvalCounts[conversation.id] ?? 0) > 0 || Object.keys(state.scope(conversation.id).approvalFrames).length > 0) && <div className="conversation-approvals">
            <ApprovalsPanel key={conversation.id} client={client} conversationId={conversation.id} conversationLabel={sessionLabel} onOpenConversation={id => void openNeed(id)} onBusy={approvalBusyChanged} session={approvalSession.current} />
          </div>}
          {renderQuestion(agentName)}

          <div className="composer-area">
          {view === 'chat' && collapsed.right && !panel && attentionText && <div className="activity-attention" role="status">
            <span>{attentionText}</span>
            <button type="button" aria-controls="activity-panel" onClick={() => togglePanel('right')}>Show</button>
          </div>}
          <div className="composer">
            {notice && (![PENDING_READBACK_NOTICE, MISSING_READBACK_NOTICE].includes(notice) || pendingReadbackRows(messages)) && <p className="notice">{notice}</p>}
            {error && !(unknownOutcome && fault && fault.detail === error.detail) && (
              <p role="alert" className="fault-inline">
                {isRateLimited(error) ? error.detail : restarting && !unknownOutcome ? restarting : <><code>{error.code}</code> {error.detail}</>}
                {error.hint && (!restarting || unknownOutcome) && <span className="hint"> {error.hint}</span>}
              </p>
            )}
            <div className="composer-content">
              <label htmlFor="composer" className="sr-only">
                Message
              </label>
              {view === 'workspace' && (
                <label className={`attach-chip${attachFile && idePath ? ' on' : ''}`}>
                  <input type="checkbox" checked={attachFile} onChange={(event) => setAttachFile(event.target.checked)} />
                  {idePath ? `Include ${idePath.split('/').pop()} (or the selection)` : 'No file open to include'}
                </label>
              )}
              <div className="composer-box">
                <textarea
                  id="composer"
                  rows={2}
                  value={input}
                  disabled={!conversation}
                  placeholder={conversation ? `Message ${agentName}…` : 'Open or start a session to type'}
                  onChange={(e) => setInput(e.target.value)}
                  onCompositionStart={() => {
                    composing.current = true;
                  }}
                  onCompositionEnd={() => {
                    composing.current = false;
                  }}
                  onKeyDown={(event) => {
                    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing || composing.current || event.keyCode === 229)
                      return;
                    event.preventDefault();
                    if (!event.repeat) void submit();
                  }}
                />
                <div className="composer-send">
                  {conversation && (face || conversation.face_id) === 'builder' && <BuilderModeBar key={conversation.id} client={client} catalog={catalog} conversationId={conversation.id} scope={runScope} refreshKey={link} onWorkspace={workspaceBinding} libraryStore={libraryStore} />}
                  <button type="button" className="primary" disabled={!conversation || !input.trim() || locked} onClick={() => void submit()}>
                    Send to Bob
                  </button>
                </div>
              </div>
              <div className="composer-actions">
                <ModelPicker id="portal-chat-model" compact catalog={catalog} pin={pin} defaultName={defaultModelName} defaultModel={defaultModel} disabled={locked || !conversation} onChange={setPin} onRefresh={() => void reloadCatalog().catch(() => {})} />
                {!conversation && <small>Open or start a session to choose its model.</small>}
                {catalog && !catalog.models.some(row => row.available) && <button type="button" onClick={connectModel}>Add a provider</button>}
                <p role="status" aria-live="polite" className="status">
                  {status}
                </p>
                <small>
                  Enter sends · Shift+Enter new line
                  {unknownOutcome ? ' · locked after an unknown send outcome until you reconnect and inspect history' : ''}
                </small>
              </div>
            </div>
          </div>
          </div>
        </section>

        <aside id="activity-panel" ref={activityScroll.scroller} onScroll={activityScroll.onScroll} aria-label={panel ? 'Portal panel' : 'Activity'} className="portal-panel" hidden={view === 'chat' && collapsed.right && !panel}>
          {view === 'chat' && !panel && <button type="button" className="panel-edge" aria-label="Collapse Activity" aria-controls="activity-panel" aria-expanded="true" aria-keyshortcuts="Control+Alt+B" onClick={() => togglePanel('right')}>Hide Activity {attention ? `(${attention}) ` : ''}›</button>}
          {panel && (
            <div className="panel-close">
              <button type="button" className="link" onClick={() => setPanel(null)}>
                {view === 'tasks' ? '← Back to the team assistant' : view === 'projects' ? '← Back to the PM' : view === 'memory' || view === 'library' ? '← Back to the agent' : '← Back to activity'}
              </button>
            </div>
          )}
          {panel === 'runs' && workflowEnabled && <WorkflowRuns />}
          {panel === 'runs' && <RunsPanel key={runScope} client={client} catalog={catalog} scope={runScope} runs={runs} onRuns={setRuns} seed={runsSeed} identity={hello?.identity} />}
          {!panel && (
            <div className="portal-panel-body activity">
              <h2>Activity</h2>
              <SelfRepair invoke={invoke} control={repair} onRepaired={() => void reconnectAndInspect()} />
              <PcAccess invoke={invoke} scope={pcScope} queue={pcQueue} refresh={refreshPc} conversationId={conversation?.id} />
              <div className={`activity-state ${activityState}`}>
                <span className="pulse" aria-hidden="true" />
                <strong>
                  {inlinePc.length ? pcWaitingLine : activityState === 'working'
                    ? `${agentName} is working`
                    : activityState === 'needs_you'
                      ? `${agentName} needs you`
                      : activityState === 'done'
                        ? 'Done'
                        : 'Idle'}
                </strong>
                <small>{toolCalls ? `${toolCalls} tool call${toolCalls === 1 ? '' : 's'}` : 'no tool calls this turn'}</small>
              </div>
              {activity.length > 0 || Object.values(pcQueue.outcomes).some(r => r.conversation_id === conversation?.id) ? (
                <ActivityTrail activity={activity} queue={pcQueue} conversationId={conversation?.id} live={!!live} />
              ) : (
                <p className="subtle">When {agentName} uses tools, each step shows here as it happens.</p>
              )}

              {selectedAgent && selectedAgent.sessions.length > 0 && (
                <section className="session-list">
                  <h2>Sessions in this thread · {selectedAgent.sessions.length}</h2>
                  <ul>
                    {selectedAgent.sessions.slice(0, 12).map((row) => (
                      <li key={row.id}>
                        <button
                          type="button"
                          className={conversation?.id === row.id ? 'selected' : undefined}
                          onClick={() => void openConversation(row.id)}
                          disabled={navLocked}
                        >
                          <span>{sessionTitle(row)}</span>
                          <em>{sessionState(row.id)}</em><time>{ago(String(row.updated_at ?? row.created_at ?? ''))}</time>
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              <PcModeRow queue={pcQueue} conversationId={conversation?.id} invoke={invoke} refresh={refreshPc} />
              <details id="model-routing" className="pin-bar" open={routingOpen} onToggle={(e) => setRoutingOpen(e.currentTarget.open)}>
                <summary>
                  Routing · face {pins?.face_id ?? conversation?.face_id ?? 'server default'} · model {modelEntryLabel}
                  {research ? ' · research on' : ''}
                </summary>
                {!conversation && (
                  <p id="model-routing-no-session" className="pin-note" role="note">
                    No session is open. Face, model and research choices apply to the open session&apos;s next turn, so open or start a
                    session to choose them. Providers can be set up without one.
                  </p>
                )}
                <label htmlFor="portal-face">Face (next turn)</label>
                <select id="portal-face" value={face} disabled={!conversation} aria-describedby={conversation ? undefined : 'model-routing-no-session'} onChange={(e) => setFace(e.target.value)}>
                  <option value="">Server pin / project default</option>
                  {!!face && !(catalog?.faces ?? []).some((row) => row.id === face) && (
                    <option value={face} disabled>
                      {face} · server pin, not in this catalog
                    </option>
                  )}
                  {(catalog?.faces ?? []).map((row) => (
                    <option key={row.id} value={row.id} disabled={row.allowed === false}>
                      {row.allowed === false ? `${row.name} · ${row.id} · not permitted by this portal token` : `${row.name} · ${row.id}`}
                    </option>
                  ))}
                </select>
                {(catalog?.faces ?? []).some((row) => row.allowed === false) && (
                  <p className="pin-note">
                    {(catalog?.faces ?? []).filter((row) => row.allowed === false).length} registered face
                    {(catalog?.faces ?? []).filter((row) => row.allowed === false).length === 1 ? '' : 's'} outside this portal token&apos;s
                    allow-list — listed but not selectable; the server would refuse the send.
                  </p>
                )}
                {catalog && !catalog.models.length && !catalog.backends.length && <p>No models set up yet <button onClick={connectModel}>Add a provider</button></p>}
                <ModelPicker id="portal-model" catalog={catalog} pin={pin} defaultName={defaultModelName} defaultModel={defaultModel} disabled={locked || !conversation} onChange={setPin} onRefresh={() => void reloadCatalog().catch(() => {})} />
                {locked && <p id="portal-model-note" className="subtle">Model choice is locked while a message, turn or approval is in progress; it applies to the next turn.</p>}
                <label className="check">
                  <input type="checkbox" checked={research} disabled={!conversation} onChange={(e) => setResearch(e.target.checked)} /> research turn (opt-in, may fan
                  out)
                </label>
                <p className="subtle">
                  Selected pin: {pinLine(pin)}
                  {sentPin ? ` · last send asked for ${pinLine(sentPin)}` : ''}. Pins apply to the next turn only; the server echoes what it
                  applied.
                </p>
                <p className="subtle">
                  Backends:{' '}
                  {(catalog?.backends ?? [])
                    .map((row) => `${row.id} ${row.available ? 'available' : `unavailable (${row.unavailable_reason ?? 'no reason reported'})`}`)
                    .join(' · ') || 'catalog not loaded'}
                </p>
              </details>

              {conversation && (
                <details className="pin-bar">
                  <summary>Audit · this session</summary>
                  <dl className="facts">
                    <dt>session</dt>
                    <dd>
                      <code>{conversation.id}</code>
                    </dd>
                    <dt>face</dt>
                    <dd>{pins?.face_id ?? conversation.face_id ?? 'server default'}</dd>
                    <dt>model</dt>
                    <dd>{pins?.model ?? conversation.model ?? 'server default'}</dd>
                    <dt>backend</dt>
                    <dd>{pins?.backend ?? conversation.backend ?? 'server default'}</dd>
                    <dt>project</dt>
                    <dd>{conversation.project_id ?? 'none'}</dd>
                    <dt>identity</dt>
                    <dd>{hello?.identity.sub ?? 'unknown'}</dd>
                  </dl>
                  <p className="subtle">Each message keeps its own server source under “source”.</p>
                </details>
              )}
            </div>
          )}
          {!panel && activityScroll.away && (activity.length > 0 || Object.values(pcQueue.outcomes).some(r => r.conversation_id === conversation?.id)) && <div className="activity-jump">
            <button type="button" onClick={activityScroll.jump}>{live || pendingHere ? 'Jump to latest ↓' : 'Skip to bottom ↓'}</button>
          </div>}
        </aside>
        </>}
      </div>
      </div>
      {builderConversation && conversation && <WorkspaceFilePreview client={client} conversationId={conversation.id} />}
    </main>
    </WorkspaceFileProvider>
    </ProviderRestartClient.Provider>
    </WorkflowContext.Provider>
    </ExperienceContext.Provider>
  );
}


export default function PortalApp(props: Parameters<typeof ConnectedPortalApp>[0]) {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [setupError, setSetupError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [mode, setMode] = useState<'server' | 'this_pc' | null>(null);
  useEffect(() => {
    let alive = true;
    void props.invoke<{ connection_configured: boolean }>('portal_connection_get').then(
      (payload) => { if (alive) { setMode(connectionMode(payload)); setConfigured(payload.connection_configured === true); } },
      (reason) => { if (alive) setSetupError(toPortalError(reason).detail); },
    );
    return () => { alive = false; };
  }, [props.invoke, attempt]);
  if (configured === null) return <main className="portal first-run"><section className="set-card">
    {setupError ? <><p role="alert">{setupError}</p><button onClick={() => { setSetupError(''); setAttempt((n) => n + 1); }}>Retry</button><RecoveryActions invoke={props.invoke} /></> : <p role="status">Reading connection settings…</p>}
  </section></main>;
  // First run reports the mode it set up; the saved connection is then re-read because environment
  // overrides take precedence over what was saved.
  function firstRunDone(chosen?: 'server' | 'this_pc') {
    if (chosen) setMode(chosen);
    setConfigured(true);
    void props.invoke<unknown>('portal_connection_get').then((payload) => { const read = connectionMode(payload); if (read) setMode(read); }, () => undefined);
  }
  return configured
    ? <ConnectedPortalApp {...props} mode={mode} onConnection={(payload) => { const read = connectionMode(payload); if (read) setMode(read); }} />
    : <FirstRun invoke={props.invoke} onConnect={firstRunDone} />;
}
