import type { PortalClient, RunStartV2 } from './portal-client';
import type { AcceptanceContract, AcceptanceCriterion, Catalog, Identity, RunBudget, WireInteger, SavedTeam, TeamDefinition, JobCoverage, JobGrantPolicy, FlagSummary } from './portal-protocol';
import { toPortalError } from './portal-protocol';

export const BUDGET_LEVELS = [10, 50, 100, 200];
export const UPGRADE_REASON = 'Update Bob on the server to use saved teams and execution v2. The request was not reduced or resent.';
export const LOOP = 'agent-loop/v1';
export const FOUR_STEP = 'four-step-readonly/v1';
export function operationReason(catalog: Catalog | null | undefined, op: string, version = 2): string {
  const cap = catalog?.capabilities.operations?.[op];
  if (!cap?.contract_versions?.includes(version)) return cap?.reason || `Update Bob on the server to use ${op}.`;
  return cap.available === false || cap.implemented === false ? cap.reason || `${op} unavailable` : '';
}
export function ownerEligible(identity: Identity | null | undefined, catalog?: Catalog | null): boolean {
  return humanOwner(identity) || (identity?.token_type === 'portal' && identity.owner_controls_eligible === true && !operationReason(catalog, 'owner.begin'));
}
export function featureReason(catalog: Catalog | null | undefined, feature: 'loop' | 'growth' | 'runs.resume' | 'budgets.set'): string {
  if (feature === 'runs.resume' || feature === 'budgets.set') return operationReason(catalog, feature);
  const caps = catalog?.capabilities.execution_v2;
  const available = supportsV2(catalog) && (feature === 'loop' ? caps?.topologies?.includes(LOOP) : caps?.growth?.topologies?.includes(LOOP));
  return available ? '' : `Update Bob on the server to use ${feature === 'loop' ? 'loop teams' : feature === 'growth' ? 'automatic budget growth' : feature}.`;
}
export function humanOwner(identity?: Identity | null): boolean {
  return identity?.token_type === 'human' && !!identity.owner && identity.actor === identity.owner && identity.sub === identity.owner;
}
export const PAUSE_REASONS: Record<string, string> = {
  call_budget_exhausted: 'Call allowance exhausted', budget_exhausted_with_output: 'Budget exhausted with accepted output',
  confirmation_required: 'More than 200 calls needs your OK', owner_ceiling: 'Owner call ceiling reached',
  pool_shortfall: 'Project or sprint budget has too few free calls', no_progress: 'Repeated checks made no progress',
  wall_time_expired: 'Time limit reached', unknown_effect: 'An earlier action has an unknown outcome',
};
export function resumable(reason?: string | null): boolean {
  return !!reason && ['call_budget_exhausted', 'budget_exhausted_with_output', 'confirmation_required', 'owner_ceiling', 'pool_shortfall', 'no_progress', 'wall_time_expired'].includes(reason);
}
export function criterionSummary(c: AcceptanceCriterion): string {
  const noun = { answer: 'An answer', report: 'A report', proposal: 'A proposal', json: 'A JSON output' }[c.output_kind] ?? 'An output';
  return `${noun}${c.checks.required_sections?.length ? ` with ${c.checks.required_sections.join(', ')} section(s)` : ''}, ${c.checks.require_citations ? 'citing the evidence' : 'citations optional'}, ${c.checks.min_length ?? 1}–${c.checks.max_length ?? 60000} chars`;
}
export function checkedAcceptance(contract: AcceptanceContract): AcceptanceContract {
  const only = (value: object, fields: string[]) => Object.keys(value).every(key => fields.includes(key));
  if (!Array.isArray(contract?.criteria) || !contract.criteria.length || contract.criteria.length > 100) throw new Error('Add at least one required criterion (up to 100 rows).');
  if (!only(contract, ['criteria'])) throw new Error('Unsupported acceptance field; ask the setup agent to correct the proposal.');
  const seen = new Set<string>();
  for (const c of contract.criteria) {
    if (!c || typeof c.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(c.id) || seen.has(c.id)) throw new Error('Criteria need unique valid IDs.');
    seen.add(c.id);
    if (typeof c.required !== 'boolean' || !['answer', 'report', 'proposal', 'json'].includes(c.output_kind) || !c.checks) throw new Error('Choose an output kind and checks for each criterion.');
    if (!only(c, ['id', 'required', 'output_kind', 'checks']) || !only(c.checks, ['min_length', 'max_length', 'required_sections', 'require_citations', 'json_type'])) throw new Error('Unsupported criterion check; ask the setup agent to correct the proposal.');
    const { min_length: lo = 1, max_length: hi = 60000, required_sections: sections = [], require_citations: citations = false } = c.checks;
    if (!Number.isSafeInteger(lo) || !Number.isSafeInteger(hi) || lo < 1 || hi < lo || hi > 60000) throw new Error('Length bounds must be positive integers, ordered, and at most 60000.');
    if (!Array.isArray(sections) || sections.length > 32 || sections.some(s => typeof s !== 'string' || !s.trim() || s.length > 200) || typeof citations !== 'boolean') throw new Error('Use up to 32 nonempty sections and a citations choice.');
    if (c.checks.json_type !== undefined && !['object', 'array', 'string', 'number', 'boolean', 'null'].includes(c.checks.json_type)) throw new Error('Choose a valid JSON type.');
  }
  if (!contract.criteria.some(c => c.required)) throw new Error('Start needs at least one required criterion.');
  return contract;
}
export function acceptanceReason(contract: AcceptanceContract): string {
  try { checkedAcceptance(contract); return ''; } catch (e) { return (e as Error).message; }
}
export function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, entry: unknown) => entry && typeof entry === 'object' && !Array.isArray(entry)
    ? Object.fromEntries(Object.entries(entry).sort(([a], [b]) => a.localeCompare(b))) : entry);
}
export function supportsV2(catalog: Catalog | null | undefined): boolean {
  return catalog?.capabilities.execution_v2?.contract_versions?.includes(2) === true;
}
export function executionReason(catalog: Catalog | null | undefined): string {
  if (!supportsV2(catalog)) return UPGRADE_REASON;
  const caps = catalog!.capabilities.execution_v2!;
  return caps.execution_enabled === false || caps.storage_available === false
    ? (caps.blocked_reasons?.join(', ') || 'Server execution is unavailable') : '';
}
export function compatibilityError(code: string, detail = ''): boolean {
  return ['unknown_op', 'client_upgrade_required', 'unsupported_run_contract'].includes(code) ||
    /unknown portal command|command .* not found|unknown command/i.test(detail);
}
export function executionRefused(code: string): boolean {
  if (code === 'rate_limited' || code === 'gateway_http_429') return true;
  return ['job_coverage_required', 'job_coverage_budget_conflict', 'invalid_job_coverage', 'invalid_job_limit', 'job_grant_required', 'extended_job_grant_required', 'custom_job_grant_required', 'extended_job_requires_long_runs', 'installed_job_limit', 'owner_job_ceiling', 'job_escalation_disabled', 'cli_jobs_unavailable', 'flag_not_found', 'invalid_flag_decision', 'proposal_stale', 'invalid_coverage', 'explicit_grant_policy_required', 'invalid_expiry', 'project_required', 'invalid_grant_policy', 'job_grants_unavailable', 'replacement_grant_attachment_unavailable', 'admission_intent_conflict', 'credential_epoch_changed', 'owner_controls_unavailable', 'owner_confirmation_required', 'owner_session_invalid', 'owner_session_expired', 'budget_in_use', 'operation_unavailable', 'confirmation_required', 'team_blocked', 'revision_conflict', 'invalid_args',
    'invalid_integer', 'invalid_wall_mode', 'conflicting_wall_fields', 'insufficient_call_allowance',
    'execution_executor_unavailable', 'client_upgrade_required', 'unsupported_run_contract',
    'unsupported_fields', 'unknown_op', 'bad_request', 'below_committed_budget', 'not_found', 'forbidden',
    'authorization_revoked', 'unsettled_attempts', 'wall_extension_required', 'run_not_resumable',
    'owner_ceiling', 'pool_shortfall', 'invalid_acceptance', 'required_acceptance_missing', 'invalid_text'].includes(code);
}
export function checkedBudget(calls: WireInteger, mode: RunBudget['wall_mode'], seconds: WireInteger | null): RunBudget {
  if (!validInteger(calls)) throw new Error('Calls must be a positive integer; no clamping.');
  if (!['fixed', 'follow_budget', 'none'].includes(mode) ||
      (mode === 'none' ? seconds !== null : !validInteger(seconds))) {
    throw new Error('Choose a positive integral time limit, or No time limit.');
  }
  return { start_calls: calls, wall_mode: mode, wall_seconds: seconds };
}

