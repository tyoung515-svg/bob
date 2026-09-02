"""MS9-F8 — LIVE round-trip for core.forest.projection against bobclaw's own Qdrant (:6353).

Gated behind ``@pytest.mark.integration`` — NOT collected by the default ``pytest`` run (conftest skips
it), so it never affects the core baseline nor requires infra to be up. Run explicitly with::

    pytest -m integration tests/forest/test_forest_projection_live.py -v

Invariant 2 (no live corpus): every test uses a UNIQUELY-NAMED test collection
``research_forest__test_<uuid>`` and DROPS it at teardown. It never touches the live ``bobclaw__768``
or the plain ``research_forest`` collection. Embedder: prefer bobclaw's on-demand embedder on :8081 if
reachable, else deterministic fixture vectors (the test proves projection MECHANICS, not embed quality).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

import pytest

from core.forest import events as fe
from core.forest.store import create_program
from core.forest.projection import ForestProjection, deterministic_embedder

pytestmark = pytest.mark.integration

_QDRANT_URL = os.getenv("FOREST_QDRANT_URL", "http://localhost:6353")
_EMBED_URL = os.getenv("FOREST_EMBED_URL", "http://localhost:8081")
_FIXTURE_DIM = 64


# ---------------------------------------------------------------------------
# Embedder: real :8081 if reachable, else deterministic fixture vectors.
# ---------------------------------------------------------------------------

def _live_embedder_or_none():
    """Return a sync embed callable hitting :8081/v1/embeddings, or None if unreachable/misbehaving."""
    try:
        req = urllib.request.Request(f"{_EMBED_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                return None
    except Exception:
        return None

    def embed(texts):
        out = []
        for t in texts:
            body = json.dumps({"input": t, "model": "embed"}).encode("utf-8")
            req = urllib.request.Request(
                f"{_EMBED_URL}/v1/embeddings", data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out.append([float(x) for x in data["data"][0]["embedding"]])
        return out

    # Prove it actually returns a usable vector; otherwise fall back.
    try:
        v = embed(["probe"])
        if not v or not v[0] or not any(abs(x) > 1e-9 for x in v[0]):
            return None
    except Exception:
        return None
    return embed


@pytest.fixture(scope="module")
def qdrant_client():
    try:
        from qdrant_client import QdrantClient
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"qdrant_client not installed: {exc}")
    try:
        client = QdrantClient(url=_QDRANT_URL, timeout=10)
        client.get_collections()  # probe reachability
    except Exception as exc:
        pytest.skip(f"Qdrant not reachable at {_QDRANT_URL}: {exc}")
    return client


@pytest.fixture
def test_collection(qdrant_client):
    """A uniquely-named TEST collection; dropped at teardown (inv. 2 — never a live collection)."""
    name = f"research_forest__test_{uuid.uuid4().hex[:12]}"
    assert name not in ("bobclaw__768", "research_forest")
    yield name
    try:
        if qdrant_client.collection_exists(name):
            qdrant_client.delete_collection(collection_name=name)
    except Exception:
        pass


@pytest.fixture
def embedder():
    return _live_embedder_or_none() or deterministic_embedder(_FIXTURE_DIM)


# ---------------------------------------------------------------------------
# Live round-trip
# ---------------------------------------------------------------------------

def test_live_roundtrip_rebuild_retrieve_freshness(qdrant_client, test_collection, embedder, tmp_path):
    proj = ForestProjection(qdrant_client, embedder=embedder, collection=test_collection)

    store = create_program("live-uplift", root=tmp_path)
    store.append_events([
        fe.measurement(node_id="H1", metric="uplift", value=0.42, source="testpipe", ts=1000),
        fe.measurement(node_id="H1", metric="uplift", value=0.55, source="testpipe", ts=2000),
        fe.epoch_open(epoch_id="E1", trigger="delta", ts=1001),
    ])

    # ledger -> project -> retrieve, on the real TEST collection
    r = proj.rebuild_from_store(store, tree_id="tree-A")
    assert r["count"] == 3
    assert qdrant_client.collection_exists(test_collection)

    hits = proj.retrieve(query_text="uplift measurement", tree_id="tree-A", k=5)
    assert hits and all(h.payload["tree_id"] == "tree-A" for h in hits)

    # freshness against the real projection
    assert proj.is_stale("tree-A", store.projection_key()) is False
    store.append_events([fe.measurement(node_id="H1", metric="uplift", value=0.9, source="s", ts=3000)])
    assert proj.is_stale("tree-A", store.projection_key()) is True
    proj.rebuild_from_store(store, tree_id="tree-A")
    assert proj.is_stale("tree-A", store.projection_key()) is False


def test_live_tree_id_filter_isolates_trees(qdrant_client, test_collection, embedder):
    proj = ForestProjection(qdrant_client, embedder=embedder, collection=test_collection)
    la = {
        "ref": "a", "falsifiers": [], "claims": {},
        "events": [fe.measurement(node_id="A", metric="uplift", value=1.0, source="s", ts=1)],
    }
    lb = {
        "ref": "b", "falsifiers": [], "claims": {},
        "events": [fe.measurement(node_id="B", metric="latency", value=9.0, source="s", ts=2)],
    }
    proj.rebuild("tree-A", la, "ka")
    proj.rebuild("tree-B", lb, "kb")   # both trees share the one TEST collection (tree_id filter)

    a_hits = proj.retrieve(query_text="uplift", tree_id="tree-A", k=10)
    assert a_hits and all(h.payload["tree_id"] == "tree-A" for h in a_hits)
    b_hits = proj.retrieve(query_text="latency", tree_id="tree-B", k=10)
    assert b_hits and all(h.payload["tree_id"] == "tree-B" for h in b_hits)


def test_live_drop_collection_cleans_up(qdrant_client, test_collection, embedder):
    proj = ForestProjection(qdrant_client, embedder=embedder, collection=test_collection)
    proj.rebuild("tree-A", {"ref": "a", "falsifiers": [], "claims": {},
                            "events": [fe.measurement(node_id="A", metric="m", value=1.0, source="s", ts=1)]}, "k")
    assert qdrant_client.collection_exists(test_collection)
    proj.drop_collection()
    assert not qdrant_client.collection_exists(test_collection)
