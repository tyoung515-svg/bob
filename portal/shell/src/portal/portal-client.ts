import type { WorkflowCatalog, WorkflowRecord, WorkflowDefinition, WorkflowProposal, WorkflowPropose, WorkflowStart, WorkflowRun, WorkflowSummary, WorkflowEvent, WorkflowPage } from './workflow-types';
import { validCheckedRunDetail } from './checked-edits';
import { createWorkspaceClient } from './workspace-client';
import type { NativeInvoke } from '../workbench/workbench-client';
import { isRateLimited, toPortalError } from './portal-protocol';
import { observeRestartLink } from './provider-restart';
import { checkedModelCatalog, modelGeneration, notifyModelChanged, retryModelRead } from './model-reload';
import type {
  Approval,
  Catalog,
  Conversation,
  DecisionResult,
  Hello,
  MemoryStatus,
  MemoryPage,
  Message,
  Pins,
  ProfilesPayload,
  Project,
  Run,
  RunsList,
  RunBudget, RunDetail, ServerRunsPage, TeamDefinition, TeamsPage, TeamSaveResult,
  JobCoverage, JobGrantPolicy, FlagsPage, FlagDetail, FlagDecision, FlagResolution, AcceptanceContract, ProjectCallBudget, WireInteger, OwnerChallenge, OwnerSession, GrantProposal, ExecutionGrant,
  RecoveryPair, RecoveryAction, RecoveryStatus, PasswordResult, SecretReceipt, SettingsStatus, ApprovalPolicy, PolicyProjection,
  BuilderModeState, BuilderModeSet, BuilderApproval, BuilderApprovalResult, BuilderDecision, BuilderPreviewData,
  BuilderNetworkSet, BuilderNetworkState, BuilderNetworkLogQuery, BuilderNetworkLog, WorkspaceBrowse,
} from './portal-protocol';

export type SendArgs = {
  conversation_id: string;
  content: string;
  face_id?: string;
  model?: string;
  backend?: string;
  profile?: string;
  locale?: string;
  research?: boolean;
};

export type RunStartArgs = { team: string; task: string; max_model_calls?: number; max_seconds?: number };
export type RunStartV2 = {
  contract_version: 2; team: string; team_revision: number; task: string; request_key: string; budget: RunBudget;
  job_coverage?: JobCoverage;
  intent_id?: string; project_id?: string; conversation_id?: string; evidence_ids?: string[]; acceptance?: AcceptanceContract | string;
};
export type RunResumeV2 = { contract_version: 2; run_id: string; expected_revision: WireInteger; request_key: string; budget?: Partial<RunBudget> };
export type BudgetSetV2 = { contract_version: 2; project_id: string; call_limit: WireInteger; expected_revision: WireInteger | null; request_key: string };

export type ProjectSaveArgs = {
  id?: string;
  name?: string;
  description?: string;
  instructions?: string;
  default_face_id?: string | null;
  default_backend?: string | null;
  delete?: boolean;
};

type OpenedConversation = { conversation: Conversation; messages: Message[]; has_more: boolean };
type ConversationPage = { items: Conversation[]; limit: number; offset: number; excluded: number };
type MessagePage = { items: Message[]; has_more: boolean };

