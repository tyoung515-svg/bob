import type { Conversation, Message } from './portal-protocol';
import { activitySteps, type ToolActivity } from './ActivityTrail';
import type { SendMark } from './portal-state';

export type ManagedOutcome = 'finished' | 'stopped' | 'ran_out_of_time' | 'approval_expired' | 'question_unanswered' | 'error';
export type ManagedRun = {
  id: string; origin: string; originFace?: 'assistant' | 'planner'; agent: string; name: string; title: string; session: Conversation;
  request: string; brief?: string; before?: string[]; state: 'working' | 'needs you' | 'interrupted' | ManagedOutcome;
  content: string; rows: Message[]; activity: ToolActivity[];
  report: 'pending' | 'sending' | 'sent' | 'uncertain'; reportText?: string; reportMark?: SendMark; reportDispatched?: boolean;
};
export function managedOrigin(face: string | null | undefined): 'assistant' | 'planner' {
  if (face !== 'assistant' && face !== 'planner') throw new Error('Managed work needs an Assistant or Planner conversation.');
  return face;
}
export const originName = (face: string | null | undefined) => face === 'planner' ? 'Planner' : 'Assistant';
export const managedKey = (scope: string) => `bob:managed:v1:${scope}`;
export function readManaged(scope: string): ManagedRun[] {
  try {
    const rows = JSON.parse(localStorage.getItem(managedKey(scope)) ?? '[]');
    if (!Array.isArray(rows)) return [];
    return rows.filter(r => r && typeof r.id === 'string' && typeof r.origin === 'string' && typeof r.session?.id === 'string' && Array.isArray(r.rows) && Array.isArray(r.activity))
      .map(r => ({ ...r, state: ['working', 'needs you'].includes(r.state) ? 'interrupted' : r.state,
        report: r.report === 'sending' ? (r.reportDispatched ? 'uncertain' : 'pending') : r.report }));
  } catch { return []; }
}
export function managedOutcome(kind: string, code?: string | null, unanswered = false): ManagedOutcome {
  if (kind === 'complete') return 'finished';
  if (unanswered || code === 'question_unanswered') return 'question_unanswered';
  if (code === 'approval_expired') return 'approval_expired';
  if (['stream_deadline', 'deadline', 'timeout'].includes(code ?? '')) return 'ran_out_of_time';
  return kind === 'error' ? 'error' : 'stopped';
}
export function managedStats(run: ManagedRun) {
  const steps = activitySteps(run.activity, { requests: [], outcomes: {} }, run.session.id);
  return { steps, failed: steps.filter(s => s.ok === false || /"status"\s*:\s*"failed"/.test(s.preview ?? '')).length };
}
export function managedReport(run: ManagedRun): string {
  const { steps, failed } = managedStats(run);
  const outcome = ['working', 'needs you', 'interrupted'].includes(run.state) ? 'error' : run.state;
  return `${run.name} report: ${run.title}\nsession: ${run.session.id}\noutcome: ${outcome}\nsteps: ${steps.length} (${failed} failed)\n--- report ---\n${Array.from(run.content).slice(0, 6000).join('')}`;
}
/** Only this dispatch's answer; later ordinary messages start another turn. */
export function managedFinal(rows: Message[], run: ManagedRun): string {
  const keyed = rows.filter(m => m.role === 'assistant' && !!run.request &&
    (m.metadata as Record<string, unknown> | null)?.request_id === run.request);
  if (keyed.length) return keyed[keyed.length - 1].content;
  const at = rows.findIndex(m => m.role === 'user' && m.content === run.brief && !(run.before ?? []).includes(m.id));
  let answer = run.content;
  if (at >= 0) for (const row of rows.slice(at + 1)) {
    if (row.role === 'user' && !row.content.startsWith('Operator answer to: ')) break;
    if (row.role === 'assistant') answer = row.content;
  }
  return answer;
}
export function reportAgent(text: string): string | null {
  return /^([^\n]+) report: [^\n]+\nsession: [^\n]+\noutcome: (?:finished|stopped|ran_out_of_time|approval_expired|question_unanswered|error)\nsteps: \d+ \(\d+ failed\)\n--- report ---\n/.exec(text)?.[1] ?? null;
}
