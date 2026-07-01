from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.memory.exceptions import RetrievalProviderError
from core.memory.models import Chunk, ChunkRecord, IndexStats
from core.memory.parser import parse_markdown

if TYPE_CHECKING:
    from core.memory.interfaces import Embedder, FactStore, RetrievalProvider
    from core.memory.slots import SlotResolver

log = logging.getLogger(__name__)


class MemoryIndexer:
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

            texts = [c.text for c in chunks]
            try:
                vectors = await self._embedder.embed(texts)
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
