// Workflow builder (owner request 2026-11-02): an n8n-style canvas for showing
// an agent what you want when words are hard. It is a sketch — nothing here is
// wired or run; "Send to agent" hands over the drawing as numbered steps.
import { useEffect, useRef, useState } from 'react';
import { NODE_TYPES, newNode } from './library';
import type { NodeType, Sketch, SketchNode } from './library';

const NODE_W = 180;
const NODE_H = 58;
const PORT_Y = 29;

function curve(x1: number, y1: number, x2: number, y2: number): string {
  const bend = Math.max(40, Math.abs(x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
}

export function WorkflowSketch({
  sketch,
  onChange,
  onSend,
}: {
  sketch: Sketch;
  onChange: (sketch: Sketch) => void;
  onSend: (sketch: Sketch) => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const [selected, setSelected] = useState<{ node?: string; edge?: number } | null>(null);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [pointer, setPointer] = useState<{ x: number; y: number } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const drag = useRef<{ id: string; dx: number; dy: number; moved: boolean } | null>(null);
  const live = useRef(sketch);
  live.current = sketch;

  function update(change: Partial<Sketch>) {
    onChange({ ...live.current, ...change, updated_at: new Date().toISOString() });
  }

  function canvasPoint(event: { clientX: number; clientY: number }) {
    const box = scroller.current?.getBoundingClientRect();
    const el = scroller.current;
    if (!box || !el) return { x: 0, y: 0 };
    return { x: event.clientX - box.left + el.scrollLeft, y: event.clientY - box.top + el.scrollTop };
  }

  function add(type: NodeType) {
    const el = scroller.current;
    const count = live.current.nodes.length;
    const x = (el ? el.scrollLeft + el.clientWidth / 2 - NODE_W / 2 : 200) + (count % 4) * 24;
    const y = (el ? el.scrollTop + el.clientHeight / 2 - NODE_H / 2 : 160) + (count % 4) * 18;
    const node = newNode(type, Math.max(10, x), Math.max(10, y));
    update({ nodes: [...live.current.nodes, node] });
    setSelected({ node: node.id });
  }

  function connect(to: string) {
    const from = connecting;
    setConnecting(null);
    setPointer(null);
    if (!from || from === to) return;
    if (live.current.edges.some((edge) => edge.from === from && edge.to === to)) return;
    update({ edges: [...live.current.edges, { from, to }] });
  }

  function removeSelected() {
    if (!selected) return;
    if (selected.node) {
      const id = selected.node;
      update({ nodes: live.current.nodes.filter((node) => node.id !== id), edges: live.current.edges.filter((edge) => edge.from !== id && edge.to !== id) });
    } else if (selected.edge !== undefined) {
      const at = selected.edge;
      update({ edges: live.current.edges.filter((_, index) => index !== at) });
    }
    setSelected(null);
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (editing) return;
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (event.key === 'Delete' || event.key === 'Backspace') {
        if (selected) {
          event.preventDefault();
          removeSelected();
        }
      }
      if (event.key === 'Escape') {
        setConnecting(null);
        setPointer(null);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  function startDrag(event: React.PointerEvent, node: SketchNode) {
    if (event.button !== 0) return;
    const at = canvasPoint(event);
    drag.current = { id: node.id, dx: at.x - node.x, dy: at.y - node.y, moved: false };
    (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
  }

  function onPointerMove(event: React.PointerEvent) {
    const at = canvasPoint(event);
    if (connecting) setPointer(at);
    const held = drag.current;
    if (!held) return;
    held.moved = true;
    const nodes = live.current.nodes.map((node) =>
      node.id === held.id ? { ...node, x: Math.max(0, at.x - held.dx), y: Math.max(0, at.y - held.dy) } : node,
    );
    onChange({ ...live.current, nodes });
  }

  function onPointerUp() {
    if (drag.current?.moved) update({});
    drag.current = null;
  }

  const byId = new Map(sketch.nodes.map((node) => [node.id, node]));
  const from = connecting ? byId.get(connecting) : null;

  return (
    <div className="sk">
      <aside className="sk-palette" aria-label="Steps">
        <h3>Add a step</h3>
        {NODE_TYPES.map((row) => (
          <button key={row.type} type="button" className={`sk-add ${row.type}`} onClick={() => add(row.type)} title={row.hint}>
            <span className={`sk-swatch ${row.type}`} /> {row.label}
          </button>
        ))}
        <p className="subtle">
          Drag steps by their header. Click a step&apos;s right dot, then another step, to link them. Double-click to rename. Delete removes
          what is selected.
        </p>
        <button type="button" className="primary" disabled={!sketch.nodes.length} onClick={() => onSend(sketch)}>
          Ask the Assistant to turn this into a workflow
        </button>
        <p className="caveat">Sketch — run the saved definition. Rearranging or annotating this drawing never changes the saved workflow.</p>
        <label>Annotations<textarea aria-label="Sketch annotations" value={sketch.annotations ?? ''} onChange={e=>update({annotations:e.target.value})} /></label>
      </aside>
      <div
        ref={scroller}
        className={`sk-canvas-wrap${connecting ? ' connecting' : ''}`}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerDown={(event) => {
          if (event.target === event.currentTarget || (event.target as HTMLElement).classList.contains('sk-canvas')) {
            setSelected(null);
            setConnecting(null);
            setPointer(null);
          }
        }}
      >
        <div className="sk-canvas">
          <svg className="sk-edges" width="2600" height="1600">
            {sketch.edges.map((edge, index) => {
              const a = byId.get(edge.from);
              const b = byId.get(edge.to);
              if (!a || !b) return null;
              const path = curve(a.x + NODE_W, a.y + PORT_Y, b.x, b.y + PORT_Y);
              return (
                <g key={`${edge.from}-${edge.to}`} className={selected?.edge === index ? 'selected' : undefined}>
                  <path d={path} className="hit" onClick={() => setSelected({ edge: index })} />
                  <path d={path} className="line" markerEnd="url(#sk-arrow)" />
                </g>
              );
            })}
            {from && pointer && <path d={curve(from.x + NODE_W, from.y + PORT_Y, pointer.x, pointer.y)} className="line pending" />}
            <defs>
              <marker id="sk-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
                <path d="M0,0 L10,5 L0,10 z" fill="#8a949c" />
              </marker>
            </defs>
          </svg>
          {sketch.nodes.map((node) => (
            <div
              key={node.id}
              className={`sk-node ${node.type}${selected?.node === node.id ? ' selected' : ''}${connecting && connecting !== node.id ? ' target' : ''}`}
              style={{ left: node.x, top: node.y, width: NODE_W }}
              onClick={() => (connecting ? connect(node.id) : setSelected({ node: node.id }))}
              onDoubleClick={() => setEditing(node.id)}
            >
              <div className="sk-head" onPointerDown={(event) => startDrag(event, node)}>
                <span className={`sk-swatch ${node.type}`} />
                {NODE_TYPES.find((row) => row.type === node.type)?.label}
              </div>
              {editing === node.id ? (
                <input
                  className="sk-label-edit"
                  autoFocus
                  defaultValue={node.label}
                  onBlur={(event) => {
                    const label = event.target.value.trim() || node.label;
                    update({ nodes: live.current.nodes.map((row) => (row.id === node.id ? { ...row, label } : row)) });
                    setEditing(null);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') (event.target as HTMLInputElement).blur();
                    if (event.key === 'Escape') setEditing(null);
                  }}
                />
              ) : (
                <div className="sk-label">{node.label}</div>
              )}
              <span className="sk-port in" aria-hidden="true" />
              <button
                type="button"
                className="sk-port out"
                aria-label={`Link from ${node.label}`}
                onClick={(event) => {
                  event.stopPropagation();
                  setConnecting(node.id);
                  setPointer(null);
                }}
              />
            </div>
          ))}
          {!sketch.nodes.length && <p className="sk-empty">Add steps from the left and link them to sketch what you want built.</p>}
        </div>
      </div>
    </div>
  );
}
