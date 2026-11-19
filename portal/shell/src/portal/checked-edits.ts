import type { JobCapabilities, CheckedAcceptance, CheckedReceipt, CheckedManifest, StepKind } from './workspace-v2-protocol';
import { validInteger } from './durable-runs';
export const CHECKED_EDITS = 'checked-edits/1';
export const STEP_KINDS: StepKind[] = ['model_task', 'workspace_read', 'workspace_edit', 'worker_command', 'registered_check', 'proposal_publish', 'await_owner_decision', 'integrate_approved_proposal'];
export const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
export const sha256 = (v: unknown): v is string => typeof v === 'string' && v.length === 64 && /^[a-f0-9]{64}$/.test(v);
const bounded = (v: unknown, n = 2048): v is string => typeof v === 'string' && new TextEncoder().encode(v).length <= n;
const identifier = (v: unknown): v is string => bounded(v, 128) && !/[\r\n]/.test(v) && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(v);
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.length <= 100 && v.every(identifier);
const only = (v: Record<string, unknown>, fields: string[]) => Object.keys(v).every(k => fields.includes(k));
export function checkedEditsReason(caps?: JobCapabilities | null): string {
  if (caps?.coder?.worker_contract !== CHECKED_EDITS || caps.acceptance_revision !== CHECKED_EDITS || caps.checks?.selection !== 'frozen_attachment_registration') return 'Update Bob on the server to use checked edits.';
  if (!caps.coder.available) return caps.coder.reason || 'Checked edits unavailable.';
  if (!caps.checks.available) return caps.checks.reason || 'Registered checks unavailable.';
  if (!sha256(caps.profile_revisions.checked)) return 'Installed checked profile required.';
  return '';
}
export function validCheckedAcceptance(v: unknown): v is CheckedAcceptance {
  if (!record(v) || !only(v, ['criteria']) || !Array.isArray(v.criteria) || !v.criteria.length || v.criteria.length > 100) return false;
  const seen = new Set<string>();
  return v.criteria.every(c => {
    if (!record(c) || !only(c, ['id', 'required', 'output_kind', 'checks']) || !identifier(c.id) || seen.has(c.id) || typeof c.required !== 'boolean' || c.output_kind !== 'proposal' || !record(c.checks) || !only(c.checks, ['required_check_ids']) || !strings(c.checks.required_check_ids) || !c.checks.required_check_ids.length || new Set(c.checks.required_check_ids).size !== c.checks.required_check_ids.length) return false;
    seen.add(c.id); return true;
  }) && v.criteria.some(c => c.required === true);
}
export function validCheckedReceipt(v: unknown): v is CheckedReceipt {
  if (!record(v) || !STEP_KINDS.includes(v.kind as StepKind) || !bounded(v.run_id, 128) || !v.run_id || !validInteger(v.step, true) || !validInteger(v.plan_step, true) || !bounded(v.execution_id, 128) || !v.execution_id || !sha256(v.input_digest) || !sha256(v.snapshot_digest) || !bounded(v.workspace_id, 128) || !['queued','dispatch_intent','running','succeeded','failed','timed_out','cancelled','unknown'].includes(String(v.outcome)) || !['not_started','pending','idle_proved','unknown'].includes(String(v.cleanup)) || typeof v.passed !== 'boolean' || !record(v.worker_receipt) || !(typeof v.log_artifact === 'string' && v.log_artifact.length === 44 && /^worker-[a-f0-9]{32}\.json$/.test(v.log_artifact)) || ![v.started_at, v.finished_at].every(x => x === null || bounded(x, 128)) || !(v.execution_seconds === null || typeof v.execution_seconds === 'number' && Number.isFinite(v.execution_seconds) && v.execution_seconds >= 0) || !validInteger(v.selected_seconds) || !(v.effective_seconds === null || validInteger(v.effective_seconds) || typeof v.effective_seconds === 'number' && Number.isFinite(v.effective_seconds) && v.effective_seconds > 0 && v.effective_seconds <= Number.MAX_SAFE_INTEGER) || !(v.limiting_reason === null || bounded(v.limiting_reason))) return false;
  for (const k of ['scratch_digest','observed_snapshot_digest']) if (v[k] !== undefined && !sha256(v[k])) return false;
  if (v.continuation_blocked_reason !== undefined && !bounded(v.continuation_blocked_reason)) return false;
  if (v.passed && (v.outcome !== 'succeeded' || v.cleanup !== 'idle_proved')) return false;
  if (v.kind === 'registered_check') {
    if (!identifier(v.check_id) || !sha256(v.definition_digest) || !record(v.definition) || !sha256(v.command_spec_digest) || !sha256(v.tool_revision) || !(v.exit_status === null || typeof v.exit_status === 'number' && Number.isSafeInteger(v.exit_status)) || typeof v.timed_out !== 'boolean' || typeof v.truncated !== 'boolean' || !bounded(v.assertion_summary) || (v.source_unchanged !== undefined && typeof v.source_unchanged !== 'boolean')) return false;
    if (v.input_digest !== v.snapshot_digest || v.passed && (v.exit_status !== 0 || v.timed_out || v.truncated || v.source_unchanged !== true)) return false;
  }
  return true;
}
export function validCheckRuns(v: unknown, passed: unknown, outputDigest?: string): boolean {
  if (!Array.isArray(v) || v.length > 64 || typeof passed !== 'boolean') return false;
  if (!v.every(r => validCheckedReceipt(r) && r.kind === 'registered_check')) return false;
  return !passed || v.some(r => r.passed && (!outputDigest || r.snapshot_digest === outputDigest));
}
export function validCheckedManifest(v: unknown): v is CheckedManifest {
  return record(v) && v.schema_version === 1 && v.kind === 'checked_proposal' && bounded(v.run_id, 128) && !!v.run_id && Array.isArray(v.step_lineage) && v.step_lineage.length === 2 && v.step_lineage.every(n => validInteger(n, true)) && strings(v.criteria_ids) && v.criteria_ids.length > 0 && ['digest','proposal_digest','base_digest','output_digest'].every(k => sha256(v[k])) && v.independently_verified === false && validCheckRuns(v.checks, true, String(v.output_digest)) && (v.checks as CheckedReceipt[]).every(r => r.run_id === v.run_id && r.passed && r.snapshot_digest === v.output_digest) && new Set((v.checks as CheckedReceipt[]).map(r => r.check_id)).size === (v.checks as CheckedReceipt[]).length;
}

