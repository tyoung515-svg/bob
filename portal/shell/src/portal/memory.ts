// Memory explorer model (owner request 2026-11-02): the server's central memory
// as a flat file tree and a 3D "brain", with health flags and server coverage.
import type { PortalClient } from './portal-client';
import { toPortalError } from './portal-protocol';
import type { Hello, MemoryHit, MemoryStatus } from './portal-protocol';

export type Flag = 'test' | 'error' | 'duplicate' | 'short' | 'orphan';

export const FLAG_LABEL: Record<Flag, string> = {
  test: 'test / probe data',
  error: 'turn recorded an error',
  duplicate: 'duplicate text',
  short: 'very short',
  orphan: 'source event missing',
};

export type MemItem = MemoryHit & {
  title: string;
  /** Folder path in the tree, e.g. ["Family memory", "dad", "MEMORY.md"]. */
  folder: string[];
  when: string | null;
  flags: Flag[];
  /** Links for the brain view. */
  conversation: string | null;
  source_event: string | null;
  order: number;
};

export type MemoryHealth = {
  total: number;
  expected: number | null;
  byKind: Record<string, number>;
  flags: Record<Flag, number>;
  flagged: number;
  /** Share of items with no flag, 0–100. */
  score: number;
};

const LETTERS = ['e', 'a', 'o', 'i', 't', 'n', 's', 'r'];
const PAGE = 50;

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function parseJson(text: string): Record<string, unknown> | null {
  if (!text.trim().startsWith('{')) return null;
  try {
    return record(JSON.parse(text));
  } catch {
    return null;
  }
}

