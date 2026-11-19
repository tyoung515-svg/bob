// Types for the server Bob portal (CONTRACT §3). Everything here is a
// projection of what the broker actually returned; nothing is invented.

export type PortalError = { code: string; detail: string; hint: string; retry_after?: number; retry_after_seconds?: number; applied?: boolean; request_key?: string };

export const RATE_LIMIT_SEND_LINE = "Bob is handling a lot of requests right now, so this wasn't sent. Try again in a few seconds.";
export const RATE_LIMIT_WRITE_LINE = "Bob is handling a lot of requests right now. This wasn't applied. Try again in a few seconds.";
export function isRateLimited(error: Pick<PortalError, 'code'>): boolean {
  return error.code === 'rate_limited' || error.code === 'gateway_http_429';
}

export type BuilderMode = 'suggest' | 'approve' | 'auto' | 'yolo';
export type BuilderNetwork = { allowlist: string[]; project_extra: string[]; revision?: WireInteger };
export type BuilderNetworkState = BuilderNetwork & { project_id: string; revision: WireInteger };
export type BuilderNetworkSet = { project_id: string; add?: string[]; remove?: string[]; expected_revision: WireInteger; request_key: string };
export type BuilderNetworkLogQuery = { conversation_id: string; job_id?: never } | { job_id: string; conversation_id?: never };
export type BuilderNetworkLogEntry = { job_id: string; conversation_id: string; approval_id?: string | null; domain: string; method?: string | null; bytes_in: WireInteger; bytes_out: WireInteger; decision: string };
export type BuilderNetworkLog = { items: BuilderNetworkLogEntry[]; truncated: boolean };
export type WorkspaceReceipt = { status: 'completed'; project_id: string; conversation_id: string; name: string; mode: 'approve'; receipt: { kind: 'workspace'; text: string } };
export type WorkspaceBrowse = { project_id: string; root_label: string; path: string; entries: { name: string; kind: 'dir' | 'file' | 'symlink'; size: number; mtime: number }[]; truncated: boolean };
export type BuilderModeState = { conversation_id: string; project_id: string | null; result_path?: string | null; mode: BuilderMode; expires_at: string | number | null; session_until?: number | null; network: BuilderNetwork; revision: WireInteger; approval_records?: BuilderApproval[]; approvals?: { approval_id: string; conversation_id: string }[]; workspace_receipt?: WorkspaceReceipt | null };
export type BuilderModeSet = { conversation_id: string; mode: BuilderMode; expected_revision: WireInteger; request_key: string; yolo_acknowledged?: true };
export type BuilderDecision = 'allow_once' | 'allow_session' | 'allow_project' | 'deny';
export type PermissionDisplay = { title: string; lines: string[]; scope: string };
export type BuilderExecution = Omit<Partial<WorkspaceReceipt>, 'status'> & { status?: string; exit_code?: number; path?: string; error?: {code?: string}; diagnostics?: Record<string, unknown>; stdout?: string; stderr?: string; detail?: string };
export type BuilderApproval = {
  schema_version: 1; approval_id: string; conversation_id: string;
  kind: 'write' | 'edit' | 'command' | 'network' | 'workspace'; summary: string;
  display?: PermissionDisplay;
  detail: { path?: string; diff?: string; command?: string; cwd?: string; network?: boolean; domain?: string; method?: string | null; action?: 'create' | 'mount'; name?: string; project_id?: string; from_project_id?: string | null; root_label?: string };
  mode: BuilderMode; expires_at: string | number; session_until?: number | null; state: 'pending' | 'allowed' | 'denied' | 'expired' | 'stopped';
  execution_state?: 'pending' | 'running' | 'completed' | 'failed' | 'stopped' | 'unknown';
  outcome?: string;
  result?: BuilderExecution | null;
};
export type BuilderApprovalResult = { approval_id: string; state: BuilderApproval['state']; execution_state?: BuilderApproval['execution_state']; result_ref?: string; result?: BuilderExecution };
export type BuilderPreviewData = { path: string; mime: string; bytes_b64: string | null; size: number; truncated: boolean; provenance?: { source: 'workspace'; modified_at: number; sha256: string | null } };

export const builderModes: BuilderMode[] = ['suggest', 'approve', 'auto', 'yolo'];
export const builderTime = (value: string | number | null) => value === null ? NaN : typeof value === 'number' ? value * 1000 : Date.parse(value);

