"""
BoBClaw Core — orchestration topology export (G1).

Surfaces the compiled LangGraph DAG (nodes/edges) as inspectable data + renderers,
plus a declarative overlay for the DYNAMIC council fan-out shape the static compiled
graph cannot show (per-seat panel width is chosen at RUNTIME from ``council_spec``).

Source of truth is the compiled graph itself (``get_graph()``) — never a
hand-maintained node/edge list. A second, hand-kept copy of the topology would be
exactly the kind of predicate mirror the A-10 / G2 change removed: it would drift
silently the moment someone adds a node or edge without updating it.

Observed topology (what actually ran) is intentionally NOT built here: the telemetry
layer (``core/telemetry/emit.py``) emits per-flight orchestration events
(fleet_start/worker_state/fleet_join/council_seat/council_synth/cost) to a LIVE stream
+ Redis pub/sub, but ``FlightStore`` persists flight *records*, not those events — so
there is no persisted per-flight event log to reconstruct from. See
``observed_topology_gap()`` for the documented gap + the live-tap design.
"""
from __future__ import annotations

from typing import Optional

_START = "__start__"
_END = "__end__"

VALID_FORMATS = ("json", "mermaid", "text")


def _node_kind(node_id: str) -> str:
    if node_id == _START:
        return "start"
    if node_id == _END:
        return "end"
    return "node"


def _compiled(compiled_graph=None):
    """Return a compiled graph's drawable form. Uses the passed running graph when
    given (honest: what's actually wired), else builds a fresh one (deterministic —
    topology is a static property of the code)."""
    if compiled_graph is None:
        from core.graph import build_graph
        compiled_graph = build_graph()
    return compiled_graph.get_graph()


def build_topology(compiled_graph=None) -> dict:
    """The compiled DAG as JSON: nodes + edges + the council-shape overlay.

    Keeps the compiled-DAG data (``nodes``/``edges``) DISTINCT from the council
    configuration overlay (``council_shapes``) so a reader never confuses the static
    graph with the runtime deliberation shape.
    """
    dg = _compiled(compiled_graph)
    nodes = [{"id": nid, "kind": _node_kind(nid)} for nid in dg.nodes]
    edges = [
        {"source": e.source, "target": e.target, "conditional": bool(e.conditional)}
        for e in dg.edges
    ]
    return {
        "meta": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "source": "compiled",  # derived from the compiled graph, not hand-authored
        },
        "nodes": nodes,
        "edges": edges,
        "council_shapes": _council_shapes(),
    }


def _council_shapes() -> dict:
    """The DYNAMIC council fan-out overlay, from the COUNCIL_* config (single source —
    imported, never re-typed)."""
    from core.config import (
        COUNCIL_DEFAULT_SEATS,
        COUNCIL_DEFAULT_SYNTH_POSTURE,
        COUNCIL_MODE_DEFAULT,
        COUNCIL_SEAT_BACKENDS,
    )

    seat_backends = {
        posture: (cfg.get("backend") if isinstance(cfg, dict) else None)
        for posture, cfg in COUNCIL_SEAT_BACKENDS.items()
    }
    return {
        "note": "dynamic per-council_spec; the per-seat fan-out is not visible in the compiled DAG",
        "entry": {"face": "council-max", "or_profile_with_key": "shape"},
        "modes": ["fusion", "sequential", "debate"],
        "default_mode": COUNCIL_MODE_DEFAULT,
        "default_seats": list(COUNCIL_DEFAULT_SEATS),
        "synth_posture": COUNCIL_DEFAULT_SYNTH_POSTURE,
        "seat_backends": seat_backends,
        # The compiled spine each mode runs through (the ×seats fan-out is runtime).
        "spine": {
            "fusion": ["panel_dispatch", "panel_worker (×seats)", "synthesize", "ground"],
            "debate": ["panel_dispatch", "panel_worker (×seats)", "synthesize", "debate_converge (loop)"],
            "sequential": ["council"],
        },
    }


