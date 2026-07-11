from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Iterator, Literal, Protocol, runtime_checkable

from core.memory.models import (
    ChunkRecord,
    Event,
    Fact,
    FilterExpr,
    HealthStatus,
    IndexReceipt,
    IndexStats,
    RankedResults,
    RetrievedChunk,
    Section,
)


@runtime_checkable
class EventLog(Protocol):
    async def append(self, event: Event) -> str:
        ...

    async def get(self, event_id: str) -> Event:
        ...

    async def replay(self, since_event_id: str | None = None) -> AsyncIterator[Event]:
        ...


@runtime_checkable
class FactStore(Protocol):
    async def put(self, fact: Fact) -> str:
        ...

    async def get(self, fact_id: str) -> Fact:
        ...

    async def query(self, filters: dict) -> list[Fact]:
        ...

    async def delete(self, fact_id: str) -> None:
        ...

    async def all_ids(self) -> list[str]:
        ...


@runtime_checkable
class Splicer(Protocol):
    def recompute(self, affected_fact_ids: list[str]) -> list[Section]:
        ...

    def get_section(self, section_id: str) -> Section:
        ...

    def all_sections(self) -> list[Section]:
        ...


@runtime_checkable
class Renderer(Protocol):
    def render(self, sections: list[Section], output_dir: Path) -> list[Path]:
        ...


@runtime_checkable
class Embedder(Protocol):
    embedding_dimension: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Deprecated symmetric alias; delegates to embed_doc for one release."""
        ...

    async def embed_query(self, texts: list[str]) -> list[list[float]]:
        ...

    async def embed_doc(self, texts: list[str]) -> list[list[float]]:
        ...


@runtime_checkable
class RetrievalProvider(Protocol):
    provider_id: str
    locality: Literal["local", "remote"]
    capability_classes: set[str]

    def index(self, store_id: str, items: list[ChunkRecord]) -> IndexReceipt:
        ...

    def query_vector(
        self, store_id: str, vector: list[float], k: int, filters: FilterExpr | None
    ) -> RankedResults:
        ...

    def delete(self, store_id: str, item_ids: list[str]) -> None:
        ...

    def scroll_payload(
        self, store_id: str, payload_filter: dict, batch_size: int = 128
    ) -> Iterator[str]:
        ...

    def health(self) -> HealthStatus:
        ...


@runtime_checkable
class Indexer(Protocol):
    async def reindex_all(self) -> IndexStats:
        ...

    async def reindex_facts(self, fact_ids: list[str]) -> IndexStats:
        ...

    async def drop_facts(self, fact_ids: list[str]) -> int:
        ...


@runtime_checkable
class Retriever(Protocol):
    async def search(
        self,
        query: str,
        top_k: int = 3,
        threshold: float = 0.35,
        filters: dict | None = None,
        hop_budget: int = 1,
    ) -> list[RetrievedChunk]:
        ...
