"""MS9-F8 — tests for core.forest.projection (the LKS projection plane).

Pins the F8 accept criteria WITHOUT touching a live corpus (inv. 2) and WITHOUT a socket
(pytest.ini runs --disable-socket): a faithful in-memory ``FakeQdrant`` that interprets the REAL
qdrant model objects the module builds (``Filter`` / ``FieldCondition`` / ``MatchValue`` / ``MatchAny``
/ ``FilterSelector`` / ``PointStruct`` / ``VectorParams``) exercises the exact client-call path used
against a real Qdrant. The deterministic fixture embedder makes the round-trip self-contained (the test
proves projection MECHANICS — filter / freshness / rebuild / norm-gate / retrieve — not embedding
quality). The live round-trip against Qdrant :6353 lives in test_forest_projection_live.py (integration).

Accept criteria covered here:
  1. ledger -> project -> retrieve round-trip; the tree_id payload filter isolates a tree.
  2. staleness detected on ledger advance (projection_key mismatch).
  3. rebuild converges + the norm gate passes (and a zero-vector fixture is rejected by the gate).
  5. writes go only through rebuild; the collection param threads through retrieve.
"""
from __future__ import annotations

import math

import pytest

from core.forest import events as fe
from core.forest.store import create_program
from core.forest.projection import (
    DEFAULT_COLLECTION,
    PROJECTION_KEY_KEY,
    TREE_ID_KEY,
    ForestProjection,
    Hit,
    ProjectionError,
    deterministic_embedder,
    point_id,
    render_claim_text,
    render_event_text,
)


# ---------------------------------------------------------------------------
# In-memory Qdrant double — interprets the REAL qdrant model objects the module
# builds, so projection.py runs its production client-call path unchanged.
# ---------------------------------------------------------------------------

class _Pt:
    def __init__(self, pid, score, payload):
        self.id = pid
        self.score = score
        self.payload = payload


class _Resp:
    def __init__(self, points):
        self.points = points


class _CollInfo:
    def __init__(self, name):
        self.name = name


class _Collections:
    def __init__(self, names):
        self.collections = [_CollInfo(n) for n in names]


def _match_cond(cond, payload) -> bool:
    key = cond.key
    m = cond.match
    if hasattr(m, "value") and m.value is not None:
        return payload.get(key) == m.value
    if hasattr(m, "any") and m.any is not None:
        return payload.get(key) in list(m.any)
    return False


def _match_filter(flt, payload) -> bool:
    if flt is None:
        return True
    for cond in (getattr(flt, "must", None) or []):
        if not _match_cond(cond, payload):
            return False
    return True


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class FakeQdrant:
    """Minimal, faithful in-memory Qdrant: only the calls projection.py makes, on real model objects."""

    def __init__(self):
        # name -> {"dim": int, "points": {id: {"vector": [...], "payload": {...}}}}
        self._colls: dict = {}
        # call counters for tripwire / atomicity assertions
        self.upserts = 0
        self.deletes = 0
        self.creates = 0

    # ---- read ----
    def collection_exists(self, collection_name) -> bool:
        return collection_name in self._colls

    def get_collections(self):
        return _Collections(list(self._colls.keys()))

    def scroll(self, collection_name, scroll_filter=None, limit=10,
               with_payload=True, with_vectors=False, offset=None):
        coll = self._colls.get(collection_name)
        if coll is None:
            return [], None
        pts = [
            _Pt(pid, 0.0, rec["payload"])
            for pid, rec in coll["points"].items()
            if _match_filter(scroll_filter, rec["payload"])
        ]
        return pts[:limit], None

    def query_points(self, collection_name, query, limit=10, query_filter=None):
        coll = self._colls.get(collection_name)
        if coll is None:
            raise RuntimeError(f"collection {collection_name!r} not found")
        scored = [
            _Pt(pid, _cosine(query, rec["vector"]), rec["payload"])
            for pid, rec in coll["points"].items()
            if _match_filter(query_filter, rec["payload"])
        ]
        scored.sort(key=lambda p: p.score, reverse=True)
        return _Resp(scored[:limit])

    # ---- write ----
    def create_collection(self, collection_name, vectors_config):
        self.creates += 1
        self._colls[collection_name] = {"dim": vectors_config.size, "points": {}}

    def delete_collection(self, collection_name):
        self._colls.pop(collection_name, None)

    def upsert(self, collection_name, points):
        self.upserts += 1
        coll = self._colls[collection_name]
        for p in points:
            coll["points"][p.id] = {"vector": list(p.vector), "payload": dict(p.payload)}

    def delete(self, collection_name, points_selector):
        self.deletes += 1
        coll = self._colls.get(collection_name)
        if coll is None:
            return
        flt = getattr(points_selector, "filter", None)
        doomed = [
            pid for pid, rec in coll["points"].items()
            if _match_filter(flt, rec["payload"])
        ]
        for pid in doomed:
            del coll["points"][pid]

    # ---- test helpers ----
    def count(self, collection_name) -> int:
        coll = self._colls.get(collection_name)
        return 0 if coll is None else len(coll["points"])


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def proj():
    return ForestProjection(FakeQdrant(), embedder=deterministic_embedder(32))


