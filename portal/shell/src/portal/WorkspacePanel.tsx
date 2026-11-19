import { ReadFaultText } from './provider-restart';
import { WorkspaceBrowser } from './WorkspaceBrowser';
import { useCallback, useEffect, useRef, useState } from 'react';
import { toPortalError } from './portal-protocol';
import type { PortalError } from './portal-protocol';
import { BUDGETS, DEFAULT_BUDGET, requestKey } from './workspace-client';
import type { WorkspaceClient } from './workspace-client';
import type { AttachmentList, DecisionResult, JobAudit, JobSummary, Proposal } from './workspace-v2-protocol';
import { checkedEditsReason, adapterRefusal } from './checked-edits';
import { CheckedRunEvidence, CheckReceipts, typedSteps } from './CheckedEditsDetail';
import type { RunDetail } from './portal-protocol';
import { JOB_OPERATIONS } from './workspace-v2-protocol';
import { jobsReason, isJobsRefusal } from './workspace-v2-client';
import type { JobStart } from './workspace-v2-protocol';
import type { Catalog, Identity, SavedTeam } from './portal-protocol';
import type { PortalClient } from './portal-client';
import { canonical, ownerEligible, operationReason, validInteger } from './durable-runs';
import { useOwnerAction } from './OwnerControls';
import './workspace.css';

const POLL_MS = 1000;
// Bounded retry: a failed audit read must not end live status, but it must not
// retry forever either. After this many consecutive read failures the panel
// stops claiming liveness and asks the operator to Retry.
const MAX_POLL_FAILURES = 8;

// One submitted start, frozen at submit time. Resending it reuses the SAME key
// with the SAME fields, so the server returns the first job if it landed. Any
// other tuple is a different request and gets a fresh key.
type StartRequest = Readonly<{ role: string; task: string; attachment: string | null; budget: number; setup: Omit<JobStart, 'role' | 'task' | 'attachment' | 'request_key'> | null; key: string }>;
type Unconfirmed = { request: StartRequest; fault: PortalError };

// The receipt of the latest approve/deny WRITE for one job. Reads never clear
// it: a refusal stays a refusal, and an unknown outcome is resolved only by a
// read-back decision for the SAME choice, digest AND request key.
type DecisionAction = {
  decision: 'approve' | 'deny';
  digest: string;
  // The exact idempotency key of the write that produced this receipt — a
  // read-back for any other key is not this request.
  key: string;
  phase: 'sending' | 'returned' | 'refused' | 'unknown' | 'not_sent';
  fault: PortalError | null;
  result: DecisionResult | null;
};

function sameRequest(a: Omit<StartRequest, 'key'>, b: Omit<StartRequest, 'key'>): boolean {
  return a.role === b.role && a.task === b.task && a.attachment === b.attachment && a.budget === b.budget && canonical(a.setup) === canonical(b.setup);
}

function asFault(error: unknown): PortalError {
  if (typeof error === 'string') return toPortalError(error);
  if (typeof error === 'object' && error !== null && 'code' in error && 'detail' in error) {
    const value = error as PortalError;
    return { code: String(value.code), detail: String(value.detail), hint: String(value.hint ?? '') };
  }
  return { code: 'client_error', detail: error instanceof Error ? error.message : String(error), hint: '' };
}

function when(epoch: number | null | undefined): string {
  return typeof epoch === 'number' && Number.isFinite(epoch) ? new Date(epoch * 1000).toISOString().replace('.000Z', 'Z') : '—';
}

function short(digest: string | null | undefined): string {
  return digest ? digest.slice(0, 12) : 'none';
}

