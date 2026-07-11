from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.exceptions import EmbedderUnavailable, RetrievalProviderError
from core.memory.indexer import DIMENSION_PROBE_TEXT, MemoryIndexer
from core.memory.models import (
    ConfidenceStub,
    Fact,
    RankedResults,
    SlotResolution,
)
from core.memory.retriever import MemoryRetriever


class _ProbeEmbedder:
    def __init__(self, doc_results: list[object], query_result: list[float]) -> None:
        self._doc_results = list(doc_results)
        self._query_result = query_result
        self.doc_calls: list[list[str]] = []
        self.query_calls: list[list[str]] = []

    async def embed_doc(self, texts: list[str]) -> list[list[float]]:
        self.doc_calls.append(list(texts))
        result = self._doc_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def embed_query(self, texts: list[str]) -> list[list[float]]:
        self.query_calls.append(list(texts))
        return [self._query_result]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("indexing and retrieval must use their asymmetric methods")


class _EqualProbeEmbedder(_ProbeEmbedder):
    def __eq__(self, other: object) -> bool:
        return isinstance(other, _EqualProbeEmbedder)

    def __hash__(self) -> int:
        return 1


def _fact(fact_id: str = "f1") -> Fact:
    return Fact(
        fact_id=fact_id,
        generation_method="test",
        body={"text": "source text"},
        source_event_id="evt1",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )


def _slot_resolver(dim: int) -> MagicMock:
    resolver = MagicMock()
    resolver.get.return_value = SlotResolution(
        slot_name="embed_text",
        model="test-model",
        backend="lmstudio",
        endpoint="http://localhost:1234",
        embedding_dimension=dim,
    )
    return resolver


def _indexer(embedder: _ProbeEmbedder, provider: MagicMock, dim: int) -> MemoryIndexer:
    facts = MagicMock()
    facts.get = AsyncMock(return_value=_fact())
    return MemoryIndexer(
        fact_store=facts,
        embedder=embedder,
        provider=provider,
        store_id="test_store",
        slot_resolver=_slot_resolver(dim),
    )


@pytest.mark.asyncio
async def test_dim_probe_mismatch_refuses_first_index_before_provider_mutation():
    embedder = _ProbeEmbedder([[[0.1, 0.2]]], [0.1, 0.2, 0.3])
    provider = MagicMock()
    indexer = _indexer(embedder, provider, dim=3)

    with pytest.raises(
        RetrievalProviderError,
        match="unverified embedding dimension.*2.*configured 3",
    ):
        await indexer.reindex_facts(["f1"])

    assert embedder.doc_calls == [[DIMENSION_PROBE_TEXT]]
    provider.delete.assert_not_called()
    provider.index.assert_not_called()


@pytest.mark.asyncio
async def test_successful_dim_probe_is_cached_across_writes():
    vector = [0.1, 0.2, 0.3]
    embedder = _ProbeEmbedder([[vector], [vector], [vector]], vector)
    provider = MagicMock()
    indexer = _indexer(embedder, provider, dim=3)

    await indexer.reindex_facts(["f1"])
    await indexer.reindex_facts(["f1"])

    assert embedder.doc_calls == [
        [DIMENSION_PROBE_TEXT],
        ["source text"],
        ["source text"],
    ]
    assert provider.index.call_count == 2


@pytest.mark.asyncio
async def test_unreachable_probe_refuses_write_but_read_path_still_serves():
    embedder = _ProbeEmbedder(
        [EmbedderUnavailable("http://localhost:1234", "down")],
        [0.1, 0.2, 0.3],
    )
    provider = MagicMock()
    indexer = _indexer(embedder, provider, dim=3)

    with pytest.raises(RetrievalProviderError, match="unverified embedding dimension"):
        await indexer.reindex_facts(["f1"])

    provider.delete.assert_not_called()
    provider.index.assert_not_called()

    provider.query_vector.return_value = RankedResults(
        hits=[], provider_id="test", latency_ms=0,
    )
    query_log = MagicMock()
    retriever = MemoryRetriever(
        embedder=embedder,
        provider=provider,
        fact_store=MagicMock(),
        store_id="test_store",
        slot_resolver=_slot_resolver(3),
        query_log=query_log,
    )
    assert await retriever.search("query") == []
    assert embedder.query_calls == [["query"]]
    provider.query_vector.assert_called_once()


