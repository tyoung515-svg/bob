from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Literal

from core.memory.interfaces import (
    Embedder,
    EventLog,
    FactStore,
    Indexer,
    Renderer,
    RetrievalProvider,
    Retriever,
    Splicer,
)
from core.memory.models import (
    ChunkRecord,
    Event,
    Fact,
    FilterExpr,
    HealthStatus,
    IndexReceipt,
    IndexStats,
    Query,
    RankedResults,
    RetrievedChunk,
    Section,
)


class _StubEventLog:
    def append(self, event: Event) -> str:
        return event.event_id

    def get(self, event_id: str) -> Event:
        raise NotImplementedError

    def replay(self, since_event_id: str | None = None) -> Iterator[Event]:
        raise NotImplementedError


class _StubFactStore:
    def put(self, fact: Fact) -> str:
        return fact.fact_id

    def get(self, fact_id: str) -> Fact:
        raise NotImplementedError

    def query(self, filters: dict) -> list[Fact]:
        return []

    def delete(self, fact_id: str) -> None:
        return None

    def all_ids(self) -> list[str]:
        return []


class _StubSplicer:
    def recompute(self, affected_fact_ids: list[str]) -> list[Section]:
        return []

    def get_section(self, section_id: str) -> Section:
        raise NotImplementedError

    def all_sections(self) -> list[Section]:
        return []


class _StubRenderer:
    def render(self, sections: list[Section], output_dir: Path) -> list[Path]:
        return []


class _StubEmbedder:
    embedding_dimension: int = 768

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return []


class _StubRetrievalProvider:
    provider_id: str = "stub"
    locality: Literal["local", "remote"] = "local"
    capability_classes: set[str] = {"text_dense"}

    def index(self, store_id: str, items: list[ChunkRecord]) -> IndexReceipt:
        raise NotImplementedError

    def query(
        self, store_id: str, q: Query, k: int, filters: FilterExpr | None
    ) -> RankedResults:
        raise NotImplementedError

    def delete(self, store_id: str, item_ids: list[str]) -> None:
        return None

    def scroll_payload(
        self, store_id: str, payload_filter: dict, batch_size: int = 128
    ):
        return iter([])

    def health(self) -> HealthStatus:
        return HealthStatus(ok=True)


class _StubIndexer:
    async def reindex_all(self) -> IndexStats:
        return IndexStats()

    async def reindex_facts(self, fact_ids: list[str]) -> IndexStats:
        return IndexStats()

    async def drop_facts(self, fact_ids: list[str]) -> int:
        return 0


class _StubRetriever:
    async def search(
        self,
        query: str,
        top_k: int = 3,
        threshold: float = 0.35,
        filters: dict | None = None,
        hop_budget: int = 1,
    ) -> list[RetrievedChunk]:
        return []


def test_event_log_protocol():
    assert isinstance(_StubEventLog(), EventLog)


def test_fact_store_protocol():
    assert isinstance(_StubFactStore(), FactStore)


def test_splicer_protocol():
    assert isinstance(_StubSplicer(), Splicer)


def test_renderer_protocol():
    assert isinstance(_StubRenderer(), Renderer)


def test_embedder_protocol():
    assert isinstance(_StubEmbedder(), Embedder)


def test_retrieval_provider_protocol():
    assert isinstance(_StubRetrievalProvider(), RetrievalProvider)


def test_indexer_protocol():
    assert isinstance(_StubIndexer(), Indexer)


def test_retriever_protocol():
    assert isinstance(_StubRetriever(), Retriever)
