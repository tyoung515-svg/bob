import type { NativeInvoke } from '../workbench/workbench-client';
import { isDefiniteRefusal, toPortalError } from './portal-protocol';
import type { Catalog, WireInteger, PortalError } from './portal-protocol';
import { validCheckedAcceptance, validCheckRuns } from './checked-edits';
import { checkedJobBudget, validInteger, operationReason } from './durable-runs';
import type { ArtifactChunk, AttachmentList, DecisionResult, JobAudit, JobDecision, JobList, JobOperation, JobStart, Proposal, ToolJobRow } from './workspace-v2-protocol';
const record = (v: unknown): v is Record<string, unknown> => !!v &&
  typeof v === 'object' &&
  !Array.isArray(v);
const digest = (v: unknown): v is string => typeof v === 'string' && v.length === 64 &&
  /^[a-f0-9]{64}$/.test(v);
const id = (v: unknown): v is string => typeof v === 'string' && v.length === 32 &&
  /^[a-f0-9]{32}$/.test(v);
const texts = (v: unknown): v is string[] => Array.isArray(v) &&
  v.every(x => typeof x === 'string');
const bounded = (v: unknown, n: number): v is string => typeof v === 'string' &&
  new TextEncoder().encode(v).length <= n;
const path = (v: unknown): v is string => bounded(v, 2048) &&
  !!v &&
  !v.startsWith('/') &&
  !/[\\:\x00-\x1f]/.test(v) &&
  v.split('/').every(p => !!p &&
    p !== '..');
const key = (v: unknown) => bounded(v, 128) &&
  !!v.trim() &&
  !/[\x00-\x1f]/.test(v);
const states = new Set(['accepted', 'queued', 'dispatch_intent', 'running', 'checking', 'completed', 'succeeded', 'failed', 'timed_out', 'cancelled', 'cancelling', 'paused', 'unknown', 'unavailable']);
const decision = (v: unknown): boolean => v === null ||
  record(v) &&
  typeof v.id === 'string' &&
  typeof v.key === 'string' &&
  ['approve', 'deny'].includes(String(v.decision)) &&
  v.state === (v.decision === 'approve' ? 'approved' : 'denied') &&
  digest(v.proposal_digest) &&
  typeof v.at === 'number' && Number.isFinite(v.at) &&
  v.applied === false &&
  typeof v.apply_reason === 'string';
const counters = (v: unknown): boolean => record(v) &&
  Object.values(v).every(x => validInteger(x, true));
const file = (v: unknown): boolean => record(v) &&
  path(v.path) &&
  ['create', 'replace', 'delete'].includes(String(v.operation)) &&
  (v.pre_sha256 === null ||
    digest(v.pre_sha256)) &&
  (v.post_sha256 === null ||
    digest(v.post_sha256)) &&
  (v.operation === 'delete' ? v.post_sha256 === null &&
    digest(v.pre_sha256) &&
    v.created === false : v.operation === 'create' ? v.pre_sha256 === null &&
      digest(v.post_sha256) &&
      v.created === true : digest(v.pre_sha256) &&
      digest(v.post_sha256) &&
  v.created === false) &&
  validInteger(v.bytes, true) &&
  typeof v.created === 'boolean';
const row = (v: unknown): boolean => record(v) &&
  id(v.id) &&
  typeof v.run_id === 'string' &&
  states.has(String(v.status)) &&
  ['role', 'task', 'model_pin', 'backend', 'operation_id'].every(k => typeof v[k] === 'string') &&
  typeof v.terminal === 'boolean' &&
  validInteger(v.revision) &&
  digest(v.base_digest) &&
  (v.output_digest === null ||
    digest(v.output_digest)) &&
  (v.execution_id === null ||
    typeof v.execution_id === 'string');
const proposal = (v: unknown): v is Proposal => record(v) &&
  id(v.job_id) &&
  ['digest', 'base_digest', 'output_digest'].every(k => digest(v[k])) &&
  validInteger(v.revision) &&
  Array.isArray(v.files) &&
  v.files.length <= 2048 &&
  v.files.every(file) &&
  Array.isArray(v.output_manifest) &&
  v.output_manifest.length <= 2048 &&
  v.output_manifest.every(m => record(m) &&
    path(m.path) &&
    validInteger(m.mode, true) &&
    (m.kind === 'directory' ||
      m.kind === 'file' &&
      digest(m.sha256) &&
      validInteger(m.size, true))) &&
  bounded(v.diff, 65540) &&
  typeof v.diff_truncated === 'boolean' &&
  v.applicable === false &&
  typeof v.apply_reason === 'string' &&
  v.independently_verified === false &&
  validCheckRuns(v.check_runs, v.checks_passed, String(v.output_digest)) &&
  (v.required_checks === undefined || texts(v.required_checks) && v.required_checks.length <= 100 && (!v.checks_passed || v.required_checks.every(cid => (v.check_runs as import('./workspace-v2-protocol').CheckedReceipt[]).some(r => r.check_id === cid && r.passed && r.snapshot_digest === v.output_digest)))) &&
  texts(v.stale_paths) &&
  decision(v.decision);
