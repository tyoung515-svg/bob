"""BoBClaw memory-graph assembly (MS9 U4a) — read-only, additive-new.

This package lives OUTSIDE ``core/memory/`` and imports it READ-ONLY (same
discipline as ``core/forest`` inv. 11): it assembles a client-renderable graph
of the internal memory substrate (L0/L1/Qdrant) WITHOUT writing anything to
memory, Qdrant, or any live collection.

Nodes (type-tagged for client colouring):
  * ``fact``          — L1 auto-extracted facts (``SQLiteFactStore``).
  * ``conversation``  — L0 agent-turn events (``SQLiteEventLog``); this is the
                        substrate's record of a conversation turn — a fact's
                        ``source_event_id`` points here (the provenance field).
  * ``<collection>``  — one type per *additional* live Qdrant collection
                        enumerated at runtime (e.g. ``research_forest`` once
                        F8 lands — it becomes visible in the same graph for
                        free). The memory substrate's own collection is NOT
                        surfaced separately (its points are the fact vectors).

Edges:
  * ``provenance`` — fact → source conversation (``Fact.source_event_id``).
  * ``knn``        — vector k-NN between fact nodes (cap k≈5, score floor).

Caps: total node count is capped (default 500, query-overridable); the server
assembles, the client only renders.
"""
from __future__ import annotations

from core.memory_graph.assembler import (
    DEFAULT_KNN_K,
    DEFAULT_KNN_SCORE_FLOOR,
    DEFAULT_NODE_CAP,
    EDGE_KNN,
    EDGE_PROVENANCE,
    KNN_K_MAX,
    L1_GENERATION_METHOD,
    NODE_CONVERSATION,
    NODE_FACT,
    GraphEdge,
    GraphNode,
    assemble_memory_graph,
    build_graph_from_memory,
)

__all__ = [
    "DEFAULT_NODE_CAP",
    "DEFAULT_KNN_K",
    "DEFAULT_KNN_SCORE_FLOOR",
    "KNN_K_MAX",
    "EDGE_KNN",
    "EDGE_PROVENANCE",
    "NODE_FACT",
    "NODE_CONVERSATION",
    "L1_GENERATION_METHOD",
    "GraphNode",
    "GraphEdge",
    "assemble_memory_graph",
    "build_graph_from_memory",
]
