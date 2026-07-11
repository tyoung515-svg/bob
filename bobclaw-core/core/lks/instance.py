"""BoB's one local LKS instance backed by the opt-in Zvec provider.

``ingest`` is deliberately the only writer and runs in-process under the armed
family fence. The Phase 4 freshness harness is its only intended caller; this
module owns no watcher, API, UI, federation registry, or multi-instance surface.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.memory.bootstrap import _initialize_zvec_instance, _zvec_instance_dir
from core.memory.fingerprint import (
    ZVEC_MANIFEST_FINGERPRINT_FILE,
    ensure_zvec_instance_fingerprint,
    fingerprint_from_slot,
)
from core.memory.models import ChunkRecord, RankedResults
from core.memory.parser import ParsedDocument, parse_markdown
from core.memory.providers.zvec_provider import ZvecRetrievalProvider
from core.memory.write_fence import WriteFenceViolation

if TYPE_CHECKING:
    from core.memory.interfaces import Embedder
    from core.memory.slots import SlotResolver
    from core.memory.write_fence import WriteFence


class BobLKSWriteLocked(WriteFenceViolation):
    """A local write refusal with the stable shape used for HTTP 423 responses."""

    status_code = 423
    code = "memory_write_locked"

    def __init__(self, violation: WriteFenceViolation, reason: str) -> None:
        self.reason = reason
        super().__init__(violation.resource, violation.detail)


class BobLKS:
    """The one local Zvec-backed LKS instance owned by BoB."""

    def __init__(
        self,
        *,
        provider: ZvecRetrievalProvider,
        embedder: Embedder,
        slot_resolver: SlotResolver,
        write_fence: WriteFence,
        instance_root: str | Path,
        store_id: str,
        collection_prefix: str,
        parser: Callable[[Path], ParsedDocument] = parse_markdown,
    ) -> None:
        if not isinstance(provider, ZvecRetrievalProvider):
            raise TypeError("BobLKS requires the opt-in ZvecRetrievalProvider")
        if not isinstance(collection_prefix, str) or not collection_prefix.strip():
            raise ValueError("collection_prefix must be a non-empty string")
        if provider.collection_prefix != collection_prefix:
            raise ValueError("provider collection_prefix must match BobLKS")
        if write_fence.collection_prefix != collection_prefix:
            raise ValueError("write fence collection_prefix must match BobLKS")

        self._provider = provider
        self._embedder = embedder
        self._slot_resolver = slot_resolver
        self._write_fence = write_fence
        self._instance_root = Path(instance_root).expanduser().resolve()
        self._store_id = store_id
        self._collection_prefix = collection_prefix
        self._parser = parser
        self._ingest_lock = asyncio.Lock()
        self._fingerprint = fingerprint_from_slot(slot_resolver.get("embed_text"))
        self._collection = f"{collection_prefix}_{self._fingerprint.dim}"
        self._instance_dir = _zvec_instance_dir(self._instance_root, store_id)
        self._reopen_or_initialize()

    async def ingest(self, documents: Iterable[str | Path]) -> None:
        """Parse and index changed Markdown documents as the sole local writer."""
        if isinstance(documents, (str, Path)):
            raise TypeError("documents must be an iterable of document paths")
        async with self._ingest_lock:
            await self._ingest_locked(documents)

    async def _ingest_locked(self, documents: Iterable[str | Path]) -> None:
        """Serialize the one in-process writer before crossing the family fence."""
        self._assert_writable()

        for document in documents:
            path = Path(document).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"LKS document is not a file: {path}")
            source_doc_id = path.as_posix()
            document_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if self._is_unchanged(source_doc_id, document_hash):
                continue

            parsed = self._parser(path)
            chunk_ids = [
                f"chunk:{source_doc_id}:{chunk.chunk_hash}" for chunk in parsed.chunks
            ]
            vectors = await self._embed_changed_chunks(parsed)
            items = self._chunk_records(
                source_doc_id, path, parsed, chunk_ids, vectors
            )
            prior_ids = list(
                self._provider.scroll_payload(
                    self._store_id, {"source_fact_id": source_doc_id}
                )
            )

            self._assert_writable()
            if prior_ids:
                self._provider.delete(self._store_id, prior_ids)
            if items:
                self._provider.index(self._store_id, items)
            self._write_state(source_doc_id, document_hash, chunk_ids)

    async def retrieve(self, query: str, k: int) -> RankedResults:
        """Embed one query through G-3 and query the local provider."""
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            raise ValueError("k must be a positive integer")
        vectors = await self._embedder.embed_query([query])
        self._validate_vectors(vectors, 1)
        return self._provider.query_vector(self._store_id, vectors[0], k)

    def _reopen_or_initialize(self) -> None:
        manifest_dir = self._instance_dir / "manifest"
        fingerprint_path = manifest_dir / ZVEC_MANIFEST_FINGERPRINT_FILE
        existing_layout = (
            manifest_dir.is_dir()
            and (self._instance_dir / "collections").is_dir()
            and (self._instance_dir / "l0").is_dir()
            and fingerprint_path.is_file()
        )
        if existing_layout:
            ensure_zvec_instance_fingerprint(manifest_dir, self._fingerprint)
            return
        _initialize_zvec_instance(
            self._write_fence,
            self._slot_resolver,
            self._instance_root,
            self._store_id,
            self._collection_prefix,
        )

    async def _embed_changed_chunks(
        self, parsed: ParsedDocument
    ) -> list[list[float]]:
        if not parsed.chunks:
            return []
        vectors = await self._embedder.embed_doc([chunk.text for chunk in parsed.chunks])
        self._validate_vectors(vectors, len(parsed.chunks))
        return vectors

    def _chunk_records(
        self,
        source_doc_id: str,
        path: Path,
        parsed: ParsedDocument,
        chunk_ids: list[str],
        vectors: list[list[float]],
    ) -> list[ChunkRecord]:
        return [
            ChunkRecord(
                id=chunk_ids[index],
                vector=vectors[index],
                payload={
                    "text": chunk.text,
                    "source_fact_id": source_doc_id,
                    "source_doc_id": source_doc_id,
                    "source_path": path.as_posix(),
                    "heading_path": chunk.heading_path,
                    "wikilinks": parsed.wikilinks,
                    "inline_tags": parsed.inline_tags,
                },
            )
            for index, chunk in enumerate(parsed.chunks)
        ]

    def _validate_vectors(self, vectors: Any, expected_count: int) -> None:
        if not isinstance(vectors, (list, tuple)) or len(vectors) != expected_count:
            actual = len(vectors) if isinstance(vectors, (list, tuple)) else "non-list"
            raise ValueError(f"embedder returned {actual} vectors, expected {expected_count}")
        for vector in vectors:
            if not isinstance(vector, (list, tuple)):
                raise ValueError("embedder returned a malformed vector")
            if len(vector) != self._fingerprint.dim:
                raise ValueError(
                    f"embedding dimension {len(vector)} != expected {self._fingerprint.dim}"
                )

    def _state_path(self, source_doc_id: str) -> Path:
        source_key = hashlib.sha256(source_doc_id.encode("utf-8")).hexdigest()
        return self._instance_dir / "l0" / "documents" / f"{source_key}.json"

    def _is_unchanged(self, source_doc_id: str, document_hash: str) -> bool:
        state_path = self._state_path(source_doc_id)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(state, dict)
            and state.get("source_doc_id") == source_doc_id
            and state.get("document_hash") == document_hash
        )

    def _write_state(
        self, source_doc_id: str, document_hash: str, chunk_ids: list[str]
    ) -> None:
        self._assert_writable()
        state_path = self._state_path(source_doc_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source_doc_id": source_doc_id,
            "document_hash": document_hash,
            "chunk_ids": chunk_ids,
        }
        tmp_path = state_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(state_path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _assert_writable(self) -> None:
        try:
            self._write_fence.assert_writable(self._collection)
        except WriteFenceViolation as exc:
            raise BobLKSWriteLocked(exc, self._fence_reason()) from exc

    def _fence_reason(self) -> str:
        if self._write_fence.degraded:
            return self._write_fence.degraded_reason or "write_fence_violation"
        if not self._write_fence.lock_held:
            return "lock_not_held"
        return "write_fence_violation"