function invalid(what: string): never { throw JSON.stringify({ code: 'protocol_invalid', detail: 'Invalid ' + what + '; nothing was assumed.' }); }
function only(v: object, fields: string[]) {
  if(Object.keys(v).some(k => !fields.includes(k)))
    invalid('Jobs request fields');
}
export function checkedJobStart(v: JobStart): JobStart {
  only(v, ['contract_version', 'role', 'task', 'attachment', 'request_key', 'team', 'team_revision', 'budget', 'job_policy', 'job_coverage', 'intent_id', 'project_id', 'worker_contract', 'acceptance']);
  if(v.contract_version !== 2 ||
    !['researcher', 'reviewer', 'proposer', 'coder'].includes(v.role) ||
    !bounded(v.task, 16384) ||
    !v.task.trim() ||
    !key(v.request_key) ||
    !/^[A-Za-z0-9_-]{1,64}$/.test(v.attachment) ||
    !bounded(v.team, 64) ||
    !v.team.trim() ||
    !validInteger(v.team_revision))
    invalid('Jobs start');
  for (const field of ['intent_id', 'project_id'] as const) {
    if (v[field] !== undefined && (!key(v[field]) || !bounded(v[field], 128))) invalid('Jobs identifier');
  }
  if(!record(v.budget) ||
    !record(v.job_policy))
    invalid('Jobs budget or policy');
  only(v.budget, ['start_calls', 'wall_mode', 'wall_seconds', 'max_job_attempts', 'job_seconds_budget']);
  checkedJobBudget(v.budget);
  if (v.role === 'coder' ? v.worker_contract !== 'checked-edits/1' || !validCheckedAcceptance(v.acceptance) || !validInteger(v.budget.max_job_attempts) : v.worker_contract !== undefined || v.acceptance !== undefined) invalid('checked Jobs contract');
  if(v.budget.wall_mode === 'none' && v.role !== 'coder')
    invalid('finite Jobs budget');
  only(v.job_policy, ['proposed_seconds', 'rationale', 'auto_escalate']);
  if(!validInteger(v.job_policy.proposed_seconds) ||
    !bounded(v.job_policy.rationale, 2000) ||
    !v.job_policy.rationale.trim() ||
    (v.role === 'coder' ? typeof v.job_policy.auto_escalate !== 'boolean' : v.job_policy.auto_escalate !== false))
    invalid('Jobs policy');
  const c = v.job_coverage;
  if(c === undefined)
    return v; // The server still enforces grant coverage; the client never inserts authority.
  if(!record(c))
    invalid('Jobs coverage');
  only(c, ['profiles', 'workspaces', 'attachments', 'max_job_attempts', 'job_seconds_budget', 'allow_cli']);
  if(!record(c.profiles) ||
    !Object.keys(c.profiles).length || Object.keys(c.profiles).length > 16 ||
    !Object.keys(c.profiles).every(k => /^[A-Za-z0-9_-]{1,64}$/.test(k)) ||
    !Object.values(c.profiles).every(digest) ||
    !record(c.workspaces) ||
    !Object.keys(c.workspaces).length || Object.keys(c.workspaces).length > 32 ||
    !Object.entries(c.workspaces).every(([w, ps]) => /^[A-Za-z0-9_-]{1,64}$/.test(w) &&
      Array.isArray(ps) &&
      ps.length > 0 && ps.length <= 32 &&
      ps.every(path)) ||
    !texts(c.attachments) || !c.attachments.length || c.attachments.length > 32 ||
    !c.attachments.every(a => /^[A-Za-z0-9_-]{1,64}$/.test(a)) ||
    !c.attachments.includes(v.attachment) ||
    !validInteger(c.max_job_attempts) ||
    c.job_seconds_budget !== null &&
    !validInteger(c.job_seconds_budget) ||
    c.allow_cli !== false)
    invalid('Jobs coverage');
  return v;
}
export function isJobsRefusal(fault: PortalError): boolean {
  return isDefiniteRefusal(fault) || ['not_found', 'forbidden', 'owner_portal_required', 'owner_confirmation_required', 'operation_unavailable', 'client_upgrade_required', 'idempotency_conflict', 'proposal_stale', 'proposal_digest_conflict', 'decision_already_recorded', 'revision_conflict'].includes(fault.code);
}
export function jobsReason(catalog: Catalog | null | undefined, op: JobOperation): string {
  const reason = operationReason(catalog, op);
  if(reason)
    return reason;
  const cap = catalog!.capabilities.operations![op];
  const mode = ['jobs.start', 'jobs.cancel', 'jobs.decide'].includes(op) ? 'writable' : 'readable';
  return cap[mode] === false ||
    op === 'jobs.start' &&
    cap.runnable === false ? cap.reason ||
  op + ' unavailable' : '';
}
export function createWorkspaceV2Client(invoke: NativeInvoke) {
  async function call<T>(op: string, args: Record<string, unknown>, check: (v: unknown) => boolean): Promise<T> {
    try {
      const v = await invoke<unknown>('portal_jobs_' + op + '_v2', { contract_version: 2, ...args });
      if(!check(v))
        invalid('Jobs ' + op + ' reply');
      return v as T;
    }
    catch(e) {
      const fault = toPortalError(e);
      if (/^gateway_http_4\d\d$/.test(fault.code) && ['not_found', 'forbidden', 'owner_portal_required', 'owner_confirmation_required', 'operation_unavailable', 'client_upgrade_required', 'idempotency_conflict', 'proposal_stale', 'proposal_digest_conflict', 'decision_already_recorded', 'revision_conflict'].includes(fault.detail)) throw { ...fault, code: fault.detail };
      throw fault;
    }
  }
  return {
    attachments: () => call<AttachmentList>('attachments', {}, v => record(v) &&
      Array.isArray(v.items) && typeof v.research === 'boolean' &&
      v.items.every(a => record(a) &&
        typeof a.id === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(a.id) &&
        typeof a.project_id === 'string' &&
        digest(a.registration_revision) &&
        typeof a.label === 'string' &&
        texts(a.roles) &&
        texts(a.checks) &&
        (a.write_paths === null ||
          texts(a.write_paths)) &&
        (a.role_write_paths === undefined || record(a.role_write_paths) && Object.values(a.role_write_paths).every(ps => Array.isArray(ps) && ps.length <= 32 && ps.every(path))) &&
        typeof a.sandbox_registered === 'boolean' &&
        record(a.coverage_target) &&
        typeof a.coverage_target.workspace === 'string' &&
        path(a.coverage_target.cwd) &&
        a.coverage_target.attachment_id === a.id) &&
      record(v.capabilities) &&
      texts(v.capabilities.protocols) &&
      v.capabilities.protocols.includes('tool-jobs/2') &&
      record(v.capabilities.profile_revisions) &&
      Object.values(v.capabilities.profile_revisions).every(digest) &&
      texts(v.capabilities.roles) &&
      record(v.capabilities.operations) &&
      record(v.capabilities.apply) &&
      v.capabilities.apply.available === false &&
      typeof v.capabilities.apply.reason === 'string'),
    list: (cursor?: string) => {
      if (cursor !== undefined && !id(cursor)) invalid('Jobs cursor');
      return call<JobList>('list', { ...(cursor ? { cursor } : {}), limit: 50 }, v => record(v) &&
      Array.isArray(v.items) &&
      v.items.every(j => record(j) &&
        id(j.id) &&
        states.has(String(j.status)) &&
        typeof j.task === 'string' &&
        typeof j.role === 'string' &&
        typeof j.terminal === 'boolean' &&
        (j.attachment === null ||
          typeof j.attachment === 'string') &&
        (j.proposal === null ||
          record(j.proposal) &&
          digest(j.proposal.digest) &&
          validInteger(j.proposal.files, true))) &&
      validInteger(v.total, true) &&
      typeof v.truncated === 'boolean' &&
      (v.next_cursor === null ||
        id(v.next_cursor)) &&
      v.total_scope === 'page');
    },
    start: (request: JobStart) => {
      checkedJobStart(request);
      return call<ToolJobRow>('start', request, row);
    },
    audit: (job_id: string, after_event_seq: WireInteger = 0) => {
      if(!id(job_id) ||
        !validInteger(after_event_seq, true))
        invalid('audit request');
      return call<JobAudit>('audit', { job_id, after_event_seq }, v => row(v) &&
        record(v) &&
        v.id === job_id &&
        record(v.authority) &&
        typeof v.authority.principal === 'string' &&
        typeof v.authority.project === 'string' &&
        validInteger(v.authority.depth, true) &&
        typeof v.authority.expires_at === 'number' &&
        texts(v.authority.grants) &&
        Array.isArray(v.history) &&
        v.history.every(e => record(e) &&
          typeof e.at === 'number' &&
          typeof e.status === 'string' &&
          typeof e.detail === 'string') &&
        bounded(v.content, 65540) &&
        Array.isArray(v.artifacts) &&
        v.artifacts.every(a => record(a) &&
          typeof a.name === 'string' &&
          typeof a.file === 'string' &&
          digest(a.sha256)) &&
        record(v.receipts) &&
        validInteger(v.receipts.model_attempts, true) &&
        validInteger(v.receipts.tool_calls, true) &&
        validInteger(v.receipts.tool_failures, true) &&
        counters(v.receipts.model_states) &&
        counters(v.receipts.usage) &&
        texts(v.receipts.reported_models) &&
        Array.isArray(v.receipts.tools) &&
        v.receipts.tools.every(t => record(t) &&
          typeof t.name === 'string' &&
          typeof t.ok === 'boolean' &&
          typeof t.error === 'string') &&
        validCheckRuns(v.receipts.check_runs, v.receipts.checks_passed, typeof v.output_digest === 'string' ? v.output_digest : undefined) &&
        texts(v.receipts.caveats) &&
        decision(v.decision) &&
        (v.proposal === null ||
          record(v.proposal) &&
          digest(v.proposal.digest) &&
          Array.isArray(v.proposal.files) &&
          v.proposal.files.every(file)) &&
        (v.next_cursor === null ||
          validInteger(v.next_cursor, true)));
    },
    cancel: (job_id: string, request_key: string) => {
      if(!id(job_id) ||
        !key(request_key))
        invalid('cancel request');
      return call<ToolJobRow>('cancel', { job_id, request_key }, v => row(v) &&
        record(v) &&
        v.id === job_id);
    },
    proposal: (job_id: string) => {
      if(!id(job_id))
        invalid('proposal request');
      return call<Proposal>('proposal', { job_id }, v => proposal(v) &&
        v.job_id === job_id);
    },
    decide: (request: JobDecision) => {
      only(request, ['contract_version', 'job_id', 'decision', 'proposal_digest', 'request_key', 'expected_revision']);
      if(request.contract_version !== 2 ||
        !id(request.job_id) ||
        !digest(request.proposal_digest) ||
        !key(request.request_key) ||
        !validInteger(request.expected_revision) ||
        !['approve', 'deny'].includes(request.decision))
        invalid('decision request');
      return call<DecisionResult>('decide', request, v => record(v) &&
        v.decision !== null &&
        decision(v.decision) &&
        record(v.decision) &&
        v.decision.key === request.request_key &&
        v.decision.decision === request.decision &&
        v.decision.proposal_digest === request.proposal_digest &&
        typeof v.replayed === 'boolean');
    },
    artifact: (job_id: string, filename: string, offset: WireInteger = 0, max_bytes = 65536) => {
      if(!id(job_id) ||
        (!/^[a-z]+-[a-f0-9]{32}\.json$/.test(filename) || !filename.endsWith('.json')) ||
        !validInteger(offset, true) ||
        BigInt(offset) > BigInt(4194304) ||
        !Number.isInteger(max_bytes) ||
        max_bytes < 1 ||
        max_bytes > 65536)
        invalid('artifact request');
      return call<ArtifactChunk>('artifact', { job_id, filename, offset, max_bytes }, v => {
        if(!record(v) ||
          v.filename !== filename ||
          v.encoding !== 'base64' ||
          !digest(v.sha256) ||
          !bounded(v.content, 87384) ||
          !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(v.content) ||
          !validInteger(v.offset, true) ||
          BigInt(v.offset) !== BigInt(offset) ||
          !validInteger(v.total_bytes, true) ||
          typeof v.truncated !== 'boolean')
          return false;
        const end = BigInt(offset) + BigInt(atob(v.content).length);
        return end <= BigInt(v.total_bytes) &&
          atob(v.content).length <= max_bytes &&
          (v.truncated ? validInteger(v.next_offset, true) &&
            BigInt(v.next_offset) === end &&
            end > BigInt(offset) &&
            end < BigInt(v.total_bytes) : v.next_offset === null &&
          end === BigInt(v.total_bytes));
      });
    },
  };
}
export type WorkspaceV2Client = ReturnType<typeof createWorkspaceV2Client>;
