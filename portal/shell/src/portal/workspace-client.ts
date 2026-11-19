import { createWorkspaceV2Client } from './workspace-v2-client';
import type { NativeInvoke } from '../workbench/workbench-client';
import { toPortalError } from './portal-protocol';
import type { PortalError } from './portal-protocol';
import type {
  AttachmentList,
  DecisionResult,
  JobAudit,
  JobList,
  Proposal,
  ToolJobRow,
} from './workspace-protocol';

// The Workspace speaks ONLY the native portal_jobs_* commands, which ride the
// same broker link and verified agent token as chat. It never names a URL,
// path, owner or project root: those are server decisions.

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function strings(value: unknown, keys: string[]): boolean {
  return record(value) && keys.every((key) => typeof value[key] === 'string');
}

function invalid(what: string): PortalError {
  return { code: 'protocol_invalid', detail: `the server returned an invalid ${what}`, hint: 'Nothing was assumed; re-open the job.' };
}

function row(value: unknown): value is ToolJobRow {
  return strings(value, ['id', 'status', 'role', 'model_pin', 'backend', 'operation_id']);
}

function audit(value: unknown): value is JobAudit {
  if (!row(value)) return false;
  const source = value as unknown as Record<string, unknown>;
  const receipts = source.receipts;
  return typeof source.terminal === 'boolean' &&
    record(source.authority) && Array.isArray(source.history) &&
    source.history.every((entry) => record(entry) && typeof entry.at === 'number' &&
      typeof entry.status === 'string' && typeof entry.detail === 'string') &&
    record(receipts) && typeof receipts.model_attempts === 'number' &&
    typeof receipts.tool_calls === 'number' && Array.isArray(receipts.check_runs) &&
    typeof source.content === 'string';
}

function proposal(value: unknown): value is Proposal {
  return strings(value, ['job_id', 'digest', 'diff']) && record(value) &&
    Array.isArray(value.files) && typeof value.applicable === 'boolean' &&
    Array.isArray(value.stale_paths) && Array.isArray(value.check_runs);
}

/** A fresh client request key; resending the SAME key can never start new work. */
export function requestKey(): string {
  const random = globalThis.crypto?.randomUUID?.();
  if (random) return 'ws-' + random.replace(/-/g, '');
  return 'ws-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
}

/** Requested ceilings; the server enforces its own maxima and reserves before each use. */
export type JobBudget = { model_calls: number; tool_calls: number; wall_seconds: number };

export const BUDGETS: JobBudget[] = [
  { model_calls: 4, tool_calls: 12, wall_seconds: 60 },
  { model_calls: 8, tool_calls: 24, wall_seconds: 120 },
  { model_calls: 12, tool_calls: 36, wall_seconds: 120 },
];

/** A fresh form's budget: the four-call ceiling the owner approval brief names
 * (same as the native and core defaults). Larger budgets are an explicit choice. */
export const DEFAULT_BUDGET = 0;

export function createWorkspaceClient(invoke: NativeInvoke) {
  async function call<T>(command: string, args: Record<string, unknown>, check: (value: unknown) => boolean, what: string): Promise<T> {
    let value: unknown;
    try {
      value = await invoke<unknown>(command, args);
    } catch (error: unknown) {
      throw toPortalError(error);
    }
    if (!check(value)) throw invalid(what);
    return value as T;
  }
  return {
    v2: createWorkspaceV2Client(invoke),
    attachments: () =>
      call<AttachmentList>('portal_jobs_attachments', {}, (v) => record(v) && Array.isArray(v.items), 'attachment list'),
    list: () => call<JobList>('portal_jobs_list', {}, (v) => record(v) && Array.isArray(v.items), 'job list'),
    start: (role: string, task: string, attachment: string | null, request_key: string, budget: JobBudget) =>
      call<ToolJobRow>(
        'portal_jobs_start',
        attachment ? { role, task, attachment, request_key, ...budget } : { role, task, request_key, ...budget },
        row,
        'job row',
      ),
    audit: (job_id: string) => call<JobAudit>('portal_jobs_audit', { job_id }, audit, 'job audit'),
    cancel: (job_id: string) => call<ToolJobRow>('portal_jobs_cancel', { job_id }, row, 'job row'),
    proposal: (job_id: string) => call<Proposal>('portal_jobs_proposal', { job_id }, proposal, 'proposal'),
    decide: (job_id: string, decision: 'approve' | 'deny', proposal_digest: string, request_key: string) =>
      call<DecisionResult>(
        'portal_jobs_decide',
        { job_id, decision, proposal_digest, request_key },
        (v) => record(v) && record(v.decision) && typeof v.decision.state === 'string',
        'decision',
      ),
    artifact: (job_id: string, filename: string) =>
      call<{ filename: string; content: unknown }>('portal_jobs_artifact', { job_id, filename }, (v) => record(v) && 'content' in v, 'artifact'),
  };
}

export type WorkspaceClient = ReturnType<typeof createWorkspaceClient>;