export function validCheckedRunDetail(v: unknown): boolean {
  if (!record(v) || !record(v.run)) return false;
  if (v.run.worker_contract == null) return true;
  if (v.run.worker_contract !== CHECKED_EDITS || !Array.isArray(v.receipts) || v.receipts.length > 256 || !Array.isArray(v.events) || v.events.length > 10000 || !Array.isArray(v.run.accepted_outputs) || v.run.accepted_outputs.length > 100) return false;
  return v.receipts.every(r => record(r) && validInteger(r.step, true) && STEP_KINDS.includes(r.kind as StepKind) && bounded(r.output, 500000) && (r.kind === 'model_task' ? typeof r.model_face === 'string' && typeof r.model_pin === 'string' : r.model_face === null && r.model_pin === null)) &&
    v.run.accepted_outputs.every(o => record(o) && validCheckedManifest(o.check_receipts) && o.check_receipts.run_id === (v.run as Record<string, unknown>).id && o.digest === o.check_receipts.digest);
}

// Worker logs are read through their exact audit-manifest entry, never a raw path.
export function adapterRefusal(content: string): string | null {
  if (content.length > 65536) return null;
  try {
    const receipt: unknown = JSON.parse(content);
    if (!record(receipt) || typeof receipt.stderr_b64 !== 'string' || receipt.stderr_b64.length > 87384 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(receipt.stderr_b64)) return null;
    const stderr = new TextDecoder().decode(Uint8Array.from(atob(receipt.stderr_b64), c => c.charCodeAt(0)));
    for (const line of stderr.split('\n').slice(-32)) {
      try {
        const value: unknown = JSON.parse(line);
        if (record(value) && value.adapter === CHECKED_EDITS && ['stale_preimage','stale_snapshot'].includes(String(value.error))) return String(value.error);
      } catch { /* A worker's ordinary stderr is not a typed adapter refusal. */ }
    }
  } catch { /* Truncated / malformed evidence cannot establish a refusal. */ }
  return null;
}
