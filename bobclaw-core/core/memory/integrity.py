"""Stored-side vector integrity probe (P4) — catch the zero-vector corruption nothing else watches.

The 2026-06-20 incident: a half-loaded embedder returns **all-zero vectors with HTTP 200**. They store
with valid content-hashes, so the incremental indexer skips them forever (hash match -> no re-embed) and
semantic retrieval silently dies — every query returns arbitrary points at score ~0. It bit all three LKS
collections during the 2026-06-02 reorg and went unnoticed for **18 days**.

``embed.py``'s guard closed the WRITE side (new corruption can't be stored). Nothing watched the STORED
side — so the 2026-06-02 corruption was still sitting in ``:6333`` when P4 tripped over it on 2026-07-16,
**26 days later**, and again only because someone happened to look. Twice found by luck is not a control.
This module is the missing read-side scan.

Implements the detection LKS's own ``CLAUDE.md`` prescribes: *"scroll a collection with_vector=True and
count points with L2-norm < 1e-6"*. READ-ONLY by construction — it scrolls, it never writes.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any, Optional

# LKS CLAUDE.md's prescribed threshold: a healthy embedding of non-empty text is never ~zero.
ZERO_NORM_EPSILON = 1e-6


class IntegrityProbeError(RuntimeError):
    """The probe could not run (collection unreadable / client failure). Distinct from 'found corruption'."""
    pass


@dataclasses.dataclass(frozen=True)
class VectorIntegrityReport:
    """Outcome of a stored-side zero-vector scan over one collection.

    ``exhaustive`` records whether the scan reached the END of the collection or stopped at a cap. A
    spot-check that happens to be clean says nothing about the points it never looked at — see ``healthy``.
    """
    collection: str
    sampled: int
    zero: int
    live: int
    exhaustive: bool = False

    @property
    def healthy(self) -> bool:
        """True iff the sample contains at least one point and NO zero vectors.

        An EMPTY sample is NOT healthy: a collection we could not see any vectors in is unverified, not
        proven-good. Fail-closed — the whole point is that this corruption reads as success everywhere else.
        """
        return self.sampled > 0 and self.zero == 0

    @property
    def zero_fraction(self) -> float:
        """Fraction of the sample that was degenerate (0.0 when nothing was sampled)."""
        return (self.zero / self.sampled) if self.sampled else 0.0

    def summary(self) -> str:
        """One-line human summary (for status output / assertion messages)."""
        if self.sampled == 0:
            return f"{self.collection}: NO VECTORS SAMPLED — unverified (is the collection empty?)"
        verdict = "OK" if self.healthy else f"CORRUPT ({self.zero}/{self.sampled} zero)"
        scope = "full scan" if self.exhaustive else "SPOT-CHECK (capped — unscanned points may be corrupt)"
        return (f"{self.collection}: {verdict} — sampled={self.sampled} zero={self.zero} "
                f"live={self.live} [{scope}]")


def _vectors_of(raw: Any) -> list:
    """Return ALL of a point's vectors as a list of vectors; [] when absent.

    Qdrant returns either a bare list (default vector) or a {name: vector} dict (named vectors). Named
    collections yield EVERY named vector, not an arbitrary one: picking `next(iter(...))` would let dict
    order decide the verdict, so {"dense": healthy, "sparse": null} could report healthy on a half-corrupt
    point. Any degenerate vector on a point condemns that point.
    """
    if isinstance(raw, dict):
        return [v for v in raw.values() if v is not None]
    if raw is None:
        return []
    return [raw]


def is_zero_vector(vec, *, epsilon: float = ZERO_NORM_EPSILON) -> bool:
    """True iff the vector's L2 norm is below *epsilon* (the degenerate/null-vector signature).

    A non-numeric element (a sparse/multivector shape this probe does not model) raises IntegrityProbeError
    rather than a raw TypeError — an unmodellable vector is 'unverified', and unverified must never read as
    healthy.
    """
    if not vec:
        return True
    try:
        return math.sqrt(sum(float(x) * float(x) for x in vec)) < epsilon
    except (TypeError, ValueError) as exc:
        raise IntegrityProbeError(
            f"cannot compute a norm for vector of type {type(vec).__name__} "
            f"(unsupported vector shape — sparse/multivector?): {exc}"
        ) from exc


def probe_collection(
    client: Any,
    collection: str,
    *,
    sample: Optional[int] = 200,
    page: int = 256,
    epsilon: float = ZERO_NORM_EPSILON,
) -> VectorIntegrityReport:
    """Scroll *collection* and count degenerate vectors. READ-ONLY.

    ``sample=None`` scans the collection to EXHAUSTION (paged); an int caps the scan at that many points.
    **A capped scan is a spot-check, not a proof** — Qdrant's scroll walks points in id order, so a cap
    only ever sees the first N ids. A re-index that corrupted a late batch would sit entirely outside a
    capped window and report clean. The 2026-06-20 corruption was 100% of the store, so any window caught
    it; the next one may not be. For the post-reindex gate LKS CLAUDE.md demands ("verify zero-count == 0"),
    use ``assert_collection_healthy``, which scans exhaustively by default.

    Raises IntegrityProbeError if the scroll fails — an unreadable collection is an unknown, and must never
    be reported as healthy.
    """
    if not isinstance(collection, str) or not collection.strip():
        raise IntegrityProbeError(f"invalid collection name {collection!r}")
    if sample is not None and (not isinstance(sample, int) or isinstance(sample, bool) or sample <= 0):
        raise IntegrityProbeError(f"sample must be a positive int or None, got {sample!r}")
    if not isinstance(page, int) or isinstance(page, bool) or page <= 0:
        raise IntegrityProbeError(f"page must be a positive int, got {page!r}")

    zero = live = 0
    offset = None
    exhaustive = True
    while True:
        remaining = (sample - (zero + live)) if sample is not None else page
        if sample is not None and remaining <= 0:
            # Hit the cap with points still unscanned ⇒ this is a spot-check, not a full scan.
            exhaustive = False
            break
        limit = min(page, remaining) if sample is not None else page
        try:
            points, offset = client.scroll(
                collection, limit=limit, with_vectors=True, offset=offset
            )
        except Exception as exc:  # noqa: BLE001 — any read failure is 'unverified', not 'healthy'
            raise IntegrityProbeError(f"could not scroll {collection!r}: {exc}") from exc

        for p in points:
            vecs = _vectors_of(getattr(p, "vector", None))
            if not vecs:
                continue  # no vector returned for this point — not counted either way
            # Any degenerate vector on a point condemns the point (named-vector collections).
            if any(is_zero_vector(v, epsilon=epsilon) for v in vecs):
                zero += 1
            else:
                live += 1

        if offset is None or not points:
            break  # reached the end of the collection

    return VectorIntegrityReport(
        collection=collection, sampled=zero + live, zero=zero, live=live, exhaustive=exhaustive
    )


def assert_collection_healthy(
    client: Any,
    collection: str,
    *,
    sample: Optional[int] = None,
    epsilon: float = ZERO_NORM_EPSILON,
) -> VectorIntegrityReport:
    """probe_collection + raise IntegrityProbeError unless the collection is clean.

    Scans to EXHAUSTION by default (``sample=None``) — this is the post-reindex gate LKS CLAUDE.md demands
    (*"Always verify zero-count == 0 after any reindex"*), and "zero-count == 0" over a capped window is
    not that claim. Pass an int only when you knowingly want a spot-check.
    """
    report = probe_collection(client, collection, sample=sample, epsilon=epsilon)
    if not report.healthy:
        raise IntegrityProbeError(report.summary())
    return report
