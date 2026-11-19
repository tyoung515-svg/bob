// Tasks calendar model (owner request 2026-11-02): one calendar for the person
// and the agent systems. Entries are stored on this PC (src-tauri local_store.rs);
// agent entries never start on their own — when due they wait for "Run now".
import type { NativeInvoke } from '../workbench/workbench-client';
import type { RunBudget } from './portal-protocol';

export type Repeat = 'none' | 'daily' | 'weekdays' | 'weekly' | 'monthly';
export type Who = 'person' | 'agent';

export type AgentPlan = { team: string; task: string; max_calls: import('./portal-protocol').WireInteger; budget?: RunBudget; workflow_ref?: import('./workflow-library').DefinitionReference; workflow_inputs?: Record<string,unknown>; workflow_choices?: import('./WorkflowLibrary').WorkflowChoices };

/** One run an entry started, for the occurrence on `date`. */
export type EntryRun = { date: string; run_id: string; at: string };

export type CalEntry = {
  id: string;
  title: string;
  who: Who;
  /** First occurrence, YYYY-MM-DD. */
  date: string;
  /** HH:MM, or null for all day. */
  start: string | null;
  minutes: number;
  repeat: Repeat;
  notes: string;
  agent: AgentPlan | null;
  runs: EntryRun[];
  created_at: string;
};

export type Occurrence = { entry: CalEntry; date: string };

export const REPEAT_LABEL: Record<Repeat, string> = {
  none: 'Does not repeat',
  daily: 'Every day',
  weekdays: 'Every weekday',
  weekly: 'Every week',
  monthly: 'Every month',
};

export function ymd(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function parseYmd(text: string): Date {
  const [y, m, d] = text.split('-').map(Number);
  const date = new Date(0);
  date.setHours(0, 0, 0, 0);
  date.setFullYear(y, (m || 1) - 1, d || 1);
  return date;
}

/** Does the entry occur on this day? */
export function occursOn(entry: CalEntry, day: string): boolean {
  if (day < entry.date) return false;
  if (day === entry.date) return true;
  const first = parseYmd(entry.date);
  const date = parseYmd(day);
  switch (entry.repeat) {
    case 'daily':
      return true;
    case 'weekdays':
      return date.getDay() !== 0 && date.getDay() !== 6;
    case 'weekly':
      return date.getDay() === first.getDay();
    case 'monthly':
      return date.getDate() === first.getDate();
    default:
      return false;
  }
}

export function occurrencesOn(entries: CalEntry[], day: string): Occurrence[] {
  return entries
    .filter((entry) => occursOn(entry, day))
    .map((entry) => ({ entry, date: day }))
    .sort((a, b) => (a.entry.start ?? '').localeCompare(b.entry.start ?? '') || a.entry.title.localeCompare(b.entry.title));
}

/** The 6×7 grid of days shown for a month, Monday first. */
export function monthGrid(year: number, month: number): string[] {
  const first = new Date(year, month, 1);
  const offset = (first.getDay() + 6) % 7;
  const start = new Date(year, month, 1 - offset);
  return Array.from({ length: 42 }, (_, i) => ymd(new Date(start.getFullYear(), start.getMonth(), start.getDate() + i)));
}

/** An agent occurrence is due once its time has passed today and it has not run for that date. */
export function isDue(occurrence: Occurrence, now = new Date()): boolean {
  const { entry, date } = occurrence;
  if (entry.who !== 'agent' || !entry.agent) return false;
  if (entry.runs.some((run) => run.date === date)) return false;
  const today = ymd(now);
  if (date !== today) return false;
  if (!entry.start) return true;
  const [h, m] = entry.start.split(':').map(Number);
  return now.getHours() * 60 + now.getMinutes() >= h * 60 + m;
}

export function newEntry(date: string, who: Who): CalEntry {
  const id = globalThis.crypto?.randomUUID?.() ?? `cal-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  return {
    id,
    title: '',
    who,
    date,
    start: '09:00',
    minutes: 30,
    repeat: 'none',
    notes: '',
    agent: who === 'agent' ? { team: '', task: '', max_calls: 50, budget: { start_calls: 50, wall_mode: 'follow_budget', wall_seconds: 9000 } } : null,
    runs: [],
    created_at: new Date().toISOString(),
  };
}

function isEntry(value: unknown): value is CalEntry {
  if (!value || typeof value !== 'object') return false;
  const row = value as Record<string, unknown>;
  return typeof row.id === 'string' && typeof row.title === 'string' && typeof row.date === 'string' && (row.who === 'person' || row.who === 'agent');
}

export function createCalendarStore(invoke: NativeInvoke) {
  let pending = Promise.resolve();
  const store = {
    async load(): Promise<CalEntry[]> {
      const parsed = JSON.parse(await invoke<string>('store_load', { name: 'calendar' })) as unknown;
      return Array.isArray(parsed)
        ? parsed.filter(isEntry).map((row) => ({ ...row, runs: Array.isArray(row.runs) ? row.runs : [], notes: row.notes ?? '' }))
        : [];
    },
    save: (entries: CalEntry[]) => invoke<string>('store_save', { name: 'calendar', data: JSON.stringify(entries, null, 2) }),
    addOnce(entry: CalEntry): Promise<void> {
      const next = pending.then(async () => {
        const rows = await store.load();
        if (!rows.some(row => row.id === entry.id)) await store.save([...rows, entry]);
      });
      pending = next.catch(() => {});
      return next;
    },
  };
  return store;
}

export type CalendarStore = ReturnType<typeof createCalendarStore>;