/** Native code rejects with a JSON string `{code,detail,hint}`; keep it typed. */
export function toPortalError(error: unknown): PortalError {
  const text = typeof error === 'string' ? error : error instanceof Error ? error.message : txt(error, 'Unknown error');
  try {
    // Workspace clients already throw typed objects. Coercing one with String
    // loses the server's error and displays the literal [object Object].
    const parsed = (error && typeof error === 'object' && !(error instanceof Error) ? error : JSON.parse(text)) as Partial<PortalError>;
    if (parsed && typeof parsed.code === 'string') {
      const limited = isRateLimited({ code: parsed.code });
      const seconds = parsed.retry_after_seconds ?? parsed.retry_after;
      return {
        code: limited ? 'rate_limited' : parsed.code,
        detail: limited ? RATE_LIMIT_WRITE_LINE : txt(parsed.detail, ''),
        hint: limited ? '' : typeof parsed.hint === 'string' ? parsed.hint : '',
        ...(limited ? { applied: false } : typeof parsed.applied === 'boolean' ? { applied: parsed.applied } : {}),
        ...(Number.isSafeInteger(seconds) && seconds! >= 0 && seconds! <= 86400 ? { retry_after_seconds: seconds, retry_after: seconds } : {}),
        ...(typeof parsed.request_key === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(parsed.request_key) ? { request_key: parsed.request_key } : {}),
      };
    }
  } catch {
    /* not a typed portal error */
  }
  return { code: 'app_error', detail: text, hint: 'The native app reported an error with no portal code.' };
}

export function isTransportFault(error: PortalError): boolean {
  return error.code === 'transport_unavailable' || error.code === 'timeout_no_reply' || error.code === 'protocol_invalid';
}

// After a write has been sent, only a code that means "the server answered and
// refused" may be shown as a refusal. Every other code — other `gateway_http_*`,
// `internal_error`, `invalid_json`, `response_oversize`, and any code this build
// does not know — leaves the outcome unknown: the write may have landed. Unknown
// codes therefore fail toward ambiguous, never toward "applied nothing".
const DEFINITE_REFUSALS: ReadonlySet<string> = new Set([
  'not_dispatched',
  'bad_request',
  'approval_foreign',
  'approval_stale',
  'approval_not_found',
  'approval_not_permitted',
  'invalid_decision',
  'unsupported_decision_fields',
  'idempotency_key_required',
  'proposal_preimage_changed',
  'proposal_digest_mismatch',
  'proposal_already_decided',
  'attachment_root_changed',
  'role_no_longer_registered',
  'path_outside_current_write_scope',
  'path_outside_token_scope',
  'check_request_changed',
  'attachment_scope_unavailable',
]);

// True only for a server-answered, definite refusal. An unlisted code is NOT a
// refusal (fail-safe direction). HTTP 429 is the known pre-handler exception.
export function isDefiniteRefusal(error: PortalError): boolean {
  return isRateLimited(error) || DEFINITE_REFUSALS.has(error.code);
}

export type Face = { id: string; name: string; /** false = registered but outside this portal token's allow-list (W-5). */ allowed?: boolean };
export type ModelChoice = { model: string; label: string; default: boolean };
export type ModelEntry = {
  id: string; backend: string; label?: string | null; model: string | null; available: boolean; reason: string | null;
  provider_label?: string | null; choices?: ModelChoice[]; tool_capable?: boolean; billing?: 'subscription' | 'api_key' | null;
};
export type BackendEntry = { id: string; label?: string | null; model?: string | null; kind: string; available: boolean; unavailable_reason: string | null };

export type Identity = {
  sub: string;
  owner_controls_eligible?: boolean;
  token_type?: 'human' | 'agent' | 'portal';
  actor?: string;
  owner?: string;
  gateway: string;
  faces: Face[];
  may_touch: string[];
  may_not_touch: string[];
  expires_at: string;
};

export type Hello = {
  identity: Identity;
  server: { boot: string; version: string };
  /** The broker may return strings or structured rows ({area,code,text}); never assume. */
  gaps: unknown[];
  operations?: Record<string, OperationCapability>;
  owner_session?: OwnerSession;
};