function Fault({ fault, onRetry }: { fault: PortalError; onRetry?: () => void }) {
  return (
    <div role="alert" className="portal-fault">
      <ReadFaultText error={fault} fallback={<><p><code>{fault.code}</code> {fault.detail}</p>
        {fault.hint && <p className="hint">{fault.hint}</p>}</>} />
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

function Diff({ text }: { text: string }) {
  return (
    <pre className="workspace-diff" aria-label="Proposed diff">
      {text.split('\n').map((line, index) => (
        <span
          key={index}
          className={
            line.startsWith('+++') || line.startsWith('---')
              ? 'file'
              : line.startsWith('@@')
                ? 'hunk'
                : line.startsWith('+')
                  ? 'add'
                  : line.startsWith('-')
                    ? 'del'
                    : undefined
          }
        >
          {line + '\n'}
        </span>
      ))}
    </pre>
  );
}

export function WorkspacePanel({
  client,
  portalClient, catalog, identity, runId, acceptedProposalDigest,
  generation = 0,
  onOpenDiff,
}: {
  client: WorkspaceClient;
  portalClient?: PortalClient;
  catalog?: Catalog | null;
  identity?: Identity | null;
  scope?: string;
  runId?: string;
  acceptedProposalDigest?: string;
  generation?: number;
  /** IDE host: show a proposal's diff in an editor tab (display only; decisions stay here). */
  onOpenDiff?: (title: string, diff: string) => void;
}) {
  const api = client.v2;
  const reason = useCallback((op: typeof JOB_OPERATIONS[number]) => jobsReason(catalog, op), [catalog]);
  const owner = !!portalClient && ownerEligible(identity, catalog);
  const ownerAction = useOwnerAction(portalClient!, catalog);
  const [teams, setTeams] = useState<SavedTeam[]>([]);
  const [teamId, setTeamId] = useState('');
  const [teamFault, setTeamFault] = useState<PortalError | null>(null);
  const [jobSeconds, setJobSeconds] = useState('900');
  const [jobAttempts, setJobAttempts] = useState('16');
  const [noWall, setNoWall] = useState(false);
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null);
  const [runFault, setRunFault] = useState<PortalError | null>(null);
  const [workerRefusal, setWorkerRefusal] = useState<string | null>(null);
  const [listCursor, setListCursor] = useState<string | null>(null);
  const cancelKeys = useRef(new Map<string, string>());
  // The revision is frozen together with each decision key, including lost replies.
  const decisionRequests = useRef(new Map<string, { expected_revision: JobStart['team_revision']; key: string }>());
  const [attachments, setAttachments] = useState<AttachmentList | null>(null);
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [attachmentFault, setAttachmentFault] = useState<PortalError | null>(null);
  const [listFault, setListFault] = useState<PortalError | null>(null);
  const [target, setTarget] = useState('');
  const [role, setRole] = useState('');
  const [task, setTask] = useState('');
  const [budget, setBudget] = useState(DEFAULT_BUDGET);
  const [starting, setStarting] = useState(false);
  const [unconfirmed, setUnconfirmed] = useState<Unconfirmed[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [audit, setAudit] = useState<JobAudit | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [jobFault, setJobFault] = useState<PortalError | null>(null);
  const [pollFailures, setPollFailures] = useState(0);
  const [lastReadAt, setLastReadAt] = useState<number | null>(null);
  const [stopping, setStopping] = useState(false);
  const [deciding, setDeciding] = useState<'approve' | 'deny' | null>(null);
  const [actions, setActions] = useState<Record<string, DecisionAction>>({});
  // The artifact shown is bound to the job AND the read that produced it; any
  // newer read or job selection supersedes it (also A -> B -> A).
  const [artifact, setArtifact] = useState<
    { jobId: string; gen: number; name: string; content: string | null; fault: PortalError | null } | null
  >(null);
  const decisionKeys = useRef(new Map<string, string>());
  const openRef = useRef<string | null>(null);
  const artifactGen = useRef(0);

  const refreshList = useCallback(async () => {
    // Independent operations: an old server lacking one feature must not hide the others.
    await Promise.allSettled([
      (async () => {
        if (reason('jobs.attachments')) return;
        try {
          const registered = await api.attachments(); setAttachments(registered); setAttachmentFault(null);
          setTarget(current => current || registered.items[0]?.id);
        } catch (error) { setAttachmentFault(asFault(error)); }
      })(),
      (async () => {
        if (reason('jobs.list')) return;
        try {
          const listed = await api.list(); setJobs(listed.items); setListCursor(listed.next_cursor); setListFault(null);
        } catch (error) { setListFault(asFault(error)); }
      })(),
    ]);
  }, [api, reason]);

  useEffect(() => { void refreshList(); }, [refreshList]);
  useEffect(() => {
    let live = true;
    if (!portalClient || operationReason(catalog, 'teams.list')) return;
    void (async () => {
      try {
        const all: SavedTeam[] = [], seen = new Set<string>(); let cursor: string | undefined;
        do {
          const page = await portalClient.teamsList(cursor); all.push(...page.items);
          cursor = page.next_cursor || undefined;
          if (cursor && seen.has(cursor)) throw new Error('Repeated teams cursor.');
          if (cursor) seen.add(cursor);
        } while (cursor);
        if (live) { setTeams(all); setTeamId(current => current || all.find(t => t.runnable)?.id || ''); setTeamFault(null); }
      } catch (error) { if (live) setTeamFault(asFault(error)); }
    })();
    return () => { live = false; };
  }, [portalClient, catalog]);

  const project = attachments?.items.find((item) => item.id === target) ?? null;
  const coderReason = checkedEditsReason(attachments?.capabilities);
  const roles = (project?.roles ?? []).filter(name => attachments?.capabilities.roles.includes(name));
  const checked = role === 'coder';
  const checkedReason = checked ? coderReason || (!project?.checks.length ? 'No registered checks for this attachment.' : '') : '';
  const canStartChecked = !checked || !checkedReason;
  const attempts = checked ? jobAttempts : 1;
  const secondsBudget = validInteger(jobSeconds) && validInteger(attempts) ? (BigInt(jobSeconds) * BigInt(attempts)).toString() : '';
  const writePaths = project?.role_write_paths?.[role] ?? project?.write_paths;
  const selectedTeam = teams.find(t => t.id === teamId && t.runnable && (!checked || t.topology === 'agent-loop/v1'));
  const setup: StartRequest['setup'] = selectedTeam && project && attachments && validInteger(jobSeconds) && validInteger(attempts) && validInteger(secondsBudget) && canStartChecked ? {
    contract_version: 2, team: selectedTeam.id, team_revision: selectedTeam.revision,
    project_id: project.project_id,
    ...(checked ? { worker_contract: 'checked-edits/1' as const, acceptance: { criteria: [{ id: 'checked-proposal', required: true, output_kind: 'proposal' as const, checks: { required_check_ids: [...project.checks] } }] } } : {}),
    budget: { start_calls: BUDGETS[budget].model_calls, wall_mode: checked && noWall ? 'none' : 'fixed', wall_seconds: checked && noWall ? null : BUDGETS[budget].wall_seconds, max_job_attempts: attempts, job_seconds_budget: checked ? secondsBudget : jobSeconds },
    job_policy: { proposed_seconds: jobSeconds, rationale: checked ? 'Registered checks and exact file edits' : 'Bounded proposal report', auto_escalate: false },
    job_coverage: { profiles: checked ? { checked: attachments.capabilities.profile_revisions.checked } : attachments.capabilities.profile_revisions, workspaces: { [project.coverage_target.workspace]: [project.coverage_target.cwd] }, attachments: [project.id], max_job_attempts: attempts, job_seconds_budget: checked ? secondsBudget : jobSeconds, allow_cli: false },
  } : null;
  useEffect(() => {
    if (!roles.includes(role) || role === 'coder' && !!coderReason) setRole(roles.find(n => n !== 'coder' || !coderReason) ?? '');
  }, [target, roles.join(','), role, coderReason]);

  const loadJob = useCallback(
    async (id: string, after = 0 as import('./portal-protocol').WireInteger) => {
      try {
        if (reason('jobs.audit')) return null;
        const next = await api.audit(id, after);
        if (openRef.current !== id) return null;
        setAudit(previous => after && previous?.id === id ? { ...next, history: [...previous.history, ...next.history] } : next);
        setJobFault(null);
        setLastReadAt(Date.now());
        setPollFailures(0);
        if (next.role === 'coder' && !coderReason && portalClient && !operationReason(catalog, 'runs.get')) {
          try {
            const detail = await portalClient.runGetV2(next.run_id);
            if (openRef.current === id) { setRunDetail(prior => prior?.run.id === detail.run.id ? { ...detail, events: [...new Map([...prior.events, ...detail.events].map(e => [e.seq, e])).values()], receipts: [...new Map([...prior.receipts, ...detail.receipts].map(r => [r.step, r])).values()] } : detail); setRunFault(null); }
          } catch (error) { if (openRef.current === id) setRunFault(asFault(error)); }
        }
        if (next.terminal && next.proposal && !reason('jobs.proposal')) {
          const shown = await api.proposal(id);
          if (openRef.current === id) setProposal(shown);
        } else {
          setProposal(null);
        }
        return next;
      } catch (error: unknown) {
        if (openRef.current === id) {
          setJobFault(asFault(error));
          // Bump a counter the poll effect depends on: the consumed timeout is
          // replaced with a backed-off retry instead of ending the schedule.
          setPollFailures((n) => n + 1);
        }
        return null;
      }
    },
    [api, reason, coderReason, portalClient, catalog],
  );

  function open(id: string) {
    openRef.current = id;
    setOpenId(id);
    setAudit(null);
    setProposal(null);
    setRunDetail(null); setRunFault(null); setWorkerRefusal(null);
    artifactGen.current += 1;
    setArtifact(null);
    setJobFault(null);
    setPollFailures(0);
    setLastReadAt(null);
    void loadJob(id);
  }

  // Live status while the job runs: re-read the persisted audit, never infer.
  // A failed read schedules a backed-off retry instead of silently ending the
  // poll; `generation` (bumped by Reconnect) revalidates immediately.
  useEffect(() => {
    if (!openId || reason('jobs.audit')) return;
    if (audit && (audit.terminal || audit.id !== openId)) return;
    if (pollFailures >= MAX_POLL_FAILURES) return;
    const delay = Math.min(POLL_MS * 2 ** Math.min(pollFailures, 4), 16000);
    const timer = setTimeout(() => {
      void loadJob(openId).then((next) => {
        if (next?.terminal) void refreshList();
      });
    }, delay);
    return () => clearTimeout(timer);
  }, [openId, audit, loadJob, refreshList, pollFailures, generation, reason]);

  // A replaced link (Reconnect) is a reason to revalidate the open job now.
  useEffect(() => {
    if (!generation) return;
    setPollFailures(0);
    const id = openRef.current;
    if (id) void loadJob(id);
  }, [generation, loadJob]);

  useEffect(() => {
    if (!acceptedProposalDigest || openRef.current) return;
    const job = jobs.find(j => j.proposal?.digest === acceptedProposalDigest);
    if (job) open(job.id);
  }, [acceptedProposalDigest, jobs]);

  async function moreRunEvidence(kind: 'events' | 'receipts') {
    if (!portalClient || !runDetail || !openId) return;
    const id = openId;
    try {
      const next = await portalClient.runGetV2(runDetail.run.id, kind === 'events' ? runDetail.next_cursor ?? 0 : 0, kind === 'receipts' ? runDetail.next_receipt_cursor ?? 0 : 0);
      if (openRef.current !== id) return;
      setRunDetail(prior => prior && { ...next,
        events: [...new Map([...prior.events, ...next.events].map(e => [e.seq, e])).values()],
        receipts: [...new Map([...prior.receipts, ...next.receipts].map(r => [r.step, r])).values()],
        next_cursor: kind === 'events' ? next.next_cursor : prior.next_cursor,
        next_receipt_cursor: kind === 'receipts' ? next.next_receipt_cursor : prior.next_receipt_cursor });
      setRunFault(null);
    } catch (error) { if (openRef.current === id) setRunFault(asFault(error)); }
  }

  const failedEditLog = runDetail?.run.id === audit?.run_id ? typedSteps(runDetail?.receipts ?? [], runDetail?.events ?? []).find(s => s.receipt?.kind === 'workspace_edit' && !s.receipt.passed)?.receipt?.log_artifact : undefined;
  const failureArtifact = audit?.artifacts.find(a => a.file === failedEditLog)?.file;
  useEffect(() => {
    let live = true; setWorkerRefusal(null);
    if (!audit || !failureArtifact || reason('jobs.artifact')) return;
    void api.artifact(audit.id, failureArtifact).then(chunk => {
      if (!live || chunk.truncated) return;
      const content = new TextDecoder().decode(Uint8Array.from(atob(chunk.content), c => c.charCodeAt(0)));
      setWorkerRefusal(adapterRefusal(content));
    }).catch(() => { /* Keep the read fault separate; the artifact remains available for explicit retry. */ });
    return () => { live = false; };
  }, [api, audit?.id, failureArtifact, reason]);

  const formRequest = { role, task: task.trim(), attachment: target || null, budget, setup };
  const resendable = unconfirmed.find((entry) => sameRequest(entry.request, formRequest)) ?? null;

  async function start(request: StartRequest) {
    if (reason('jobs.start') || !request.setup || !request.attachment || request.role === 'coder' && !!coderReason) return;
    setStarting(true);
    try {
      const row = await api.start({ ...request.setup, role: request.role, task: request.task, attachment: request.attachment, request_key: request.key });
      setUnconfirmed((list) => list.filter((entry) => entry.request.key !== request.key));
      if (sameRequest(request, formRequest)) setTask('');
      await refreshList();
      open(row.id);
    } catch (error: unknown) {
      // The request is KEPT, frozen with its key: resending it returns the same
      // job if the first start landed. Editing the form never changes it.
      const fault = asFault(error);
      setUnconfirmed((list) =>
        list.some((entry) => entry.request.key === request.key)
          ? list.map((entry) => (entry.request.key === request.key ? { request: entry.request, fault } : entry))
          : [...list, { request, fault }],
      );
    } finally {
      setStarting(false);
    }
  }

  function describe(request: StartRequest): string {
    const where =
      request.attachment === null ? 'research' : attachments?.items.find((item) => item.id === request.attachment)?.label ?? request.attachment;
    const ceiling = BUDGETS[request.budget];
    const wall = request.setup?.budget.wall_mode === 'none' ? 'no root wall limit' : String(request.setup?.budget.wall_seconds ?? ceiling.wall_seconds) + ' s root wall';
    const text = request.task.length > 80 ? `${request.task.slice(0, 80)}…` : request.task;
    return `${request.role} · ${where} · ${ceiling.model_calls} model calls, ${wall} · team ${request.setup?.team ?? 'none'} · job ${request.setup?.job_policy.proposed_seconds ?? 'none'} s · “${text}”`;
  }

  async function stop(id: string) {
    setStopping(true);
    try {
      if (reason('jobs.cancel')) return;
      const key = cancelKeys.current.get(id) ?? requestKey(); cancelKeys.current.set(id, key);
      await api.cancel(id, key);
      await loadJob(id);
      await refreshList();
    } catch (error: unknown) {
      setJobFault(asFault(error));
    } finally {
      setStopping(false);
    }
  }

  async function decide(id: string, decision: 'approve' | 'deny', digest: string) {
    if (!owner || reason('jobs.decide') || !proposal || proposal.job_id !== id) return;
    const slot = `${id}:${decision}:${digest}`;
    const key = decisionKeys.current.get(slot) ?? requestKey();
    decisionKeys.current.set(slot, key);
    setDeciding(decision);
    const record = (next: Omit<DecisionAction, 'decision' | 'digest' | 'key'>) =>
      setActions((all) => ({ ...all, [id]: { decision, digest, key, ...next } }));
    record({ phase: 'sending', fault: null, result: null });
    let sent = false;
    try {
      const saved = decisionRequests.current.get(slot) ?? { expected_revision: proposal.revision, key };
      decisionRequests.current.set(slot, saved);
      const result = await ownerAction.run('jobs.decide', { job_id: id }, () => { sent = true; return api.decide({ contract_version: 2, job_id: id, decision, proposal_digest: digest, request_key: saved.key, expected_revision: saved.expected_revision }); });
      record({ phase: 'returned', fault: null, result });
    } catch (error: unknown) {
      // The action's own outcome, kept apart from read faults: a sent request
      // whose answer is a gateway HTTP error, an internal fault, an undecodable
      // or oversize reply, or any code that is not a server-answered refusal
      // leaves the outcome AMBIGUOUS — the write may have landed.
      const fault = asFault(error);
      record({ phase: !sent ? 'not_sent' : isJobsRefusal(fault) ? 'refused' : 'unknown', fault, result: null });
    } finally {
      setDeciding(null);
      await loadJob(id);
      await refreshList();
    }
  }

  async function readArtifact(id: string, name: string, file: string) {
    const gen = ++artifactGen.current;
    setArtifact({ jobId: id, gen, name, content: null, fault: null });
    const current = () => artifactGen.current === gen && openRef.current === id;
    try {
      if (reason('jobs.artifact')) return;
      const value = await api.artifact(id, file);
      if (!current()) return;
      // Decode bounded bytes once; a chunk may end inside a UTF-8 character.
      const bytes = Uint8Array.from(atob(value.content), c => c.charCodeAt(0));
      const content = new TextDecoder().decode(bytes) + (value.truncated ? '\n[Artifact readback truncated; remaining bytes were not read.]' : '');
      if (!value.truncated && file === failedEditLog) setWorkerRefusal(adapterRefusal(content));
      setArtifact({ jobId: id, gen, name, content, fault: null });
    } catch (error: unknown) {
      if (!current()) return;
      setArtifact({ jobId: id, gen, name, content: null, fault: asFault(error) });
    }
  }

  const noProject = attachments !== null && !attachments.items.length;
  const decided = audit?.decision ?? proposal?.decision ?? null;
  const action = audit ? actions[audit.id] ?? null : null;
  // A read-back confirms only the SAME choice, digest AND request key, because a
  // different key is a different write and proves nothing about this one.
  const confirmed = !!(
    action &&
    decided &&
    decided.decision === action.decision &&
    decided.proposal_digest === action.digest &&
    decided.key === action.key
  );
  // Fail closed: never approve bytes this UI did not display (either read says truncated).
  const truncated = !!(proposal?.diff_truncated || audit?.proposal?.diff_truncated);

  return (
    <section className="workspace-panel" aria-label="Workspace">
      {ownerAction.dialog}
      {JOB_OPERATIONS.filter(op => op !== 'jobs.decide' || owner).map(op => reason(op) && <p key={op} role="status">{reason(op)}</p>)}
      {runId && <p className="subtle">Authorized jobs alongside run <code>{runId}</code>. New jobs create a separate root run.</p>}
      <div className="workspace-columns">
        <div className="workspace-side">
          <form
            aria-label="Start a job"
            onSubmit={(event) => {
              event.preventDefault();
              // The unchanged tuple of an unconfirmed start is resent with ITS key;
              // anything else is a new request with a fresh key.
              if (reason('jobs.start') || !setup || !canStartChecked) return;
              void start(resendable ? resendable.request : Object.freeze({ ...formRequest, key: requestKey() }));
            }}
          >
            <h2>New job</h2>
            {attachments && <p role="status">Apply unavailable: {attachments.capabilities.apply.reason}</p>}
            <label htmlFor="workspace-project">Project</label>
            <select id="workspace-project" value={target} disabled={starting} onChange={(e) => setTarget(e.target.value)}>
              {attachments?.items.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
            {portalClient && project && <details><summary>Browse project workspace</summary><WorkspaceBrowser key={project.project_id} client={portalClient} projectId={project.project_id} /></details>}
            {project && (
              <p className="subtle">
                checks: {project.checks.length ? project.checks.join(', ') : 'none registered'} · writable:{' '}
                {writePaths ? writePaths.join(', ') || 'read only' : 'any admitted file'} · sandbox{' '}
                {project.sandbox_registered ? 'registered' : 'NOT registered'}
              </p>
            )}
            {noProject && (
              <p className="caveat">The server has registered no project for this identity, so no job can start here.</p>
            )}
            <label htmlFor="workspace-role">Role</label>
            <select id="workspace-role" value={role} disabled={starting || !roles.length} onChange={(e) => setRole(e.target.value)}>
              {roles.map((name) => (
                <option key={name} value={name} disabled={name === 'coder' && !!coderReason}>
                  {name}
                </option>
              ))}
            </select>
            {coderReason && <p role="status">Checked edits unavailable: {coderReason}</p>}
            {checkedReason && <p role="status">{checkedReason}</p>}
            {checked && <><p>Required registered checks: {project?.checks.join(', ') || 'none'}. Every check must pass on the exact output snapshot.</p>
              <label htmlFor="workspace-job-attempts">Maximum worker attempts (read, edit, build and checks)</label>
              <input id="workspace-job-attempts" inputMode="numeric" value={jobAttempts} disabled={starting} onChange={e => setJobAttempts(e.target.value)} />
              <label><input type="checkbox" checked={noWall} disabled={starting} onChange={e => setNoWall(e.target.checked)} />No root wall limit (finite job tiers and attempts still apply)</label>
              <p>Aggregate worker budget: {secondsBudget || 'invalid'} seconds. Continuation is off.</p></>}
            <label htmlFor="workspace-team">Saved team</label>
            <select id="workspace-team" value={teamId} disabled={starting} onChange={e => setTeamId(e.target.value)}>
              <option value="">Choose a runnable team</option>
              {teams.map(t => <option key={t.id} value={t.id} disabled={!t.runnable || checked && t.topology !== 'agent-loop/v1'}>{t.id}{!t.runnable ? ' · ' + t.blocked_reasons.join(', ') : ''}</option>)}
            </select>
            {operationReason(catalog, 'teams.list') && <p role="status">{operationReason(catalog, 'teams.list')}</p>}
            {teamFault && <Fault fault={teamFault} />}
            {checked && !selectedTeam && <p role="status">Choose a runnable agent-loop/v1 team for checked edits.</p>}
            <label htmlFor="workspace-job-seconds">Proposed job time</label>
            <select id="workspace-job-seconds" value={jobSeconds} disabled={starting} onChange={e => setJobSeconds(e.target.value)}>
              {(catalog?.capabilities.execution_v2?.job_time?.presets_seconds ?? [900, 1800, 3600]).map(n => <option key={n} value={n}>{n / 60} min</option>)}
            </select>
            <p className="subtle">The root wall budget can reduce effective job time. Each worker attempt has a finite tier.</p>
            <label htmlFor="workspace-budget">Root budget (server-enforced; each call is reserved first)</label>
            <select
              id="workspace-budget"
              value={budget}
              disabled={starting}
              onChange={(e) => setBudget(Number(e.target.value))}
            >
              {BUDGETS.map((value, index) => (
                <option key={index} value={index}>
                  {value.model_calls} model calls, {value.wall_seconds} s root wall
                </option>
              ))}
            </select>
            <label htmlFor="workspace-task">Task</label>
            <textarea
              id="workspace-task"
              rows={5}
              value={task}
              disabled={starting}
              onChange={(e) => setTask(e.target.value)}
              placeholder="Describe the bounded change or question"
            />
            <button className="primary" disabled={starting || !role || !task.trim() || !target || !setup || !!reason('jobs.start') || !!reason('jobs.attachments')}>
              {starting
                ? 'Starting…'
                : resendable
                  ? 'Resend same request'
                  : unconfirmed.length
                    ? 'Start as a new request'
                    : 'Start job'}
            </button>
            {resendable && (
              <p className="subtle">
                This form is exactly an unconfirmed start below. Resending uses its request key, so the server returns the first
                job if it was created instead of starting a second one.
              </p>
            )}
            {!resendable && unconfirmed.length > 0 && (
              <p className="caveat">
                This differs from the unconfirmed start{unconfirmed.length === 1 ? '' : 's'} below (project, role, task or budget),
                so it is a NEW request with a new key and may start a second job. Check the job list, or resend an original
                exactly.
              </p>
            )}
          </form>
          {unconfirmed.length > 0 && (
            <section aria-label="Unconfirmed starts">
              <h3>Unconfirmed starts</h3>
              <ul>
                {unconfirmed.map((entry) => (
                  <li key={entry.request.key}>
                    <p>
                      {isJobsRefusal(entry.fault) ? 'Refused' : 'Outcome unknown'}: <code>{entry.fault.code}</code>{' '}
                      {entry.fault.detail}
                    </p>
                    <p className="subtle">
                      {describe(entry.request)} · key <code>{entry.request.key.slice(0, 11)}</code>
                    </p>
                    <button type="button" disabled={starting || !!reason('jobs.start')} onClick={() => void start(entry.request)}>
                      Resend this exact request
                    </button>{' '}
                    <button type="button" onClick={() => void refreshList()}>
                      Re-read the job list
                    </button>{' '}
                    <button
                      type="button"
                      onClick={() => setUnconfirmed((list) => list.filter((other) => other.request.key !== entry.request.key))}
                    >
                      Forget (checked)
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {listCursor && <button type="button" disabled={!!reason('jobs.list')} onClick={() => void (async () => {
            try {
              const page = await api.list(listCursor);
              if (page.next_cursor === listCursor) throw new Error('Repeated Jobs cursor.');
              setJobs(prior => [...new Map([...prior, ...page.items].map(job => [job.id, job])).values()]); setListCursor(page.next_cursor);
            } catch (error) { setListFault(asFault(error)); }
          })()}>More jobs</button>}
          <h2>
            Jobs <small>{jobs.length}</small>
          </h2>
          {attachmentFault && <Fault fault={attachmentFault} onRetry={() => void refreshList()} />}
          {listFault && <Fault fault={listFault} onRetry={() => void refreshList()} />}
          <ul className="workspace-jobs" aria-label="Jobs">
            {jobs.map((job) => (
              <li key={job.id}>
                <button type="button" aria-current={openId === job.id ? 'true' : undefined} disabled={!!reason('jobs.audit')} onClick={() => open(job.id)}>
                  <span>{job.task}</span>
                  <small>
                    {job.status}
                    {job.decision ? ` · ${job.decision}` : job.proposal ? ` · proposal (${job.proposal.files} file${job.proposal.files === 1 ? '' : 's'})` : ''}
                    {' · '}
                    {job.role} · {job.attachment ?? 'research'} · {when(job.created_at)}
                  </small>
                </button>
              </li>
            ))}
          </ul>
          {!jobs.length && !listFault && !reason('jobs.list') && <p className="subtle">No jobs recorded for this identity yet.</p>}
        </div>

        <div className="workspace-main">
          {!openId && <p className="subtle">Start a job or open one from the list. Jobs and their audit are stored on the server.</p>}
          {openId && !audit && !jobFault && <p role="status">Reading job {openId}…</p>}
          {jobFault && <Fault fault={jobFault} onRetry={openId ? () => void loadJob(openId) : undefined} />}
          {audit && (
            <article aria-label="Job">
              <header className="workspace-job-header">
                <div>
                  <h2>{audit.task.length > 120 ? `${audit.task.slice(0, 120)}…` : audit.task}</h2>
                  <p className="subtle">
                    <code>{audit.id}</code> · {audit.role} · project {audit.attachment ?? 'research'} · {audit.model_pin} ·{' '}
                    {when(audit.created_at)} · run <code>{audit.run_id}</code>
                  </p>
                </div>
                <p className="workspace-status" role="status">
                  Status: <strong>{audit.status}</strong>
                  {!audit.terminal &&
                    (pollFailures >= MAX_POLL_FAILURES
                      ? ' (live updates stopped after repeated failed reads — Retry to resume)'
                      : jobFault
                        ? ` (last observed ${lastReadAt ? new Date(lastReadAt).toISOString().slice(11, 19) : '—'}Z · read failing, retrying)`
                        : ' (live — re-read every second)')}
                </p>
                {!audit.terminal && (
                  <button type="button" className="danger" disabled={stopping || !!reason('jobs.cancel')} onClick={() => void stop(audit.id)}>
                    {stopping ? 'Stopping… waiting for the server to confirm' : 'Stop'}
                  </button>
                )}
              </header>
              {audit.role === 'coder' && <>
                {runFault && <Fault fault={runFault} onRetry={() => void loadJob(audit.id)} />}
                {runDetail?.run.id === audit.run_id ? <CheckedRunEvidence detail={runDetail} proposal={proposal} /> : <p>No accepted output yet. Typed run evidence has not been read.</p>}
                {runDetail?.next_cursor != null && <button onClick={() => void moreRunEvidence('events')}>More typed events</button>}
                {runDetail?.next_receipt_cursor != null && <button onClick={() => void moreRunEvidence('receipts')}>More typed receipts</button>}
                <CheckReceipts runs={audit.receipts.check_runs} />
                {audit.status === 'cancelling' && <p>Cancellation requested; waiting for descendant cleanup. No completed build is assumed.</p>}
              </>}
              {(workerRefusal || audit.error && /preimage|stale/.test(audit.error)) && <p role="alert">Stale preimage refused: source bytes changed; the edit batch was refused without fuzzy application.</p>}
              {audit.error && (
                <p className="caveat">
                  Error: <code>{audit.error}</code>
                </p>
              )}

              {action && action.phase !== 'sending' && (
                <section aria-label="Decision request" role={action.phase === 'returned' ? 'status' : 'alert'}
                  className={action.phase === 'returned' ? 'workspace-decision' : 'portal-fault'}
                >
                  {(() => {
                    const label = action.decision === 'approve' ? 'Approve' : 'Deny';
                    if (action.phase === 'returned' && action.result) {
                      return (
                        <p>
                          {label} request answered: <strong>{action.result.decision.state}</strong>
                          {action.result.replayed ? ' (replayed: an earlier identical request had already decided)' : ''}.
                        </p>
                      );
                    }
                    const fault = action.fault ?? { code: 'unknown', detail: '', hint: '' };
                    if (action.phase === 'not_sent') return <p>{label} request was not sent: <code>{fault.code}</code> {fault.detail}</p>;
                    if (action.phase === 'refused') {
                      return (
                        <p>
                          {label} request refused by the server: <code>{fault.code}</code> {fault.detail} This request applied
                          nothing; what is shown below is the server&apos;s current state, re-read afterwards.
                        </p>
                      );
                    }
                    return confirmed && decided ? (
                      <p>
                        {label} request reply was lost (<code>{fault.code}</code>); the server read-back confirms it:{' '}
                        <strong>{decided.state}</strong>.
                      </p>
                    ) : (
                      <p>
                        {label} request outcome unknown: <code>{fault.code}</code> {fault.detail}{' '}
                        The request was already sent, so this app cannot tell whether the decision was recorded — it may have been.{' '}
                        {decided
                          ? `The server shows a different decision (${decided.decision}, digest ${short(decided.proposal_digest)}, key ${short(decided.key ?? null)}) than this request (key ${short(action.key)}).`
                          : `The read-back shows no decision for digest ${short(action.digest)} / key ${short(action.key)} yet. Sending ${label} again reuses the same request key, so it cannot be recorded twice.`}
                      </p>
                    );
                  })()}
                </section>
              )}

              {audit.terminal && (
                <section aria-label="Result">
                  <h3>Result</h3>
                  {audit.content ? <pre className="workspace-result">{audit.content}</pre> : <p className="subtle">No final model text.</p>}
                  <p className="subtle">
                    {audit.completion === 'source_backed' ? 'Cites retained sources' : 'Model output'} — not independent verification.
                  </p>
                  {audit.receipts.caveats.length > 0 && (
                    <ul className="caveat-list" aria-label="Caveats">
                      {audit.receipts.caveats.map((item, index) => (
                        <li key={index}>{item}</li>
                      ))}
                    </ul>
                  )}
                </section>
              )}

              {proposal && (
                <section aria-label="Proposed change">
                  <h3>
                    Proposed change <small>digest {short(proposal.digest)}</small>
                  </h3>
                  <p>Base digest <code>{proposal.base_digest}</code> · Output digest <code>{proposal.output_digest}</code></p>
                  <p role="status">Apply unavailable: {proposal.apply_reason}</p>
                  <table className="workspace-files">
                    <thead>
                      <tr>
                        <th>File</th>
                        <th>Before</th>
                        <th>After</th>
                      </tr>
                    </thead>
                    <tbody>
                      {proposal.files.map((file) => (
                        <tr key={file.path}>
                          <td>
                            <code>{file.path}</code>
                            {file.operation === 'delete' ? ' (delete)' : file.created ? ' (new)' : ''}
                          </td>
                          <td>
                            <code>{short(file.pre_sha256)}</code>
                          </td>
                          <td>
                            <code>{short(file.post_sha256)}</code>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {onOpenDiff && (
                    <button type="button" className="link" onClick={() => onOpenDiff(`Proposal ${short(proposal.digest)}`, proposal.diff)}>
                      Open diff in editor
                    </button>
                  )}
                  <Diff text={proposal.diff} />
                  {truncated && (
                    <p className="caveat">
                      The diff display is truncated, so some proposed bytes are not shown here. Approval records a decision for the FULL
                      proposal (digest {short(proposal.digest)}), and this app has no path to show the rest, so it cannot be approved
                      in this app. Deny is still available; review the full proposal out of band before any approval elsewhere.
                    </p>
                  )}
                  <h4>Checks on these exact bytes</h4>
                  <CheckReceipts runs={proposal.check_runs} />
                  {!decided && proposal.stale_paths.length > 0 && (
                    <p className="caveat">
                      These files changed on the server since the job read them, so approval would be refused:{' '}
                      {proposal.stale_paths.join(', ')}
                    </p>
                  )}
                  {decided ? (
                    <div className="workspace-decision" role="status">
                      <p>
                        Decision: <strong>{decided.state}</strong> ({decided.decision}){decided.at ? ` at ${when(decided.at)}` : ''}
                      </p>
                      <p>Decision recorded; applied: false. Apply unavailable: {decided.apply_reason}</p>
                    </div>
                  ) : owner ? (
                    <div className="workspace-actions" aria-label="Decide">
                      <button
                        type="button"
                        className="primary"
                        disabled={!!deciding || !!reason('jobs.decide') || proposal.stale_paths.length > 0 || truncated}
                        onClick={() => void decide(audit.id, 'approve', proposal.digest)}
                      >
                        {deciding === 'approve' ? 'Recording…' : 'Approve'}
                      </button>
                      <button type="button" disabled={!!deciding || !!reason('jobs.decide')} onClick={() => void decide(audit.id, 'deny', proposal.digest)}>
                        {deciding === 'deny' ? 'Denying…' : 'Deny'}
                      </button>
                      <p className="subtle">
                        Decide records approve or deny for exactly digest {short(proposal.digest)} after owner confirmation.
                        One decision per proposal. Apply is unavailable.
                      </p>
                    </div>
                  ) : <p className="subtle">Only a confirmed owner can decide this proposal.</p>}
                </section>
              )}

              <section aria-label="Receipts">
                <h3>Receipts</h3>
                {!audit.terminal ? (
                  <p className="subtle">
                    The server records model and tool receipts when the job ends; while it runs, the persisted history below is the
                    authoritative progress.
                  </p>
                ) : (
                <>
                <p>
                  Model attempts reserved: {audit.receipts.model_attempts}
                  {Object.keys(audit.receipts.model_states).length > 0 &&
                    ` (${Object.entries(audit.receipts.model_states)
                      .map(([state, count]) => `${count} ${state}`)
                      .join(', ')})`}
                  {audit.receipts.usage.total_tokens !== undefined && ` · ${audit.receipts.usage.total_tokens} tokens`}
                  {audit.receipts.reported_models.length > 0 && ` · provider reported ${audit.receipts.reported_models.join(', ')}`}
                </p>
                <p>
                  Tool calls: {audit.receipts.tool_calls} ({audit.receipts.tool_failures} failed)
                </p>
                {audit.receipts.tools.length > 0 && (
                  <ol aria-label="Tool calls">
                    {audit.receipts.tools.map((tool, index) => (
                      <li key={index}>
                        <code>{tool.name}</code> {tool.ok ? 'ok' : 'failed'}
                        {tool.error ? ` — ${tool.error}` : ''}
                      </li>
                    ))}
                  </ol>
                )}
                <p className="subtle">Counts come from runtime-recorded receipts on the server, not from the model&apos;s text.</p>
                </>
                )}
              </section>

              {audit.artifacts.length > 0 && (
                <section aria-label="Artifacts">
                  <h3>Artifacts</h3>
                  <ul>
                    {audit.artifacts.map((item) => (
                      <li key={item.file}>
                        <button type="button" className="link" disabled={!!reason('jobs.artifact')} onClick={() => void readArtifact(audit.id, item.name, item.file)}>
                          {item.name}
                        </button>{' '}
                        <code>{short(item.sha256)}</code>
                      </li>
                    ))}
                  </ul>
                  {artifact && artifact.jobId === audit.id && artifact.gen === artifactGen.current && (
                    <div aria-label="Artifact text">
                      <h4>{artifact.name}</h4>
                      {artifact.fault ? (
                        <Fault fault={artifact.fault} />
                      ) : artifact.content === null ? (
                        <p role="status">Reading {artifact.name}…</p>
                      ) : (
                        <pre>{artifact.content}</pre>
                      )}
                    </div>
                  )}
                </section>
              )}

              <section aria-label="History">
                <h3>
                  History <small>{audit.history.length} persisted entries</small>
                </h3>
                {audit.next_cursor !== null && <button type="button" disabled={!!reason('jobs.audit')} onClick={() => void loadJob(audit.id, audit.next_cursor!)}>More history</button>}
                <ol className="workspace-history">
                  {audit.history.map((entry, index) => (
                    <li key={index}>
                      <time>{when(entry.at)}</time> <strong>{entry.status}</strong> <span>{entry.detail}</span>
                    </li>
                  ))}
                </ol>
                <p className="subtle">
                  principal <code>{audit.authority.principal}</code> · grants {audit.authority.grants.join(', ')}
                </p>
              </section>
            </article>
          )}
        </div>
      </div>
      <details className="aside-note">
        <summary>What this Workspace does not do yet</summary>
        <ul className="gap-list" aria-label="Workspace gaps">
          <li>Status is re-read from the server every second while a job runs; there is no pushed event stream.</li>
          <li>No manager role or delegation: registered research/review/proposal jobs use a saved team; coder capability is server-controlled.</li>
          <li>Apply is unavailable. Owner decisions record approval or denial only.</li>
          <li>A proposal whose diff is too large to display in full cannot be approved here (only denied); there is no full-proposal viewer yet.</li>
          <li>Model results and executed checks are recorded evidence, not independent review.</li>
        </ul>
      </details>
    </section>
  );
}
