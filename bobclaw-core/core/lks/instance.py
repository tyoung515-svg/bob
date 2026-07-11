"""BoB's one local LKS instance backed by the opt-in Zvec provider.

``ingest`` is deliberately the only writer and runs in-process under the armed
family fence. It is a single-async-writer object: all ingest calls must use the
construction thread and one event loop. The Phase 4 freshness harness is its
only intended caller; this module owns no watcher, API, UI, federation registry,
or multi-instance surface.

Changed documents use INDEX-then-DELETE-STALE replacement. New content-hash
chunks are durable before stale chunk ids are removed. A crash after indexing
can leave both versions, never zero versions; because the document state advances
only after stale deletion succeeds, the next ingest deterministically heals the
duplicates.

A non-empty persisted collection requires its manifest fingerprint for every
read and write. A missing stamp makes corpus compatibility unverifiable and
fails closed; only an empty/fresh store may initialize a missing stamp. A
zero-chunk replacement is a pure deletion, so it removes stale chunks without
an embedding-dimension probe.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any

from core.memory.bootstrap import _initialize_zvec_instance, _zvec_instance_dir
from core.memory.exceptions import RetrievalProviderError
from core.memory.fingerprint import (
    FingerprintMissing,
    ZVEC_MANIFEST_FINGERPRINT_FILE,
    ensure_zvec_instance_fingerprint,
    fingerprint_from_slot,
)
from core.memory.indexer import MemoryIndexer
from core.memory.models import ChunkRecord, RankedResults
from core.memory.parser import ParsedDocument, _TOKENIZER, parse_markdown
from core.memory.providers.zvec_provider import ZvecRetrievalProvider
from core.memory.write_fence import WriteFenceViolation

if TYPE_CHECKING:
    from core.memory.interfaces import Embedder
    from core.memory.slots import SlotResolver
    from core.memory.write_fence import WriteFence


MAX_CHUNK_TOKENS = 500


@dataclass(frozen=True)
class _BoundedChunk:
    heading_path: list[str]
    text: str
    stable_hash: str
    parent_chunk_hash: str
    subchunk_index: int


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
        self._owner_thread_id = threading.get_ident()
        try:
            self._owner_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._owner_loop = None
        self._ingest_lock = asyncio.Lock()
        self._fingerprint = fingerprint_from_slot(slot_resolver.get("embed_text"))
        self._collection = f"{collection_prefix}_{self._fingerprint.dim}"
        self._instance_dir = _zvec_instance_dir(self._instance_root, store_id)
        self._compatibility_error: FingerprintMissing | None = None
        self._dimension_probe = MemoryIndexer(
            fact_store=None,
            embedder=embedder,
            provider=provider,
            store_id=store_id,
            slot_resolver=slot_resolver,
            parser=parser,
        )
        self._reopen_or_initialize()

    async def ingest(self, documents: Iterable[str | Path]) -> None:
        """Parse and index changed Markdown documents as the sole local writer."""
        if isinstance(documents, (str, Path)):
            raise TypeError("documents must be an iterable of document paths")
        self._assert_ingest_context()
        async with self._ingest_lock:
            await self._ingest_locked(documents)

    async def _ingest_locked(self, documents: Iterable[str | Path]) -> None:
        """Serialize the one in-process writer before crossing the family fence."""
        self._assert_writable()
        self._assert_corpus_compatible()

        for document in documents:
            path = Path(document).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"LKS document is not a file: {path}")
            source_doc_id = path.as_posix()
            document_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if self._is_unchanged(source_doc_id, document_hash):
                continue

            parsed = self._parser(path)
            chunks = self._bounded_chunks(parsed)
            chunk_ids = [
                f"chunk:{source_doc_id}:{chunk.stable_hash}" for chunk in chunks
            ]
            if chunks:
                await self._dimension_probe._verify_dimension_before_write(
                    self._fingerprint.dim
                )
                vectors = await self._embed_changed_chunks(chunks)
            else:
                vectors = []
            items = self._chunk_records(
                source_doc_id, path, parsed, chunks, chunk_ids, vectors
            )
            prior_ids = list(
                self._provider.scroll_payload(
                    self._store_id, {"source_fact_id": source_doc_id}
                )
            )
            new_ids = set(chunk_ids)
            stale_ids = [chunk_id for chunk_id in prior_ids if chunk_id not in new_ids]

            self._assert_writable()
            if items:
                self._provider.index(self._store_id, items)
            if stale_ids:
                self._provider.delete(self._store_id, stale_ids)
            self._write_state(source_doc_id, document_hash, chunk_ids)

    async def retrieve(self, query: str, k: int) -> RankedResults:
        """Embed one query through G-3 and query the local provider."""
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            raise ValueError("k must be a positive integer")
        self._assert_corpus_compatible()
        vectors = await self._embedder.embed_query([query])
        self._validate_vectors(vectors, 1)
        return self._provider.query_vector(self._store_id, vectors[0], k)

    def _reopen_or_initialize(self) -> None:
        manifest_dir = self._instance_dir / "manifest"
        fingerprint_path = manifest_dir / ZVEC_MANIFEST_FINGERPRINT_FILE
        layout_dirs_exist = (
            manifest_dir.is_dir()
            and (self._instance_dir / "collections").is_dir()
            and (self._instance_dir / "l0").is_dir()
        )
        if fingerprint_path.is_file():
            ensure_zvec_instance_fingerprint(
                manifest_dir,
                self._fingerprint,
                assert_writable=lambda: self._write_fence.assert_writable(
                    self._collection
                ),
            )
            if layout_dirs_exist:
                return
        else:
            try:
                has_documents = self._provider._has_documents(self._store_id)
            except RetrievalProviderError as exc:
                self._compatibility_error = FingerprintMissing(
                    "zvec corpus compatibility is unverifiable because the "
                    "fingerprint stamp is missing and persisted collections "
                    f"could not be inspected: {fingerprint_path}: {exc}"
                )
                return
            if has_documents:
                self._compatibility_error = FingerprintMissing(
                    "zvec corpus compatibility is unverifiable because a "
                    "non-empty collection exists but its fingerprint stamp is "
                    f"missing: {fingerprint_path}"
                )
                return
        if self._write_fence.degraded or not self._write_fence.lock_held:
            return
        try:
            _initialize_zvec_instance(
                self._write_fence,
                self._slot_resolver,
                self._instance_root,
                self._store_id,
                self._collection_prefix,
            )
        except WriteFenceViolation:
            # Construction remains read-capable if the lock is lost between the
            # state check and L2 initialization. Ingest maps the live state to 423.
            return

    async def _embed_changed_chunks(
        self, chunks: list[_BoundedChunk]
    ) -> list[list[float]]:
        if not chunks:
            return []
        vectors = await self._embedder.embed_doc([chunk.text for chunk in chunks])
        self._validate_vectors(vectors, len(chunks))
        return vectors

    def _chunk_records(
        self,
        source_doc_id: str,
        path: Path,
        parsed: ParsedDocument,
        chunks: list[_BoundedChunk],
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
                    "parent_chunk_hash": chunk.parent_chunk_hash,
                    "subchunk_index": chunk.subchunk_index,
                    "wikilinks": parsed.wikilinks,
                    "inline_tags": parsed.inline_tags,
                },
            )
            for index, chunk in enumerate(chunks)
        ]

    def _bounded_chunks(self, parsed: ParsedDocument) -> list[_BoundedChunk]:
        bounded: list[_BoundedChunk] = []
        for chunk in parsed.chunks:
            token_ids = _TOKENIZER.encode(chunk.text)
            if len(token_ids) <= MAX_CHUNK_TOKENS:
                bounded.append(
                    _BoundedChunk(
                        heading_path=list(chunk.heading_path),
                        text=chunk.text,
                        stable_hash=chunk.chunk_hash,
                        parent_chunk_hash=chunk.chunk_hash,
                        subchunk_index=0,
                    )
                )
                continue

            for index, part_text in enumerate(self._split_chunk_text(token_ids)):
                part_hash = hashlib.sha256(part_text.encode("utf-8")).hexdigest()
                stable_hash = hashlib.sha256(
                    f"{chunk.chunk_hash}:{index}:{part_hash}".encode("utf-8")
                ).hexdigest()
                bounded.append(
                    _BoundedChunk(
                        heading_path=list(chunk.heading_path),
                        text=part_text,
                        stable_hash=stable_hash,
                        parent_chunk_hash=chunk.chunk_hash,
                        subchunk_index=index,
                    )
                )
        return bounded

    @staticmethod
    def _split_chunk_text(token_ids: list[int]) -> list[str]:
        parts: list[str] = []
        start = 0
        while start < len(token_ids):
            end = min(start + MAX_CHUNK_TOKENS, len(token_ids))
            part_text = None
            while end > start:
                raw = b"".join(
                    _TOKENIZER.decode_single_token_bytes(token_id)
                    for token_id in token_ids[start:end]
                )
                try:
                    candidate = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    end -= 1
                    continue
                if len(_TOKENIZER.encode(candidate)) <= MAX_CHUNK_TOKENS:
                    part_text = candidate
                    break
                end -= 1
            if part_text is None:
                raise ValueError("could not split parser chunk on a UTF-8 token boundary")
            parts.append(part_text)
            start = end
        return parts

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

    def _assert_ingest_context(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError(
                "BobLKS.ingest must run on its construction thread and event loop"
            )
        current_loop = asyncio.get_running_loop()
        if self._owner_loop is None:
            self._owner_loop = current_loop
        elif current_loop is not self._owner_loop:
            raise RuntimeError(
                "BobLKS.ingest must run on the same event loop for its lifetime"
            )

    def _assert_corpus_compatible(self) -> None:
        if self._compatibility_error is not None:
            raise self._compatibility_error

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