export type Catalog = {
  faces: Face[];
  models: ModelEntry[];
  backends: BackendEntry[];
  capabilities: {
    operations?: Record<string, OperationCapability>;
    execution_v2?: ExecutionV2 | null;
    legacy_teams?: { id: string }[];
    execution_models?: { id: string; available?: boolean; reason?: string; enabled?: boolean }[];
    faces: number;
    backends: number;
    actions: unknown[];
    warnings: string[];
    /** ids this portal token may use; empty means the token is unrestricted (W-5). */
    allowed_face_ids?: string[];
    faces_allowed?: number;
  };
};

export type Conversation = {
  id: string;
  title: string;
  face_id: string | null;
  backend: string | null;
  model: string | null;
  project_id: string | null;
  created_at?: string;
  updated_at?: string;
};

export type Message = {
  id: string;
  role: string;
  content: string;
  created_at: string;
  metadata?: unknown;
};

export type Pins = {
  face_id: string | null;
  backend: string | null;
  model: string | null;
  profile: string | null;
  locale: string | null;
};

export type Approval = {
  id: string;
  conversation_id: string;
  action_type: string;
  details: unknown;
  status: string;
  created_at: string;
  user_id: string;
};

export type DecisionResult = { decision: string; agent_resume: string; approval: Approval };

export type Project = {
  id: string;
  name: string;
  description: string;
  instructions: string;
  default_face_id: string | null;
  default_backend: string | null;
  conversation_count: number;
  updated_at: string;
};

export type RunReceipt = {
  role: string;
  step?: number;
  output_digest?: string;
  check_receipts?: unknown;
  kind?: import('./workspace-v2-protocol').StepKind;
  model_pin?: string | null;
  model_face?: string | null;
  seat?: string;
  elapsed_seconds?: number;
  output?: string;
  usage?: unknown;
};

export type ExecutionV2 = {
  contract_versions: number[];
  topologies?: string[];
  start_calls?: number[];
  confirmation_threshold?: number;
  above_threshold_reason?: string;
  worker_loop?: { contracts: string[]; topology: string; step_kinds: string[]; max_depth?: number; max_ready_wave?: number };
  capabilities_revision?: string;
  execution_enabled?: boolean;
  storage_available?: boolean;
  blocked_reasons?: string[];
  job_time?: JobTimeCapability;
  growth?: { presets: number[]; topologies?: string[]; above_200?: string; output_basis?: string };
  owner_operations?: Partial<Record<'runs.resume' | 'budgets.set', string>>;
  project_pools?: boolean;
  follow_budget_seconds_per_call?: number;
};
export type RunBudget = { start_calls: WireInteger; wall_mode: 'fixed' | 'follow_budget' | 'none'; wall_seconds: WireInteger | null; max_job_attempts?: WireInteger; job_seconds_budget?: WireInteger | null };
export type AcceptanceCriterion = {
  id: string; required: boolean; output_kind: 'answer' | 'report' | 'proposal' | 'json';
  checks: { min_length?: number; max_length?: number; required_sections?: string[]; require_citations?: boolean; json_type?: 'object' | 'array' | 'string' | 'number' | 'boolean' | 'null' };
};
export type AcceptanceContract = { criteria: AcceptanceCriterion[] };
export type AcceptedOutput = { output_id: string; digest: string; step: number; step_lineage?: string[]; criteria_ids: string[]; check_receipts: unknown };
export type OutputReceipt = { id: string; ordinal: number; digest: string; content: string; criteria_ids: string[]; check_receipts: unknown };
export type ProjectCallBudget = { project_id: string; configured?: boolean; call_limit: WireInteger | null; revision: WireInteger | null; consumed?: WireInteger; reserved?: WireInteger; remaining?: WireInteger | null; observed_at?: string; counter_coverage?: string };
export type TeamDefinition = {
  schema_version: 1; id: string; state?: 'draft' | 'saved'; topology: string;
  roles: Partial<Record<'manager' | 'worker' | 'critic', string | null>>;
  review_independence?: 'same_model' | 'different_model';
  permitted_tools?: string[]; job_profiles?: string[]; max_parallelism?: number;
};
export type SavedTeam = TeamDefinition & {
  revision: number; eligibility: 'runnable' | 'blocked'; runnable: boolean; blocked_reasons: string[];
};
export type TeamsPage = { items: SavedTeam[]; next_cursor: string | null };
export type TeamSaveResult = { team: SavedTeam; saved: boolean; revision: number; operation_id: string };
export type ServerRunsPage = { items: Run[]; next_cursor: string | null; source: 'server'; coverage: string };
export type RunEvent = { seq: number; kind: string; detail: unknown; at: string };
export type RunDetail = { run: Run; events: RunEvent[]; receipts: RunReceipt[]; next_cursor: number | null; outputs?: OutputReceipt[]; next_receipt_cursor?: number | null };

