import pytest
from core.memory.retriever import MemoryRetriever
from core.memory.models import Hit, RetrievedChunk, RankedResults, Fact, ConfidenceStub
from core.memory.exceptions import (
    EmbedderUnavailable,
    RetrievalProviderError,
    ACLViolation,
    L1ValidationFailed,
)
from core.memory.fingerprint import FingerprintMismatch, FingerprintMissing, EmbedFingerprint
from core.memory.lks_adapter import ReadAdapterError
from core.ledger.federation import FederationError

DIM = 768

class FakeEmbedder:
    def __init__(self):
        self.calls = []
    async def embed_query(self, texts):
        self.calls.append(list(texts))
        return [[0.1] + [0.0]*(DIM-1) for _ in texts]

    async def embed_doc(self, texts):
        return [[0.1] + [0.0]*(DIM-1) for _ in texts]

class FakeProvider:
    def __init__(self, hits=None):
        self.hits = hits or []
        self.calls = []
    def query_vector(self, store_id, vector, top_k, filters, *, offset=0):
        self.calls.append((store_id, top_k, filters))
        # single-page fake: only the first page (offset 0) carries hits; the
        # retriever's paging then sees a short/empty follow-up page and stops.
        hits = list(self.hits) if offset == 0 else []
        return RankedResults(hits=hits, provider_id="fake", latency_ms=0)

class FakeFactStore:
    def __init__(self, facts=None):
        self.facts = facts or {}
    async def get(self, fid):
        if fid not in self.facts:
            raise L1ValidationFailed(fid, ["fact not found"])
        return self.facts[fid]

class FakeSlotResolver:
    def get(self, name):
        return None

class FakeQueryLog:
    def __init__(self):
        self.rows = []
    def append(self, row):
        self.rows.append(row)

class FakeAdapter:
    def __init__(self, hits=None, raises=None):
        self.hits = hits or []
        self.raises = raises
        self.calls = []
    async def search(self, instance, *, query=None, query_vector=None, k=10, filters=None):
        self.calls.append((instance, query, k, filters))
        if self.raises is not None:
            raise self.raises
        return list(self.hits)

def bob_hit(i, score, fid=None, text="bob"):
    pl = {"text": text, "source_path": f"{i}.md"}
    if fid is not None:
        pl["source_fact_id"] = fid
    return Hit(id=str(i), score=score, payload=pl)

def lks_hit(i, score, text="lks", **extra):
    pl = {"chunk_text": text, "source_path": f"lks{i}.md", "heading_path": ["H"]}
    pl.update(extra)
    return Hit(id=f"lks{i}", score=score, payload=pl)

def make_retriever(*, adapter=None, instance=None, lks_first=False, provider_hits=None, facts=None):
    return MemoryRetriever(
        embedder=FakeEmbedder(),
        provider=FakeProvider(provider_hits),
        fact_store=FakeFactStore(facts),
        store_id="s",
        slot_resolver=FakeSlotResolver(),
        query_log=FakeQueryLog(),
        lks_adapter=adapter,
        lks_instance=instance,
        lks_first=lks_first,
    )

def a_fact(fid):
    return Fact(
        fact_id=fid,
        generation_method="m",
        body={"text": "f"},
        source_event_id="e",
        input_hash="h",
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )

# ------------------------------
# Tests
# ------------------------------

@pytest.mark.asyncio
async def test_flag_off_byte_identical_uses_bob():
    r = make_retriever(lks_first=False, provider_hits=[bob_hit(1, 0.9)])
    out = await r.search("q", top_k=3)
    assert [c.content for c in out] == ["bob"]
    assert r._provider.calls

@pytest.mark.asyncio
async def test_lks_first_hit_short_circuits_bob():
    ad = FakeAdapter(hits=[lks_hit(1, 0.9, "alpha"), lks_hit(2, 0.8, "beta")])
    r = make_retriever(adapter=ad, instance="lkstest", lks_first=True, provider_hits=[bob_hit(9, 0.99)])
    out = await r.search("q", top_k=3)
    assert [c.content for c in out] == ["alpha", "beta"]
    assert all(c.source_fact_id is None for c in out)
    assert ad.calls and ad.calls[0][0] == "lkstest"
    assert r._provider.calls == []

@pytest.mark.asyncio
async def test_mapping_chunk_text_and_fields():
    ad = FakeAdapter(hits=[lks_hit(1, 0.9, "body", heading_path=["a", "b"])])
    r = make_retriever(adapter=ad, instance="i", lks_first=True)
    out = await r.search("q")
    c = out[0]
    assert c.content == "body"
    assert c.source_path == "lks1.md"
    assert c.heading_path == ["a", "b"]
    assert c.source_fact_id is None
    assert c.boosted_score == 0.9

    # fallback when no chunk_text
    ad2 = FakeAdapter(hits=[Hit(id="x", score=0.9, payload={"text": "viatext"})])
    r2 = make_retriever(adapter=ad2, instance="i", lks_first=True)
    out2 = await r2.search("q")
    assert out2[0].content == "viatext"

    # source_fact_id is FORCED None even if a payload carries one (corpus chunks are not BoB facts; audit r5)
    ad3 = FakeAdapter(hits=[Hit(id="z", score=0.9,
                               payload={"chunk_text": "c", "source_fact_id": "should-not-leak"})])
    r3 = make_retriever(adapter=ad3, instance="i", lks_first=True)
    out3 = await r3.search("q")
    assert out3[0].source_fact_id is None