def _synthetic_ledger():
    """A ledger dict shaped like read_ledger_at output, with BOTH claims and events."""
    return {
        "ref": "deadbeef",
        "events": [
            fe.measurement(node_id="H1", metric="uplift", value=0.42, source="testpipe", ts=1000),
            fe.epoch_open(epoch_id="E1", trigger="delta", ts=1001),
        ],
        "claims": {
            "C1": {"id": "C1", "statement": "uplift trends up over the window"},
            "C2": {"id": "C2", "text": "slot S contributes 0.1 uplift"},
        },
        "falsifiers": [],
    }


def _seed_store(tmp_path, pid="bobpipe-uplift"):
    store = create_program(pid, root=tmp_path)
    store.append_events([
        fe.measurement(node_id="H1", metric="uplift", value=0.42, source="testpipe", ts=1000),
        fe.measurement(node_id="H1", metric="uplift", value=0.55, source="testpipe", ts=2000),
        fe.epoch_open(epoch_id="E1", trigger="delta", ts=1001),
    ])
    return store


# ---------------------------------------------------------------------------
# project_points — pure ledger -> point specs
# ---------------------------------------------------------------------------

def test_project_points_covers_claims_and_events(proj):
    ledger = _synthetic_ledger()
    specs = proj.project_points("tree-A", ledger, "proj:sha256:key1")
    # one point per claim (2) + one per event with an id (2)
    assert len(specs) == 4
    kinds = [s["payload"]["kind"] for s in specs]
    assert kinds[:2] == ["claim", "claim"]                 # claims first, sorted by id
    assert any(k.startswith("event:measurement") for k in kinds)
    assert any(k == "event:epoch_open" for k in kinds)
    # every spec payload carries the freshness key + tree_id + item_id + text
    for s in specs:
        p = s["payload"]
        assert p[TREE_ID_KEY] == "tree-A"
        assert p[PROJECTION_KEY_KEY] == "proj:sha256:key1"
        assert p["item_id"] and p["text"]
        assert s["id"] == point_id("tree-A", p["kind"], p["item_id"])


def test_project_points_deterministic(proj):
    ledger = _synthetic_ledger()
    a = proj.project_points("t", ledger, "k")
    b = proj.project_points("t", ledger, "k")
    assert a == b                                          # identical inputs -> identical specs


def test_render_helpers():
    m = fe.measurement(node_id="H1", metric="uplift", value=0.42, source="testpipe", ts=1)
    txt = render_event_text(m)
    assert "measurement" in txt and "uplift" in txt and "0.42" in txt
    assert render_claim_text("C1", {"statement": "hi"}) == "hi"
    assert render_claim_text("C1", {"text": "yo"}) == "yo"
    assert "z" in render_claim_text("C1", {"z": 1})       # json fallback


# ---------------------------------------------------------------------------
# norm gate — the zero-vector lesson
# ---------------------------------------------------------------------------

def test_norm_gate_passes_on_healthy_vectors(proj):
    vecs = deterministic_embedder(16)(["a", "b", "c"])
    proj.assert_norms(vecs)                                # no raise
    assert all(abs(ForestProjection.vector_norm(v) - 1.0) < 1e-6 for v in vecs)


