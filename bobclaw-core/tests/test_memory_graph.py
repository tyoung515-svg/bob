"""MS9 U4a — memory-graph assembler tests (fixture-backed, read-only).

Covers the accept criteria:
  1. nodes typed correctly; provenance edges correct; k-NN capped at k + score
     floored; node cap binds.
  2. no-writes: the live adapter makes ZERO writes to memory / Qdrant / any live
     collection (inv. 16) — every fake write hook must stay untouched.
  3. (gateway route tested separately in the gateway suite.)
"""
from __future__ import annotations

import asyncio

import pytest

from core.memory_graph import (
    DEFAULT_KNN_SCORE_FLOOR,
    EDGE_KNN,
    EDGE_PROVENANCE,
    NODE_CONVERSATION,
    NODE_FACT,
    assemble_memory_graph,
    build_graph_from_memory,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── fixtures ──────────────────────────────────────────────────────────────────


def _fact(fid, *, source_event_id=None, text="a fact", ts="2026-07-08T00:00:00+00:00"):
    return {
        "fact_id": fid,
        "text": text,
        "subject": "s",
        "predicate": "p",
        "ts": ts,
        "source_event_id": source_event_id,
        "rank": "normal",
    }


def _conv(eid, *, user_message="hello", turn_id="t1"):
    return {
        "event_id": eid,
        "user_message": user_message,
        "assistant_response": "hi",
        "face_id": "assistant",
        "turn_id": turn_id,
        "ts": "2026-07-08T00:00:00+00:00",
    }


# ── 1. node typing ────────────────────────────────────────────────────────────


def test_nodes_typed_correctly():
    graph = assemble_memory_graph(
        facts=[_fact("f1")],
        conversations=[_conv("e1")],
        collections={"research_forest": [{"id": "p1", "payload": {"text": "forest point"}}]},
    )
    by_type = {n["type"]: n for n in graph["nodes"]}
    assert by_type[NODE_FACT]["id"] == "fact:f1"
    assert by_type[NODE_CONVERSATION]["id"] == "conversation:e1"
    # A foreign collection contributes nodes typed by the collection name (so
    # research_forest is visible for free once F8 lands).
    assert "research_forest" in by_type
    assert by_type["research_forest"]["id"] == "research_forest:p1"
    assert graph["meta"]["counts_by_type"] == {NODE_FACT: 1, NODE_CONVERSATION: 1, "research_forest": 1}
    assert graph["meta"]["collections"] == ["research_forest"]


# ── 2. provenance edges (fact -> source conversation) ─────────────────────────


def test_provenance_edge_links_fact_to_source_conversation():
    graph = assemble_memory_graph(
        facts=[_fact("f1", source_event_id="e1")],
        conversations=[_conv("e1")],
    )
    prov = [e for e in graph["edges"] if e["type"] == EDGE_PROVENANCE]
    assert prov == [{"source": "fact:f1", "target": "conversation:e1", "type": EDGE_PROVENANCE}]


def test_no_provenance_edge_when_source_conversation_absent():
    graph = assemble_memory_graph(
        facts=[_fact("f1", source_event_id="missing")],
        conversations=[_conv("e1")],
    )
    assert [e for e in graph["edges"] if e["type"] == EDGE_PROVENANCE] == []


def test_fact_without_source_event_has_no_provenance_edge():
    graph = assemble_memory_graph(facts=[_fact("f1", source_event_id=None)], conversations=[])
    assert graph["edges"] == []


# ── 3. k-NN cap + score floor ─────────────────────────────────────────────────


def test_knn_capped_at_k_and_score_floored():
    # f0 has 8 neighbours above the floor + one below it. With k=5 exactly 5
    # survive, all >= floor, ordered by descending score (top-5 kept).
    facts = [_fact("f0")] + [_fact(f"n{i}") for i in range(8)] + [_fact("low")]
    neighbors = {
        "f0": [(f"n{i}", 0.90 - i * 0.05) for i in range(8)] + [("low", 0.10)],
    }
    graph = assemble_memory_graph(
        facts=facts, conversations=[], knn_neighbors=neighbors, knn_k=5,
        knn_score_floor=DEFAULT_KNN_SCORE_FLOOR,
    )
    knn = [e for e in graph["edges"] if e["type"] == EDGE_KNN]
    assert len(knn) == 5, "k-NN must be capped at k=5"
    targets = {e["target"] for e in knn}
    assert "fact:low" not in targets, "below-floor neighbour must be dropped"
    # the 5 kept are the highest-scoring (n0..n4)
    assert targets == {f"fact:n{i}" for i in range(5)}
    assert all(e["weight"] >= DEFAULT_KNN_SCORE_FLOOR for e in knn)


def test_knn_undirected_dedup_and_no_self_loop():
    facts = [_fact("a"), _fact("b")]
    neighbors = {"a": [("b", 0.9), ("a", 0.99)], "b": [("a", 0.8)]}
    graph = assemble_memory_graph(facts=facts, conversations=[], knn_neighbors=neighbors, knn_k=5)
    knn = [e for e in graph["edges"] if e["type"] == EDGE_KNN]
    assert len(knn) == 1, "a<->b is one undirected edge; self-loop a-a dropped"


def test_knn_edge_dropped_when_neighbor_not_a_node():
    graph = assemble_memory_graph(
        facts=[_fact("a")], conversations=[], knn_neighbors={"a": [("ghost", 0.9)]}, knn_k=5,
    )
    assert [e for e in graph["edges"] if e["type"] == EDGE_KNN] == []


# ── 4. node cap binds ─────────────────────────────────────────────────────────


def test_node_cap_binds_and_edges_reference_surviving_nodes():
    facts = [_fact(f"f{i}", source_event_id="e1") for i in range(50)]
    graph = assemble_memory_graph(facts=facts, conversations=[_conv("e1")], node_cap=10)
    assert len(graph["nodes"]) == 10
    assert graph["meta"]["truncated"] is True
    assert graph["meta"]["node_cap"] == 10
    surviving = {n["id"] for n in graph["nodes"]}
    for e in graph["edges"]:
        assert e["source"] in surviving and e["target"] in surviving


def test_node_cap_priority_keeps_facts_first():
    # facts come before conversations in priority; a cap of 2 with 2 facts + 2
    # conversations keeps both facts, no conversations.
    graph = assemble_memory_graph(
        facts=[_fact("f1"), _fact("f2")],
        conversations=[_conv("e1"), _conv("e2")],
        node_cap=2,
    )
    assert {n["type"] for n in graph["nodes"]} == {NODE_FACT}


def test_type_filter_restricts_node_types():
    graph = assemble_memory_graph(
        facts=[_fact("f1")],
        conversations=[_conv("e1")],
        collections={"research_forest": [{"id": "p1", "payload": {}}]},
        type_filter=["fact"],
    )
    assert {n["type"] for n in graph["nodes"]} == {NODE_FACT}


def test_knn_k_clamped_to_max_five():
    facts = [_fact("f0")] + [_fact(f"n{i}") for i in range(8)]
    neighbors = {"f0": [(f"n{i}", 0.9) for i in range(8)]}
    graph = assemble_memory_graph(facts=facts, conversations=[], knn_neighbors=neighbors, knn_k=50)
    assert graph["meta"]["knn_k"] == 5
    assert len([e for e in graph["edges"] if e["type"] == EDGE_KNN]) == 5


# ── 5. no-writes / live adapter over fakes ────────────────────────────────────


class _WriteTripwire(Exception):
    pass


class _FakeFactStore:
    def __init__(self, facts):
        self._facts = facts

    async def query(self, filters):
        assert filters == {"generation_method": "extract_facts_from_event"}
        return self._facts

    # any mutating call must never fire during assembly
    async def put(self, *a, **k):
        raise _WriteTripwire("fact_store.put called")

    async def delete(self, *a, **k):
        raise _WriteTripwire("fact_store.delete called")


class _FakeEventLog:
    def __init__(self, events):
        self._events = events

    async def replay(self, *a, **k):
        for e in self._events:
            yield e

    async def append(self, *a, **k):
        raise _WriteTripwire("event_log.append called")

    async def atomic_append(self, *a, **k):
        raise _WriteTripwire("event_log.atomic_append called")


class _FakePoint:
    def __init__(self, id, payload, vector=None):
        self.id = id
        self.payload = payload
        self.vector = vector


class _FakeHit:
    def __init__(self, id, score, payload):
        self.id = id
        self.score = score
        self.payload = payload


class _FakeCollections:
    def __init__(self, names):
        self.collections = [type("C", (), {"name": n})() for n in names]


class _FakeQdrantClient:
    def __init__(self, mem_points, foreign):
        self._mem_points = mem_points
        self._foreign = foreign  # {name: [points]}

    def get_collections(self):
        return _FakeCollections(["bobclaw_768", *self._foreign.keys()])

    def scroll(self, collection_name, limit, with_payload, with_vectors, offset=None):
        if collection_name == "bobclaw_768":
            pts = self._mem_points
        else:
            pts = self._foreign.get(collection_name, [])
        return list(pts), None

    def query_points(self, collection_name, query, limit):
        # nearest neighbours = every other memory point (fake), high score
        hits = [
            _FakeHit(p.id, 0.9, p.payload)
            for p in self._mem_points
        ]
        return type("R", (), {"points": hits[:limit]})()

    # mutating calls are tripwires
    def upsert(self, *a, **k):
        raise _WriteTripwire("qdrant.upsert called")

    def delete(self, *a, **k):
        raise _WriteTripwire("qdrant.delete called")

    def create_collection(self, *a, **k):
        raise _WriteTripwire("qdrant.create_collection called")


class _FakeProvider:
    def __init__(self, client):
        self._client = client
        self.collection_prefix = "bobclaw_"


class _FakeRetriever:
    def __init__(self, provider):
        self._provider = provider


class _FakeMem:
    def __init__(self, fact_store, event_log, client):
        self.fact_store = fact_store
        self.event_log = event_log
        self.retriever = _FakeRetriever(_FakeProvider(client))


def _make_event(event_id, kind="agent_turn"):
    body = {"user_message": "hi", "assistant_response": "yo", "face_id": "f", "turn_id": "t"}
    return type("E", (), {"event_id": event_id, "kind": kind, "body": body, "ts": "2026-07-08T00:00:00+00:00"})()


def _make_fact(fid, source_event_id):
    conf = type("Conf", (), {"rank": "normal"})()
    body = {"text": f"text {fid}", "subject": "s", "predicate": "p"}
    return type("F", (), {
        "fact_id": fid, "body": body, "source_event_id": source_event_id,
        "ts": "2026-07-08T00:00:00+00:00", "confidence": conf,
    })()


def test_live_adapter_assembles_and_makes_zero_writes():
    facts = [_make_fact("f1", "e1"), _make_fact("f2", "e1")]
    events = [_make_event("e1"), _make_event("e2", kind="not_a_turn")]
    mem_points = [
        _FakePoint("pt1", {"source_fact_id": "f1"}, vector=[0.1, 0.2]),
        _FakePoint("pt2", {"source_fact_id": "f2"}, vector=[0.2, 0.1]),
    ]
    foreign = {"research_forest": [_FakePoint("rp1", {"text": "forest"})]}
    mem = _FakeMem(_FakeFactStore(facts), _FakeEventLog(events), _FakeQdrantClient(mem_points, foreign))

    graph = _run(build_graph_from_memory(mem, node_cap=100, knn_k=5))

    types = {n["type"] for n in graph["nodes"]}
    assert NODE_FACT in types
    assert NODE_CONVERSATION in types  # only the agent_turn event (e1), not e2
    assert "research_forest" in types
    conv_ids = [n["id"] for n in graph["nodes"] if n["type"] == NODE_CONVERSATION]
    assert conv_ids == ["conversation:e1"]
    # provenance: both facts -> e1
    prov = [e for e in graph["edges"] if e["type"] == EDGE_PROVENANCE]
    assert {(e["source"], e["target"]) for e in prov} == {("fact:f1", "conversation:e1"), ("fact:f2", "conversation:e1")}
    # k-NN: f1<->f2 (undirected, deduped)
    knn = [e for e in graph["edges"] if e["type"] == EDGE_KNN]
    assert len(knn) == 1
    assert graph["meta"]["warnings"] == []


def test_live_adapter_degrades_on_qdrant_failure_without_writing():
    class _BoomClient(_FakeQdrantClient):
        def get_collections(self):
            raise RuntimeError("qdrant down")

    facts = [_make_fact("f1", "e1")]
    events = [_make_event("e1")]
    mem = _FakeMem(_FakeFactStore(facts), _FakeEventLog(events), _BoomClient([], {}))
    graph = _run(build_graph_from_memory(mem))
    # facts + conversation still present; a warning records the qdrant outage.
    assert {n["type"] for n in graph["nodes"]} == {NODE_FACT, NODE_CONVERSATION}
    assert any("collections" in w for w in graph["meta"]["warnings"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