@pytest.mark.asyncio
async def test_fallback_on_miss_empty():
    ad = FakeAdapter(hits=[])
    r = make_retriever(adapter=ad, instance="i", lks_first=True, provider_hits=[bob_hit(1, 0.9, text="frombob")])
    out = await r.search("q")
    assert [c.content for c in out] == ["frombob"]
    assert r._provider.calls

@pytest.mark.asyncio
async def test_fallback_on_all_sub_threshold():
    ad = FakeAdapter(hits=[lks_hit(1, 0.1), lks_hit(2, 0.2)])
    r = make_retriever(adapter=ad, instance="i", lks_first=True, provider_hits=[bob_hit(1, 0.9, text="bob")])
    out = await r.search("q", threshold=0.35)
    assert [c.content for c in out] == ["bob"]

@pytest.mark.asyncio
async def test_fallback_on_availability_error():
    for exc in (ReadAdapterError("x"),
                RetrievalProviderError("p", "down"),
                EmbedderUnavailable("e", "down"),
                FederationError("unknown")):
        ad = FakeAdapter(raises=exc)
        r = make_retriever(adapter=ad, instance="i", lks_first=True, provider_hits=[bob_hit(1, 0.9, text="bob")])
        out = await r.search("q")
        assert [c.content for c in out] == ["bob"]

@pytest.mark.asyncio
async def test_propagate_correctness_security():
    reg_fp = EmbedFingerprint("m", 768, True, "cosine")
    for exc in (FingerprintMismatch(reg_fp, EmbedFingerprint("n", 768, True, "cosine"), ["model_id"]),
                FingerprintMissing("no stamp"),
                ACLViolation("inst", "denied")):
        ad = FakeAdapter(raises=exc)
        r = make_retriever(adapter=ad, instance="i", lks_first=True, provider_hits=[bob_hit(1, 0.9)])
        with pytest.raises(type(exc)):
            await r.search("q")
        assert r._provider.calls == []

@pytest.mark.asyncio
async def test_dangling_vector_failopen_preserved_bob_path():
    facts = {"real": a_fact("real")}
    provider_hits = [
        bob_hit(1, 0.9, fid="missing", text="gone"),
        bob_hit(2, 0.8, fid="real", text="kept"),
    ]
    r = make_retriever(lks_first=False, provider_hits=provider_hits, facts=facts)
    out = await r.search("q", top_k=5)
    contents = [c.content for c in out]
    assert "kept" in contents
    assert "gone" not in contents

@pytest.mark.asyncio
async def test_dangling_vector_failopen_on_lks_miss_fallback_path():
    # The fail-open must survive the DISPATCH: lks_first ON + an LKS MISS ⇒ fall back to _search_bob_store,
    # where a BoB hit whose source_fact_id is absent is SKIPPED (no raise). (audit r3 — pins the integration
    # the live E2E already exercised.)
    ad = FakeAdapter(hits=[])  # LKS miss ⇒ fallback
    facts = {"real": a_fact("real")}
    provider_hits = [
        bob_hit(1, 0.9, fid="missing", text="gone"),
        bob_hit(2, 0.8, fid="real", text="kept"),
    ]
    r = make_retriever(adapter=ad, instance="i", lks_first=True, provider_hits=provider_hits, facts=facts)
    out = await r.search("q", top_k=5)
    contents = [c.content for c in out]
    assert ad.calls            # the LKS adapter WAS consulted (and missed)
    assert "kept" in contents
    assert "gone" not in contents


@pytest.mark.asyncio
async def test_threshold_applied_to_lks_hits():
    ad = FakeAdapter(hits=[lks_hit(1, 0.9, "keep"), lks_hit(2, 0.2, "drop")])
    r = make_retriever(adapter=ad, instance="i", lks_first=True)
    out = await r.search("q", threshold=0.35)
    assert [c.content for c in out] == ["keep"]

@pytest.mark.asyncio
async def test_flag_on_unconfigured_instance_uses_bob():
    ad = FakeAdapter(hits=[lks_hit(1, 0.9, "lks")])
    r = make_retriever(adapter=ad, instance=None, lks_first=True, provider_hits=[bob_hit(1, 0.9, text="bob")])
    out = await r.search("q")
    assert [c.content for c in out] == ["bob"]
    assert ad.calls == []

