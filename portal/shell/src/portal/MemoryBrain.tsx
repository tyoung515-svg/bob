// The 3D "brain" over central memory (owner request 2026-11-02, after the
// Kotlin app's memory graph). Loaded lazily: three.js only when it is opened.
import { useEffect, useRef } from 'react';
import ForceGraph3D from '3d-force-graph';
import type { ForceGraph3DInstance } from '3d-force-graph';
import { KIND_COLOR, buildGraph } from './memory';
import type { GraphLink, GraphNode, MemItem } from './memory';

const FLAGGED = '#e0816f';
const LINK_COLOR: Record<GraphLink['kind'], string> = {
  in: 'rgba(200,220,205,0.10)',
  next: 'rgba(127,179,230,0.35)',
  from: 'rgba(233,207,149,0.8)',
  conversation: 'rgba(143,211,165,0.45)',
};

export default function MemoryBrain({
  items,
  selected,
  onSelect,
}: {
  items: MemItem[];
  selected: string | null;
  onSelect: (item: MemItem) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const graph = useRef<ForceGraph3DInstance | null>(null);
  const byId = useRef(new Map<string, MemItem>());
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  useEffect(() => {
    const element = host.current as HTMLDivElement;
    const instance = ForceGraph3D({ controlType: 'orbit' })(element)
      .backgroundColor('#0e1012')
      .showNavInfo(false)
      .nodeLabel((node: object) => (node as GraphNode).label)
      .nodeVal((node: object) => {
        const row = node as GraphNode;
        return row.hub ? 6 + Math.sqrt(row.size) * 2 : 1.4;
      })
      .nodeColor((node: object) => {
        const row = node as GraphNode;
        if (row.id === selectedRef.current) return '#ffffff';
        if (row.flagged) return FLAGGED;
        return KIND_COLOR[row.hub ? 'hub' : row.kind] ?? '#aeb2b8';
      })
      .nodeOpacity(0.92)
      .linkColor((link: object) => LINK_COLOR[(link as GraphLink).kind])
      .linkWidth((link: object) => ((link as GraphLink).kind === 'from' ? 1.2 : 0))
      .linkDirectionalParticles((link: object) => ((link as GraphLink).kind === 'conversation' || (link as GraphLink).kind === 'from' ? 2 : 0))
      .linkDirectionalParticleWidth(1.2)
      .onNodeClick((node: object) => {
        const item = byId.current.get((node as GraphNode).id);
        if (item) selectRef.current(item);
      });
    graph.current = instance;
    const fit = () => instance.width(element.clientWidth).height(element.clientHeight);
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(element);
    return () => {
      observer.disconnect();
      instance._destructor();
      graph.current = null;
    };
  }, []);

  useEffect(() => {
    byId.current = new Map(items.map((item) => [item.source_id, item]));
    const data = buildGraph(items);
    graph.current?.graphData({ nodes: data.nodes.map((node) => ({ ...node })), links: data.links.map((link) => ({ ...link })) });
    const timer = window.setTimeout(() => graph.current?.zoomToFit(800, 40), 1600);
    return () => window.clearTimeout(timer);
  }, [items]);

  useEffect(() => {
    // Re-colour so the picked memory lights up.
    graph.current?.nodeColor(graph.current.nodeColor());
  }, [selected]);

  return <div ref={host} className="mem-brain" />;
}
