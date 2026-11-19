import type { Conversation } from './portal-protocol';
import { localSessionTime } from './local-time';

const key = (id: string) => `bob:default-session-title:v1:${id}`;
const NAMED = '@owner-or-seeded-title';
/** Display only: recognise the two historical agent/date defaults as well as
 * locally remembered defaults, without renaming a server conversation. */
export function displaySessionTitle(row: Conversation, agentName: string): string {
  const title = row.title?.trim() ?? '';
  let remembered: string | null = null;
  try { remembered = localStorage.getItem(key(row.id)); } catch { /* optional storage */ }
  if (!title) return 'New conversation';
  if (remembered === NAMED) return title;
  if (remembered === row.title) return 'New conversation';
  const prefix = `${agentName} · `;
  if (title.startsWith(prefix)) {
    const date = title.slice(prefix.length);
    const legacy = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(date);
    const english = /^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}, \d{4}, \d{1,2}:\d{2}[\s\u202f]+[AP]M$/i.test(date);
    const created = row.created_at ? new Date(row.created_at) : null;
    if (legacy || english || created && Number.isFinite(created.getTime()) && date === localSessionTime(created)) return 'New conversation';
  }
  return title;
}

export function rememberDefaultTitle(id: string, title: string | null) {
  try { localStorage.setItem(key(id), title ?? NAMED); } catch { /* Storage may be unavailable. */ }
}
export function firstMessageTitle(id: string, title: string | null | undefined, text: string, creationDefault?: string): string | null {
  try {
    const remembered = localStorage.getItem(key(id));
    // Older empty sessions have no local marker. Require the exact agent/time
    // title derived from the server's creation timestamp, not a loose pattern.
    if (!title || remembered === NAMED || title !== (remembered ?? creationDefault)) return null;
  } catch { return null; }
  const line = text.trim().split(/\r?\n/)[0].trim();
  const chars = Array.from(line);
  if (chars.length <= 60) return line || null;
  const head = chars.slice(0, 60).join('');
  if (/\s/.test(chars[60])) return head.trim();
  const cut = head.search(/\s+\S*$/);
  return (cut > 0 ? head.slice(0, cut) : head).trim();
}
