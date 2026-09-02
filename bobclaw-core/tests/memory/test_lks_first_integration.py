"""P4.4 — LKS-first read path against a REAL embedder and a HEALTHY corpus. Marked `integration`.

Why this file exists: `test_retriever_lks_first.py` is 100% fakes (FakeAdapter/FakeEmbedder/FakeProvider)
— it pins dispatch logic and cannot see a real vector space. Nothing exercised `LKSReadAdapter` against a
live embedder + a live store, which is exactly where the interesting failures live.

Why the corpus is EPHEMERAL and not LKS's live `:6333` (P4 gate decision (A), 2026-07-16):
  1. Rail #2 — `:6333` is LKS's LIVE corpus Qdrant, read-only. Tests use ephemeral stores.
  2. `:6333` is currently **100% zero vectors** (all 7 collections, 3,241 points) — the known 2026-06-20
     incident, never rebuilt. See `P4-BLOCKER-corpus-zero-vectors.md`. Retrieval there returns arbitrary
     points at score ~0, so it CANNOT prove ranking. The rebuild is a separately-gated work order.
This mirrors `ABLATION.md`'s proven method: production code + real Granite -> ephemeral in-memory Qdrant.

REQUIRES: LKS's Granite embed_text slot live on 127.0.0.1:8081 (768-dim, `-ngl 0`). Skips if absent.
"""
from __future__ import annotations

import copy
import uuid
from pathlib import Path

import pytest

from core.ledger.federation import FederationRegistry
from core.memory.exceptions import ACLViolation
from core.memory.fingerprint import FingerprintMismatch, FingerprintMissing
from core.memory.integrity import probe_collection
from core.memory.lks_adapter import LKSReadAdapter
from core.memory.slots import SlotResolver

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DIM = 768
_CONFIG = Path(__file__).resolve().parents[2] / "config" / "memory_slots.toml"

# The four-field stamp P4 wrote into the real registry record for the `wiki` instance. Every value is
# host-verified: model_id/dim from the slot, normalize=True from the live server (L2==1.0), distance from
# LKS's store.py (Distance.COSINE) and wiki_chunks/config.json.
STAMP = {"model_id": "granite-embedding-311m", "dim": DIM, "normalize": True, "distance": "cosine"}
ACL = {"writer": "lks", "readers": ["bobclaw", "lks"], "mode": "ro"}

DOCS = [
    ("amp.md", ["Portable Amp"],
     "The portable battery amplifier runs a class-D power stage from a sodium-ion pack with USB-PD input."),
    ("siop.md", ["SIOP"],
     "SIOP architecture coordinates sales inventory and operations planning across the brand portfolio."),
    ("council.md", ["Council"],
     "The council protocol seats three voices: problem definition, adversarial stress-testing, synthesis."),
]


def _live_slot():
    return SlotResolver(_CONFIG).get("embed_text")


async def _embedder():
    from core.memory.embedder import SlotResolvedEmbedder
    return SlotResolvedEmbedder(SlotResolver(_CONFIG), "embed_text")


@pytest.fixture(scope="module")
def granite_or_skip():
    """Skip the module unless a live 768-dim embedder answers on the embed_text slot."""
    import asyncio

    async def probe():
        emb = await _embedder()
        return (await emb.embed(["healthcheck"]))[0]

    try:
        vec = asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"embed_text slot not live on 127.0.0.1:8081 ({type(exc).__name__}: {exc})")
    if len(vec) != DIM:
        pytest.skip(f"embed_text returned dim {len(vec)}, expected {DIM}")
    return True


@pytest.fixture
async def healthy_corpus(granite_or_skip):
    """An EPHEMERAL in-memory Qdrant holding DOCS embedded by the REAL Granite. Dies with the test."""
    from qdrant_client import QdrantClient
    from qdrant_client.http.models import Distance, PointStruct, VectorParams

    client = QdrantClient(":memory:")  # ephemeral — never :6333 (rail #2)
    collection = f"p4_ephemeral_{uuid.uuid4().hex[:8]}"
    client.create_collection(collection, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))

    emb = await _embedder()
    vectors = await emb.embed([body for _, _, body in DOCS])
    client.upsert(collection, points=[
        PointStruct(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, path)),
            vector=vec,
            payload={"chunk_text": body, "source_path": path, "heading_path": heading},
        )
        for (path, heading, body), vec in zip(DOCS, vectors)
    ])
    return client, collection


def _registry(tmp_path, collection, *, meta) -> FederationRegistry:
    """A throwaway registry pointing at the ephemeral collection. Never the real registry file."""
    reg = FederationRegistry(tmp_path / "reg.json")
    reg.register("wiki", "C:/dev/projects/lks", collection=collection, dim=DIM,
                 ledger_dir="ledger", meta=copy.deepcopy(meta))
    return reg


async def _adapter(reg, client, *, reader_id="bobclaw", live_slot=None, require_stamp=True):
    return LKSReadAdapter(
        reg, client=client, embedder=await _embedder(),
        live_slot=live_slot if live_slot is not None else _live_slot(),
        reader_id=reader_id, require_stamp=require_stamp, require_acl=True,
    )


# --------------------------------------------------------------------------- the happy path

