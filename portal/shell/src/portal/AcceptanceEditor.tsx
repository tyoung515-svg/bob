import type { AcceptanceContract, AcceptanceCriterion } from './portal-protocol';
import { criterionSummary } from './durable-runs';

export function AcceptanceEditor({ value, onChange, disabled = false }: {
  value: AcceptanceContract; onChange: (value: AcceptanceContract) => void; disabled?: boolean;
}) {
  function edit(index: number, patch: Partial<AcceptanceCriterion>) {
    onChange({ criteria: value.criteria.map((c, i) => i === index ? { ...c, ...patch } : c) });
  }
  return <fieldset disabled={disabled}><legend>What done looks like</legend>
    {value.criteria.map((c, i) => <div className="run-row" key={c.id}>
      <p>{criterionSummary(c)}</p>
      <label><input aria-label={`Criterion ${i + 1} required`} type="checkbox" checked={c.required} onChange={e => edit(i, { required: e.target.checked })} /> Required</label>
      <label>Output kind <select aria-label={`Criterion ${i + 1} kind`} value={c.output_kind} onChange={e => edit(i, { output_kind: e.target.value as AcceptanceCriterion['output_kind'] })}>
        {['answer', 'report', 'proposal', 'json'].map(kind => <option key={kind}>{kind}</option>)}
      </select></label>
      <label>Required sections (one per line)<textarea aria-label={`Criterion ${i + 1} sections`} value={(c.checks.required_sections ?? []).join('\n')} onChange={e => edit(i, { checks: { ...c.checks, required_sections: e.target.value ? e.target.value.split('\n') : [] } })} /></label>
      <label><input aria-label={`Criterion ${i + 1} citations`} type="checkbox" checked={c.checks.require_citations ?? false} onChange={e => edit(i, { checks: { ...c.checks, require_citations: e.target.checked } })} /> Cite supplied evidence</label>
      {(['min_length', 'max_length'] as const).map(key => <label key={key}>{key === 'min_length' ? 'Minimum' : 'Maximum'} characters <input aria-label={`Criterion ${i + 1} ${key}`} type="number" min={1} max={60000} step={1} value={Number.isNaN(c.checks[key]) ? '' : c.checks[key] ?? (key === 'min_length' ? 1 : 60000)} onChange={e => edit(i, { checks: { ...c.checks, [key]: e.target.valueAsNumber } })} /></label>)}
      {c.output_kind === 'json' && <label>JSON type <select aria-label={`Criterion ${i + 1} JSON type`} value={c.checks.json_type ?? 'object'} onChange={e => edit(i, { checks: { ...c.checks, json_type: e.target.value as NonNullable<AcceptanceCriterion['checks']['json_type']> } })}>{['object', 'array', 'string', 'number', 'boolean', 'null'].map(t => <option key={t}>{t}</option>)}</select></label>}
      <button type="button" onClick={() => onChange({ criteria: value.criteria.filter((_, n) => n !== i) })}>Remove criterion {i + 1}</button>
    </div>)}
    <button type="button" disabled={value.criteria.length >= 100} onClick={() => onChange({ criteria: [...value.criteria, { id: `criterion-${crypto.randomUUID()}`, required: true, output_kind: 'answer', checks: { min_length: 200, max_length: 2000, required_sections: ['Conclusion'], require_citations: true } }] })}>Add criterion</button>
  </fieldset>;
}
