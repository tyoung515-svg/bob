from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.memory.exceptions import RetrievalProviderError
from core.memory.models import Chunk, ChunkRecord, IndexStats
from core.memory.parser import parse_markdown

if TYPE_CHECKING:
    from core.memory.interfaces import Embedder, FactStore, RetrievalProvider
    from core.memory.slots import SlotResolver

log = logging.getLogger(__name__)

DIMENSION_PROBE_TEXT = "bobclaw embedding dimension verification"


class MemoryIndexer:
    _dimension_probe_lock = asyncio.Lock()
    _verified_embedder_dimensions: dict[int, set[int]] = {}
    _dimension_probe_finalizers: dict[int, weakref.finalize] = {}

    def __init__(
        self,
        fact_store: FactStore,
        embedder: Embedder,
        provider: RetrievalProvider,
        store_id: str,
        slot_resolver: SlotResolver,
        parser=parse_markdown,
    ) -> None:
        self._fact_store = fact_store
        self._embedder = embedder
        self._provider = provider
        self._store_id = store_id
        self._slot_resolver = slot_resolver
        self._parser = parser

    async def reindex_all(self) -> IndexStats:
        fact_ids = await self._fact_store.all_ids()
        return await self.reindex_facts(fact_ids)

    async def reindex_facts(self, fact_ids: list[str]) -> IndexStats:
        return await self._reindex_facts_async(fact_ids)

    async def drop_facts(self, fact_ids: list[str]) -> int:
        removed = 0
        for fact_id in fact_ids:
            chunk_ids = list(
                self._provider.scroll_payload(
                    self._store_id, {"source_fact_id": fact_id}
                )
            )
            if chunk_ids:
                self._provider.delete(self._store_id, chunk_ids)
                removed += len(chunk_ids)
        return removed

    async def _reindex_facts_async(self, fact_ids: list[str]) -> IndexStats:
        stats = IndexStats()
        resolution = self._slot_resolver.get("embed_text")
        expected_dim = resolution.embedding_dimension

        for fact_id in fact_ids:
            try:
                fact = await self._fact_store.get(fact_id)
            except Exception as e:
                stats.errors.append((fact_id, str(e)))
                continue

            chunks, wikilinks = self._chunk_fact(fact)

            if not chunks:
                stats.chunks_skipped += 1
                stats.facts_processed += 1
                continue

            await self._verify_dimension_before_write(expected_dim)

            texts = [c.text for c in chunks]
            try:
                vectors = await self._embed_documents(texts)
            except Exception as e:
                stats.errors.append((fact_id, f"embed failed: {e}"))
                continue

            if expected_dim is not None and vectors:
                for v in vectors:
                    if len(v) != expected_dim:
                        raise RetrievalProviderError(
                            self._store_id,
                            f"embedding dim {len(v)} != expected {expected_dim}",
                        )

            chunk_ids = [f"chunk:{fact_id}:{c.chunk_hash}" for c in chunks]
            try:
                self._provider.delete(self._store_id, chunk_ids)
                stats.chunks_deleted += len(chunk_ids)
            except Exception as e:
                msg = str(e).lower()
                if "not found" in msg or "doesn't exist" in msg:
                    log.debug("idempotent pre-delete for %s: %s", fact_id, e)
                else:
                    raise

            items = []
            for i, chunk in enumerate(chunks):
                chunk_id = f"chunk:{fact_id}:{chunk.chunk_hash}"
                payload: dict[str, Any] = {
                    "text": chunk.text,
                    "source_fact_id": fact.fact_id,
                    "source_path": f"fact://{fact.fact_id}",
                    "heading_path": chunk.heading_path,
                    "wikilinks": wikilinks,
                }
                items.append(
                    ChunkRecord(
                        id=chunk_id,
                        vector=vectors[i],
                        payload=payload,
                    )
                )

            try:
                self._provider.index(self._store_id, items)
                stats.chunks_changed += len(items)
                stats.facts_processed += 1
            except Exception as e:
                stats.errors.append((fact_id, str(e)))

        return stats

    async def _embed_documents(self, texts: list[str]) -> list[list[float]]:
        embed_doc = getattr(self._embedder, "embed_doc", None)
        if embed_doc is not None:
            return await embed_doc(texts)
        log.warning(
            "MemoryIndexer using deprecated embed() fallback for embedder type %s",
            type(self._embedder).__name__,
        )
        return await self._embedder.embed(texts)

    async def _verify_dimension_before_write(self, expected_dim: int | None) -> None:
        """Live-probe the document embedder once before the first vector write."""
        if (
            isinstance(expected_dim, bool)
            or not isinstance(expected_dim, int)
            or expected_dim <= 0
        ):
            raise RetrievalProviderError(
                self._store_id,
                f"unverified embedding dimension: configured value {expected_dim!r} is invalid",
            )
        if self._is_dimension_verified(expected_dim):
            return
        async with self._dimension_probe_lock:
            if self._is_dimension_verified(expected_dim):
                return
            try:
                vectors = await self._embed_documents([DIMENSION_PROBE_TEXT])
            except Exception as exc:
                raise RetrievalProviderError(
                    self._store_id,
                    f"unverified embedding dimension: live probe failed: {exc}",
                ) from exc
            if not isinstance(vectors, (list, tuple)) or len(vectors) != 1:
                count = (
                    len(vectors) if isinstance(vectors, (list, tuple)) else "non-list"
                )
                raise RetrievalProviderError(
                    self._store_id,
                    "unverified embedding dimension: "
                    f"probe returned {count} vectors, expected 1",
                )
            vector = vectors[0]
            if not isinstance(vector, (list, tuple)):
                raise RetrievalProviderError(
                    self._store_id,
                    "unverified embedding dimension: probe returned a malformed vector",
                )
            actual_dim = len(vector)
            if actual_dim != expected_dim:
                raise RetrievalProviderError(
                    self._store_id,
                    "unverified embedding dimension: "
                    f"probe returned {actual_dim}, configured {expected_dim}; refusing write",
                )
            self._mark_dimension_verified(expected_dim)

    def _is_dimension_verified(self, expected_dim: int) -> bool:
        embedder_id = id(self._embedder)
        finalizer = self._dimension_probe_finalizers.get(embedder_id)
        if finalizer is None:
            return False
        state = finalizer.peek()
        if state is None or state[0] is not self._embedder:
            return False
        verified_dims = self._verified_embedder_dimensions.get(embedder_id)
        return verified_dims is not None and expected_dim in verified_dims

    def _mark_dimension_verified(self, expected_dim: int) -> None:
        embedder_id = id(self._embedder)
        finalizer = self._dimension_probe_finalizers.get(embedder_id)
        if finalizer is not None:
            state = finalizer.peek()
            if state is not None and state[0] is self._embedder:
                self._verified_embedder_dimensions.setdefault(
                    embedder_id, set()
                ).add(expected_dim)
                return
            self._evict_verified_embedder(embedder_id)
        try:
            finalizer = weakref.finalize(
                self._embedder,
                type(self)._evict_verified_embedder,
                embedder_id,
            )
        except TypeError:
            return
        self._dimension_probe_finalizers[embedder_id] = finalizer
        self._verified_embedder_dimensions[embedder_id] = {expected_dim}

    @classmethod
    def _evict_verified_embedder(cls, embedder_id: int) -> None:
        cls._verified_embedder_dimensions.pop(embedder_id, None)
        cls._dimension_probe_finalizers.pop(embedder_id, None)

    def _chunk_fact(self, fact) -> tuple[list[Chunk], list[str]]:
        body = fact.body
        if body.get("kind") == "markdown":
            return self._chunk_markdown(body, fact.fact_id)
        text = body.get("text", "")
        if not text or not text.strip():
            return [], []
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunk = Chunk(
            text=text,
            heading_path=[],
            chunk_hash=h,
            source_fact_id=fact.fact_id,
        )
        return [chunk], []

    def _chunk_markdown(self, body: dict, fact_id: str) -> tuple[list[Chunk], list[str]]:
        md_text = body.get("text", "")
        if not md_text.strip():
            return [], []
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            f.write(md_text)
            tmp_path = Path(f.name)
        try:
            parsed = self._parser(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        chunks = []
        for pc in parsed.chunks:
            h = hashlib.sha256(pc.text.encode("utf-8")).hexdigest()
            chunks.append(
                Chunk(
                    text=pc.text,
                    heading_path=pc.heading_path,
                    chunk_hash=h,
                    source_fact_id=fact_id,
                )
            )
        return chunks, list(parsed.wikilinks)