export function createPortalClient(nativeInvoke: NativeInvoke) {
  // Reads also feed the existing provider-restart wording observer.
  const healthReads = new Set(['portal_hello', 'portal_catalog', 'portal_conversations', 'portal_conversation_open',
    'portal_builder_mode_get_v2', 'portal_builder_approval_get_v2', 'portal_owner_status_v2',
    'portal_workspace_browse_v2', 'portal_builder_preview_v2', 'portal_builder_network_log_v2',
    'portal_projects', 'portal_pins', 'portal_messages_more', 'portal_approvals', 'portal_approval_get',
    'portal_memory_status', 'portal_memory_list', 'portal_memory_search', 'portal_memory_read',
    'portal_teams_list_v2', 'portal_runs_list_v2', 'portal_run_get_v2', 'portal_runs', 'portal_run_get']);
  // Explicit read allow-list: new/unknown commands and every write get one attempt.
  const reads = new Set([...healthReads, 'portal_runs_list', 'portal_profiles',
    'portal_flags_list_v2', 'portal_flags_get_v2', 'portal_grants_list_v2', 'portal_grants_get_v2',
    'portal_budgets_get_v2', 'portal_budgets_list_v2', 'portal_policy_get_v2',
    'portal_recovery_capability_v2', 'portal_recovery_status_v2', 'portal_password_pending_v1',
    'portal_settings_status_v1', 'portal_work_catalog_v2', 'portal_work_status_v2',
    'portal_jobs_attachments', 'portal_jobs_attachments_v2', 'portal_jobs_list', 'portal_jobs_list_v2', 'portal_jobs_audit', 'portal_jobs_audit_v2',
    'portal_jobs_proposal', 'portal_jobs_proposal_v2', 'portal_jobs_artifact', 'portal_jobs_artifact_v2']);
  for (const name of ["catalog", "list", "get", "proposal", "proposals", "runs", "run", "events", "artifact"]) reads.add('portal_workflow_' + name + '_v1');
  const flights = new Map<string, Promise<unknown>>();
  const invoke: NativeInvoke = <T,>(command: string, args?: Record<string, unknown>): Promise<T> => {
    const read = reads.has(command);
    const key = JSON.stringify([command, args, modelGeneration(nativeInvoke)]);
    if (read && flights.has(key)) return flights.get(key) as Promise<T>;
    const flight = (async () => {
      for (let attempt = 0; ; attempt++) {
        try {
          const value = read
            ? await retryModelRead(nativeInvoke, async () => {
              const value = await nativeInvoke<T>(command, args);
              if (command === 'portal_catalog') checkedModelCatalog(value as Catalog);
              return value;
            }, command === 'portal_catalog')
            : await nativeInvoke<T>(command, args);
          if (healthReads.has(command)) observeRestartLink(client, true);
          return value;
        } catch (error) {
          const typed = toPortalError(error);
          if (read && isRateLimited(typed) && attempt < 2) {
            await new Promise(resolve => setTimeout(resolve, Math.max(250, (typed.retry_after_seconds ?? 2 ** attempt * 2) * 1000)));
            continue;
          }
          if (healthReads.has(command)) observeRestartLink(client, false, typed);
          throw isRateLimited(typed) ? JSON.stringify(typed) : error;
        }
      }
    })();
    if (read) {
      flights.set(key, flight);
      void flight.finally(() => { if (flights.get(key) === flight) flights.delete(key); }).catch(() => {});
    }
    return flight;
  };
  async function v2<T>(command: string, args: Record<string, unknown>): Promise<T> {
    try {
      const value = await invoke<T>(command, args);
      if (['portal_run_get_v2', 'portal_runs_start_v2', 'portal_run_resume_v2', 'portal_run_cancel_v2'].includes(command) && !validCheckedRunDetail(value)) throw JSON.stringify({ code: 'protocol_invalid', detail: 'Invalid checked run evidence; nothing was assumed.' });
      return value;
    }
    catch (error) {
      const typed = toPortalError(error);
      // The shipping HTTP bridge returns the server's error code as detail.
      // Translate only known pre-admission refusals; opaque HTTP/transport
      // failures keep the original unknown-outcome semantics and request key.
      if (/^gateway_http_4\d\d$/.test(typed.code) && [
        'same_model_review_choice_required', 'idempotency_conflict', 'workflow_unavailable', 'workflow_contract_required', 'invalid_state', 'budget_exhausted', 'unsupported_provider', 'invalid_key', 'request_key_used', 'invalid_policy', 'invalid_policy_action', 'invalid_policy_path', 'resource_not_found',
        'confirmation_required', 'team_blocked', 'revision_conflict', 'invalid_integer',
        'invalid_wall_mode', 'conflicting_wall_fields', 'insufficient_call_allowance',
        'unsupported_run_contract', 'client_upgrade_required', 'unsupported_fields',
        'execution_executor_unavailable', 'invalid_args', 'below_committed_budget', 'run_not_resumable',
        'authorization_revoked', 'unsettled_attempts', 'wall_extension_required', 'not_found', 'forbidden',
        'job_coverage_required', 'job_coverage_budget_conflict', 'invalid_job_coverage', 'invalid_job_limit', 'job_grant_required', 'extended_job_grant_required', 'custom_job_grant_required', 'extended_job_requires_long_runs', 'installed_job_limit', 'owner_job_ceiling', 'job_escalation_disabled', 'cli_jobs_unavailable', 'flag_not_found', 'invalid_flag_decision', 'proposal_stale', 'invalid_coverage', 'explicit_grant_policy_required', 'invalid_expiry', 'project_required', 'invalid_grant_policy', 'job_grants_unavailable', 'replacement_grant_attachment_unavailable', 'admission_intent_conflict', 'credential_epoch_changed', 'owner_controls_unavailable', 'owner_confirmation_required', 'owner_session_invalid', 'owner_session_expired', 'budget_in_use', 'operation_unavailable', 'owner_ceiling', 'pool_shortfall', 'invalid_text', 'invalid_acceptance', 'required_acceptance_missing',
      ].includes(typed.detail)) throw JSON.stringify({ ...typed, code: typed.detail });
      throw error;
    }
  }
  const client = {
    modelsChanged: () => notifyModelChanged(nativeInvoke),
    workflowCatalog: () => v2<WorkflowCatalog>('portal_workflow_catalog_v1', { contract_version: 1 }),
    workflowList: (cursor?: string) => v2<WorkflowPage<WorkflowRecord>>('portal_workflow_list_v1', { contract_version: 1, ...(cursor ? {cursor} : {}) }),
    workflowGet: (id: string, revision?: WireInteger) => v2<{definition: WorkflowRecord}>('portal_workflow_get_v1', { contract_version: 1, id, ...(revision === undefined ? {} : {revision}) }),
    workflowSave: (args: { id?: string; definition: WorkflowDefinition; expected_revision: WireInteger | null; request_key: string }) => v2<{definition: WorkflowRecord}>('portal_workflow_save_v1', { contract_version: 1, ...args }),
    workflowCopy: (args: { id: string; revision: WireInteger; name: string; request_key: string }) => v2<{definition: WorkflowRecord}>('portal_workflow_copy_v1', { contract_version: 1, ...args }),
    workflowDelete: (args: { id: string; expected_revision: WireInteger; request_key: string }) => v2<{id: string; revision: WireInteger; deleted: boolean}>('portal_workflow_delete_v1', { contract_version: 1, ...args }),
    workflowPropose: (args: WorkflowPropose) => v2<{proposal: WorkflowProposal}>('portal_workflow_propose_v1', { contract_version: 1, ...args }),
    workflowProposal: (proposal_id: string) => v2<{proposal: WorkflowProposal}>('portal_workflow_proposal_v1', { contract_version: 1, proposal_id }),
    workflowProposals: (cursor?: string, limit = 20, includeExpired = false) => v2<WorkflowPage<WorkflowProposal & {kind: 'workflow_proposal'}>>('portal_workflow_proposals_v1', { contract_version: 1, ...(cursor ? {cursor} : {}), limit, ...(includeExpired ? {include_expired: true} : {}) }),
    workflowProposeAgain: (proposal_id: string) => v2<{proposal: WorkflowProposal}>('portal_workflow_propose_again_v1', { contract_version: 1, proposal_id }),
    workflowStart: (args: WorkflowStart) => v2<{run: WorkflowRun; grant_id: string}>('portal_workflow_start_v1', { contract_version: 1, ...args }),
    workflowRuns: (cursor?: string) => v2<WorkflowPage<WorkflowSummary>>('portal_workflow_runs_v1', { contract_version: 1, ...(cursor ? {cursor} : {}) }),
    workflowRun: (run_id: string, stage_cursor?: string) => v2<{run: WorkflowRun}>('portal_workflow_run_v1', { contract_version: 1, run_id, ...(stage_cursor ? {stage_cursor} : {}) }),
    workflowEvents: (run_id: string, after_seq: WireInteger, limit = 100) => v2<{events: WorkflowEvent[]; next_seq: WireInteger; has_more: boolean}>('portal_workflow_events_v1', { contract_version: 1, run_id, after_seq, limit }),
    workflowPause: (args: {run_id: string; expected_revision: WireInteger; request_key: string}) => v2<{run: WorkflowRun}>('portal_workflow_pause_v1', {contract_version: 1, ...args}),
    workflowResume: (args: {run_id: string; expected_revision: WireInteger; wait_id?: string; request_key: string}) => v2<{run: WorkflowRun}>('portal_workflow_resume_v1', {contract_version: 1, ...args}),
    workflowStop: (run_id: string, request_key: string) => v2<{run: WorkflowRun}>('portal_workflow_stop_v1', {contract_version: 1, run_id, request_key}),
    workflowMessage: (args: {run_id: string; stage_key: string; text: string; request_key: string}) => v2<{message_id: string; state: string}>('portal_workflow_message_v1', {contract_version: 1, ...args}),
    workflowGate: (args: {run_id: string; wait_id: string; decision: 'approve' | 'reject'; expected_revision: WireInteger; request_key: string}) => v2<{run: WorkflowRun}>('portal_workflow_gate_v1', {contract_version: 1, ...args}),
    workflowArtifact: (run_id: string, artifact_id: string, offset = 0) => v2<{artifact_id: string; digest: string; encoding: string; content: string; offset: number; next_offset: number | null; total_bytes: number}>('portal_workflow_artifact_v1', {contract_version: 1, run_id, artifact_id, offset}),
    workflowWatch: (run_id: string, after_seq: WireInteger, watch: boolean) => v2<{run_id: string; watching: boolean}>('portal_workflow_watch_v1', {contract_version: 1, run_id, after_seq, watch}),

    workspaceBrowse: (project_id: string, path = '') => v2<WorkspaceBrowse>('portal_workspace_browse_v2', { contract_version: 2, project_id, path }),
    builderModeGet: (conversation_id: string) => v2<BuilderModeState>('portal_builder_mode_get_v2', { contract_version: 2, conversation_id }),
    builderTaskEnd: (conversation_id: string) => v2<{status: string}>('portal_builder_task_end_v2', { contract_version: 2, conversation_id }),
    builderProjectResult: (project_id: string) => v2<{project_id: string; conversation_id: string; path: string}>('portal_builder_project_result_v2', { contract_version: 2, project_id }),
    builderNetworkSet: (args: BuilderNetworkSet) => v2<BuilderNetworkState>('portal_builder_network_set_v2', { contract_version: 2, ...args }),
    builderNetworkLog: (query: BuilderNetworkLogQuery) => v2<BuilderNetworkLog>('portal_builder_network_log_v2', { contract_version: 2, ...query }),
    builderModeSet: (args: BuilderModeSet) => v2<BuilderModeState>('portal_builder_mode_set_v2', { contract_version: 2, ...args }),
    builderApprovalGet: (approval_id: string) => v2<BuilderApproval>('portal_builder_approval_get_v2', { contract_version: 2, approval_id }),
    builderApprove: (approval_id: string, decision: BuilderDecision, request_key: string) => v2<BuilderApprovalResult>('portal_builder_approve_v2', { contract_version: 2, approval_id, decision, request_key }),
    builderPreview: (conversation_id: string, path: string) => v2<BuilderPreviewData>('portal_builder_preview_v2', { contract_version: 2, conversation_id, path }),
    builderOpenCopy: (conversation_id: string, path: string) => v2<{ opened: boolean }>('portal_builder_open_temp_copy_v2', { contract_version: 2, conversation_id, path }),
    jobs: createWorkspaceClient(invoke),
    work: <T = Record<string, unknown>>(operation: 'catalog'|'preparation'|'status'|'workspace.create'|'publish'|'start', args: Record<string, unknown> = {}) =>
      v2<T>('portal_work_'+operation.replaceAll('.', '_')+'_v2', { contract_version: 2, ...args }),
    recoveryCapability: () => v2<{ available: boolean; reason: string }>('portal_recovery_capability_v2', { contract_version: 2 }),
    recoveryPending: () => v2<RecoveryStatus | null>('portal_recovery_status_v2', { contract_version: 2 }),
    recoveryStart: (pair: RecoveryPair, printed: boolean) => v2<RecoveryStatus>('portal_recovery_start_v2', { contract_version: 2, pair, printed }),
    recoveryAct: (handle: string, action: RecoveryAction) => v2<RecoveryStatus | null>('portal_recovery_act_v2', { contract_version: 2, handle, action }),
    flagsList: (run_id: string, cursor?: string, status: 'open' | 'resolved' | 'all' = 'open') => v2<FlagsPage>('portal_flags_list_v2', { contract_version: 2, run_id, limit: 50, status, ...(cursor ? { cursor } : {}) }),
    flagGet: (flag_id: string, after_event_seq: WireInteger = 0, job_cursor?: string) => v2<FlagDetail>('portal_flags_get_v2', { contract_version: 2, flag_id, after_event_seq, ...(job_cursor ? { job_cursor } : {}) }),
    flagResolve: (args: FlagResolve) => v2<FlagResolution>('portal_flags_resolve_v2', args),
    ownerBegin: (operation: string, resource: Record<string, string>, proposal_revision?: WireInteger) => v2<OwnerChallenge>('portal_owner_begin_v2', { contract_version: 2, operation, resource, ...(proposal_revision === undefined ? {} : { proposal_revision }) }),
    ownerConfirm: async (challenge_id: string, password?: string, totp?: string) => {
      const proof = password === undefined && totp === undefined ? { confirmation_mode: 'signed_in_user' } : { password, totp };
      const result = await v2<{ owner_session: OwnerSession; security_notice?: string }>('portal_owner_confirm_v2', { contract_version: 2, challenge_id, ...proof });
      if (typeof result.security_notice === 'string' && /^two-factor was reset on [0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(result.security_notice)) {
        window.dispatchEvent(new CustomEvent('bob-security-notice', { detail: result.security_notice }));
      }
      return result;
    },
    ownerStatus: () => v2<{ owner_session: OwnerSession }>('portal_owner_status_v2', { contract_version: 2 }),
    ownerLock: () => v2<{ owner_session: OwnerSession }>('portal_owner_lock_v2', { contract_version: 2 }),
    ownerLogout: () => v2<{ owner_session: OwnerSession }>('portal_owner_logout_v2', { contract_version: 2 }),
    grantsPrepare: (args: GrantPrepare) => v2<GrantProposal>('portal_grants_prepare_v2', args),
    grantsIssue: (args: GrantIssue) => v2<{ grant: ExecutionGrant }>('portal_grants_issue_v2', args),
    grantsList: (cursor?: string) => v2<{ items: ExecutionGrant[]; next_cursor: string | null }>('portal_grants_list_v2', { contract_version: 2, limit: 50, ...(cursor ? { cursor } : {}) }),
    grantGet: (grant_id: string) => v2<{ grant: ExecutionGrant }>('portal_grants_get_v2', { contract_version: 2, grant_id }),
    grantRevoke: (grant_id: string, expected_revision: WireInteger, request_key: string) => v2<{ grant: ExecutionGrant; affected_runs: unknown }>('portal_grants_revoke_v2', { contract_version: 2, grant_id, expected_revision, request_key }),
    budgetGet: (project_id: string) => v2<ProjectCallBudget>('portal_budgets_get_v2', { contract_version: 2, project_id }),
    budgetsList: (cursor?: string) => v2<{ items: ProjectCallBudget[]; next_cursor: string | null }>('portal_budgets_list_v2', { contract_version: 2, limit: 50, ...(cursor ? { cursor } : {}) }),
    budgetRemove: (project_id: string, expected_revision: WireInteger, request_key: string) => v2<ProjectCallBudget>('portal_budgets_remove_v2', { contract_version: 2, project_id, expected_revision, request_key }),
    profiles: () => invoke<ProfilesPayload>('portal_profiles'),
    selectProfile: (profileId: string) => invoke<ProfilesPayload>('portal_select_profile', { profileId }),
    reconnect: () => invoke<{ target: string; hello: Hello }>('portal_reconnect'),
    hello: () => invoke<Hello>('portal_hello'),
    catalog: () => invoke<Catalog>('portal_catalog'),
    conversations: (limit = 30, offset = 0) => invoke<ConversationPage>('portal_conversations', { limit, offset }),
    conversationNew: (args: { title: string; face_id?: string; model_preference?: string; project_id?: string }) =>
      invoke<{ conversation: Conversation }>('portal_conversation_new', args),
    conversationOpen: (id: string, limit = 50) =>
      invoke<OpenedConversation>('portal_conversation_open', { id, limit }),
    conversationRename: (id: string, title: string) =>
      invoke<{ conversation: Conversation }>('portal_conversation_rename', { id, title }),
    conversationAssignProject: (id: string, project_id: string | null) =>
      invoke<{ conversation: Conversation }>('portal_conversation_assign_project', { id, project_id }),
    messagesMore: (id: string, before: string | null, limit = 50) =>
      invoke<MessagePage>('portal_messages_more', { id, before, limit }),
    send: (args: SendArgs) => invoke<{ request_id: string; accepted: boolean }>('portal_send', args),
    stop: (conversation_id: string) => invoke<{ stopped: boolean; code?: string }>('portal_stop', { conversation_id }),
    answerQuestion: (question_id: string, answer: string) =>
      invoke<{ resolved: boolean; question_id: string; delivered_to_turn?: boolean }>('portal_answer', { question_id, answer }),
    pins: (conversation_id: string) => invoke<Pins>('portal_pins', { conversation_id }),
    approvals: (status = 'pending', limit = 30) =>
      invoke<{ items: Approval[]; excluded: number }>('portal_approvals', { status, limit }),
    approvalGet: (approval_id: string) => invoke<{ approval: Approval }>('portal_approval_get', { approval_id }),
    approvalDecide: (approval_id: string, decision: 'approve' | 'reject', edit_content?: string) =>
      invoke<DecisionResult>(
        'portal_approval_decide',
        edit_content ? { approval_id, decision, edit_content } : { approval_id, decision },
      ),
    projects: () => invoke<{ items: Project[] }>('portal_projects'),
    projectSave: (args: ProjectSaveArgs) =>
      invoke<{ project?: Project; deleted?: boolean }>('portal_project_save', args),
    memoryStatus: () => invoke<MemoryStatus>('portal_memory_status'),
    memoryList: (limit = 50, cursor?: string, kind?: string) =>
      invoke<MemoryPage>('portal_memory_list', { limit, ...(cursor ? { cursor } : {}), ...(kind ? { kind } : {}) }),
    memorySearch: (query: string, limit = 20, offset = 0) =>
      invoke<{ items: { source_id: string; owner: string; kind: string; provenance: string; excerpt: string; truncated: boolean }[]; coverage?: unknown }>(
        'portal_memory_search',
        offset ? { query, limit, offset } : { query, limit },
      ),
    memoryRead: (source_id: string, max_chars = 8000) =>
      invoke<{ source_id: string; offset: number; content: string; truncated?: boolean }>('portal_memory_read', {
        source_id,
        max_chars,
      }),
    memorySave: (content: string, category: string) => invoke<unknown>('portal_memory_save', { content, category }),
    /** The server checks the current password and two-factor code before changing anything. */
    changePassword: (current: string, next: string, totp: string) =>
      v2<PasswordResult>('portal_auth_change_password_v2', { current, new: next, totp }),
    /** Native creates the single-use request key; credentials are never replayed. */
    secretSet: (provider: string, key: string) => v2<SecretReceipt>('portal_secret_set_v2', { provider, key }),
    passwordPending: () => v2<{ operation: 'auth.change_password'; status: 'unknown'; request_key: string } | null>('portal_password_pending_v1', { contract_version: 1 }),
    passwordStatus: (lookup: { request_key: string } | { operation_id: string }) => v2<SettingsStatus<PasswordResult>>('portal_settings_status_v1', { contract_version: 1, ...lookup }),
    settingsStatus: (lookup: { request_key: string } | { operation_id: string }) => v2<SettingsStatus>('portal_settings_status_v1', { contract_version: 1, ...lookup }),
    policyGet: () => v2<PolicyProjection>('portal_policy_get_v2', { contract_version: 2 }),
    policySet: (policy: ApprovalPolicy, expected_revision: WireInteger, request_key: string) => v2<PolicyProjection>('portal_policy_set_v2', { contract_version: 2, policy, expected_revision, request_key }),
    runsStart: (args: RunStartArgs) => invoke<{ run: Run; warnings?: string[] }>('portal_runs_start', args),
    teamsList: (cursor?: string) => v2<TeamsPage>('portal_teams_list_v2', { contract_version: 2, limit: 50, ...(cursor ? { cursor } : {}) }),
    teamsSave: (definition: TeamDefinition, expected_revision: number | null, request_key: string) =>
      v2<TeamSaveResult>('portal_teams_save_v2', { contract_version: 2, definition, expected_revision, request_key }),
    runsStartV2: (args: RunStartV2) => v2<RunDetail>('portal_runs_start_v2', args),
    runsListV2: (cursor?: string) => v2<ServerRunsPage>('portal_runs_list_v2', { contract_version: 2, limit: 20, ...(cursor ? { cursor } : {}) }),
    runGetV2: (run_id: string, after_event_seq = 0, receipt_cursor = 0) => v2<RunDetail>('portal_run_get_v2', { contract_version: 2, run_id, after_event_seq, receipt_cursor }),
    runResumeV2: (args: RunResumeV2) => v2<RunDetail>('portal_run_resume_v2', args),
    budgetSetV2: (args: BudgetSetV2) => v2<ProjectCallBudget>('portal_budget_set_v2', args),
    runCancelV2: (run_id: string, request_key: string) => v2<RunDetail>('portal_run_cancel_v2', { contract_version: 2, run_id, request_key }),
    runsList: (limit = 20, offset = 0) => invoke<RunsList>('portal_runs_list', { limit, offset }),
    runGet: (run_id: string) => invoke<{ run: Run; warnings?: string[] }>('portal_run_get', { run_id }),
    runCancel: (run_id: string) => invoke<{ run: Run; warnings?: string[] }>('portal_run_cancel', { run_id }),
  };
  return client;
}

export type PortalClient = ReturnType<typeof createPortalClient>;

export type GrantPrepare = { contract_version: 2; request_key: string; request?: RunStartV2; run_id?: string; expected_revision?: WireInteger };
export type FlagResolve = { contract_version: 2; flag_id: string; expected_revision: WireInteger; request_key: string; decision: FlagDecision; guidance: string };
export type GrantIssue = JobGrantPolicy & { contract_version: 2; proposal_id: string; proposal_revision: WireInteger; expected_revision: null; request_key: string; coverage_kind: 'task' | 'project'; allow_long_runs: boolean; auto_escalate: boolean; call_ceiling: WireInteger | null; expires_at: string | null; allow_extended_jobs: boolean; allow_custom_job_limit: boolean; job_seconds_ceiling: WireInteger | null };