@pytest.mark.asyncio
async def test_filters_passthrough_strips_include_deprecated():
    ad = FakeAdapter(hits=[lks_hit(1, 0.9)])
    r = make_retriever(adapter=ad, instance="i", lks_first=True)
    await r.search("q", filters={"source_path": "a.md", "include_deprecated": True})
    sent = ad.calls[0][3]
    assert sent == {"source_path": "a.md"}

@pytest.mark.asyncio
async def test_top_k_truncation_lks():
    ad = FakeAdapter(hits=[lks_hit(i, 1.0 - i*0.1) for i in range(5)])
    r = make_retriever(adapter=ad, instance="i", lks_first=True)
    out = await r.search("q", top_k=2)
    assert len(out) == 2
    assert out[0].score >= out[1].score


# ------------------------------
# Default-OFF bootstrap seam (_maybe_build_lks_adapter) — pins the wiring + soft-stamp posture.
# (Adjudication of audit r1 finding: the seam reads its opt-in flag from os.environ by design — the
# same pattern as MS2-C4's _maybe_build_write_fence — so we PIN the default-off + flag-on behavior
# rather than re-route through a config object that carries neither flag.)
# ------------------------------

REG_JSON = (
    '{"version":1,"instances":{"x":{"repo":"C:/d","ledger_dir":"ledger","collection":"x_768",'
    '"dim":768,"meta":{"acl":{"writer":"lks","readers":["bobclaw"],"mode":"ro"}}}}}'
)


class GoodSlotResolver:
    """Returns a valid embed_text SlotResolution (the seam constructs a SlotResolvedEmbedder)."""
    def get(self, name):
        from core.memory.models import SlotResolution
        return SlotResolution(slot_name=name, model="m", backend="lmstudio",
                              endpoint="http://localhost:8081", embedding_dimension=DIM)


def test_maybe_build_lks_adapter_off_by_default(monkeypatch):
    from core.memory.bootstrap import _maybe_build_lks_adapter
    monkeypatch.delenv("MEMORY_LKS_FIRST", raising=False)
    assert _maybe_build_lks_adapter(GoodSlotResolver(), object()) == (None, None, False)


def test_maybe_build_lks_adapter_on_but_no_instance(monkeypatch):
    from core.memory.bootstrap import _maybe_build_lks_adapter
    monkeypatch.setenv("MEMORY_LKS_FIRST", "true")
    monkeypatch.delenv("MEMORY_LKS_INSTANCE", raising=False)
    assert _maybe_build_lks_adapter(GoodSlotResolver(), object()) == (None, None, False)


def test_maybe_build_lks_adapter_on_builds_soft_stamp_adapter(monkeypatch, tmp_path):
    from core.memory.bootstrap import _maybe_build_lks_adapter
    from core.memory.lks_adapter import LKSReadAdapter
    reg = tmp_path / "reg.json"
    reg.write_text(REG_JSON, encoding="utf-8")
    monkeypatch.setenv("MEMORY_LKS_FIRST", "true")
    monkeypatch.setenv("MEMORY_LKS_INSTANCE", "x")
    monkeypatch.delenv("MEMORY_LKS_QDRANT_URL", raising=False)
    monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(reg))
    sentinel_client = object()
    adapter, instance, lks_first = _maybe_build_lks_adapter(GoodSlotResolver(), sentinel_client)
    assert isinstance(adapter, LKSReadAdapter)
    assert instance == "x"
    assert lks_first is True
    # soft-stamp posture + reuse of the provider client (no MEMORY_LKS_QDRANT_URL)
    assert adapter._require_stamp is False
    assert adapter._require_acl is True
    assert adapter._reader_id == "bobclaw"
    assert adapter._client is sentinel_client


def test_maybe_build_lks_adapter_truthy_parse_matches_config(monkeypatch):
    # The seam and config.MEMORY_LKS_FIRST parse IDENTICALLY: .strip().lower() == "true" (audit r2 + r5).
    # "1"/"yes"/"on"/"" are OFF; "true"/" true "/"True " are ON. The seam returns (None,None,False) iff OFF.
    from core.memory.bootstrap import _maybe_build_lks_adapter
    monkeypatch.setenv("MEMORY_LKS_INSTANCE", "x")
    for val in ("1", "yes", "on", "FALSE", "", "truthy"):
        assert val.strip().lower() != "true"   # sanity: these are genuinely OFF values
        monkeypatch.setenv("MEMORY_LKS_FIRST", val)
        assert _maybe_build_lks_adapter(GoodSlotResolver(), object()) == (None, None, False)


def test_maybe_build_lks_adapter_degrades_on_bad_registry(monkeypatch, tmp_path):
    # Flag ON but the registry is malformed ⇒ degrade to (None, None, False), never crash bootstrap (audit r2).
    from core.memory.bootstrap import _maybe_build_lks_adapter
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    monkeypatch.setenv("MEMORY_LKS_FIRST", "true")
    monkeypatch.setenv("MEMORY_LKS_INSTANCE", "x")
    monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(bad))
    assert _maybe_build_lks_adapter(GoodSlotResolver(), object()) == (None, None, False)
