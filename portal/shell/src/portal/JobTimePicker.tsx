import { useState } from 'react';
import type { Catalog, JobGrantPolicy, RunBudget, WireInteger } from './portal-protocol';
import type { JobTimeProposal } from './durable-runs';
import { greater, validInteger, integerInput, jobGrantReason, jobPolicy, jobTimeReason } from './durable-runs';

export function JobTimePicker({ catalog, proposal, policy, onPolicy, budget, onBudget, disabled = false, owner }: {
  catalog?: Catalog | null; proposal?: JobTimeProposal; policy?: JobGrantPolicy; onPolicy: (value: JobGrantPolicy) => void;
  budget: RunBudget; onBudget: (value: RunBudget) => void; disabled?: boolean; owner: boolean;
}) {
  const [custom, setCustom] = useState(!!policy?.allow_custom_job_limit);
  const reason = jobTimeReason(catalog), grantReason = jobGrantReason(catalog);
  if (reason) return <fieldset disabled><legend>Job-time options</legend><p>{reason} · coming with build jobs</p></fieldset>;
  if (!owner) return <section aria-label="Proposed job time"><p>Agent proposal: {proposal ? proposal.proposed_seconds + ' seconds · ' + proposal.rationale : 'selected per job by Bob'}</p><p>Maximum job attempts: {budget.max_job_attempts ?? 'server default'} · job seconds budget: {budget.job_seconds_budget ?? 'not set'}. Owner approval is required for overrides.</p></section>;
  const presets = catalog!.capabilities.execution_v2!.job_time!.presets_seconds;
  const seconds = policy?.job_override_seconds ?? proposal?.proposed_seconds;
  const selected = custom || (seconds != null && !presets.some(p => String(p) === String(seconds))) ? 'custom' : String(seconds ?? '');
  function choose(value: WireInteger, isCustom = custom) { onPolicy(jobPolicy(value, isCustom, policy?.job_auto_escalate ?? true)); }
  return <fieldset disabled={disabled || !owner}><legend>Job-time options</legend>
    <p>Agent proposal: {proposal ? proposal.proposed_seconds + ' seconds · ' + proposal.rationale : 'not supplied; Bob selects a fitting tier for each job'}</p>
    <label>Job time <select aria-label="Job time tier" value={selected} disabled={!!grantReason} onChange={e => {
      setCustom(e.target.value === 'custom'); if (e.target.value === 'custom') choose(seconds ?? 3600, true); else choose(integerInput(e.target.value), false);
    }}><option value="" disabled>Agent selects per job</option>{presets.map(p => <option key={p} value={p}>{p / 60} min</option>)}<option value="custom">Custom / above 60 min</option></select></label>
    {selected === 'custom' && <label>Custom job seconds <input aria-label="Custom job seconds" inputMode="numeric" value={seconds ?? ''} onChange={e => onPolicy({ ...policy, job_override_seconds: integerInput(e.target.value), allow_custom_job_limit: true, allow_extended_jobs: validInteger(e.target.value) && greater(e.target.value, 3600) })} /></label>}
    <label><input aria-label="Job automatic continuation" type="checkbox" disabled={!!grantReason} checked={policy?.job_auto_escalate ?? true} onChange={e => onPolicy({ ...policy, job_auto_escalate: e.target.checked })} /> Continue at the next tier after trusted reconciliation</label>
    <label>Maximum job attempts <input aria-label="Maximum job attempts" inputMode="numeric" value={budget.max_job_attempts ?? ''} placeholder={String(catalog?.capabilities.execution_v2?.job_time?.max_job_attempts_default ?? '')} onChange={e => onBudget({ ...budget, max_job_attempts: integerInput(e.target.value) })} /></label>
    <label>Total job seconds budget (optional) <input aria-label="Job seconds budget" inputMode="numeric" value={budget.job_seconds_budget ?? ''} onChange={e => onBudget({ ...budget, job_seconds_budget: e.target.value ? integerInput(e.target.value) : null })} /></label>
    <p>Owner overrides, custom limits and jobs above 60 minutes need a reviewed job grant. Call-only approval supplies no job authority.</p>
    {grantReason && <p role="status">{grantReason}</p>}
  </fieldset>;
}
