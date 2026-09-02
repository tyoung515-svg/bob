"""T1 dynamic-adaptive MCP — the thin meta-surface ``search_tools`` (MS#4 · Slice 1, [C]).

A data-driven successor to the ``_select_face`` regex dispatch: ``search_tools(job_shape)`` ranks
a flat tool index by relevance and returns tool ids + schemas. The internal relevance score
(``confidence``) is SUPPRESSED from the output (used only for fallback / widen decisions, SPEC
§2 [C]). Slice 1 is a FLAT in-process index (deterministic token overlap); the LKS vector index
+ chapter coarse-router are Slice 2. PURE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


@dataclass(frozen=True)
class ToolDescriptor:
    tool_id: str
    description: str = ""
    tags: tuple[str, ...] = ()
    schema: dict = field(default_factory=dict)
    schema_version_hash: str = ""

    def _haystack(self) -> set[str]:
        return _tokens(self.description) | {t.lower() for t in self.tags} | _tokens(self.tool_id)


def _score(desc: ToolDescriptor, job_tokens: set[str]) -> float:
    """Deterministic relevance: Jaccard-ish overlap of the job tokens with the tool's haystack."""
    hay = desc._haystack()
    if not hay or not job_tokens:
        return 0.0
    return len(job_tokens & hay) / len(job_tokens | hay)


def search_tools(
    index: list[ToolDescriptor],
    job_shape: str,
    *,
    context: str = "",
    limit: int = 5,
) -> list[dict]:
    """Rank ``index`` by relevance to ``job_shape`` (+ optional ``context``) and return the top
    ``limit`` as ``[{tool_id, schema}]`` — the confidence is SUPPRESSED from the output. Ties break
    by tool_id for determinism. A zero-overlap tool is never returned."""
    job_tokens = _tokens(job_shape) | _tokens(context)
    scored = [(desc, _score(desc, job_tokens)) for desc in index]
    ranked = sorted(
        (t for t in scored if t[1] > 0.0),
        key=lambda t: (-t[1], t[0].tool_id),
    )
    return [{"tool_id": d.tool_id, "schema": d.schema} for d, _ in ranked[:limit]]


def rank_with_confidence(
    index: list[ToolDescriptor], job_shape: str, *, context: str = "", limit: int = 5
) -> list[tuple[str, float]]:
    """Same ranking but EXPOSING the internal confidence — for the fallback / proceed-vs-widen
    decision (never surfaced to the tool caller). ``[(tool_id, confidence)]``, best first."""
    job_tokens = _tokens(job_shape) | _tokens(context)
    scored = [(d.tool_id, _score(d, job_tokens)) for d in index]
    return sorted((t for t in scored if t[1] > 0.0), key=lambda t: (-t[1], t[0]))[:limit]


__all__ = ["ToolDescriptor", "search_tools", "rank_with_confidence"]