/** A string field from JSON text that may be cut off part-way (search excerpts are). */
function field(text: string, key: string): string | null {
  const match = new RegExp(`"${key}": "((?:[^"\\\\]|\\\\.)*)`).exec(text);
  if (!match || !match[1]) return null;
  return match[1].replace(/\\n/g, '\n').replace(/\\"/g, '"').replace(/\\\\/g, '\\');
}

function firstLine(text: string, max = 110): string {
  const line = text.replace(/^[-*#>\s]+/, '').split('\n')[0].trim();
  return line.length > max ? `${line.slice(0, max - 1)}…` : line || '(empty)';
}

export function expectedTotal(status: MemoryStatus | null): number | null {
  const counts = record(status?.counts_by_kind);
  if (!counts) return null;
  return Object.values(counts).reduce<number>((sum, n) => sum + (typeof n === 'number' ? n : 0), 0);
}

/** Enumeration is authoritative only for the sources in its returned coverage. */
export async function loadAll(
  client: PortalClient,
  expected: number | null,
  onProgress: (count: number, calls: number) => void,
  operations?: Hello['operations'],
): Promise<{ hits: MemoryHit[]; calls: number; coverage: MemoryStatus | null; mode: 'list' | 'search' }> {
  const seen = new Map<string, MemoryHit>();
  let calls = 0;
  // With no discovery (e.g. a caller outside PortalApp), probe once. Only a
  // missing operation permits legacy search; permission/expiry faults surface.
  if (operations === undefined || operations['memory.list']?.implemented) {
    let cursor: string | undefined;
    let snapshot: string | undefined;
    let coverage: MemoryStatus | null = null;
    const cursors = new Set<string>();
    try {
      do {
        const page = await client.memoryList(PAGE, cursor);
        calls += 1;
        if (!page.snapshot || (snapshot && snapshot !== page.snapshot)) throw new Error('Memory snapshot changed during paging. Refresh to retry.');
        snapshot = page.snapshot;
        coverage = page.coverage;
        for (const hit of page.items) if (hit.source_id) seen.set(hit.source_id, hit);
        onProgress(seen.size, calls);
        cursor = page.next_cursor ?? undefined;
        if (cursor && cursors.has(cursor)) throw new Error('Memory cursor repeated. Refresh to retry.');
        if (cursor) cursors.add(cursor);
      } while (cursor);
      return { hits: [...seen.values()], calls, coverage, mode: 'list' };
    } catch (error) {
      if (calls || !['unknown_op', 'unavailable'].includes(toPortalError(error).code)) throw error;
    }
  }
  for (const letter of LETTERS) {
    for (let offset = 0; ; offset += PAGE) {
      const page = await client.memorySearch(letter, PAGE, offset);
      calls += 1;
      for (const hit of page.items) if (hit.source_id) seen.set(hit.source_id, hit as MemoryHit);
      onProgress(seen.size, calls);
      if (page.items.length < PAGE) break;
      if (expected !== null && seen.size >= expected) break;
    }
    if (expected !== null && seen.size >= expected) break;
  }
  return { hits: [...seen.values()], calls, coverage: null, mode: 'search' };
}

function describe(hit: MemoryHit, order: number): MemItem {
  const provenance = record(hit.provenance) ?? (typeof hit.provenance === 'string' ? parseJson(hit.provenance) : null) ?? {};
  const body = parseJson(hit.excerpt ?? '');
  let title = firstLine(hit.excerpt ?? '');
  let folder: string[] = ['Other', hit.kind || 'unknown'];
  let when: string | null = typeof provenance.timestamp === 'string' ? provenance.timestamp : null;
  let conversation: string | null = null;
  let sourceEvent: string | null = typeof provenance.source_event_id === 'string' ? provenance.source_event_id : null;
  if (hit.kind === 'family_memory') {
    const path = typeof provenance.path === 'string' ? provenance.path : '';
    const parts = path.split('/').filter(Boolean).slice(-2);
    folder = ['Family memory', ...(parts.length ? parts : ['unknown'])];
  } else if (hit.kind === 'bob_event') {
    const excerpt = hit.excerpt ?? '';
    const said = field(excerpt, 'user_message');
    const answered = field(excerpt, 'assistant_response');
    const face = field(excerpt, 'face_id');
    title = said ? `You: ${firstLine(said, 100)}` : answered ? `${face ?? 'Bob'}: ${firstLine(answered, 100)}` : `${face ?? 'Bob'} turn (no text recorded)`;
    conversation = field(excerpt, 'conversation_id');
    folder = ['Bob events', when ? when.slice(0, 7) : 'undated'];
  } else if (hit.kind === 'bob_fact') {
    const text = (body && typeof body.text === 'string' ? body.text : null) ?? field(hit.excerpt ?? '', 'text');
    if (text) title = firstLine(text);
    folder = ['Bob facts'];
  }
  return {
    ...hit,
    title,
    folder,
    when,
    flags: [],
    conversation,
    source_event: sourceEvent ? `event:${sourceEvent}` : null,
    order,
  };
}

const TEST_PATTERN = /\b(probe|re-?test|marker|diagnostic|smoke ?test|canary|dummy|lorem)\b/i;

export function describeAll(hits: MemoryHit[]): MemItem[] {
  const items = hits.map(describe);
  const ids = new Set(items.map((item) => item.source_id));
  const seenText = new Map<string, string>();
  for (const item of items) {
    const text = (item.excerpt ?? '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (TEST_PATTERN.test(item.excerpt ?? '')) item.flags.push('test');
    if (item.kind === 'bob_event' && field(item.excerpt ?? '', 'error')) item.flags.push('error');
    if (text.length > 30) {
      if (seenText.has(text)) item.flags.push('duplicate');
      else seenText.set(text, item.source_id);
    }
    if (text.length > 0 && text.length < 25) item.flags.push('short');
    if (item.kind === 'bob_fact' && item.source_event && !ids.has(item.source_event)) item.flags.push('orphan');
  }
  // Paragraphs read in file order; events and facts newest first.
  const paragraph = (id: string) => Number(id.split(':').pop()) || 0;
  return items.sort(
    (a, b) =>
      a.folder.join('/').localeCompare(b.folder.join('/')) ||
      (a.kind === 'family_memory' ? paragraph(a.source_id) - paragraph(b.source_id) : String(b.when).localeCompare(String(a.when))),
  );
}

export function health(items: MemItem[], expected: number | null): MemoryHealth {
  const flags: Record<Flag, number> = { test: 0, error: 0, duplicate: 0, short: 0, orphan: 0 };
  const byKind: Record<string, number> = {};
  let flagged = 0;
  for (const item of items) {
    byKind[item.kind] = (byKind[item.kind] ?? 0) + 1;
    for (const flag of item.flags) flags[flag] += 1;
    if (item.flags.length) flagged += 1;
  }
  return {
    total: items.length,
    expected,
    byKind,
    flags,
    flagged,
    score: items.length ? Math.round(((items.length - flagged) / items.length) * 100) : 100,
  };
}

export type TreeNode = { name: string; path: string; count: number; flagged: number; children: TreeNode[] };

export function buildTree(items: MemItem[]): TreeNode {
  const root: TreeNode = { name: 'All memory', path: '', count: 0, flagged: 0, children: [] };
  for (const item of items) {
    let node = root;
    node.count += 1;
    if (item.flags.length) node.flagged += 1;
    let path = '';
    for (const part of item.folder) {
      path = path ? `${path}/${part}` : part;
      let child = node.children.find((row) => row.name === part);
      if (!child) {
        child = { name: part, path, count: 0, flagged: 0, children: [] };
        node.children.push(child);
      }
      child.count += 1;
      if (item.flags.length) child.flagged += 1;
      node = child;
    }
  }
  return root;
}

export type GraphNode = { id: string; label: string; kind: string; hub: boolean; flagged: boolean; size: number };
export type GraphLink = { source: string; target: string; kind: 'in' | 'next' | 'from' | 'conversation' };

/**
 * The brain: one hub per file or group, every memory linked to its hub,
 * paragraphs chained in file order, facts to the event they came from, and
 * events of the same conversation to each other.
 */
export function buildGraph(items: MemItem[]): { nodes: GraphNode[]; links: GraphLink[] } {
  const nodes: GraphNode[] = [];
  const links: GraphLink[] = [];
  const hubs = new Map<string, GraphNode>();
  const ids = new Set(items.map((item) => item.source_id));
  const lastInFolder = new Map<string, string>();
  const lastInConversation = new Map<string, string>();
  for (const item of items) {
    const hubId = `hub:${item.folder.join('/')}`;
    let hub = hubs.get(hubId);
    if (!hub) {
      hub = { id: hubId, label: item.folder.join(' / '), kind: 'hub', hub: true, flagged: false, size: 0 };
      hubs.set(hubId, hub);
      nodes.push(hub);
    }
    hub.size += 1;
    nodes.push({ id: item.source_id, label: item.title, kind: item.kind, hub: false, flagged: item.flags.length > 0, size: 1 });
    links.push({ source: item.source_id, target: hubId, kind: 'in' });
    if (item.kind === 'family_memory') {
      const prior = lastInFolder.get(hubId);
      if (prior) links.push({ source: prior, target: item.source_id, kind: 'next' });
      lastInFolder.set(hubId, item.source_id);
    }
    if (item.source_event && ids.has(item.source_event)) links.push({ source: item.source_id, target: item.source_event, kind: 'from' });
    if (item.conversation) {
      const prior = lastInConversation.get(item.conversation);
      if (prior) links.push({ source: prior, target: item.source_id, kind: 'conversation' });
      lastInConversation.set(item.conversation, item.source_id);
    }
  }
  return { nodes, links };
}

export const KIND_COLOR: Record<string, string> = {
  family_memory: '#7fb3e6',
  bob_event: '#8fd3a5',
  bob_fact: '#e9cf95',
  hub: '#c8dccd',
};