@pytest.mark.asyncio
async def test_delete_bypasses_unreachable_dim_probe_after_index_refusal():
    embedder = _ProbeEmbedder(
        [EmbedderUnavailable("http://localhost:1234", "down")],
        [0.1, 0.2, 0.3],
    )
    provider = MagicMock()
    indexer = _indexer(embedder, provider, dim=3)

    with pytest.raises(RetrievalProviderError, match="unverified embedding dimension"):
        await indexer.reindex_facts(["f1"])

    provider.scroll_payload.return_value = iter(["chunk:f1:old"])

    assert await indexer.drop_facts(["f1"]) == 1
    assert embedder.doc_calls == [[DIMENSION_PROBE_TEXT]]
    provider.delete.assert_called_once_with("test_store", ["chunk:f1:old"])
    provider.index.assert_not_called()


@pytest.mark.asyncio
async def test_successful_dim_probe_cache_is_shared_by_indexers_in_process():
    vector = [0.1, 0.2, 0.3]
    embedder = _ProbeEmbedder([[vector], [vector], [vector]], vector)
    first = _indexer(embedder, MagicMock(), dim=3)
    second = _indexer(embedder, MagicMock(), dim=3)

    await first.reindex_facts(["f1"])
    await second.reindex_facts(["f1"])

    assert embedder.doc_calls == [
        [DIMENSION_PROBE_TEXT],
        ["source text"],
        ["source text"],
    ]


@pytest.mark.asyncio
async def test_equal_comparing_embedders_are_probed_by_identity():
    vector = [0.1, 0.2, 0.3]
    first_embedder = _EqualProbeEmbedder([[vector], [vector]], vector)
    second_embedder = _EqualProbeEmbedder([[vector], [vector]], vector)

    await _indexer(first_embedder, MagicMock(), dim=3).reindex_facts(["f1"])
    await _indexer(second_embedder, MagicMock(), dim=3).reindex_facts(["f1"])

    expected_calls = [[DIMENSION_PROBE_TEXT], ["source text"]]
    assert first_embedder.doc_calls == expected_calls
    assert second_embedder.doc_calls == expected_calls


@pytest.mark.asyncio
async def test_legacy_embed_fallback_logs_embedder_type(caplog):
    vector = [0.1, 0.2, 0.3]
    embedder = SimpleNamespace(
        embed=AsyncMock(side_effect=[[vector], [vector]]),
    )
    indexer = _indexer(embedder, MagicMock(), dim=3)

    with caplog.at_level(logging.WARNING, logger="core.memory.indexer"):
        await indexer.reindex_facts(["f1"])

    assert "deprecated embed() fallback" in caplog.text
    assert "SimpleNamespace" in caplog.text


@pytest.mark.asyncio
async def test_non_weakrefable_legacy_embedder_probes_without_caching():
    vector = [0.1, 0.2, 0.3]
    embed = AsyncMock(side_effect=[[vector], [vector], [vector], [vector]])
    embedder = SimpleNamespace(embed=embed)
    provider = MagicMock()
    indexer = _indexer(embedder, provider, dim=3)

    await indexer.reindex_facts(["f1"])
    await indexer.reindex_facts(["f1"])

    assert [args.args[0] for args in embed.await_args_list] == [
        [DIMENSION_PROBE_TEXT],
        ["source text"],
        [DIMENSION_PROBE_TEXT],
        ["source text"],
    ]
    assert provider.index.call_count == 2
