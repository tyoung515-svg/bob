# tests/memory/test_consolidation.py

import pytest
from pathlib import Path

from core.memory.bootstrap import (
    _consolidation_enabled,
    _assert_single_qdrant_endpoint,
    _maybe_build_write_fence,
)
from core.memory.exceptions import MemoryConfigError
from core.memory.write_fence import WriteFence
from core.memory.indexer import MemoryIndexer
from core.memory.models import Fact, ConfidenceStub, IndexStats, ChunkRecord, Chunk


# ---------- helper mocks for tests 7, 8 ----------
class _FP:
    """Minimal fingerprint with .dim attribute."""
    dim = 768


class _FakeRegistry:
    """Minimal registry that does nothing."""
    def load(self):
        return self


class _FakeRegistryCls:
    """Mock class for FederationRegistry."""
    def __init__(self, *a, **k):
        pass
    def load(self):
        return _FakeRegistry()


class _Slot:
    """Minimal slot resolver that returns an object from get()."""
    def get(self, name):
        return object()


# ---------- helper mocks for test 9 ----------
class RecordingProvider:
    """Records all indexed items for later inspection."""
    def __init__(self):
        self.indexed = []
    def index(self, store_id, items):
        self.indexed.extend(items)
        return None
    def delete(self, store_id, ids):
        return None
    def scroll_payload(self, store_id, filter_dict):
        return []


class FakeFactStore:
    """Returns a single fact for a given id."""
    def __init__(self, fact):
        self._fact = fact
    async def get(self, fid):
        return self._fact
    async def all_ids(self):
        return [self._fact.fact_id]


class FakeSlot:
    """Returns a mock resolution with embedding_dimension."""
    def get(self, name):
        class _R:
            embedding_dimension = 768
        return _R()


class FakeEmbedder:
    """Returns a fixed embedding vector for every text."""
    async def embed(self, texts):
        return [[0.1] * 768 for _ in texts]


# ---------- test cases ----------

def test_flag_off_assert_is_noop(monkeypatch):
    """MEMORY_SINGLE_QDRANT unset -> _assert_single_qdrant_endpoint returns None even with different LKS URL."""
    monkeypatch.delenv("MEMORY_SINGLE_QDRANT", raising=False)
    monkeypatch.setenv("MEMORY_LKS_QDRANT_URL", "http://other:9999")
    # Should return None, no exception
    result = _assert_single_qdrant_endpoint("http://h:6333")
    assert result is None


def test_flag_off_fence_gate_returns_none(monkeypatch):
    """Both flags off -> _maybe_build_write_fence returns None without touching slot resolver."""
    monkeypatch.delenv("MEMORY_SINGLE_QDRANT", raising=False)
    monkeypatch.delenv("MEMORY_WRITE_FENCE_ENABLED", raising=False)
    slot = object()  # bare object safe because gate returns before using it
    result = _maybe_build_write_fence(slot, "bobclaw_")
    assert result is None


@pytest.mark.parametrize(
    "val,expected",
    [
        ("true", True), (" true ", True), ("TRUE", True), ("True", True),
        ("1", False), ("yes", False), ("on", False),
        ("false", False), ("", False), ("nope", False),
    ],
)
def test_consolidation_enabled_parse_matches_config(monkeypatch, val, expected):
    """audit r1: _consolidation_enabled() must agree with how config.py parses MEMORY_SINGLE_QDRANT
    (strict `.strip().lower() == 'true'`), or the flag would read ON in bootstrap but OFF in the config
    attribute (split-brain). Pins that the seam accepts ONLY 'true' (whitespace/case tolerant), never
    '1'/'yes'/'on'."""
    monkeypatch.setenv("MEMORY_SINGLE_QDRANT", val)
    assert _consolidation_enabled() is expected
    # the SAME parse config.py applies to the attribute — they must never disagree.
    assert (val.strip().lower() == "true") is _consolidation_enabled()


def test_consolidation_on_lks_empty_passes(monkeypatch):
    """MEMORY_SINGLE_QDRANT=true, MEMORY_LKS_QDRANT_URL empty -> no raise."""
    monkeypatch.setenv("MEMORY_SINGLE_QDRANT", "true")
    monkeypatch.delenv("MEMORY_LKS_QDRANT_URL", raising=False)
    _assert_single_qdrant_endpoint("http://h:6333")  # no exception