// Persist the complete mutation before dispatch. An unknown outcome reuses this
// exact payload and key, including after reconnect/reload. Only a received result
// releases it, so an owner can intentionally start the same task again later.
export function mutationKey(scope: string, operation: string, payload: unknown): string {
  const slot = `bob.execution-v2:${scope}:${operation}`;
  const serialized = canonical(payload);
  const saved = JSON.parse(localStorage.getItem(slot) || 'null') as { payload: string; key: string } | null;
  if (saved) {
    if (saved.payload !== serialized) throw new Error('A previous request has an unknown outcome. Restore its details and retry with its saved key before changing it.');
    return saved.key;
  }
  const key = crypto.randomUUID();
  localStorage.setItem(slot, JSON.stringify({ payload: serialized, key }));
  return key;
}
export function mutationDone(scope: string, operation: string) {
  localStorage.removeItem(`bob.execution-v2:${scope}:${operation}`);
}
export function pendingMutation<T>(scope: string, operation: string): T | null {
  const saved = JSON.parse(localStorage.getItem(`bob.execution-v2:${scope}:${operation}`) || 'null') as { payload: string } | null;
  return saved ? JSON.parse(saved.payload) as T : null;
}
export async function allTeams(client: PortalClient): Promise<SavedTeam[]> {
  const items: SavedTeam[] = [];
  let cursor: string | undefined;
  const seen = new Set<string>();
  do {
    const page = await client.teamsList(cursor);
    items.push(...page.items);
    cursor = page.next_cursor || undefined;
    if (cursor && seen.has(cursor)) throw new Error('The server repeated a teams cursor.');
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}
export type TeamProposal = { definition: TeamDefinition; expected_revision: number | null; task: string; budget: RunBudget; evidence_ids?: string[]; acceptance?: AcceptanceContract | string; job_coverage?: JobCoverage; job_time?: JobTimeProposal };
export function teamProposal(content: string): TeamProposal | null {
  const block = /```bob-team-proposal\s*\n([\s\S]*?)```/.exec(content);
  if (!block) return null;
  try {
    const p = JSON.parse(block[1]) as TeamProposal;
    if (p.definition.schema_version !== 1 || typeof p.definition.id !== 'string' ||
        typeof p.definition.topology !== 'string' || !p.definition.roles || typeof p.task !== 'string' ||
        !(p.expected_revision === null || (Number.isSafeInteger(p.expected_revision) && p.expected_revision > 0))) return null;
    checkedJobBudget(p.budget);
    if (p.job_time && (!validInteger(p.job_time.proposed_seconds) || typeof p.job_time.rationale !== "string" || !p.job_time.rationale.trim())) return null;
    // Missing/invalid required rows remain reviewable. The run editor blocks
    // Start until the owner has a valid acceptance contract.
    if (typeof p.acceptance === 'object' && (!Array.isArray(p.acceptance?.criteria) || p.acceptance.criteria.some(c => !c || !c.checks || !Array.isArray(c.checks.required_sections ?? [])))) return null;
    return p;
  } catch { return null; }
}
export async function saveTeamProposal(client: PortalClient, scope: string, proposal: TeamProposal): Promise<SavedTeam> {
  const payload = { definition: proposal.definition, expected_revision: proposal.expected_revision };
  const operation = `team:${proposal.definition.id}`;
  let result: Awaited<ReturnType<PortalClient['teamsSave']>>;
  try {
    result = await client.teamsSave(payload.definition, payload.expected_revision, mutationKey(scope, operation, payload));
  } catch (e) {
    if (executionRefused(toPortalError(e).code)) mutationDone(scope, operation);
    throw e;
  }
  const readback = (await allTeams(client)).find(t => t.id === result.team.id && t.revision === result.revision);
  if (!result.saved || !readback) throw new Error('Save receipt received, but that team revision could not be read back. Retry to reconcile.');
  mutationDone(scope, operation);
  return readback;
}
export type RunDraft = Omit<RunStartV2, 'request_key'>;

export function validInteger(value: unknown, zero = false): value is WireInteger {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value >= (zero ? 0 : 1);
  return typeof value === 'string' && /^(0|[1-9][0-9]*)$/.test(value) && value.length <= 19 && BigInt(value) >= BigInt(zero ? 0 : 1) && BigInt(value) <= BigInt('9223372036854775807');
}
export function integerInput(value: string): WireInteger {
  if (!validInteger(value)) return value;
  return BigInt(value) <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : value;
}
export function greater(a: WireInteger, b: WireInteger): boolean { return validInteger(a, true) && validInteger(b, true) && BigInt(a) > BigInt(b); }

export function budgetWallSeconds(calls: WireInteger): WireInteger {
  return validInteger(calls) ? integerInput((BigInt(calls) * BigInt(360)).toString()) : '';
}

export type JobTimeProposal = { proposed_seconds: WireInteger; rationale: string };
export function jobTimeReason(catalog?: Catalog | null): string {
  const cap = catalog?.capabilities.execution_v2?.job_time;
  return cap?.version === 1 && cap.available === true ? '' : cap?.reason || 'Job time unavailable on this server.';
}
export function jobGrantReason(catalog?: Catalog | null): string {
  const cap = catalog?.capabilities.operations?.['grants.issue'];
  return jobTimeReason(catalog) || operationReason(catalog, 'grants.prepare') || operationReason(catalog, 'grants.issue') ||
    (cap?.job_grants_available === true && cap.job_grant_matcher_version === 3 ? '' : cap?.job_grants_reason || 'Job grants unavailable on this server.');
}
export function checkedJobBudget(budget: RunBudget): RunBudget {
  const result = checkedBudget(budget.start_calls, budget.wall_mode, budget.wall_seconds);
  if (budget.max_job_attempts !== undefined) {
    if (!validInteger(budget.max_job_attempts)) throw new Error('Job attempts must be a positive integer.');
    result.max_job_attempts = budget.max_job_attempts;
  }
  if (budget.job_seconds_budget !== undefined) {
    if (budget.job_seconds_budget !== null && !validInteger(budget.job_seconds_budget)) throw new Error('Job seconds budget must be positive or unset.');
    result.job_seconds_budget = budget.job_seconds_budget;
  }
  return result;
}
export function jobPolicy(seconds: WireInteger, custom: boolean, auto: boolean): JobGrantPolicy {
  if (!validInteger(seconds)) throw new Error('Choose a positive integral job limit.');
  return { job_override_seconds: seconds, job_auto_escalate: auto,
    allow_extended_jobs: greater(seconds, 3600), allow_custom_job_limit: custom, job_seconds_ceiling: null };
}
export function openFlags(flags: FlagSummary[] = []): FlagSummary[] {
  return [...new Map(flags.map(f => [f.id, f])).values()].filter(f => f.status === 'open');
}

export function customJobLimit(value: WireInteger): boolean {
  if (!validInteger(value)) return true;
  const seconds = BigInt(value);
  if ([900, 1800, 3600].some(n => seconds === BigInt(n))) return false;
  const ratio = seconds / BigInt(3600);
  return seconds <= BigInt(3600) || seconds % BigInt(3600) !== BigInt(0) || (ratio & (ratio - BigInt(1))) !== BigInt(0);
}