def test_norm_gate_rejects_zero_vector(proj):
    with pytest.raises(ProjectionError):
        proj.assert_norms([[0.0, 0.0, 0.0]])
    with pytest.raises(ProjectionError):
        proj.assert_norms([[]])


def test_rebuild_zero_vector_fixture_is_rejected_and_leaves_index_intact():
    """A degenerate embedder trips the gate BEFORE any drop/upsert (fail-closed atomicity)."""
    client = FakeQdrant()
    good = ForestProjection(client, embedder=deterministic_embedder(16), collection="c")
    ledger = _synthetic_ledger()
    good.rebuild("tree-A", ledger, "proj:sha256:k1")
    before = client.count("c")
    assert before == 4

    zero = ForestProjection(client, embedder=lambda texts: [[0.0] * 16 for _ in texts], collection="c")
    with pytest.raises(ProjectionError):
        zero.rebuild("tree-A", ledger, "proj:sha256:k2")
    # nothing dropped, nothing written: the healthy projection is untouched
    assert client.count("c") == before
    assert good.stored_projection_key("tree-A") == "proj:sha256:k1"


# ---------------------------------------------------------------------------
# rebuild — drop + full rebuild from ledger truth; convergence
# ---------------------------------------------------------------------------

def test_rebuild_converges_and_is_idempotent(proj):
    ledger = _synthetic_ledger()
    r1 = proj.rebuild("tree-A", ledger, "proj:sha256:k1")
    assert r1["count"] == 4
    n1 = proj._client.count(DEFAULT_COLLECTION)
    r2 = proj.rebuild("tree-A", ledger, "proj:sha256:k1")   # same truth -> same points (upsert by id)
    assert r2["count"] == 4
    assert proj._client.count(DEFAULT_COLLECTION) == n1     # no duplicate points


def test_rebuild_drops_stale_points_for_the_tree():
    """A shrunk ledger drops the vanished points (drop+full-rebuild, not append-only)."""
    client = FakeQdrant()
    p = ForestProjection(client, embedder=deterministic_embedder(16))
    big = _synthetic_ledger()
    p.rebuild("tree-A", big, "k1")
    assert client.count(DEFAULT_COLLECTION) == 4
    small = {"ref": "x", "events": [big["events"][0]], "claims": {}, "falsifiers": []}
    p.rebuild("tree-A", small, "k2")
    assert client.count(DEFAULT_COLLECTION) == 1            # 3 stale points dropped


def test_rebuild_from_store_uses_ledger_truth(tmp_path):
    client = FakeQdrant()
    p = ForestProjection(client, embedder=deterministic_embedder(16))
    store = _seed_store(tmp_path)
    r = p.rebuild_from_store(store)                         # tree_id defaults to program_id
    assert r["tree_id"] == store.program_id
    assert r["count"] == 3
    assert r["projection_key"] == store.projection_key()


# ---------------------------------------------------------------------------
# freshness — staleness detected on ledger advance (projection_key mismatch)
# ---------------------------------------------------------------------------

def test_staleness_detected_on_ledger_advance(tmp_path):
    client = FakeQdrant()
    p = ForestProjection(client, embedder=deterministic_embedder(16))
    store = _seed_store(tmp_path)

    p.rebuild_from_store(store)
    k_head = store.projection_key()
    assert p.is_stale(store.program_id, k_head) is False   # fresh right after a rebuild
    assert p.freshness(store)["stale"] is False

    # advance the ledger -> projection_key changes -> the projection is now stale
    store.append_events([fe.measurement(node_id="H1", metric="uplift", value=0.9, source="s", ts=3000)])
    k_new = store.projection_key()
    assert k_new != k_head
    assert p.is_stale(store.program_id, k_new) is True
    fr = p.freshness(store)
    assert fr["stale"] is True and fr["stored"] == k_head and fr["head"] == k_new

    # rebuilding reconciles the projection back to fresh
    p.rebuild_from_store(store)
    assert p.is_stale(store.program_id, k_new) is False


def test_stored_projection_key_none_when_absent(proj):
    assert proj.stored_projection_key("never-indexed") is None   # collection absent
    proj.rebuild("tree-A", _synthetic_ledger(), "k1")
    assert proj.stored_projection_key("tree-B") is None          # tree not indexed
    assert proj.stored_projection_key("tree-A") == "k1"


