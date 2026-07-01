from __future__ import annotations

import re
from typing import get_args

import pytest

from core.memory.models import (
    AttestationEnvelope,
    CapabilityClass,
    Chunk,
    ChunkRecord,
    ConfidenceStub,
    Event,
    Fact,
    HealthStatus,
    Hit,
    IndexReceipt,
    IndexStats,
    Query,
    RankedResults,
    RetrievedChunk,
    Section,
    SlotResolution,
)


class TestEvent:
    def test_fields(self):
        e = Event(
            event_id="evt_001",
            kind="agent_save",
            body={"key": "value"},
            ts="2026-05-09T00:00:00Z",
            hash="abc123",
            prev_hash=None,
        )
        assert e.event_id == "evt_001"
        assert e.kind == "agent_save"
        assert e.body == {"key": "value"}
        assert e.ts == "2026-05-09T00:00:00Z"
        assert e.hash == "abc123"
        assert e.prev_hash is None

    def test_frozen(self):
        e = Event("id", "kind", {}, "ts", "hash", None)
        with pytest.raises(AttributeError):
            e.event_id = "other"


class TestFact:
    def test_fields(self):
        f = Fact(
            fact_id="fact_001",
            generation_method="extract_facts_from_event",
            body={"claim": "sky is blue"},
            source_event_id="evt_001",
            input_hash="def456",
            confidence=ConfidenceStub(),
            ts="2026-05-09T00:00:00Z",
        )
        assert f.fact_id == "fact_001"
        assert f.generation_method == "extract_facts_from_event"
        assert f.input_hash == "def456"
        assert f.confidence.rank == "normal"

    def test_attestation_defaults_to_none(self):
        f = Fact(
            fact_id="fact_002",
            generation_method="extract_facts_from_event",
            body={"claim": "test"},
            source_event_id="evt_001",
            input_hash="def456",
            confidence=ConfidenceStub(),
            ts="2026-05-09T00:00:00Z",
        )
        assert f.attestation is None

    def test_attestation_settable(self):
        env = AttestationEnvelope(
            producer_id="test_producer",
            producer_hash="abc123",
            producer_signature="stub:sig",
            produced_at="2026-05-12T00:00:00Z",
            runtime_env_hash="env123",
        )
        f = Fact(
            fact_id="fact_003",
            generation_method="extract_facts_from_event",
            body={"claim": "test"},
            source_event_id="evt_001",
            input_hash="def456",
            confidence=ConfidenceStub(),
            ts="2026-05-09T00:00:00Z",
            attestation=env,
        )
        assert f.attestation == env
        assert f.attestation.producer_id == "test_producer"


class TestConfidenceStub:
    def test_defaults(self):
        c = ConfidenceStub()
        assert c.alpha == 1.0
        assert c.beta == 1.0
        assert c.rank == "normal"
        assert c.decay_class == "stable_biographical"
        assert c.last_corroboration_event_id is None
        assert c.last_corroboration_ts is None

    def test_new_fields_default_to_none(self):
        c = ConfidenceStub(alpha=2.0, beta=3.0)
        assert c.last_corroboration_event_id is None
        assert c.last_corroboration_ts is None

    def test_new_fields_settable(self):
        c = ConfidenceStub(
            alpha=2.0, beta=3.0, rank="preferred",
            last_corroboration_event_id="evt_001",
            last_corroboration_ts="2026-05-12T00:00:00Z",
        )
        assert c.last_corroboration_event_id == "evt_001"
        assert c.last_corroboration_ts == "2026-05-12T00:00:00Z"

    def test_frozen(self):
        c = ConfidenceStub()
        with pytest.raises(AttributeError):
            c.alpha = 2.0


class TestSection:
    def test_fields(self):
        s = Section("sec_001", "Test Section", ["fact_001"], "v1.0", "ghi789")
        assert s.section_id == "sec_001"
        assert s.fact_ids == ["fact_001"]
        assert s.spec_version == "v1.0"


class TestChunk:
    def test_auto_hash(self):
        c = Chunk(text="hello world", heading_path=["Intro"])
        assert len(c.chunk_hash) == 64
        assert re.match(r"^[a-f0-9]{64}$", c.chunk_hash)

    def test_deterministic_hash(self):
        c1 = Chunk(text="hello world", heading_path=["Intro"])
        c2 = Chunk(text="hello world", heading_path=["Intro"])
        assert c1.chunk_hash == c2.chunk_hash

    def test_different_text_different_hash(self):
        c1 = Chunk(text="hello", heading_path=["Intro"])
        c2 = Chunk(text="world", heading_path=["Intro"])
        assert c1.chunk_hash != c2.chunk_hash

    def test_provided_hash_preserved(self):
        c = Chunk(text="hello", heading_path=[], chunk_hash="provided_hash")
        assert c.chunk_hash == "provided_hash"

    def test_frozen(self):
        c = Chunk(text="hi", heading_path=[])
        with pytest.raises(AttributeError):
            c.text = "bye"


