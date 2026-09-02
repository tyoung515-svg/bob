"""
BoBClaw Core — orchestration topology export (G1) tests.

Proves GET /api/graph/topology surfaces the compiled LangGraph DAG as inspectable
data (JSON/mermaid/text) sourced from get_graph() (NOT a hand-maintained list, so it
stays correct after the A-10/G2 head reorder), carries the dynamic council-shape
overlay DISTINCT from the compiled-DAG data, rejects bad formats with 400, and
surfaces the observed-topology capability gap honestly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from api.server import build_app
from core import graph_topology as gt
from core.faces.registry import FaceRegistry
from core.graph import build_graph

PROFILES_DIR = Path(__file__).parent.parent / "core" / "faces" / "profiles"


@pytest.fixture
def faces() -> FaceRegistry:
    return FaceRegistry(profiles_dir=PROFILES_DIR)


@pytest.fixture
async def client(faces: FaceRegistry) -> Any:
    app = build_app(faces=faces)
    async with TestClient(TestServer(app)) as c:
        yield c


# ─── pure function: build_topology ────────────────────────────────────────────

def test_build_topology_matches_compiled_graph():
    dg = build_graph().get_graph()
    topo = gt.build_topology()
    assert topo["meta"]["source"] == "compiled"
    assert topo["meta"]["node_count"] == len(dg.nodes)
    assert topo["meta"]["edge_count"] == len(dg.edges)
    # Every compiled node is present, incl. the start/end sentinels.
    ids = {n["id"] for n in topo["nodes"]}
    assert ids == set(dg.nodes)
    assert {"__start__", "__end__", "route", "decompose", "recall"}.issubset(ids)


def test_topology_edges_flag_conditional():
    topo = gt.build_topology()
    edges = {(e["source"], e["target"]): e["conditional"] for e in topo["edges"]}
    # The reordered head edges are unconditional …
    assert edges[("__start__", "route")] is False
    assert edges[("route", "decompose")] is False
    assert edges[("decompose", "recall")] is False
    # … and a recall fan-out edge is conditional (from add_conditional_edges).
    assert any(
        src == "recall" and cond for (src, _), cond in edges.items()
    )


def test_topology_reorder_proof_and_deterministic():
    """Sourced from the compiled graph, so it reflects the A-10 head and is a pure
    function of the code (a fresh build yields the identical export)."""
    a = gt.build_topology()
    b = gt.build_topology(build_graph())
    assert a == b
    edges = {(e["source"], e["target"]) for e in a["edges"]}
    assert ("route", "decompose") in edges           # A-10 order
    assert ("decompose", "route") not in edges        # old order gone


def test_council_overlay_distinct_from_dag():
    topo = gt.build_topology()
    c = topo["council_shapes"]
    # Overlay is a separate key — never mixed into nodes/edges.
    assert "council_shapes" in topo and c is not topo["nodes"]
    assert c["default_seats"] == ["framer", "stress", "wildcard"]
    assert set(c["modes"]) == {"fusion", "sequential", "debate"}
    assert c["entry"]["face"] == "council-max"
    assert "framer" in c["seat_backends"]


# ─── endpoint: formats ────────────────────────────────────────────────────────

async def test_topology_json_default(client):
    resp = await client.get("/api/graph/topology")
    assert resp.status == 200
    assert resp.content_type == "application/json"
    body = await resp.json()
    assert body["meta"]["node_count"] == len(body["nodes"])
    ids = {n["id"] for n in body["nodes"]}
    assert {"route", "decompose", "recall", "execute"}.issubset(ids)
    assert body["council_shapes"]["default_mode"] == "fusion"


async def test_topology_mermaid(client):
    resp = await client.get("/api/graph/topology?format=mermaid")
    assert resp.status == 200
    assert resp.content_type == "text/plain"
    text = await resp.text()
    assert "graph TD" in text
    assert "route" in text and "decompose" in text
    # council overlay appended
    assert "council seats" in text
    assert "seat_framer" in text


async def test_topology_text(client):
    resp = await client.get("/api/graph/topology?format=text")
    assert resp.status == 200
    assert resp.content_type == "text/plain"
    text = await resp.text()
    assert "NODES:" in text and "EDGES:" in text and "COUNCIL" in text
    assert "-> decompose" in text


async def test_topology_bad_format_400(client):
    resp = await client.get("/api/graph/topology?format=svg")
    assert resp.status == 400
    body = await resp.json()
    assert body.get("code") == "invalid_format"


# ─── endpoint: observed-topology documented gap ───────────────────────────────

async def test_topology_empty_format_defaults_json(client):
    resp = await client.get("/api/graph/topology?format=")
    assert resp.status == 200
    assert resp.content_type == "application/json"


async def test_topology_uppercase_format_rejected(client):
    """Exact-match formats (mirrors routing-view's ?format=text convention)."""
    resp = await client.get("/api/graph/topology?format=JSON")
    assert resp.status == 400


# ─── audit-driven hardening: council overlay is anchor-safe (DSv4 F1) ──────────

def test_council_overlay_anchor_safe():
    """A seat→spine edge is emitted only when the anchor exists in the compiled DAG,
    so a renamed/removed panel node never leaves a dangling mermaid edge."""
    # No anchors present ⇒ the seat subgraph renders, but NO wiring edges.
    empty = gt._council_mermaid_overlay(set())
    assert "seat_framer" in empty and 'subgraph council_seats' in empty
    assert "panel_dispatch" not in empty and "synthesize" not in empty
    # Real compiled nodes ⇒ the wiring edges appear.
    from core.graph import build_graph
    ids = set(build_graph().get_graph().nodes)
    full = gt._council_mermaid_overlay(ids)
    assert "panel_dispatch -.->|Send| seat_framer" in full
    assert "seat_framer -.-> synthesize" in full


async def test_topology_observed_gap(client):
    resp = await client.get("/api/graph/topology?observed=flight-xyz")
    assert resp.status == 200
    body = await resp.json()
    assert body["observed_available"] is False
    assert body["flight_id"] == "flight-xyz"
    assert "no persisted per-flight event log" in body["reason"]
    assert "live-tap" in body["design_note"]