# ---------------------------------------------------------------------------
# retrieve — round-trip, tree_id filter, collection param threading
# ---------------------------------------------------------------------------

def test_retrieve_roundtrip_and_tree_id_filter():
    """ledger -> project -> retrieve; the tree_id filter isolates one tree from another (shared coll)."""
    client = FakeQdrant()
    p = ForestProjection(client, embedder=deterministic_embedder(24))
    la = _synthetic_ledger()
    lb = {
        "ref": "y",
        "events": [fe.measurement(node_id="Z9", metric="latency", value=12.0, source="ses", ts=5)],
        "claims": {"CB": {"statement": "latency is flat"}},
        "falsifiers": [],
    }
    p.rebuild("tree-A", la, "ka")
    p.rebuild("tree-B", lb, "kb")                           # both trees share DEFAULT_COLLECTION

    hits = p.retrieve(query_text="uplift trends up over the window", tree_id="tree-A", k=10)
    assert hits and all(isinstance(h, Hit) for h in hits)
    assert all(h.payload[TREE_ID_KEY] == "tree-A" for h in hits)   # tree_id filter: no tree-B leakage

    # the exact indexed claim text is the top hit (deterministic embed -> cosine ~1.0)
    assert hits[0].payload["item_id"] == "C1"

    # tree-B is retrievable on its own filter, and never returns tree-A points
    hits_b = p.retrieve(query_text="latency is flat", tree_id="tree-B", k=10)
    assert hits_b and all(h.payload[TREE_ID_KEY] == "tree-B" for h in hits_b)


def test_retrieve_collection_param_threads_through():
    client = FakeQdrant()
    p = ForestProjection(client, embedder=deterministic_embedder(16), collection="research_forest__A")
    p.rebuild("tree-A", _synthetic_ledger(), "ka")
    # default collection (the one we built into)
    assert p.retrieve(query_text="uplift", tree_id="tree-A", k=5)
    # explicit collection param overrides; a missing collection is "no matches", not an error
    assert p.retrieve(query_text="uplift", tree_id="tree-A", k=5, collection="research_forest__A")
    assert p.retrieve(query_text="uplift", tree_id="tree-A", k=5, collection="does-not-exist") == []


def test_retrieve_by_vector_and_extra_filters():
    client = FakeQdrant()
    p = ForestProjection(client, embedder=deterministic_embedder(16))
    p.rebuild("tree-A", _synthetic_ledger(), "ka")
    vec = deterministic_embedder(16)(["uplift trends up over the window"])[0]
    hits = p.retrieve(query_vector=vec, tree_id="tree-A", k=5)
    assert hits and hits[0].payload["item_id"] == "C1"
    # extra_filters narrows to a payload kind
    claims_only = p.retrieve(query_vector=vec, tree_id="tree-A", k=10, extra_filters={"kind": "claim"})
    assert claims_only and all(h.payload["kind"] == "claim" for h in claims_only)


def test_retrieve_rejects_bad_args(proj):
    with pytest.raises(ProjectionError):
        proj.retrieve(k=5)                                  # neither query given
    with pytest.raises(ProjectionError):
        proj.retrieve(query_text="a", query_vector=[1.0], k=5)   # both given
    with pytest.raises(ProjectionError):
        proj.retrieve(query_text="a", k=0)                  # non-positive k
    with pytest.raises(ProjectionError):
        proj.retrieve(query_text="a", k=True)               # bool is not a valid k


# ---------------------------------------------------------------------------
# drop helpers (drop+full-rebuild is first-class)
# ---------------------------------------------------------------------------

def test_drop_collection_is_first_class(proj):
    proj.rebuild("tree-A", _synthetic_ledger(), "k1")
    assert proj._client.collection_exists(DEFAULT_COLLECTION)
    proj.drop_collection()
    assert not proj._client.collection_exists(DEFAULT_COLLECTION)
    proj.drop_collection()                                  # idempotent, no raise


def test_construct_rejects_empty_collection():
    with pytest.raises(ProjectionError):
        ForestProjection(FakeQdrant(), embedder=deterministic_embedder(8), collection="")
