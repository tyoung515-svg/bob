"""BoBClaw Core — SES eval harness: claim-model PRECISION / merge-rate (V1 · F3).

The dual of recall (``core/ses/recall.py``). Lifting ``bid_key`` recall ALONE is a
Default-FAIL regression in disguise: an over-merging canonicalizer shows recall = 1.0 while
silently dedup'ing two DISTINCT claims → ERG sets ``EXHAUSTED_SEARCH`` on the second without
ever trying it (a false-pass at the gate layer). So this measures, on a set of KNOWN
pairwise-DISTINCT claims, how many the canonical key wrongly COLLAPSES. Ship recall +
precision together or not at all (V1 SPEC §0).

PURE — stdlib-only, no model/network/clock/random/global state. Reads the ``.bid_key`` a
Claim exposes via the shared :func:`core.ses.recall.claim_key` (never imports ``core.ledger``).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable

from core.ses.recall import claim_key
from core.ses.types import EvalKind, EvalResult, SesError


class PrecisionError(SesError):
    """distinct claim set empty (merge-rate undefined)."""


DEFAULT_MERGE_RATE_THRESHOLD: float = 0.0
"""Zero false merges (V1 §5 F3): distinct claims MUST map to distinct keys. This is a
REGRESSION (protection) gate — a merge_rate ABOVE it is a real precision regression, unlike
recall (a CAPABILITY / improvement target). Ship recall + precision together (§0)."""


def _ident(obj, i: int) -> str:
    """A stable human id for an item in a merged group (its ``id`` field, else the index)."""
    if isinstance(obj, dict) and obj.get("id"):
        return str(obj["id"])
    return f"#{i}"


def merge_rate_breakdown(
    distinct: Iterable[object], *, key: Callable[[object], str] = claim_key
) -> dict:
    """PURE merge-rate over a KNOWN pairwise-distinct claim set (each MUST get its own key).

    ``merge_rate = 1 - n_unique_keys / n_distinct``: 0.0 = every distinct claim kept a distinct
    identity (perfect precision); > 0 = the key collapsed distinct claims (false-merge). Raises
    :class:`PrecisionError` iff the distinct set is empty. Returns (stable keys — do NOT rename)::

        {"merge_rate": float, "precision": float,   # precision = n_unique_keys / n_distinct
         "n_distinct": int, "n_unique_keys": int, "n_merged": int,
         "merged_groups": list[list[str]]}          # sorted groups (>1 distinct id sharing a key)
    """
    items = list(distinct)
    n = len(items)
    if n == 0:
        raise PrecisionError("distinct claim set is empty (merge-rate undefined)")
    by_key: dict[str, list[str]] = defaultdict(list)
    for i, obj in enumerate(items):
        by_key[key(obj)].append(_ident(obj, i))
    n_unique = len(by_key)
    merged_groups = sorted(sorted(ids) for ids in by_key.values() if len(ids) > 1)
    return {
        "merge_rate": 1.0 - (n_unique / n),
        "precision": n_unique / n,
        "n_distinct": n,
        "n_unique_keys": n_unique,
        "n_merged": n - n_unique,
        "merged_groups": merged_groups,
    }


def precision_eval_result(
    breakdown: dict,
    *,
    id: str = "claim_merge_rate",
    threshold: float = DEFAULT_MERGE_RATE_THRESHOLD,
    kind: EvalKind = EvalKind.REGRESSION,
) -> EvalResult:
    """Map a :func:`merge_rate_breakdown` to an :class:`EvalResult` in the REGRESSION bucket.

    Protection target: ``passed = merge_rate <= threshold``. A false-merge is a precision
    regression (§0), so — unlike recall — this IS gated. ``detail`` carries the merged groups.
    """
    passed = bool(breakdown["merge_rate"] <= threshold)
    detail = (
        f"merge_rate={breakdown['merge_rate']:.3f} "
        f"precision={breakdown['precision']:.3f} "
        f"merged={breakdown['merged_groups']}"
    )
    return EvalResult(id=id, kind=kind, passed=passed, detail=detail)


__all__ = [
    "PrecisionError",
    "DEFAULT_MERGE_RATE_THRESHOLD",
    "merge_rate_breakdown",
    "precision_eval_result",
]
