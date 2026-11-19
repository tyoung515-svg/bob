import type { RunDetail, RunEvent, RunReceipt } from './portal-protocol';
import type { CheckRun, CheckedReceipt, Proposal, StepKind } from './workspace-v2-protocol';
import { record, STEP_KINDS, validCheckedManifest, validCheckedReceipt } from './checked-edits';
import { validInteger } from './durable-runs';
export function CheckReceipts({ runs }: { runs: CheckRun[] }) {
  return <section aria-label="Registered check receipts"><h4>Registered check receipts</h4>
    {!runs.length && <p>No check ran on these exact bytes.</p>}
    <ul>{runs.map((r, i) => 'passed' in r ? <li key={i}>
      <strong>{r.check_id}</strong>: {r.passed ? 'passed' : 'failed'} · outcome {r.outcome} · exit {r.exit_status ?? 'unknown'}
      <p>Snapshot <code>{r.snapshot_digest}</code> · input <code>{r.input_digest}</code></p>
      <p>Cleanup: {r.cleanup} · timed out: {String(r.timed_out)} · truncated: {String(r.truncated)} · {r.assertion_summary}</p>
      <details><summary>Check receipt evidence</summary><pre>{JSON.stringify(r, null, 2)}</pre></details>
    </li> : <li key={i}><strong>{r.check_id}</strong>: {r.ok ? 'passed' : 'failed'} · exit {r.exit_code} · snapshot {r.snapshot_sha256 ?? 'not reported'}</li>)}</ul>
  </section>;
}
function json(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  if (value.length > 500000) return null;
  try { return JSON.parse(value); } catch { return null; }
}
export type TypedStep = { step: string; id?: string; kind: StepKind; state: string; execution?: string; receipt?: CheckedReceipt };
// Project only persisted evidence. A plan does not prove its steps were dispatched.
export function typedSteps(receipts: RunReceipt[], events: RunEvent[]): TypedStep[] {
  const steps = new Map<string, TypedStep>();
  for (const r of receipts) {
    const output = json(r.output);
    if (record(output) && Array.isArray(output.tasks) && output.tasks.length <= 64 && validInteger(r.step, true)) {
      output.tasks.forEach((t, index) => {
        if (!record(t) || !STEP_KINDS.includes(t.kind as StepKind)) return;
        const step = (BigInt(r.step!) + BigInt(index + 1)).toString();
        steps.set(step, { step, id: typeof t.id === 'string' ? t.id : undefined, kind: t.kind as StepKind, state: 'planned; latest state not reported' });
      });
    }
  }
  for (const r of receipts) {
    if (!r.kind || !validInteger(r.step, true)) continue;
    const step = String(r.step), output = json(r.output);
    steps.set(step, { ...steps.get(step), step, kind: r.kind, state: 'completed', ...(validCheckedReceipt(output) ? { receipt: output, execution: output.execution_id, state: output.outcome } : {}) });
  }
  for (const e of [...events].sort((a, b) => a.seq - b.seq)) {
    const d = json(e.detail);
    if (!record(d) || !validInteger(d.step, true)) continue;
    const step = String(d.step), old = steps.get(step);
    if (e.kind === 'worker_step_checked' && validCheckedReceipt(d)) {
      steps.set(step, { ...old, step, kind: d.kind, state: d.outcome, receipt: d, execution: d.execution_id });
    } else if (old && !old.receipt && old.state !== 'completed') {
      if (e.kind === 'worker_step_admitted') steps.set(step, { ...old, state: 'queued', execution: typeof d.execution_id === 'string' ? d.execution_id : undefined });
      else if (['step_checked', 'step_completed'].includes(e.kind)) steps.set(step, { ...old, state: 'completed' });
      else if (['reserved', 'dispatched', 'attempt_reserved', 'attempt_dispatched', 'step_ready'].includes(e.kind)) steps.set(step, { ...old, state: e.kind.replace('attempt_', '').replace('step_', '') });
    }
  }
  return [...steps.values()].sort((a, b) => BigInt(a.step) < BigInt(b.step) ? -1 : 1);
}
export function CheckedRunEvidence({ detail, proposal }: { detail: RunDetail; proposal?: Proposal | null }) {
  const run = detail.run;
  if (run.worker_contract !== 'checked-edits/1') return null;
  const steps = typedSteps(detail.receipts, detail.events);
  const checks = steps.flatMap(s => s.receipt?.kind === 'registered_check' ? [s.receipt] : []);
  return <section aria-label="Checked edits"><h4>Checked edits</h4>
    <p>Registered checks prove particular checks ran. Independent verification: false. Apply is unavailable.</p>
    <h4>Typed steps</h4>
    {!steps.length && <p>No typed step evidence in this page yet.</p>}
    <ol aria-label="Typed steps">{steps.map(s => {
      const wait = run.job_waits?.find(j => j.execution_id === s.execution);
      return <li key={s.step}>Step {s.step}{s.id ? ' · ' + s.id : ''} · <strong>{s.kind}</strong> · {wait?.state ?? s.state}
        {s.receipt && <p>Snapshot <code>{s.receipt.snapshot_digest}</code> · cleanup {s.receipt.cleanup} · {s.receipt.passed ? 'passed' : 'failed'}</p>}
      </li>;
    })}</ol>
    <CheckReceipts runs={checks} />
    {/preimage|stale/.test(run.pause_reason ?? run.reason ?? '') && <p role="alert">Stale preimage refused: source bytes changed; the edit batch was refused without fuzzy application.</p>}
    {['cancelling','cancelled'].includes(run.status) && <p>Build cancellation: {run.status}. Cleanup is established only by the worker receipt or cancellation event.</p>}
    {detail.events.filter(e => e.kind === 'cancelled' || e.kind === 'cancelling').map(e => <p key={e.seq}>Cancellation cleanup: {JSON.stringify(e.detail)}</p>)}
    <h4>Accepted output</h4>
    {!run.accepted_outputs?.length && <p>No accepted output yet.</p>}
    {run.accepted_outputs?.map(o => {
      const manifest = o.check_receipts;
      if (!validCheckedManifest(manifest)) return <p role="alert" key={o.output_id}>Invalid checked manifest; accepted output cannot be established.</p>;
      const files = proposal?.digest === manifest.proposal_digest ? proposal.files : null;
      return <article key={o.output_id}><p>Accepted proposal <code>{manifest.proposal_digest}</code></p>
        <p>Manifest <code>{manifest.digest}</code> · base <code>{manifest.base_digest}</code> · output <code>{manifest.output_digest}</code></p>
        {files ? <ul aria-label="Accepted files">{files.map(f => <li key={f.path}>{f.operation} · {f.path} · {f.bytes} bytes</li>)}</ul> : <p>Read the associated job proposal below for its file manifest.</p>}
        <CheckReceipts runs={manifest.checks} />
        <details><summary>Accepted-output manifest</summary><pre>{JSON.stringify(manifest, null, 2)}</pre></details>
      </article>;
    })}
  </section>;
}
