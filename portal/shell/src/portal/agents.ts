// Messenger-style agent threads (owner intent 2026-11-01): each main agent is
// one persistent thread, and every server conversation with that face is a
// session inside it, so new work never piles up as loose conversations. A
// thread that needs the owner rises to the top; a working one comes next.
import type { Conversation, Face } from './portal-protocol';

export type AgentStatus = 'needs_you' | 'working' | 'idle';

export type AgentThread = {
  /** '' is the server's default face (conversations with no face pin). */
  key: string;
  face_id: string | null;
  name: string;
  /** Newest first. */
  sessions: Conversation[];
  last_at: string;
  status: AgentStatus;
  /** Pending approvals that belong to this agent's sessions. */
  pending: number;
  /** false = registered but outside this portal token's allow-list. */
  allowed: boolean;
};

export type AgentSignals = {
  /** Conversation the live turn is running in, if any. */
  working: string | string[] | null;
  /** Conversation a parked question belongs to, if any. */
  asking: string | string[] | null;
  /** Pending approval count by conversation id. */
  approvals: Record<string, number>;
};

export const DEFAULT_AGENT_NAME = 'Bob';

export function agentKey(faceId: string | null | undefined): string {
  return faceId ?? '';
}

function stamp(row: Conversation): string {
  return String(row.updated_at ?? row.created_at ?? '');
}

const RANK: Record<AgentStatus, number> = { needs_you: 0, working: 1, idle: 2 };

export function buildAgents(conversations: Conversation[], faces: Face[], signals: AgentSignals): AgentThread[] {
  const byKey = new Map<string, AgentThread>();
  const faceName = new Map(faces.map((face) => [face.id, face]));
  const ensure = (faceId: string | null): AgentThread => {
    const key = agentKey(faceId);
    let thread = byKey.get(key);
    if (!thread) {
      const face = faceId ? faceName.get(faceId) : undefined;
      thread = {
        key,
        face_id: faceId,
        name: faceId ? face?.name || faceId : DEFAULT_AGENT_NAME,
        sessions: [],
        last_at: '',
        status: 'idle',
        pending: 0,
        allowed: face ? face.allowed !== false : true,
      };
      byKey.set(key, thread);
    }
    return thread;
  };
  for (const row of conversations) ensure(row.face_id).sessions.push(row);
  // Permitted faces with no sessions yet are still contacts you can start with.
  for (const face of faces) if (face.allowed !== false) ensure(face.id);
  for (const thread of byKey.values()) {
    thread.sessions.sort((a, b) => stamp(b).localeCompare(stamp(a)));
    thread.last_at = thread.sessions.length ? stamp(thread.sessions[0]) : '';
    const ids = new Set(thread.sessions.map((row) => row.id));
    thread.pending = [...ids].reduce((sum, id) => sum + (signals.approvals[id] ?? 0), 0);
    const contains = (value: string | string[] | null) => (Array.isArray(value) ? value : value ? [value] : []).some(id => ids.has(id));
    if (contains(signals.asking) || thread.pending > 0) thread.status = 'needs_you';
    else if (contains(signals.working)) thread.status = 'working';
  }
  const ordered = [...byKey.values()].sort(
    (a, b) =>
      RANK[a.status] - RANK[b.status] ||
      Number(b.sessions.length > 0) - Number(a.sessions.length > 0) ||
      b.last_at.localeCompare(a.last_at) ||
      a.name.localeCompare(b.name),
  );
  const planner = ordered.find(row => row.face_id === 'planner');
  if (planner && ordered.some(row => row.face_id === 'assistant')) {
    ordered.splice(ordered.indexOf(planner), 1);
    ordered.splice(ordered.findIndex(row => row.face_id === 'assistant') + 1, 0, planner);
  }
  return ordered;
}

export function initials(name: string): string {
  const words = name.replace(/[^\p{L}\p{N} ]+/gu, ' ').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  return (words.length === 1 ? words[0].slice(0, 2) : words[0][0] + words[1][0]).toUpperCase();
}

/** A stable hue per agent, so an avatar keeps its colour between runs. */
export function hue(key: string): number {
  let hash = 7;
  for (const ch of key || DEFAULT_AGENT_NAME) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
  return hash;
}

export function ago(iso: string, now = Date.now()): string {
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return '';
  const minutes = Math.round((now - at) / 60000);
  if (minutes < 1) return 'now';
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d`;
  return new Date(at).toISOString().slice(0, 10);
}

export const STATUS_LABEL: Record<AgentStatus, string> = {
  needs_you: 'needs you',
  working: 'working',
  idle: '',
};
