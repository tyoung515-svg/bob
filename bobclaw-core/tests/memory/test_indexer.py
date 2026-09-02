from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.memory.exceptions import RetrievalProviderError
from core.memory.indexer import MemoryIndexer
from core.memory.interfaces import Indexer
from core.memory.models import (
    Chunk,
    ChunkRecord,
    ConfidenceStub,
    Fact,
    IndexReceipt,
    IndexStats,
    SlotResolution,
)
from core.memory.parser import Chunk as ParserChunk
from core.memory.parser import ParsedDocument


@pytest.fixture
def mock_fact_store():
    store = MagicMock()
    store.all_ids = AsyncMock(return_value=["f1", "f2"])
    _fact_kwargs = dict(
        generation_method="test",
        source_event_id="evt1",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )

    async def _side_effect(fact_id: str):
        if fact_id == "f_empty" or fact_id == "f_plain":
            return store.get.return_value
        return Fact(
            fact_id=fact_id,
            body={"kind": "markdown", "text": f"# Fact {fact_id}\n\nContent {fact_id} [[link]]"},
            **_fact_kwargs,
        )

    store.get = AsyncMock(side_effect=_side_effect)
    return store


@pytest.fixture
def mock_embedder():
    emb = MagicMock()
    emb.embedding_dimension = 768
    emb.embed = AsyncMock(return_value=[[0.1] * 768, [0.2] * 768])
    return emb


@pytest.fixture
def mock_provider():
    prov = MagicMock()
    prov.index.return_value = IndexReceipt(
        provider_id="test", store_id="store1", item_count=2, ts="now"
    )
    prov.delete.return_value = None
    prov.scroll_payload.return_value = iter([])
    return prov


@pytest.fixture
def mock_slot_resolver():
    sr = MagicMock()
    sr.get.return_value = SlotResolution(
        slot_name="embed_text",
        model="test-model",
        backend="local",
        endpoint="http://localhost:1234",
        embedding_dimension=768,
    )
    return sr


@pytest.fixture
def mock_parser():
    def _parser(path: Path):
        return ParsedDocument(
            frontmatter={},
            chunks=[
                ParserChunk(heading_path=["H1"], text="chunk1 text", chunk_hash="h1"),
                ParserChunk(heading_path=["H1"], text="chunk2 text", chunk_hash="h2"),
            ],
            wikilinks=["target"],
            inline_tags=[],
        )

    return _parser


@pytest.mark.asyncio
async def test_reindex_facts_calls_embedder_and_provider(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    stats = await idx.reindex_facts(["f1"])

    assert mock_embedder.embed.called
    assert mock_provider.index.called


@pytest.mark.asyncio
async def test_index_stats_counters(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    stats = await idx.reindex_facts(["f1"])

    assert stats.facts_processed == 1
    assert stats.chunks_changed == 2
    assert stats.errors == []


@pytest.mark.asyncio
async def test_pre_deletes_existing_items_before_index(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    await idx.reindex_facts(["f1"])

    assert mock_provider.delete.called


@pytest.mark.asyncio
async def test_per_fact_error_caught_and_loop_continues(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    mock_fact_store.get.side_effect = [Exception("not found")]
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    stats = await idx.reindex_facts(["f_bad"])

    assert len(stats.errors) == 1
    assert "f_bad" in stats.errors[0][0]


@pytest.mark.asyncio
async def test_empty_fact_body_chunks_skipped(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    mock_fact_store.get.return_value = Fact(
        fact_id="f_empty",
        generation_method="test",
        body={"text": ""},
        source_event_id="evt1",
        input_hash="blake3:" + "b" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    stats = await idx.reindex_facts(["f_empty"])

    assert stats.chunks_skipped == 1
    assert stats.facts_processed == 1
    assert stats.chunks_changed == 0


@pytest.mark.asyncio
async def test_markdown_fact_body_uses_parser(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    stats = await idx.reindex_facts(["f1"])

    assert stats.chunks_changed == 2


@pytest.mark.asyncio
async def test_non_markdown_fact_body_single_chunk(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    mock_fact_store.get.return_value = Fact(
        fact_id="f_plain",
        generation_method="test",
        body={"text": "Plain text content"},
        source_event_id="evt1",
        input_hash="blake3:" + "c" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00+00:00",
    )
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    stats = await idx.reindex_facts(["f_plain"])

    assert stats.chunks_changed == 1


@pytest.mark.asyncio
async def test_embedder_uses_slot_resolver(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    await idx.reindex_facts(["f1"])

    mock_slot_resolver.get.assert_called_with("embed_text")


@pytest.mark.asyncio
async def test_dim_mismatch_raises_retrieval_provider_error(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    mock_embedder.embed = AsyncMock(return_value=[[0.1] * 128])
    mock_slot_resolver.get.return_value = SlotResolution(
        slot_name="embed_text",
        model="test-model",
        backend="local",
        endpoint="http://localhost:1234",
        embedding_dimension=768,
    )
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )

    with pytest.raises(RetrievalProviderError):
        await idx.reindex_facts(["f1"])


@pytest.mark.asyncio
async def test_drop_facts_calls_scroll_payload_and_delete(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    mock_provider.scroll_payload.return_value = iter(["chunk:f1:h1", "chunk:f1:h2"])
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    removed = await idx.drop_facts(["f1"])
    assert removed == 2
    mock_provider.scroll_payload.assert_called_once_with(
        "test_store", {"source_fact_id": "f1"}
    )
    mock_provider.delete.assert_called_once_with(
        "test_store", ["chunk:f1:h1", "chunk:f1:h2"]
    )


@pytest.mark.asyncio
async def test_drop_facts_two_facts(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    mock_provider.scroll_payload.side_effect = [
        iter(["chunk:f1:h1"]),
        iter(["chunk:f2:h1"]),
    ]
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    removed = await idx.drop_facts(["f1", "f2"])
    assert removed == 2
    assert mock_provider.scroll_payload.call_count == 2


@pytest.mark.asyncio
async def test_drop_facts_no_chunks_returns_zero(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    mock_provider.scroll_payload.return_value = iter([])
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    removed = await idx.drop_facts(["f_no_chunks"])
    assert removed == 0
    mock_provider.delete.assert_not_called()


def test_protocol_conformance(
    mock_fact_store, mock_embedder, mock_provider, mock_slot_resolver, mock_parser
):
    idx = MemoryIndexer(
        fact_store=mock_fact_store,
        embedder=mock_embedder,
        provider=mock_provider,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        parser=mock_parser,
    )
    assert isinstance(idx, Indexer)