export type Run = {
  worker_contract?: string | null;
  job_account?: JobAccount | null;
  flags?: FlagSummary[];
  failure_recovery?: unknown[];
  failure_recovery_truncated?: boolean;
  job_waits?: JobWait[];
  conversation_id?: string | null;
  selected_grant_id?: string | null;
  revision?: WireInteger;
  topology?: string;
  project_id?: string | null;
  acceptance?: AcceptanceContract | string;
  accepted_outputs?: AcceptedOutput[];
  growth_events?: { seq: number; detail: { old_limit?: number; new_limit?: number; reason?: string; [key: string]: unknown } }[];
  pause_reason?: string | null;
  owner_flag?: boolean;
  pool_holds?: { kind: string; pool_id: string; remaining: number }[];
  remaining_calls_basis?: string;
  contract_version?: 2;
  phase?: string;
  requested_start_calls?: WireInteger;
  effective_call_limit?: WireInteger;
  calls_used?: WireInteger;
  reserved_calls?: WireInteger;
  unknown_calls?: WireInteger;
  estimated_remaining_calls?: WireInteger | null;
  deadline?: string | null;
  reason?: string | null;
  created_at?: string;
  task_excerpt?: string;
  budget?: RunBudget;
  result?: string | null;
  review_independence?: string;
  id: string;
  status: string;
  team?: string;
  task?: string;
  max_model_calls?: number;
  max_seconds?: number | null;
  independently_verified?: boolean;
  receipts?: RunReceipt[];
  error?: string | null;
};

/** Calendar projection. Legacy rows cover only this portal's starts; v2 rows
 * come from the authorized server ledger and retain their distinct source. */
export type LedgerRun = {
  run_id: string;
  team?: string | null;
  worker?: string | null;
  task_excerpt?: string | null;
  started_at: string;
  last_status: string;
  last_checked_at: string;
  receipts: RunReceipt[];
  source: 'portal_run_ledger' | 'legacy_portal_ledger' | 'server';
};

export type RunsList = {
  source?: 'legacy_portal_ledger';
  items: LedgerRun[];
  total: number;
  limit: number;
  offset: number;
  scope?: { mechanism?: string; row_owner_attributable?: boolean };
  caveats?: string[];
};

export type MemoryStatus = {
  authority?: string;
  coverage_note?: string;
  complete_corpus?: boolean;
  [key: string]: unknown;
};

export type MemoryHit = {
  source_id: string;
  owner: string;
  kind: string;
  provenance: string | Record<string, unknown>;
  revision?: string;
  excerpt: string;
  truncated: boolean;
};

export type MemoryPage = { items: MemoryHit[]; next_cursor: string | null; snapshot: string; coverage: MemoryStatus };
export type ServerRoot = { attachment_id: string; project_id: string; label: string; read_only: true };
export type ServerEntry = { name: string; path: string; dir: boolean; size: number };
export type ServerFileChunk = { content: string; encoding: 'utf-8' | 'base64'; revision: string; truncated: boolean; next_offset: number | null };

export type ProfileRow = {
  id: string;
  label: string;
  transport: string;
  execution_host: string;
  effect_owner: string;
  available: boolean;
  reason: string | null;
  note: string;
};

export type ProfilesPayload = {
  developer_mode?: boolean;
  developer_fixture?: boolean;
  notice?: string | null;
  schema: string;
  active: string;
  profiles: ProfileRow[];
  data_root: string;
  settings: { path: string; hardened: boolean; schema: string };
  // `mode`/`lane` come from the native layer; in `this_pc` mode `ssh` holds the local runtime
  // launcher and `broker` the in-runtime broker name, not an SSH program or a server path.
  transport: { kind: string; targets: string[]; ssh: string; broker: string; mode?: string; lane?: string };
};

export const PORTAL_EVENT = 'portal://event';

/** Render a server value safely: a React child must never be a raw object. */
export function txt(value: unknown, fallback = 'not reported'): string {
  if (typeof value === 'string') return value.length > 0 ? value : fallback;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === null || value === undefined) return fallback;
  try {
    const text = JSON.stringify(value);
    return text.length > 600 ? `${text.slice(0, 600)}…` : text;
  } catch {
    return fallback;
  }
}

