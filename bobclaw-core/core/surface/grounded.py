"""P1 notebook-grounded surface — the GroundedRetrieval handle (MS#4 · Slice 3).

Redirects the seam: a single handle, bound to ONE notebook at construction, is the only way in
and out for a grounded notebook. Writes (sources, notes) go to the SQLite system-of-record;
reads go through it (text) and through the F-1 NotebookBoundProvider (vector) — both scoped to
the bound notebook_id by construction, so the grounded MVP ("answers only from these sources")
holds with no synthesis and no council. ``crystallize`` freezes a content digest into the
manifest. PURE orchestration over the injected store + provider (+ an injected digest clock).
"""
from __future__ import annotations

import hashlib
from typing import Optional

from core.surface.provider import NotebookBoundProvider
from core.surface.store import SurfaceStore, SurfaceStoreError


class GroundedRetrieval:
    """A read/write handle bound to a single notebook (the grounded-only spine)."""

    def __init__(self, store: SurfaceStore, provider: NotebookBoundProvider, notebook_id: str):
        if provider.notebook_id != notebook_id:
            raise SurfaceStoreError(
                f"provider bound to {provider.notebook_id!r} != handle notebook {notebook_id!r}"
            )
        if store.get_notebook(notebook_id) is None:
            raise SurfaceStoreError(f"unknown notebook {notebook_id!r}")
        self._store = store
        self._provider = provider
        self._notebook_id = notebook_id

    @property
    def notebook_id(self) -> str:
        return self._notebook_id

    # ── writes (system-of-record) ────────────────────────────────────────────
    def add_source(self, source_id: str, uri: str, kind: str) -> None:
        self._store.add_source(self._notebook_id, source_id, uri, kind)

    def add_note(self, note_id: str, body: str) -> None:
        self._store.add_note(self._notebook_id, note_id, body)

    # ── reads (both notebook-scoped by construction) ─────────────────────────
    def text_search(self, query: str, *, k: int = 10) -> list[dict]:
        return self._store.search_notes(self._notebook_id, query, k=k)

    async def vector_search(self, query_vector, *, k: int = 10, extra_filters: Optional[dict] = None):
        # delegates to the F-1 bound provider — the notebook_id filter cannot be dropped
        return await self._provider.vector_search(query_vector, k=k, extra_filters=extra_filters)

    def get_centroid(self) -> Optional[bytes]:
        m = self._store.get_manifest(self._notebook_id)
        return m["centroid"] if m else None

    # ── crystallize ──────────────────────────────────────────────────────────
    def crystallize(self, *, last_ingest_ts: Optional[float] = None) -> str:
        """Freeze a content digest of the notebook (sources + notes) into the manifest and return
        it. Deterministic over the current corpus content."""
        srcs = self._store.source_count(self._notebook_id)
        notes = self._store.search_notes(self._notebook_id, "", k=10_000)
        material = f"{self._notebook_id}|sources={srcs}|" + "|".join(
            sorted(n["note_id"] + ":" + n["body"] for n in notes)
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        self._store.upsert_manifest(
            self._notebook_id, crystallized_digest=digest, last_ingest_ts=last_ingest_ts
        )
        return digest


__all__ = ["GroundedRetrieval"]