def render_mermaid(compiled_graph=None) -> str:
    """The compiled DAG as mermaid (native ``draw_mermaid``) + a council fan-out
    overlay subgraph appended so the deliberation shape is visible beside it."""
    dg = _compiled(compiled_graph)
    base = dg.draw_mermaid()
    return base.rstrip("\n") + "\n" + _council_mermaid_overlay(set(dg.nodes))


def _council_mermaid_overlay(compiled_node_ids: set) -> str:
    """The seat subgraph, wired to the compiled panel spine. Anchor-safe: the seat
    nodes are self-contained, and a seat→anchor edge is emitted ONLY when the anchor
    (``panel_dispatch`` / ``synthesize``) actually exists in the compiled DAG — so a
    renamed/removed spine node never leaves a dangling mermaid edge (the very
    silent-drift failure this endpoint exists to prevent)."""
    from core.config import COUNCIL_DEFAULT_SEATS

    lines = [
        "%% council fan-out overlay (dynamic, per council_spec — G1)",
        '\tsubgraph council_seats["council seats (per council_spec)"]',
    ]
    seat_ids = []
    for seat in COUNCIL_DEFAULT_SEATS:
        sid = f"seat_{seat}"
        seat_ids.append(sid)
        lines.append(f'\t\t{sid}["{seat}"]')
    lines.append("\tend")
    for sid in seat_ids:
        if "panel_dispatch" in compiled_node_ids:
            lines.append(f"\tpanel_dispatch -.->|Send| {sid}")
        if "synthesize" in compiled_node_ids:
            lines.append(f"\t{sid} -.-> synthesize")
    return "\n".join(lines) + "\n"


def render_text(compiled_graph=None) -> str:
    """A plain-text view of the topology (no grandalf dependency), mirroring the
    routing-view ``?format=text`` convention."""
    topo = build_topology(compiled_graph)
    m = topo["meta"]
    out = [
        f"BoBClaw orchestration topology (source={m['source']})",
        f"nodes: {m['node_count']}   edges: {m['edge_count']}",
        "",
        "NODES:",
    ]
    for n in topo["nodes"]:
        out.append(f"  {n['id']}  ({n['kind']})")
    out += ["", "EDGES:"]
    for e in topo["edges"]:
        tag = "  [conditional]" if e["conditional"] else ""
        out.append(f"  {e['source']:<18} -> {e['target']}{tag}")
    c = topo["council_shapes"]
    out += [
        "",
        "COUNCIL (dynamic, per council_spec — NOT in the compiled DAG):",
        f"  entry: face '{c['entry']['face']}' | profile with key '{c['entry']['or_profile_with_key']}'",
        f"  modes: {', '.join(c['modes'])}  (default: {c['default_mode']})",
        f"  default seats: {', '.join(c['default_seats'])}   synth posture: {c['synth_posture']}",
        f"  seat backends: " + " ".join(f"{k}={v}" for k, v in c["seat_backends"].items()),
        f"  fusion spine: {' -> '.join(c['spine']['fusion'])}",
    ]
    return "\n".join(out) + "\n"


def observed_topology_gap(flight_id: Optional[str] = None) -> dict:
    """The (c) observed-topology response: a DOCUMENTED capability gap, not an error.

    The substrate to reconstruct 'what actually ran' after the fact does not exist:
    orchestration events are emitted live (stream + Redis pub/sub for /ws/monitor) but
    never persisted per-flight. Rather than fake a view, surface the gap + the concrete
    path to close it (Sol-approved: document + design, do not build this sprint).
    """
    return {
        "flight_id": flight_id,
        "observed_available": False,
        "reason": (
            "no persisted per-flight event log — orchestration events "
            "(fleet_start/worker_state/fleet_join/council_seat/council_synth/cost) are "
            "emitted to a live stream + Redis pub/sub (core/telemetry/emit.py) but "
            "FlightStore (core/flight/store.py) persists flight records, not events."
        ),
        "design_note": (
            "live-tap: subscribe to the monitor Redis channel for an ACTIVE flight_id "
            "and accrete the observed fan-out/council shape as KIND_* frames arrive; OR "
            "add a persisted flight event log (append on emit) to enable an after-the-fact "
            "observed view. The declared topology above is the ground truth to diff against."
        ),
    }