async def test_lks_first_returns_ranked_real_passages(healthy_corpus, tmp_path):
    """The whole seam, live: resolve -> ACL -> fingerprint -> real embed -> real search -> ranked passages.

    The score assertions are the anti-corruption canary. Against `:6333` today this test would FAIL —
    every score is a flat 0.0 and the top hit is arbitrary. That is the point: it proves the vector space
    is real, not merely that a call returned some strings.
    """
    client, collection = healthy_corpus
    reg = _registry(tmp_path, collection, meta={"acl": ACL, "embed": STAMP})
    adapter = await _adapter(reg, client)

    hits = await adapter.search("wiki", query="battery powered portable amplifier", k=3)

    assert hits, "expected hits from the ephemeral corpus"
    # 1. Semantic relevance — the amp doc must win. Impossible with null vectors.
    assert hits[0].payload["source_path"] == "amp.md", f"top hit was {hits[0].payload['source_path']}"
    # 2. Scores are real and SPREAD (LKS CLAUDE.md's own corruption test), not flat ~0.
    assert hits[0].score > 0.1, f"top score {hits[0].score} — suspiciously null-like"
    assert len({round(h.score, 6) for h in hits}) > 1, "flat scores = degenerate vector space"
    assert hits[0].score >= hits[-1].score, "hits must be sorted descending"
    # 3. The trace fields the librarian contract carries.
    assert hits[0].payload["heading_path"] == ["Portable Amp"]
    assert "chunk_text" in hits[0].payload


async def test_ephemeral_corpus_passes_the_integrity_probe(healthy_corpus):
    """Contrast with the real store: this corpus is healthy; `:6333` is 100% zero (P4-BLOCKER)."""
    client, collection = healthy_corpus
    report = probe_collection(client, collection, sample=50)
    assert report.healthy is True, report.summary()
    assert report.zero == 0 and report.live == len(DOCS)


async def test_query_vector_dim_matches_instance(healthy_corpus, tmp_path):
    client, collection = healthy_corpus
    reg = _registry(tmp_path, collection, meta={"acl": ACL, "embed": STAMP})
    assert reg.resolve("wiki").dim == DIM
    hits = await (await _adapter(reg, client)).search("wiki", query="SIOP planning", k=1)
    assert hits[0].payload["source_path"] == "siop.md"


# --------------------------------------------------------------------------- the gates, fail-closed

async def test_fingerprint_mismatch_fails_closed(healthy_corpus, tmp_path):
    """A same-dim model swap — the one corruption the dim-suffix CANNOT catch — must fail closed."""
    client, collection = healthy_corpus
    bad = {"acl": ACL, "embed": {**STAMP, "model_id": "some-other-768d-model"}}
    reg = _registry(tmp_path, collection, meta=bad)
    with pytest.raises(FingerprintMismatch) as exc:
        await (await _adapter(reg, client)).search("wiki", query="anything", k=1)
    assert "model_id" in str(exc.value)


async def test_missing_stamp_fails_closed_under_hard_gate(healthy_corpus, tmp_path):
    """P4/D7: require_stamp=True means an UNSTAMPED instance is refused, not silently read."""
    client, collection = healthy_corpus
    reg = _registry(tmp_path, collection, meta={"acl": ACL})  # no meta.embed
    with pytest.raises(FingerprintMissing):
        await (await _adapter(reg, client)).search("wiki", query="anything", k=1)


async def test_stamped_but_unverifiable_refuses_to_read(healthy_corpus, tmp_path):
    """COUPLING GUARD (see bootstrap): stamped + no live_slot => refuse. Never read unverified."""
    from core.memory.lks_adapter import ReadAdapterError
    client, collection = healthy_corpus
    reg = _registry(tmp_path, collection, meta={"acl": ACL, "embed": STAMP})
    adapter = LKSReadAdapter(reg, client=client, embedder=await _embedder(), live_slot=None,
                             reader_id="bobclaw", require_stamp=True, require_acl=True)
    with pytest.raises(ReadAdapterError, match="refusing to read unverified"):
        await adapter.search("wiki", query="anything", k=1)


async def test_acl_denies_unlisted_reader(healthy_corpus, tmp_path):
    client, collection = healthy_corpus
    reg = _registry(tmp_path, collection, meta={"acl": ACL, "embed": STAMP})
    with pytest.raises(ACLViolation, match="not in readers allowlist"):
        await (await _adapter(reg, client, reader_id="mallory")).search("wiki", query="x", k=1)


async def test_acl_required_when_undeclared(healthy_corpus, tmp_path):
    client, collection = healthy_corpus
    reg = _registry(tmp_path, collection, meta={"embed": STAMP})  # no acl
    with pytest.raises(ACLViolation, match="no acl declared"):
        await (await _adapter(reg, client)).search("wiki", query="x", k=1)


# --------------------------------------------------------------------------- read-only posture

async def test_adapter_exposes_no_write_surface():
    for banned in ("upsert", "delete", "index", "write", "create_collection", "set_payload"):
        assert not hasattr(LKSReadAdapter, banned), f"LKSReadAdapter grew a write method: {banned}"


async def test_search_does_not_mutate_the_corpus(healthy_corpus, tmp_path):
    client, collection = healthy_corpus
    before = client.count(collection).count
    reg = _registry(tmp_path, collection, meta={"acl": ACL, "embed": STAMP})
    await (await _adapter(reg, client)).search("wiki", query="battery amplifier", k=3)
    assert client.count(collection).count == before
