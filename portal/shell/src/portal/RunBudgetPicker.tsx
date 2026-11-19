import { BUDGET_LEVELS, budgetWallSeconds, integerInput } from './durable-runs';
import type { RunBudget } from './portal-protocol';
import { useState } from 'react';

export function RunBudgetPicker({ budget, onChange, disabled = false, growthReason }: {
  budget: RunBudget; onChange: (budget: RunBudget) => void; disabled?: boolean; growthReason?: string;
}) {
  const [custom, setCustom] = useState(false);
  const preset = !custom && BUDGET_LEVELS.some(n => String(n) === String(budget.start_calls)) ? String(budget.start_calls) : 'custom';
  return <fieldset disabled={disabled}>
    <legend>Run budget</legend>
    {growthReason !== undefined && <p>{growthReason || 'If nothing usable comes out, Bob steps up 10→50→100→200; above 200 needs your OK.'}</p>}
    <label>Model calls <select aria-label="Call budget preset" value={preset} onChange={e => { setCustom(e.target.value === 'custom'); if (e.target.value !== 'custom') onChange({ ...budget, start_calls: Number(e.target.value) }); }}>
      {BUDGET_LEVELS.map(n => <option key={n} value={n}>{n}</option>)}<option value="custom">Custom</option>
    </select></label>
    {preset === 'custom' && <label>Custom calls <input aria-label="Custom calls" type="text" inputMode="numeric" value={Number.isNaN(budget.start_calls) ? '' : budget.start_calls} onChange={e => onChange({ ...budget, start_calls: integerInput(e.target.value) })} /></label>}
    <label>Time limit <select aria-label="Wall time mode" value={budget.wall_mode} onChange={e => onChange({ ...budget, wall_mode: e.target.value as RunBudget['wall_mode'], wall_seconds: e.target.value === 'none' ? null : budget.wall_seconds ?? budgetWallSeconds(budget.start_calls) })}>
      <option value="fixed">Proposed time limit</option><option value="follow_budget">Follow budget (initial deadline)</option><option value="none">No time limit</option>
    </select></label>
    {budget.wall_mode !== 'none' && <label>Seconds <input aria-label="Wall seconds" type="text" inputMode="numeric" value={budget.wall_seconds ?? ''} onChange={e => onChange({ ...budget, wall_seconds: integerInput(e.target.value) })} /></label>}
  </fieldset>;
}
