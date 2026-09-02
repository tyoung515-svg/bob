"""T1 dynamic-adaptive MCP — the chapter index (MS#4 · Slice 2, workstream [A]).

Coarse-to-fine retrieval: job-shape archetypes are ``chapter`` nodes (tags + child tool ids).
Retrieve the top-k chapters for a job shape, then fine-retrieve tools WITHIN them — cheaper +
more precise than a flat scan at corpus scale. Slice 2 is an in-process chapter list
(deterministic token overlap); the LKS ``node_type:"chapter"`` L4 nodes + the
``_filters_to_qdrant`` allowlist entry are the cross-repo backing (follow-up). PURE.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.mcp_tools.search import ToolDescriptor, _score, _tokens


@dataclass(frozen=True)
class Chapter:
    chapter_id: str
    tags: tuple[str, ...] = ()
    tool_ids: tuple[str, ...] = ()
    description: str = ""

    def _haystack(self) -> set[str]:
        return {t.lower() for t in self.tags} | _tokens(self.description) | _tokens(self.chapter_id)


def chapter_retrieve(chapters: list[Chapter], job_shape: str, *, k: int = 2) -> list[Chapter]:
    """Top-k chapters by token overlap with the job shape (ties break by chapter_id).
    Zero-overlap chapters are excluded (never widen into an irrelevant chapter)."""
    job = _tokens(job_shape)
    scored = [(c, len(job & c._haystack()) / len(job | c._haystack()) if (job and c._haystack()) else 0.0)
              for c in chapters]
    ranked = sorted((t for t in scored if t[1] > 0.0), key=lambda t: (-t[1], t[0].chapter_id))
    return [c for c, _ in ranked[:k]]


def coarse_to_fine(
    chapters: list[Chapter],
    index: list[ToolDescriptor],
    job_shape: str,
    *,
    k_chapters: int = 2,
    limit: int = 5,
) -> list[dict]:
    """Retrieve top-k chapters, then rank the tools they contain against the job shape.
    Returns ``[{tool_id, schema}]`` (confidence suppressed, like search_tools). Falls back to an
    empty list if no chapter matches (the caller then widens to the flat search_tools)."""
    hits = chapter_retrieve(chapters, job_shape, k=k_chapters)
    allowed = {tid for c in hits for tid in c.tool_ids}
    if not allowed:
        return []
    job = _tokens(job_shape)
    candidates = [d for d in index if d.tool_id in allowed]
    ranked = sorted(
        ((d, _score(d, job)) for d in candidates),
        key=lambda t: (-t[1], t[0].tool_id),
    )
    return [{"tool_id": d.tool_id, "schema": d.schema} for d, s in ranked[:limit] if s > 0.0]


__all__ = ["Chapter", "chapter_retrieve", "coarse_to_fine"]
