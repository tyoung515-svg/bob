import type { WireInteger } from './portal-protocol';

export type WInt = WireInteger;
export type WorkflowBinding = { binding_id: string; backend: string; model: string; model_identity: string; family: string | null; face_ids: string[]; tool_capable: boolean; runnable: boolean; reasons: string[] };
export type WorkflowBudget = { calls: number; active_seconds: number; job_seconds: number; max_job_attempts: number };
export type WorkflowStage = { id: string; kind: string; depends_on: string[]; agent?: string; build?: { agent: string }; review?: { agent: string }; fix?: { agent: string }; body?: WorkflowStage[]; prompt?: string; [key: string]: unknown };
export type WorkflowDefinition = { schema: 'workflow/v1'; name: string; summary: string; agents: Record<string, { face_id: string; binding_id: string | null }>; stages: WorkflowStage[]; review_policy: string; authority: { workspace_ops: string[]; checks: WorkflowCheck[] }; budget: WorkflowBudget; [key: string]: unknown };
export type WorkflowCheck = { id: string; command: string; cwd: string; timeout_seconds: number };
export type WorkflowRecord = { id: string; revision: WInt; digest: string; schema: 'workflow/v1'; definition: WorkflowDefinition; runnable: boolean; reasons: string[]; read_only: boolean; deleted: boolean; origin: 'premade'|'user'; created_at: string; updated_at: string };
export type WorkflowReview = { policy: string; policy_met: boolean; violations: unknown[]; same_model_choice_required: boolean; allow_same_model_review?: boolean; downgraded?: boolean };
export type WorkflowProposal = { created_at?: string; previous_proposal_id?: string | null; proposed_again_ids?: string[]; origin_conversation_id?: string | null; proposed_by?: { actor: string; token_type: string; via?: string; face_id?: string }; id: string; revision: WInt; digest: string; state: string; expires_at: string; planned_run_id: string; definition_ref: { id: string; revision: WInt } | null; definition_digest: string; runnable: boolean; reasons: string[]; run_id: string | null; coverage: {
  definition: WorkflowDefinition; inputs: Record<string, unknown>; evidence_digests: Record<string, unknown>; bindings: Record<string, WorkflowBinding>; project_id: string | null; workspace: { registration_digest?: string; path_label?: string; create?: string } | null;
  authority: WorkflowDefinition['authority']; budget: WorkflowBudget; grant_expires_at: string; run_mode: 'auto' | 'approve'; run_modes: { id: 'auto' | 'approve'; label: string; default: boolean }[]; checks: WorkflowCheck[]; checks_notice: string; review: WorkflowReview; assistant_summary_calls: number; [key: string]: unknown;
} };
export type WorkflowPropose = { definition_ref?: { id: string; revision: WInt }; definition?: WorkflowDefinition; inputs: Record<string, unknown>; bindings: Record<string, string>; project_id: string | null; origin_conversation_id: string | null; expires_at: string; request_key: string; run_mode?: 'auto' | 'approve'; workspace?: { create: string } };
export type WorkflowStart = { proposal_id: string; proposal_revision: WInt; proposal_digest: string; request_key: string; allow_same_model_review?: boolean };
export type WorkflowSnapshot = {digest: string; artifact_id: string; registration_digest: string};
export type WorkflowStageView = { key: string; ordinal: number; kind: string; state: string; unit_id: string | null; agent_alias: string | null; conversation_id: string | null; request_id: string | null; actual_models: WorkflowBinding[]; input_digest: string; output_artifact_id: string | null; snapshot: WorkflowSnapshot | null; reason: string | null; [key: string]: unknown };
export type WorkflowWait = { wait_id: string; kind: string; stage_key: string; conversation_id: string | null; request_id: string | null; record_id: string; prompt: string; expires_at: string | null; state: string; requires_owner: boolean; revision?: WInt };
export type WorkflowReport = { digest: string; artifact_id: string; outcome: string; summary: string; units: { id: string; verdict: string; snapshot_digest: string | null; stage_keys: string[]; receipt_ids: string[]; reason: string | null; [key: string]: unknown }[]; checks: string[]; caveats: string[]; assistant_delivery: string; [key: string]: unknown };
export type WorkflowSummary = { proposal_id?: string; id: string; status: string; reason: string | null; name: string; project_id: string | null; origin_conversation_id: string | null; revision: WInt; event_seq: WInt; created_at: string; updated_at: string };
export type WorkflowRun = WorkflowSummary & { definition_ref: { id: string; revision: WInt } | null; definition_digest: string; grant: { id: string; expires_at: string; active: boolean }; budget: { limit: WorkflowBudget; consumed_calls: WInt; reserved_calls: WInt; unknown_calls: WInt; active_seconds: number; job_seconds: number; job_attempts: WInt; estimated?: boolean }; stages: WorkflowStageView[]; next_stage_cursor: string | null; needs_you: WorkflowWait[]; report: WorkflowReport | null; review: WorkflowReview };
export type WorkflowEvent = { run_id: string; seq: WInt; revision: WInt; at: string; kind: string; stage_key: string | null; conversation_id: string | null; request_id: string | null; payload: Record<string, unknown> };
export type WorkflowCatalog = { templates: WorkflowRecord[]; bindings: WorkflowBinding[]; limits: Record<string, number>; available: boolean; reasons: string[] };
export type WorkflowPage<T> = { items: T[]; next_cursor: string | null };
export const workflowInteger = (v: unknown): v is WInt => typeof v === 'number' ? Number.isSafeInteger(v) && v >= 0 : typeof v === 'string' && /^(0|[1-9][0-9]*)$/.test(v);
export const workflowTerminal = (s: string) => ['completed', 'stopped', 'failed', 'over_budget', 'expired', 'cancelled', 'blocked', 'unknown'].includes(s);

// An interruption stays actionable until readback confirms the grant ended and
// no recovery decision remains. A summary alone cannot establish that.
export const workflowEnded = (run: WorkflowSummary, detail?: WorkflowRun) => workflowTerminal(run.status) ||
  run.status === 'interrupted' && !!detail && !detail.grant.active && !detail.needs_you.some(w => w.state === 'open');
