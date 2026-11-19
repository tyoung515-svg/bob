// Library (owner request 2026-11-02): the apps and workflows Bob has built, kept
// like a game library (Steam / Xbox) — browse, launch, manage, with agents to
// drive them. The server has no artifact list yet, so entries are the owner's
// own, kept on this PC; nothing is added that the owner did not add.
import type { NativeInvoke } from '../workbench/workbench-client';

export type AppKind = 'app' | 'workflow' | 'tool';
export type AppStatus = 'planned' | 'building' | 'ready' | 'retired';

export type LibraryApp = {
  id: string;
  name: string;
  kind: AppKind;
  status: AppStatus;
  summary: string;
  /** Shell command that starts it on this PC (optional). */
  command: string;
  /** Folder that holds it on this PC (optional), opened in the Workspace. */
  folder: string;
  /** Server project it belongs to, when there is one. */
  project_id: string | null;
  /** Project-linked entries always use the sandboxed latest HTML preview. */
  launch?: 'open_result';
  /** Workflow sketch attached to it, when there is one. */
  sketch_id: string | null;
  launches: number;
  last_launched: string | null;
  created_at: string;
};

export const KIND_LABEL: Record<AppKind, string> = { app: 'App', workflow: 'Workflow', tool: 'Tool' };
export const STATUS_LABEL: Record<AppStatus, string> = { planned: 'Planned', building: 'Building', ready: 'Ready', retired: 'Retired' };

function uid(prefix: string): string {
  return globalThis.crypto?.randomUUID?.() ?? `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export function newApp(name: string, kind: AppKind = 'app'): LibraryApp {
  return {
    id: uid('app'),
    name,
    kind,
    status: 'planned',
    summary: '',
    command: '',
    folder: '',
    project_id: null,
    sketch_id: null,
    launches: 0,
    last_launched: null,
    created_at: new Date().toISOString(),
  };
}

// ── Workflow sketches: a drawing for the agent, not a wired pipeline ──────

export type NodeType = 'trigger' | 'agent' | 'team' | 'tool' | 'decision' | 'human' | 'output' | 'note';

export const NODE_TYPES: { type: NodeType; label: string; hint: string }[] = [
  { type: 'trigger', label: 'Trigger', hint: 'What starts it: a time, a message, a file…' },
  { type: 'agent', label: 'Agent', hint: 'One agent doing one thing' },
  { type: 'team', label: 'Team', hint: 'A team of agents working together' },
  { type: 'tool', label: 'Tool / app', hint: 'A program, API or service' },
  { type: 'decision', label: 'Decision', hint: 'A choice or a check' },
  { type: 'human', label: 'Me', hint: 'Where you look, approve or answer' },
  { type: 'output', label: 'Result', hint: 'What comes out' },
  { type: 'note', label: 'Note', hint: 'Anything else the agent should know' },
];

export type SketchNode = { id: string; type: NodeType; label: string; x: number; y: number; stage_key?: string };
export type SketchEdge = { from: string; to: string };
export type Sketch = { id: string; name: string; nodes: SketchNode[]; edges: SketchEdge[]; updated_at: string; definition_ref?: import('./workflow-library').DefinitionReference; annotations?: string };

export function newSketch(name: string): Sketch {
  return { id: uid('sketch'), name, nodes: [], edges: [], updated_at: new Date().toISOString() };
}

export function newNode(type: NodeType, x: number, y: number): SketchNode {
  const label = NODE_TYPES.find((row) => row.type === type)?.label ?? type;
  return { id: uid('n'), type, label, x, y };
}

/** The sketch in words, in flow order, so an agent can work from it. */
export function sketchText(sketch: Sketch): string {
  const byId = new Map(sketch.nodes.map((node) => [node.id, node]));
  const incoming = new Map<string, number>();
  for (const edge of sketch.edges) incoming.set(edge.to, (incoming.get(edge.to) ?? 0) + 1);
  // Kahn order from the starts; whatever is left (cycles, loose nodes) follows.
  const queue = sketch.nodes.filter((node) => !incoming.get(node.id)).map((node) => node.id);
  const order: string[] = [];
  const left = new Map(incoming);
  while (queue.length) {
    const id = queue.shift() as string;
    if (order.includes(id)) continue;
    order.push(id);
    for (const edge of sketch.edges.filter((row) => row.from === id)) {
      left.set(edge.to, (left.get(edge.to) ?? 1) - 1);
      if ((left.get(edge.to) ?? 0) <= 0) queue.push(edge.to);
    }
  }
  for (const node of sketch.nodes) if (!order.includes(node.id)) order.push(node.id);
  const number = new Map(order.map((id, index) => [id, index + 1]));
  const lines = order.map((id) => {
    const node = byId.get(id) as SketchNode;
    const next = sketch.edges.filter((edge) => edge.from === id).map((edge) => number.get(edge.to));
    const kind = NODE_TYPES.find((row) => row.type === node.type)?.label ?? node.type;
    return `${number.get(id)}. [${kind}] ${node.label}${next.length ? ` → ${next.join(', ')}` : ''}`;
  });
  return `Workflow sketch “${sketch.name}” (${sketch.nodes.length} steps, ${sketch.edges.length} links):\n${lines.join('\n') || '(empty)'}${sketch.definition_ref ? `\nSaved definition reference: ${JSON.stringify(sketch.definition_ref)}` : ''}\nAnnotations: ${sketch.annotations || '(none)'}`;
}

export function createLibraryStore(invoke: NativeInvoke) {
  async function load<T>(name: string): Promise<T[]> {
    const parsed = JSON.parse(await invoke<string>('store_load', { name })) as unknown;
    return Array.isArray(parsed) ? (parsed as T[]) : [];
  }
  return {
    apps: () => load<LibraryApp>('library'),
    saveApps: (apps: LibraryApp[]) => invoke<string>('store_save', { name: 'library', data: JSON.stringify(apps, null, 2) }),
    sketches: () => load<Sketch>('sketches'),
    saveSketches: (sketches: Sketch[]) => invoke<string>('store_save', { name: 'sketches', data: JSON.stringify(sketches, null, 2) }),
    launch: (command: string, cwd: string) => invoke<number>('library_launch', cwd ? { command, cwd } : { command }),
  };
}

export type LibraryStore = ReturnType<typeof createLibraryStore>;
