// Workspace tool-job shapes, exactly as the gateway's /api tool-job sub-app
// returns them through the broker's fixed jobs.* ops. Nothing here is
// client-authoritative: identity, project roots and permissions are server state.

export type JobAttachment = {
  id: string;
  label: string;
  roles: string[];
  checks: string[];
  write_paths: string[] | null;
  sandbox_registered: boolean;
};

export type AttachmentList = { items: JobAttachment[]; research: boolean };

export type ToolJobRow = {
  id: string;
  status: string;
  role: string;
  task?: string;
  model_pin: string;
  backend: string;
  terminal?: boolean;
  operation_id: string;
  replayed?: boolean;
};

export type JobSummary = {
  id: string;
  status: string;
  role: string;
  task: string;
  attachment: string | null;
  created_at: number | null;
  terminal: boolean;
  proposal: { digest: string; files: number } | null;
  decision: string | null;
};

export type JobList = { items: JobSummary[]; total: number; truncated: boolean };

export type JobAuthority = {
  principal: string;
  project: string;
  grants: string[];
  depth: number;
  expires_at: number;
};

export type AuditEntry = { at: number; status: string; detail: string };

export type CheckRun = {
  check_id: string;
  ok: boolean;
  exit_code: number;
  proposal_digest: string;
  snapshot_sha256?: string | null;
  image_id?: string | null;
};

export type Receipts = {
  model_attempts: number;
  model_states: Record<string, number>;
  usage: Record<string, number>;
  reported_models: string[];
  tool_calls: number;
  tool_failures: number;
  tools: { name: string; ok: boolean; error: string }[];
  check_runs: CheckRun[];
  checks_passed: boolean;
  caveats: string[];
};

export type ProposalFile = {
  path: string;
  pre_sha256: string | null;
  post_sha256: string;
  bytes: number;
  created: boolean;
};

export type Decision = {
  id?: string;
  // The idempotency request key of the write that produced this row.
  key?: string;
  decision: 'approve' | 'deny';
  state: string;
  proposal_digest: string;
  at?: number;
  outcomes?: { path?: string; state: string; post_sha256?: string; error?: string }[];
};

export type JobAudit = {
  id: string;
  status: string;
  operation_id: string;
  authority: JobAuthority;
  role: string;
  model_pin: string;
  backend: string;
  terminal: boolean;
  history: AuditEntry[];
  task: string;
  attachment: string | null;
  principal: Record<string, unknown> | null;
  created_at: number | null;
  error: string | null;
  content: string;
  completion: string | null;
  artifacts: { name: string; file: string; sha256: string }[];
  evidence: Record<string, { url: string; sha256: string; file: string; summary: string }>;
  receipts: Receipts;
  proposal: { digest: string; file: string; files: ProposalFile[]; diff_truncated: boolean } | null;
  decision: Decision | null;
};

export type Proposal = {
  job_id: string;
  digest: string;
  files: ProposalFile[];
  diff: string;
  diff_truncated: boolean;
  check_runs: CheckRun[];
  checks_passed: boolean;
  stale_paths: string[];
  applicable: boolean;
  decision: Decision | null;
};

export type DecisionResult = { decision: Decision; replayed: boolean };
