import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from core.memory.event_log import SQLiteEventLog
from core.memory.fact_store import SQLiteFactStore
from core.memory.indexer import MemoryIndexer
from core.memory.retriever import MemoryRetriever
from core.memory.providers.qdrant_provider import QdrantRetrievalProvider
from core.memory.models import Event, Fact, ConfidenceStub, ChunkRecord, IndexReceipt, HealthStatus, RetrievedChunk

@pytest.mark.asyncio
async def test_wave2_smoke(tmp_path: Path):
    from core.memory._db import init_schema
    db_path = tmp_path / "memory.db"
    await init_schema(db_path)
    
    event_log = SQLiteEventLog(db_path)
    fact_store = SQLiteFactStore(db_path)
    
    from core.memory._hashing import _compute_event_hash
    event_body = {"text": "smoke test fact"}
    event_hash = _compute_event_hash(event_body, None)
    
    event = Event(
        event_id="evt_1",
        kind="test",
        body=event_body,
        ts="2026-05-12T00:00:00Z",
        hash=event_hash,
        prev_hash=None
    )
    await event_log.append(event)
    
    fact = Fact(
        fact_id="fct_1",
        generation_method="extract_facts_from_event",
        body={"text": "smoke test fact"},
        source_event_id="evt_1",
        input_hash="blake3:" + "0" * 64,
        confidence=ConfidenceStub(alpha=1.0, beta=1.0, rank="normal", decay_class="stable_biographical"),
        ts="2026-05-12T00:00:00Z"
    )
    await fact_store.put(fact)
    
    mock_provider = MagicMock(spec=QdrantRetrievalProvider)
    mock_provider.index = MagicMock(return_value=IndexReceipt("qdrant-local", "store_1", 1, "2026-05-12T00:00:00Z"))
    mock_provider.query_vector = MagicMock(return_value=MagicMock(hits=[MagicMock(id="fct_1_0", score=0.8, payload={"source_fact_id": "fct_1", "text": "smoke test fact"})]))
    
    mock_embedder = MagicMock()
    mock_embedder.embedding_dimension = 3
    mock_embedder.embed_doc = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    mock_embedder.embed_query = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    
    mock_slot_resolver = MagicMock()
    mock_slot_resolver.get = MagicMock(
        return_value=MagicMock(
            model="test_model",
            endpoint="test_endpoint",
            embedding_dimension=mock_embedder.embedding_dimension,
        )
    )
    
    from core.memory.query_log import QueryLog
    query_log = QueryLog(tmp_path / "query_log.jsonl")
    
    indexer = MemoryIndexer(fact_store, mock_embedder, mock_provider, "store_1", mock_slot_resolver)
    await indexer.reindex_all()
    
    retriever = MemoryRetriever(mock_embedder, mock_provider, fact_store, "store_1", mock_slot_resolver, query_log)
    results = await retriever.search("matching query", hop_budget=1)
    
    assert len(results) == 1
    expected_fused = 1.0 / 61.0
    expected_boost = expected_fused * 0.5
    assert abs(results[0].boosted_score - expected_boost) < 1e-10
    
    with open(tmp_path / "query_log.jsonl", "r") as f:
        lines = f.readlines()
        assert len(lines) == 1
