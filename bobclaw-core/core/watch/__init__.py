"""BoBClaw watch pipe (substrate S1) — sources registry + item dedupe.

Additive-new package (MS8-W1). Public surface:
  * ``registry`` — load/validate a watch-sources registry (``sources_v0.yaml``) and
    build a P5-scheduler profile envelope (the recurrence binding, see W0 audit).
  * ``dedupe``   — content fingerprint + mark-previously-seen over the LKS blake3
    fingerprint (read-only reuse of ``core.memory``).
  * ``run_watch``— the one-shot standing-watch run a scheduled profile reaches.
"""
from __future__ import annotations

from core.watch.registry import (
    SourcesRegistry,
    WatchSchedule,
    WatchSource,
    default_sources_path,
    load_sources,
)
from core.watch.dedupe import (
    MarkedItem,
    fingerprint,
    fingerprints_of,
    mark_seen,
    new_items,
)
from core.watch.run import WatchRunResult, run_watch
from core.watch.digest import (
    AlertFiring,
    AlertThreshold,
    DigestArtifact,
    WatchEvent,
    build_digest,
    digest_from_run,
    evaluate_thresholds,
    persist_digest,
    to_events,
)

__all__ = [
    "SourcesRegistry",
    "WatchSchedule",
    "WatchSource",
    "default_sources_path",
    "load_sources",
    "MarkedItem",
    "fingerprint",
    "fingerprints_of",
    "mark_seen",
    "new_items",
    "WatchRunResult",
    "run_watch",
    "WatchEvent",
    "AlertThreshold",
    "AlertFiring",
    "DigestArtifact",
    "to_events",
    "evaluate_thresholds",
    "build_digest",
    "digest_from_run",
    "persist_digest",
]