class TestChunkRecord:
    def test_fields(self):
        r = ChunkRecord(id="chunk_001", vector=[0.1, 0.2], payload={"key": "val"})
        assert r.id == "chunk_001"
        assert r.vector == [0.1, 0.2]
        assert r.payload == {"key": "val"}


class TestHit:
    def test_fields(self):
        h = Hit(id="hit_001", score=0.95, payload={"fact_id": "fact_001"})
        assert h.id == "hit_001"
        assert h.score == 0.95


class TestRetrievedChunk:
    def test_default_boosted_score(self):
        r = RetrievedChunk(
            content="some text",
            score=0.9,
            source_fact_id="fact_001",
            source_path="/path/to/file",
            heading_path=["Section"],
        )
        assert r.boosted_score is None

    def test_with_boosted_score(self):
        r = RetrievedChunk(
            content="text",
            score=0.9,
            source_fact_id="fact_001",
            source_path=None,
            heading_path=[],
            boosted_score=0.95,
        )
        assert r.boosted_score == 0.95


class TestIndexStats:
    def test_defaults(self):
        s = IndexStats()
        assert s.chunks_changed == 0
        assert s.chunks_skipped == 0
        assert s.chunks_deleted == 0
        assert s.facts_processed == 0
        assert s.errors == []

    def test_not_frozen(self):
        s = IndexStats()
        s.chunks_changed = 5
        assert s.chunks_changed == 5


class TestIndexReceipt:
    def test_fields(self):
        r = IndexReceipt("qdrant", "store_001", 10, "2026-05-09T00:00:00Z")
        assert r.provider_id == "qdrant"
        assert r.item_count == 10


class TestQuery:
    def test_fields(self):
        q = Query(text="find facts about X", capability_class="text_dense")
        assert q.text == "find facts about X"
        assert q.capability_class == "text_dense"


class TestRankedResults:
    def test_fields(self):
        hits = [Hit(id="h1", score=0.9, payload={})]
        r = RankedResults(hits=hits, provider_id="qdrant", latency_ms=42)
        assert len(r.hits) == 1
        assert r.latency_ms == 42


class TestHealthStatus:
    def test_ok(self):
        h = HealthStatus(ok=True)
        assert h.ok
        assert h.detail == ""

    def test_fail(self):
        h = HealthStatus(ok=False, detail="Qdrant not reachable")
        assert not h.ok
        assert h.detail == "Qdrant not reachable"


class TestCapabilityClass:
    def test_valid_literals(self):
        c: CapabilityClass = "text_dense"
        assert c == "text_dense"

        c = "multimodal_dense"
        assert c == "multimodal_dense"

    def test_all_values(self):
        expected = {
            "text_dense",
            "text_sparse",
            "multimodal_dense",
            "visual_doc_late_interaction",
            "managed_remote",
            "rerank_cross",
        }
        assert set(get_args(CapabilityClass)) == expected


class TestSlotResolution:
    def test_fields(self):
        sr = SlotResolution(
            slot_name="embed_text",
            model="granite-embedding-311m",
            backend="lmstudio",
            endpoint="http://localhost:1234",
            embedding_dimension=768,
        )
        assert sr.slot_name == "embed_text"
        assert sr.model == "granite-embedding-311m"
        assert sr.embedding_dimension == 768

    def test_optional_dimension(self):
        sr = SlotResolution(
            slot_name="extract_small",
            model="gemma-4-e4b-it",
            backend="lmstudio",
            endpoint="http://localhost:1234",
        )
        assert sr.embedding_dimension is None


class TestAttestationEnvelope:
    def test_constructs(self):
        env = AttestationEnvelope(
            producer_id="test_producer",
            producer_hash="abc123",
            producer_signature="stub:sig",
            produced_at="2026-05-12T00:00:00Z",
            runtime_env_hash="env123",
        )
        assert env.producer_id == "test_producer"
        assert env.producer_hash == "abc123"
        assert env.producer_signature == "stub:sig"
        assert env.produced_at == "2026-05-12T00:00:00Z"
        assert env.runtime_env_hash == "env123"

    def test_frozen(self):
        env = AttestationEnvelope(
            producer_id="p", producer_hash="h", producer_signature="s",
            produced_at="t", runtime_env_hash="e",
        )
        with pytest.raises(AttributeError):
            env.producer_id = "other"
