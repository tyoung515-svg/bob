// tool-jobs/2 projections. References identify server evidence; they grant no access.
import type { JobCoverage, RunBudget, WireInteger, OperationCapability } from './portal-protocol';
export const JOB_OPERATIONS = ['jobs.attachments', 'jobs.list', 'jobs.start', 'jobs.audit', 'jobs.cancel', 'jobs.proposal', 'jobs.decide', 'jobs.artifact'] as const;
export type JobOperation = typeof JOB_OPERATIONS[number];
export type JobState = 'accepted' | 'queued' | 'dispatch_intent' | 'running' | 'checking' | 'completed' | 'succeeded' | 'failed' | 'timed_out' | 'cancelled' | 'cancelling' | 'paused' | 'unknown' | 'unavailable';
export type SnapshotReference = {
  digest: string;
  manifest: ({
    path: string;
    kind: 'file';
    sha256: string;
    size: WireInteger;
    mode: WireInteger;
  } | {
    path: string;
    kind: 'directory';
    mode: WireInteger;
  })[];
};
export type WorkspaceReference = {
  workspace_id: string;
  base_digest: string;
  output_digest: string | null;
};
export type ArtifactReference = {
  name: string;
  file: string;
  sha256: string;
};
export type JobCapabilities = {
  protocols: string[];
  operations: Partial<Record<JobOperation, OperationCapability>>;
  roles: string[];
  profile_revisions: Record<string, string>;
  apply: {
    available: false;
    reason: string | null;
  };
  coder?: {
    available: boolean;
    reason: string | null;
    worker_contract?: string;
  };
  checks?: {
    available: boolean;
    reason: string | null;
    selection?: string;
  };
  cli?: {
    available: boolean;
    reason: string | null;
  };
  queue?: string;
  acceptance_revision?: string;
};
export type JobAttachment = {
  id: string;
  project_id: string;
  registration_revision: string;
  label: string;
  roles: string[];
  checks: string[];
  write_paths: string[] | null;
  role_write_paths?: Record<string, string[]>;
  sandbox_registered: boolean;
  coverage_target: {
    workspace: string;
    cwd: string;
    attachment_id: string;
  };
};
export type AttachmentList = {
  items: JobAttachment[];
  research: boolean;
  capabilities: JobCapabilities;
};
export type JobStart = {
  contract_version: 2;
  worker_contract?: 'checked-edits/1';
  acceptance?: CheckedAcceptance;
  role: string;
  task: string;
  attachment: string;
  request_key: string;
  team: string;
  team_revision: WireInteger;
  budget: RunBudget;
  job_policy: {
    proposed_seconds: WireInteger;
    rationale: string;
    auto_escalate: boolean;
  };
  job_coverage?: JobCoverage;
  intent_id?: string;
  project_id?: string;
};
export type ToolJobRow = {
  id: string;
  run_id: string;
  status: JobState;
  role: string;
  task: string;
  model_pin: string;
  backend: string;
  terminal: boolean;
  operation_id: string;
  replayed?: boolean;
  revision: WireInteger;
  base_digest: string;
  output_digest: string | null;
  execution_id: string | null;
  cancellation_operation_id?: string;
};
export type JobSummary = {
  id: string;
  status: JobState;
  role: string;
  task: string;
  attachment: string | null;
  created_at: number | null;
  terminal: boolean;
  proposal: {
    digest: string;
    files: WireInteger;
  } | null;
  decision: string | null;
};
export type JobList = {
  items: JobSummary[];
  total: WireInteger;
  truncated: boolean;
  next_cursor: string | null;
  total_scope: 'page';
};
export type JobAuthority = {
  principal: string;
  project: string;
  grants: string[];
  depth: WireInteger;
  expires_at: number;
};
export type AuditEntry = {
  at: number;
  status: string;
  detail: string;
};
export type LegacyCheckRun = {
  check_id: string;
  ok: boolean;
  exit_code: number;
  proposal_digest: string;
  snapshot_sha256?: string | null;
  image_id?: string | null;
};
export type Receipts = {
  model_attempts: WireInteger;
  model_states: Record<string, WireInteger>;
  usage: Record<string, WireInteger>;
  reported_models: string[];
  tool_calls: WireInteger;
  tool_failures: WireInteger;
  tools: {
    name: string;
    ok: boolean;
    error: string;
  }[];
  check_runs: CheckRun[];
  checks_passed: boolean;
  caveats: string[];
};
export type ProposalFile = {
  path: string;
  operation: 'create' | 'replace' | 'delete';
  pre_sha256: string | null;
  post_sha256: string | null;
  bytes: WireInteger;
  created: boolean;
};
export type Decision = {
  id: string;
  key: string;
  decision: 'approve' | 'deny';
  state: 'approved' | 'denied';
  proposal_digest: string;
  at: number;
  applied: false;
  apply_reason: string;
};
export type JobAudit = ToolJobRow & {
  authority: JobAuthority;
  history: AuditEntry[];
  attachment: string | null;
  principal: Record<string, unknown> | null;
  created_at: number | null;
  error: string | null;
  content: string;
  completion: string | null;
  artifacts: ArtifactReference[];
  evidence: Record<string, unknown>;
  receipts: Receipts;
  proposal: {
    digest: string;
    file: string;
    files: ProposalFile[];
    diff_truncated: boolean;
  } | null;
  decision: Decision | null;
  queue: unknown;
  flags: unknown[];
  next_cursor: WireInteger | null;
};
export type Proposal = {
  required_checks?: string[];
  checks_revision?: string;
  job_id: string;
  digest: string;
  base_digest: string;
  output_digest: string;
  revision: WireInteger;
  output_manifest: SnapshotReference['manifest'];
  files: ProposalFile[];
  diff: string;
  diff_truncated: boolean;
  check_runs: CheckRun[];
  checks_passed: boolean;
  stale_paths: string[];
  applicable: false;
  apply_reason: string;
  decision: Decision | null;
  independently_verified: false;
};
export type JobDecision = {
  contract_version: 2;
  job_id: string;
  decision: 'approve' | 'deny';
  proposal_digest: string;
  request_key: string;
  expected_revision: WireInteger;
};
export type DecisionResult = {
  decision: Decision;
  replayed: boolean;
};
export type ArtifactChunk = {
  filename: string;
  content: string;
  encoding: 'base64';
  sha256: string;
  offset: WireInteger;
  next_offset: WireInteger | null;
  total_bytes: WireInteger;
  truncated: boolean;
};