/** One line for a gap row, whether the broker sent a string or a structured object. */
export function gapText(gap: unknown): string {
  if (typeof gap === 'string') return gap;
  if (gap && typeof gap === 'object') {
    const row = gap as Record<string, unknown>;
    const parts = [row.area, row.code, row.text]
      .filter((value): value is string => typeof value === 'string' && value.length > 0)
      .map((value) => value.trim());
    if (parts.length > 0) return parts.join(' · ');
  }
  return txt(gap, 'unreadable gap row');
}

/** Provenance string for a rendered server message (never invented). */
export function messageProvenance(conversationId: string, messageId: string): string {
  return `bob:conversation:${conversationId}:message:${messageId}`;
}

/** CONTRACT §5 gaps, shown as explicit unsupported rows. */
export const CONTRACT_GAPS: { title: string; detail: string }[] = [
  {
    title: 'No build_request ingress at the gateway',
    detail: 'The app cannot submit a contract build. There is no control for it.',
  },
  {
    title: 'Artifacts are scoped to authorized Jobs',
    detail: 'Jobs provide bounded manifest-scoped readback. There is no general artifact browser.',
  },
  {
    title: 'No run DAG, parent-branch API or attach/replay cursor',
    detail: 'Execution v2 provides a server run list. Legacy connections expose only their portal ledger.',
  },
  {
    title: 'No structured tool event stream inside normal chat',
    detail: 'Tool provenance appears only from the server’s own message fields; otherwise it is shown as "not provided".',
  },
  {
    title: 'No idempotent send and no resend handshake',
    detail: 'After an unknown send outcome this app offers inspect-history and never retries automatically.',
  },

  {
    title: 'Local-runtime deployment on this PC is not delivered by this program',
    detail: 'The existing local/WSL profile is kept and labelled honestly; it is not the server Bob.',
  },
];

// Signed-64-bit counters are JSON numbers only when exactly representable.
export type WireInteger = number | string;
export type OperationCapability = { include_expired?: boolean; readable?: boolean; writable?: boolean; runnable?: boolean; version?: number; contract_versions?: number[]; implemented?: boolean; available?: boolean; reason?: string | null; integer_encoding?: string; job_grants_available?: boolean; job_grants_reason?: string | null; job_grant_matcher_version?: number; providers?: string[]; provider_display?: { id: string; label: string }[]; enrolls_backend?: boolean; storage_only?: boolean };
export type SecretReceipt = { provider: string; stored: true; fingerprint: string; operation_id: string };
export type PasswordResult = { changed: boolean; restarted: boolean };
export type SettingsStatus<T = SecretReceipt> = { operation_id: string; phase: 'prepared' | 'committed' | 'restart-pending' | 'ready' | 'failed'; result?: T; error?: string };
export type PolicyRule = { attachment_id: string; path: string };
export type ApprovalPolicy = { posture: 'inherit' | 'confirm' | 'scoped'; scope: { auto_actions: string[]; escalate_actions: string[]; may_touch: PolicyRule[]; may_not_touch: PolicyRule[] } };
// Effective paths are resolved by the server, and must never be resubmitted as attachment rules.
export type PolicyProjection = { policy: { posture: ApprovalPolicy['posture']; scope: { auto_actions: string[]; escalate_actions: string[]; may_touch: string[]; may_not_touch: string[] } } | null; revision: WireInteger; immutable_restrictions: Record<string, unknown>; operation_id?: string; paused_runs?: string[] };
export type OwnerSession = { active: boolean; owner?: string; actor?: string; lane?: string; expires_at?: number; allowed_operations?: string[]; credential_epoch?: WireInteger };
export type OwnerChallenge = { challenge_id: string; expires_at: number; summary: unknown; confirmation_mode?: 'password_totp' | 'signed_in_user' };
export type GrantProposal = { proposal_id: string; intent_id?: string; revision: WireInteger; digest: string; coverage: { request: { project_id?: string; budget: RunBudget; team: string; [key: string]: unknown }; snapshot: unknown; initiator: unknown; planned_run_id: string; matcher_version?: number } };
export type ExecutionGrant = JobGrantPolicy & { job_coverage?: JobCoverage | null; matcher_version?: number; id: string; revision: WireInteger; coverage_kind: 'task' | 'project'; project_id?: string | null; run_id?: string | null; auto_escalate: boolean; call_ceiling: WireInteger | null; expires_at: string | null; revoked?: boolean; [key: string]: unknown };

