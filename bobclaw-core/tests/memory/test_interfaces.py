from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
import textwrap
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
    Hit,
    IndexReceipt,
    IndexStats,
    RankedResults,
    RetrievedChunk,
    Section,
)
from core.memory.retriever import MemoryRetriever


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

    def query_vector(
        self, store_id: str, vector: list[float], k: int, filters: FilterExpr | None
    ) -> RankedResults:
        return RankedResults(hits=[], provider_id=self.provider_id, latency_ms=0)

    def delete(self, store_id: str, item_ids: list[str]) -> None:
        return None

    def scroll_payload(
        self, store_id: str, payload_filter: dict, batch_size: int = 128
    ):
        return iter([])

    def health(self) -> HealthStatus:
        return HealthStatus(ok=True)


class _MinimalRetrievalProvider:
    provider_id: str = "minimal"
    locality: Literal["local", "remote"] = "local"
    capability_classes: set[str] = {"text_dense"}

    def index(self, store_id: str, items: list[ChunkRecord]) -> IndexReceipt:
        return IndexReceipt(
            provider_id=self.provider_id,
            store_id=store_id,
            item_count=len(items),
            ts="",
        )

    def query_vector(
        self, store_id: str, vector: list[float], k: int, filters: FilterExpr | None
    ) -> RankedResults:
        return RankedResults(
            hits=[Hit(id="chunk-1", score=0.9, payload={"text": "match"})],
            provider_id=self.provider_id,
            latency_ms=0,
        )

    def delete(self, store_id: str, item_ids: list[str]) -> None:
        return None

    def scroll_payload(
        self, store_id: str, payload_filter: dict, batch_size: int = 128
    ) -> Iterator[str]:
        return iter(())

    def health(self) -> HealthStatus:
        return HealthStatus(ok=True)


class _FakeEmbedder:
    embedding_dimension: int = 3

    async def embed(self, texts: list[str]) -> list[list[float]]:
        assert texts == ["needle"]
        return [[0.1, 0.2, 0.3]]


class _StubSlotResolver:
    def get(self, slot_name: str) -> None:
        assert slot_name == "embed_text"
        return None


class _StubQueryLog:
    def append(self, entry: dict) -> None:
        return None


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


def _public_methods(protocol_or_class: type) -> set[str]:
    return {
        name
        for name, member in vars(protocol_or_class).items()
        if not name.startswith("_") and callable(member)
    }


def _provider_methods_called_by_retriever() -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(MemoryRetriever)))
    return {
        call.func.attr
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr == "_provider"
        and isinstance(call.func.value.value, ast.Name)
        and call.func.value.value.id == "self"
    }


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
    # @runtime_checkable isinstance() validates member names only, never signatures
    # or behavior. The search-path test below covers the behavior runtime misses.
    assert isinstance(_StubRetrievalProvider(), RetrievalProvider)


def test_minimal_protocol_provider_drives_retriever_search_path():
    protocol_methods = _public_methods(RetrievalProvider)
    assert _public_methods(_MinimalRetrievalProvider) == protocol_methods

    provider = _MinimalRetrievalProvider()
    assert isinstance(provider, RetrievalProvider)

    results = asyncio.run(
        MemoryRetriever(
            embedder=_FakeEmbedder(),
            provider=provider,
            fact_store=object(),
            store_id="test_store",
            slot_resolver=_StubSlotResolver(),
            query_log=_StubQueryLog(),
        ).search("needle", top_k=1, threshold=0.0)
    )

    assert [result.content for result in results] == ["match"]


def test_retriever_provider_calls_are_declared_on_protocol():
    provider_calls = _provider_methods_called_by_retriever()
    assert provider_calls
    assert provider_calls <= _public_methods(RetrievalProvider)


def test_indexer_protocol():
    assert isinstance(_StubIndexer(), Indexer)


def test_retriever_protocol():
    assert isinstance(_StubRetriever(), Retriever)
