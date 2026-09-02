"""P1 notebook-grounded surface — the bound-handle isolation spine (MS#4 · Slice 1, F-1).

A :class:`NotebookBoundProvider` binds a ``notebook_id`` at CONSTRUCTION and bakes it into the
filter of EVERY retrieval — in the method body, NEVER as a caller parameter (F-1(b)). It exposes
no unfiltered path, so NotebookLM's "answers only from these sources" holds by CONSTRUCTION, not
by a query filter a caller could forget or override. It wraps the read-only LKS ``ReadAdapter``
(injected), and is the SOLE module in ``core/surface/`` permitted to touch a Qdrant client
constructor (F-1(a), guarded by the import-boundary lint). PURE (the adapter is injected).
"""
from __future__ import annotations

from typing import Optional

_NOTEBOOK_KEY = "notebook_id"


class NotebookBoundProvider:
    """Read-only retrieval scoped, by construction, to a single notebook partition."""

    def __init__(self, adapter, *, notebook_id: str, instance_name: str):
        if not isinstance(notebook_id, str) or not notebook_id.strip():
            raise ValueError("notebook_id must be a non-empty string")
        if not isinstance(instance_name, str) or not instance_name.strip():
            raise ValueError("instance_name must be a non-empty string")
        self._adapter = adapter
        self._notebook_id = notebook_id
        self._instance = instance_name

    @property
    def notebook_id(self) -> str:
        return self._notebook_id

    def _bound_filter(self, extra: Optional[dict] = None) -> dict:
        """The must-filter with ``notebook_id`` forced from ``self._notebook_id`` LAST — so a
        caller's ``extra`` can narrow the query but can NEVER drop or override the partition key."""
        f = dict(extra or {})
        f[_NOTEBOOK_KEY] = self._notebook_id      # set last → wins over any caller-supplied value
        return f

    async def vector_search(
        self,
        query_vector,
        *,
        k: int = 10,
        extra_filters: Optional[dict] = None,
    ):
        """Vector search ALWAYS scoped to this notebook. ``extra_filters`` may narrow further; it
        can never widen past the bound ``notebook_id`` (F-1(b))."""
        return await self._adapter.search(
            self._instance,
            query_vector=query_vector,
            k=k,
            filters=self._bound_filter(extra_filters),
        )


__all__ = ["NotebookBoundProvider"]
