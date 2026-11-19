import type { PortalClient } from './portal-client';
import type { WorkflowCatalog, WorkflowEvent, WorkflowProposal, WorkflowRun, WorkflowStageView, WorkflowSummary, WorkflowWait } from './workflow-types';
import { workflowInteger, workflowTerminal } from './workflow-types';

export type ProposalSource = { conversationId: string | null; name: string; revision: number | string; digest: string };
export class WorkflowStore {
  proposals = new Map<string, { proposal: WorkflowProposal; source: ProposalSource }>();
  runs = new Map<string, WorkflowRun>();
  summaries = new Map<string, WorkflowSummary>();
  stale = new Set<string>();
  catalog: WorkflowCatalog | null = null;
  error = '';
  runsError = '';
  checkedAt: number | null = null;
  discoveredAt: number | null = null;
  includeExpired = false;
  proposeAgainAdvertised = false;
  proposalsAdvertised = false;
  private inboxIds = new Set<string>();
  refreshHistory?: (run: WorkflowRun) => Promise<void>;
  private listeners = new Set<() => void>();
  private version = 0;
  private watching = new Set<string>();
  private views = new Map<string, number>();
  private reads = new Map<string, Promise<unknown>>();
  private alive = true;
  private chatSeq = new Map<string, bigint>();
  constructor(readonly client: PortalClient, readonly scope: string) {}
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  snapshot = () => this.version;
  changed = () => { this.version++; this.listeners.forEach(fn => fn()); };
  activate() { this.alive = true; for (const r of this.runs.values()) this.syncWatch(r); }
  settleRecord(kind: string, recordId: string, state: string) {
    for (const r of this.runs.values()) if (r.needs_you.some(w => w.kind === kind && w.record_id === recordId && w.state === 'open')) this.putRun({...r, needs_you: r.needs_you.map(w => w.kind === kind && w.record_id === recordId ? {...w,state} : w)});
  }
  dispose() { this.alive = false; for (const id of this.watching) void this.client.workflowWatch(id, this.runs.get(id)?.event_seq ?? 0, false).catch(() => {}); this.watching.clear(); }
  private once<T>(key: string, fn: () => Promise<T>): Promise<T> {
    const old = this.reads.get(key); if (old) return old as Promise<T>;
    const p = fn().finally(() => this.reads.delete(key)); this.reads.set(key, p); return p;
  }
  async proposeEndedRun(runId: string) {
    return this.once('run-again:' + runId, async () => {
      let old = [...this.proposals.values()].find(entry => entry.proposal.run_id === runId);
      if (!old) { await this.refreshProposals(); old = [...this.proposals.values()].find(entry => entry.proposal.run_id === runId); }
      const proposalId = old?.proposal.id ?? this.runs.get(runId)?.proposal_id ?? this.summaries.get(runId)?.proposal_id;
      if (!proposalId) throw new Error('The server has not supplied the original proposal for this run. It must supply that proposal before the run can be proposed again.');
      const {proposal} = await this.client.workflowProposeAgain(proposalId);
      if (proposal.id === proposalId || proposal.state !== 'proposed' || proposal.run_id) throw new Error('A fresh proposal was not returned. Nothing has started.');
      if (old) this.proposals.set(proposalId, {...old, proposal: {...old.proposal, proposed_again_ids: [...(old.proposal.proposed_again_ids ?? []), proposal.id]}});
      const source: ProposalSource = {conversationId: proposal.origin_conversation_id ?? old?.source.conversationId ?? null,
        name: old?.source.name ?? 'you', revision: proposal.revision, digest: proposal.digest};
      this.proposals.set(proposal.id, {proposal: {...proposal, previous_proposal_id: proposal.previous_proposal_id ?? proposalId}, source});
      this.changed(); return proposal.id;
    });
  }
  async loadCatalog(refresh = false) {
    if (this.catalog && !refresh) return this.catalog;
    return this.once('catalog', async () => { const c = await this.client.workflowCatalog(); if (this.alive) { this.catalog = c; this.changed(); } return c; });
  }
  async refreshProposals() {
    if (!this.proposalsAdvertised) return;
    return this.once('inbox', async () => {
      const found = new Map<string, {proposal: WorkflowProposal; source: ProposalSource}>();
      let cursor: string | undefined; const seen = new Set<string>();
      do {
        const page = await this.client.workflowProposals(cursor, 20, this.includeExpired);
        for (const proposal of page.items) {
          if (proposal.kind !== 'workflow_proposal' || !workflowInteger(proposal.revision) || !proposal.proposed_by) throw new Error('The pending proposals could not be read.');
          const old = this.proposals.get(proposal.id);
          const who = proposal.proposed_by;
          const binding = proposal.coverage?.assistant_binding as {face_id?: string} | undefined;
          const originFace = binding?.face_id ?? who.face_id;
          const name = who.token_type === 'agent' ? `${who.actor} (outside agent)` : old?.source.conversationId ? old.source.name : originFace === 'planner' || who.actor === 'planner' ? 'the Planner' : originFace === 'assistant' || who.via === 'assistant' || who.actor === 'assistant' ? 'the Assistant' : who.via === 'owner' ? 'you' : who.actor;
          // A successful Ask again is durable lineage. Keep that receipt when
          // an older listing omits the optional linkage fields.
          const linked = {...proposal, previous_proposal_id: proposal.previous_proposal_id ?? old?.proposal.previous_proposal_id,
            proposed_again_ids: [...new Set([...(old?.proposal.proposed_again_ids ?? []), ...(proposal.proposed_again_ids ?? [])])]};
          found.set(proposal.id, {proposal:linked, source: {conversationId: proposal.origin_conversation_id ?? old?.source.conversationId ?? null, name, revision: proposal.revision, digest: proposal.digest}});
        }
        cursor = page.next_cursor ?? undefined;
        if (cursor && seen.has(cursor)) throw new Error('The server repeated a proposal page.');
        if (cursor) seen.add(cursor);
      } while (cursor);
      if (!this.alive) return;
      for (const id of this.inboxIds) if (!found.has(id)) this.proposals.delete(id);
      for (const [id, entry] of found) this.proposals.set(id, entry);
      this.inboxIds = new Set(found.keys()); this.error = ''; this.checkedAt = Date.now(); this.changed();
    }).catch(e => { if (this.alive) { this.error = String(e); this.changed(); } });
  }
  async proposal(id: string, source: ProposalSource, refresh = false) {
    void this.refreshProposals();
    const existing = this.proposals.get(id);
    if (existing && !refresh) {
      if (source.conversationId && !existing.source.conversationId) {
        const name = existing.proposal.proposed_by?.token_type === 'agent' ? existing.source.name : source.name;
        this.proposals.set(id, {...existing,source:{...existing.source,conversationId:source.conversationId,name}}); this.changed();
      }
      return;
    }
    await this.once('proposal:' + id, async () => {
      const { proposal } = await this.client.workflowProposal(id);
      if (proposal.id !== id || !workflowInteger(proposal.revision) || typeof proposal.digest !== 'string' || !proposal.coverage?.definition?.stages || !proposal.coverage.review) throw new Error('The proposal could not be verified. Ask the agent for a fresh one.');
      if (!this.alive) return;
      this.proposals.set(id, { proposal, source }); this.changed();
      if (proposal.run_id) await this.readRun(proposal.run_id);
    });
  }
  putRun(run: WorkflowRun, replaceStages = true) {
    if (!this.alive || !run || !workflowInteger(run.revision) || !workflowInteger(run.event_seq)) return;
    const old = this.runs.get(run.id);
    if (old && BigInt(old.revision) > BigInt(run.revision)) return;
    this.runs.set(run.id, replaceStages ? run : { ...run, stages: [...new Map([...(old?.stages ?? []), ...run.stages].map(s => [s.key, s])).values()] });
    this.summaries.set(run.id, run); this.changed(); this.syncWatch(run);
  }
  async readRun(id: string, reconnect = false) {
    return this.once('run:' + id, async () => {
      let cursor: string | undefined; const seen = new Set<string>(); let first = true;
      do { const { run } = await this.client.workflowRun(id, cursor); this.putRun(run, first); first = false; cursor = run.next_stage_cursor ?? undefined;
        if (cursor && seen.has(cursor)) throw new Error('The server repeated a stage page.'); if (cursor) seen.add(cursor);
      } while (cursor);
      if (reconnect) { this.stale.delete(id); this.watching.delete(id); const r = this.runs.get(id); if (r) { this.syncWatch(r); await this.refreshHistory?.(r); } this.changed(); }
    });
  }
  async discover(refreshCatalog = false) {
    return this.once('runs', async () => {
      await this.refreshProposals();
      await this.loadCatalog(refreshCatalog); let cursor: string | undefined; const seen = new Set<string>();
      do { const page = await this.client.workflowRuns(cursor); for (const r of page.items) { this.summaries.set(r.id, r); if (!workflowTerminal(r.status)) await this.readRun(r.id); }
        cursor = page.next_cursor ?? undefined; if (cursor && seen.has(cursor)) throw new Error('The server repeated a run page.'); if (cursor) seen.add(cursor);
      } while (cursor); this.runsError = ''; this.discoveredAt = Date.now(); this.changed();
    }).catch(e => { if (this.alive) { this.runsError = 'Workflow runs could not be checked.'; this.changed(); } throw e; });
  }
  view(id: string) { this.views.set(id, (this.views.get(id) ?? 0) + 1); const r = this.runs.get(id); if (r) this.syncWatch(r);
    return () => { this.views.set(id, Math.max(0, (this.views.get(id) ?? 1) - 1)); const r = this.runs.get(id); if (r) this.syncWatch(r); };
  }
  private syncWatch(run: WorkflowRun) {
    const wanted = this.alive && (!workflowTerminal(run.status) || run.needs_you.some(w => w.state === 'open') || !!this.views.get(run.id));
    if (wanted && !this.watching.has(run.id) && !this.stale.has(run.id)) {
      this.watching.add(run.id); void this.client.workflowWatch(run.id, run.event_seq, true).catch(() => { if (this.alive) { this.stale.add(run.id); this.watching.delete(run.id); this.changed(); } });
    } else if (!wanted && this.watching.delete(run.id)) void this.client.workflowWatch(run.id, run.event_seq, false).catch(() => {});
  }
  compatibility(data: Record<string, unknown>) {
    if (data.source !== 'workflow' || typeof data.run_id !== 'string' || typeof data.conversation_id !== 'string' || typeof data.request_id !== 'string' || !workflowInteger(data.event_seq)) return false;
    const r = this.runs.get(data.run_id);
    if (!r || !r.stages.some(s => s.key === data.stage_key && s.conversation_id === data.conversation_id && s.request_id === data.request_id)) return false;
    const seq = BigInt(data.event_seq), last = this.chatSeq.get(data.run_id) ?? 0n;
    if (seq <= last) return false; this.chatSeq.set(data.run_id,seq); return true;
  }
  observe(name: string, data: Record<string, unknown>) {
    if (name === 'workflow_connection' && data.state === 'stale' && typeof data.run_id === 'string') { this.stale.add(data.run_id); this.watching.delete(data.run_id); this.changed(); return true; }
    if (name === 'transport_fault') { for (const id of this.watching) this.stale.add(id); this.watching.clear(); this.changed(); return false; }
    if (name !== 'workflow_event') return false;
    void this.refreshProposals();
    const e = data as unknown as WorkflowEvent, old = this.runs.get(e.run_id);
    if (!old || !workflowInteger(e.seq) || !workflowInteger(e.revision) || BigInt(e.seq) <= BigInt(old.event_seq)) return true;
    // A gap means lost observation, not a stopped executor. The owner can reconnect.
    if (BigInt(e.seq) !== BigInt(old.event_seq) + 1n) { this.stale.add(e.run_id); this.changed(); return true; }
    if (BigInt(e.revision) < BigInt(old.revision)) return true;
    const r = { ...old, revision: e.revision, event_seq: e.seq, updated_at: e.at };
    if (e.kind === 'run_state' && typeof e.payload.status === 'string') { r.status = e.payload.status; r.reason = typeof e.payload.reason === 'string' ? e.payload.reason : null; }
    if (e.kind === 'stage_state' && e.payload.stage) { const stage = e.payload.stage as WorkflowStageView; r.stages = [...new Map([...old.stages, stage].map(s => [s.key, s])).values()]; }
    if (e.kind === 'wait' && e.payload.wait) { const wait = e.payload.wait as WorkflowWait; r.needs_you = [...new Map([...old.needs_you, wait].map(w => [w.wait_id, w])).values()]; }
    if (e.kind === 'report' && e.payload.report) r.report = e.payload.report as WorkflowRun['report'];
    // Controller additions may attach a budget snapshot; do not estimate spend from prose.
    if (e.payload.budget && typeof e.payload.budget === 'object') r.budget = e.payload.budget as WorkflowRun['budget'];
    this.putRun(r);
    // Terminal readback supplies final accounting/report; no recurring run polling.
    if (e.kind === 'run_state' && (workflowTerminal(r.status) || r.status === 'interrupted') || e.kind === 'report') void this.readRun(r.id).then(() => { const current = this.runs.get(r.id); if (current) return this.refreshHistory?.(current); }).catch(() => {});
    return true;
  }
}