export type JobTimeCapability = {
  version: number; available?: boolean; reason?: string | null; presets_seconds: number[];
  installed_max_seconds?: WireInteger; profile_revisions?: Record<string, string>;
  max_job_attempts_default?: WireInteger; job_seconds_budget?: boolean; matcher_version?: number;
  agent_proposes?: boolean; owner_override?: string; strict_limit?: boolean; finite_custom?: string;
  above_3600?: string; custom?: string; continuation?: string; cli_usage?: string; codex_cli?: boolean; jobs_api?: boolean;
};
export type JobCoverage = {
  profiles: Record<string, string>; workspaces: Record<string, string[]>; attachments: string[];
  max_job_attempts: WireInteger; job_seconds_budget: WireInteger | null; allow_cli: false;
};
export type JobGrantPolicy = {
  allow_extended_jobs?: boolean; allow_custom_job_limit?: boolean; job_seconds_ceiling?: WireInteger | null;
  job_override_seconds?: WireInteger | null; job_auto_escalate?: boolean;
};
// Job-time quantities can be measured fractional seconds; integer counters retain wire precision.
export type JobAccount = {
  max_job_attempts: WireInteger; job_seconds_budget: WireInteger | null;
  dispatched: WireInteger; held: WireInteger; completed: WireInteger; unknown: WireInteger;
  seconds_used: WireInteger; seconds_held: WireInteger; cleanup_seconds: WireInteger; cli_usage_basis: string;
};
export type JobWait = {
  execution_id: string; state: string; proposed_seconds?: WireInteger; rationale?: string;
  owner_override?: WireInteger | null; selected_tier?: WireInteger; effective_seconds?: WireInteger | null;
  limiting_reason?: string; logical_job_id?: string; parent_execution_id?: string | null;
  timeout_episode?: WireInteger; reconciliation?: string; grant_id?: string | null; grant_revision?: WireInteger;
  blocked_by_timeout_reconciliation?: boolean; blocked_by_unknown_outcome?: boolean; blocked_by_cleanup?: boolean;
  continuation_requires_reconciliation?: boolean;
};
export type FlagSummary = {
  id: string; run_id: string; episode_id?: string | null; ordinal?: WireInteger | null;
  cause: string; phase: string; revision: WireInteger; status: 'open' | 'resolved';
  created_at: string; updated_at: string; resolution?: unknown;
};
export type FailureFlag = FlagSummary & {
  project_id?: string | null; conversation_id?: string | null; scope: unknown; policy: unknown;
  budget: unknown; stopped_context: unknown; usage_estimates: unknown; effects: string;
  cleanup_pending: boolean; cleanup_source?: string;
  live_job_cleanup: { execution_id: string; outcome: string; cleanup: string; reconciliation: string; cancel_requested: boolean }[];
  safe_next_actions: string[]; action_contract?: Record<string, string>; episode: unknown;
  attempts: unknown[]; output_links: unknown; trail_ref?: unknown; resolution_does_not_resume: true;
};
export type FlagsPage = { items: FlagSummary[]; next_cursor: string | null; source: 'server' };
export type FlagDetail = { flag: FailureFlag; run: Pick<Run, 'id' | 'status' | 'revision' | 'project_id' | 'selected_grant_id'> & { conversation_id?: string | null }; events: RunEvent[]; next_cursor: WireInteger | null; next_job_cursor: string | null };
export type FlagDecision = 'inspected' | 'resume_with_guidance' | 'authorize_budget' | 'cancel';
export type FlagResolution = { flag_id: string; run_id: string; resolved: true; run_resumed: false; operation_id: string };

// Recovery IPC is deliberately factor-free: secrets remain in trusted native surfaces.
export type RecoveryPair = 'setup' | 'password_stick' | 'password_totp' | 'totp_stick';
export type RecoveryAction = 'seed' | 'usb' | 'print' | 'commit' | 'cancel' | 'check' | 'finish';
export type RecoveryStatus = { handle: string; pair: RecoveryPair | 'restored'; phase: 'unknown' | 'proof_verified' | 'material_saved' | 'activated' | 'not_changed' | 'cancelled'; expires_at: number; saved: boolean; material_available?: boolean; outcome: PasswordResult | null };
export type FactorPrepared = { challenge_id: string; expires_at: number; state: 'proof_verified' };
export type FactorBinding = { installation_id: string; lane_id: string; owner: string };