export type CheckedAcceptance = { criteria: { id: string; required: boolean; output_kind: 'proposal'; checks: { required_check_ids: string[] } }[] };
export type StepKind = 'model_task' | 'workspace_read' | 'workspace_edit' | 'worker_command' | 'registered_check' | 'proposal_publish' | 'await_owner_decision' | 'integrate_approved_proposal';
export type CheckedReceipt = {
  kind: StepKind; run_id: string; step: WireInteger; plan_step: WireInteger; execution_id: string;
  input_digest: string; snapshot_digest: string; workspace_id: string; outcome: string; cleanup: string;
  passed: boolean; worker_receipt: Record<string, unknown>; log_artifact: string;
  started_at: string | null; finished_at: string | null; execution_seconds: number | null;
  selected_seconds: WireInteger; effective_seconds: WireInteger | null; limiting_reason: string | null;
  check_id?: string; definition_digest?: string; definition?: Record<string, unknown>; command_spec_digest?: string;
  tool_revision?: string; exit_status?: number | null; timed_out?: boolean; assertion_summary?: string; truncated?: boolean;
  source_unchanged?: boolean; scratch_digest?: string; observed_snapshot_digest?: string; continuation_blocked_reason?: string;
};
export type CheckRun = LegacyCheckRun | CheckedReceipt;
export type CheckedManifest = { schema_version: 1; kind: 'checked_proposal'; run_id: string;
  step_lineage: (string | WireInteger)[]; criteria_ids: string[]; proposal_digest: string; base_digest: string;
  output_digest: string; digest: string; checks: CheckedReceipt[]; independently_verified: false };
