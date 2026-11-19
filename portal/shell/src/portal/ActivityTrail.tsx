import { useExperience } from './experience';
import type { PcQueue, Receipt } from './PcAccess';

export type ToolActivity = { request_id: string; phase: string; tool: string; args?: string; preview?: string; ok?: boolean; elapsed_ms?: number; observed_ms?: number };
const LABELS: Record<string, string> = {
  web_search: 'Searched the web', web_fetch: 'Read a page', pc_run: 'Ran a command on this PC', pc_read: 'Read a file on this PC',
  pc_list: 'Listed a folder on this PC', pc_write: 'Wrote a file on this PC', pc_open: 'Opened something on this PC', pc_info: "Checked this PC's hardware",
  memory_save: 'Saved a memory', memory_recall: 'Looked in memory', conversations_find: 'Looked through past conversations', conv_search: 'Looked through past conversations', conv_expand: 'Looked through past conversations',
  list_backends: 'Checked available models', get_server_time: 'Checked the time', skill_list: 'Read its instructions', skill_view: 'Read its instructions', ask_operator: 'Asked you a question',
  fs_read: 'Read project files', fs_list: 'Read project files', fs_search: 'Read project files', fs_write: 'Changed a project file', fs_edit: 'Changed a project file', fs_delete: 'Changed a project file',
  shell_run: 'Ran a command in the project', preview_file: 'Looked at the result', task_finish: 'Finished',
};
export const toolLabel = (tool: string, simple: boolean) => simple ? LABELS[tool.replace(/^pc\./, 'pc_')] ?? tool : tool;
function target(args?: string): string {
  if (!args) return '';
  let a: Record<string, unknown>;
  try { a = JSON.parse(args); }
  catch {
    // Activity arguments may be a shortened wire preview. Recover only complete
    // JSON strings; never expose a raw JSON blob as the collapsed target.
    a = {};
    for (const key of ['query', 'url', 'path', 'command', 'cmd']) {
      const value = new RegExp(`"${key}"\\s*:\\s*("(?:[^"\\\\]|\\\\.)*")`).exec(args)?.[1];
      if (value) { try { a[key] = JSON.parse(value); } catch { /* Incomplete string. */ } }
    }
    const argv = /"argv"\s*:\s*\[([^\]]*)/.exec(args)?.[1];
    if (argv) a.argv = (argv.match(/"(?:[^"\\]|\\.)*"/g) ?? []).map(s => { try { return JSON.parse(s); } catch { return ''; } });
  }
  if (!a || typeof a !== 'object') return '';
  if (typeof a.query === 'string') return a.query;
  if (typeof a.url === 'string') { try { return new URL(a.url).host; } catch { return a.url; } }
  if (typeof a.path === 'string') {
    try { if (/^https?:/.test(a.path)) return new URL(a.path).host; } catch { /* path */ }
    return a.path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() ?? '';
  }
  const command = a.command ?? a.cmd ?? (Array.isArray(a.argv) ? a.argv.join(' ') : '');
  return typeof command === 'string' ? command.split(/\s+/).slice(0, 8).join(' ') : '';
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a],[b]) => a.localeCompare(b)).map(([k,v]) => JSON.stringify(k)+':'+canonical(v)).join(',') + '}';
  return JSON.stringify(value) ?? '';
}
type Step = ToolActivity & { key: string; receipt?: Receipt; receiptId?: string };
export function activitySteps(activity: ToolActivity[], queue: PcQueue, conversationId?: string | null): Step[] {
  const steps: Step[] = [];
  for (const frame of activity) {
    const start = frame.phase !== 'start' ? [...steps].reverse().find(s => s.phase === 'start' && s.request_id === frame.request_id && s.tool === frame.tool) : undefined;
    if (start) Object.assign(start, frame, { args: frame.args ?? start.args, observed_ms: start.observed_ms ?? frame.observed_ms });
    else steps.push({ ...frame, key: `tool-${steps.length}` });
  }
  const used = new Set<string>();
  const outcomes = Object.entries(queue.outcomes).filter(([,r]) => r.conversation_id === conversationId);
  for (const step of steps) {
    if (!/^pc[_.]/.test(step.tool)) continue;
    // Previews may be truncated JSON; request_id is the explicit broker/native join key.
    const id = /"request_id"\s*:\s*"([^"\\]+)"/.exec(step.preview ?? '')?.[1];
    const receipt = id ? outcomes.find(([key]) => key === id) : undefined;
    if (receipt) { step.receipt = receipt[1]; step.receiptId = receipt[0]; used.add(receipt[0]); }
  }
  // Native polling can beat the broker's result frame. Join that short window
  // only on exact arguments and a receipt produced since this call began.
  for (const step of steps.filter(s => !s.receipt && /^pc[_.]/.test(s.tool) && s.observed_ms != null)) {
    let args: unknown;
    try { args = JSON.parse(step.args ?? '{}'); } catch { continue; }
    const candidates = outcomes.filter(([id,r]) => !used.has(id) && r.operation?.replace('pc.', 'pc_') === step.tool.replace('pc.', 'pc_') &&
      r.request_args && (r.time ?? 0) * 1000 >= step.observed_ms! - 1000 && canonical(r.request_args) === canonical(args));
    if (candidates.length === 1) { const [id,receipt] = candidates[0]; step.receipt = receipt; step.receiptId = id; used.add(id); }
  }
  for (const [id, receipt] of outcomes.sort((a,b) => (a[1].time ?? 0) - (b[1].time ?? 0))) {
    if (!used.has(id)) steps.push({ key: `pc-${id}`, request_id: id, phase: 'result', args: receipt.request_args ? JSON.stringify(receipt.request_args) : undefined, tool: (receipt.operation ?? 'PC action').replace('pc.', 'pc_'), receipt, receiptId: id });
  }
  return steps;
}
function status(step: Step) {
  if (step.receipt) return step.receipt.status;
  if (step.ok === false || /"status"\s*:\s*"failed"/.test(step.preview ?? '')) return 'failed';
  return step.phase === 'start' ? 'running' : 'done';
}
export function ReceiptDetails({ receipt }: { receipt: Receipt }) {
  return <div className="pc-receipt" aria-label="PC execution result">
    <details><summary>Native receipt</summary>{receipt.reason && <p>{receipt.reason}</p>}
    {receipt.status === 'unknown' && <p>No mutation will be replayed. Inspect the native receipt before requesting fresh work.</p>}
    {receipt.receipt_path && <p>Full native receipt: {receipt.receipt_path}</p>}
    <pre>{JSON.stringify(receipt, null, 2)}</pre></details>
  </div>;
}
export function ActivityTrail({ activity, queue, conversationId, live }: { activity: ToolActivity[]; queue: PcQueue; conversationId?: string | null; live?: boolean }) {
  const simple = useExperience() === 'simple', steps = activitySteps(activity, queue, conversationId);
  const failed = steps.filter(s => status(s) === 'failed').length;
  return <details className="turn-activity" open>
    <summary>{steps.length} steps{failed ? ` · ${failed} failed` : ''} · {live ? 'Live trail' : 'How the last answer was produced'}</summary>
    <ol>{steps.map(step => <li key={step.key} className={status(step) === 'failed' ? 'failed' : step.phase}>
      <details className="activity-step"><summary onKeyDown={e => { if (e.key === ' ') { e.preventDefault(); e.currentTarget.click(); } }}>
        <span className="tool">{toolLabel(step.tool, simple)}</span><span className="step-target" title={target(step.args)}>{target(step.args)}</span>
        <span className="ms">{(step.elapsed_ms ?? step.receipt?.elapsed_ms) != null ? `${step.elapsed_ms ?? step.receipt?.elapsed_ms} ms` : '—'}</span><span className="step-status">{status(step)}</span>
      </summary><div className="step-detail">
        {step.args && <><strong>Arguments</strong><pre className="args">{step.args}</pre></>}
        {step.preview && <><strong>Result</strong><pre className="preview">{step.preview}</pre></>}
        {step.receipt && <ReceiptDetails receipt={step.receipt} />}
      </div></details>
    </li>)}</ol>
  </details>;
}