def test_consolidation_on_lks_equal_passes(monkeypatch):
    """Both urls equal -> passes."""
    monkeypatch.setenv("MEMORY_SINGLE_QDRANT", "true")
    monkeypatch.setenv("MEMORY_LKS_QDRANT_URL", "http://h:6333")
    _assert_single_qdrant_endpoint("http://h:6333")  # no exception


def test_consolidation_on_lks_differs_raises(monkeypatch):
    """Different LKS url -> MemoryConfigError."""
    monkeypatch.setenv("MEMORY_SINGLE_QDRANT", "true")
    monkeypatch.setenv("MEMORY_LKS_QDRANT_URL", "http://h:6353")
    with pytest.raises(MemoryConfigError):
        _assert_single_qdrant_endpoint("http://h:6333")


def test_whitespace_tolerance(monkeypatch):
    """Spaces in env values are stripped and match succeeds."""
    monkeypatch.setenv("MEMORY_SINGLE_QDRANT", " true ")
    monkeypatch.setenv("MEMORY_LKS_QDRANT_URL", "  http://h:6333  ")
    _assert_single_qdrant_endpoint("http://h:6333")  # no raise
    # Also assert _consolidation_enabled returns True with spaced value
    assert _consolidation_enabled() is True


def test_consolidation_forces_fence(monkeypatch):
    """MEMORY_SINGLE_QDRANT=true, MEMORY_WRITE_FENCE_ENABLED unset -> WriteFence built."""
    monkeypatch.setenv("MEMORY_SINGLE_QDRANT", "true")
    monkeypatch.delenv("MEMORY_WRITE_FENCE_ENABLED", raising=False)

    # Patch internal imports used inside _maybe_build_write_fence
    monkeypatch.setattr(
        "core.memory.fingerprint.fingerprint_from_slot",
        lambda res: _FP()
    )
    monkeypatch.setattr(
        "core.ledger.federation.FederationRegistry",
        _FakeRegistryCls
    )
    monkeypatch.setattr(
        "core.ledger.federation.default_registry_path",
        lambda: Path("x")
    )
    monkeypatch.setattr(
        "core.memory.write_fence.register_bobclaw_memory",
        lambda *a, **k: None
    )

    slot = _Slot()
    out = _maybe_build_write_fence(slot, "bobclaw_")
    assert isinstance(out, WriteFence)


def test_write_fence_enabled_alone_builds_fence(monkeypatch):
    """MEMORY_WRITE_FENCE_ENABLED=true, MEMORY_SINGLE_QDRANT unset -> WriteFence built (C4 path)."""
    monkeypatch.delenv("MEMORY_SINGLE_QDRANT", raising=False)
    monkeypatch.setenv("MEMORY_WRITE_FENCE_ENABLED", "true")

    monkeypatch.setattr(
        "core.memory.fingerprint.fingerprint_from_slot",
        lambda res: _FP()
    )
    monkeypatch.setattr(
        "core.ledger.federation.FederationRegistry",
        _FakeRegistryCls
    )
    monkeypatch.setattr(
        "core.ledger.federation.default_registry_path",
        lambda: Path("x")
    )
    monkeypatch.setattr(
        "core.memory.write_fence.register_bobclaw_memory",
        lambda *a, **k: None
    )

    slot = _Slot()
    out = _maybe_build_write_fence(slot, "bobclaw_")
    assert isinstance(out, WriteFence)


@pytest.mark.asyncio
async def test_no_duplicate_corpus_write_path(monkeypatch):
    """Prove that every chunk written by MemoryIndexer is fact-derived (no corpus write path)."""
    fact_id = "f1"
    fact = Fact(
        fact_id=fact_id,
        generation_method="test",
        body={"text": "some plain text content"},
        source_event_id="evt1",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )

    fact_store = FakeFactStore(fact)
    embedder = FakeEmbedder()
    provider = RecordingProvider()
    slot_resolver = FakeSlot()

    indexer = MemoryIndexer(
        fact_store=fact_store,
        embedder=embedder,
        provider=provider,
        store_id="s",
        slot_resolver=slot_resolver,
    )

    stats = await indexer.reindex_facts(["f1"])

    # There should be indexed items
    assert provider.indexed, "indexer wrote nothing"
    for item in provider.indexed:
        assert item.payload["source_fact_id"] == fact_id
        assert item.payload["source_path"] == f"fact://{fact_id}"

    # Also ensure that the number of indexed chunks matches expectation (1 chunk for plain text)
    assert len(provider.indexed) == 1
