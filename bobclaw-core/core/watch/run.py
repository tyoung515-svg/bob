from __future__ import annotations

from dataclasses import dataclass

from core.watch.registry import SourcesRegistry, load_sources
from core.watch import dedupe as _dedupe


@dataclass
class WatchRunResult:
    """Result of a one-shot standing-watch run."""
    registry_version: int
    source_ids: list[str]
    marked: list  # list[dedupe.MarkedItem]
    new: list     # unseen items only
    seen_count: int
    new_count: int


def run_watch(
    incoming,
    *,
    registry: SourcesRegistry | None = None,
    seen_corpus=None,
    fields=None,
    normalize: bool = True,
) -> WatchRunResult:
    """One-shot standing-watch run — callable from a scheduled profile's task.

    Steps:
        - Load the registry (default if not provided).
        - Build the set of previously-seen fingerprints from ``seen_corpus``.
        - Mark each item in ``incoming`` as seen/unseen (intra-batch dedup).
        - Extract unseen items.
        - Return a ``WatchRunResult`` with counts.
    """
    reg = registry if registry is not None else load_sources()
    seen_fps = _dedupe.fingerprints_of(
        seen_corpus or [], fields=fields, normalize=normalize
    )
    marked = _dedupe.mark_seen(incoming, seen_fps, fields=fields, normalize=normalize)
    new = _dedupe.new_items(marked)
    seen_count = sum(1 for m in marked if m.seen)
    new_count = len(new)
    return WatchRunResult(
        registry_version=reg.version,
        source_ids=reg.ids(),
        marked=marked,
        new=new,
        seen_count=seen_count,
        new_count=new_count,
    )
