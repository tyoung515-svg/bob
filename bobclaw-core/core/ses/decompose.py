"""BoBClaw Core — SES: missed-keys DECOMPOSITION (V1 · Slice 0 analysis).

Answers the MS#2-retrospective question the V1 SPEC flags as a "needs Travis" input: of the
recall misses under a key function, what FRACTION is **predicate-drift** (surface morphology
— recoverable by Slice-1 predicate lemmatization) vs **subject-drift** (recoverable by
Slice-3 entity linking) vs **unexplained** (no near-match in the extractor output)? This
sequences Slice 1 vs Slice 3 empirically instead of by guess.

A miss is predicate-drift if some extracted claim matches the gold on every axis EXCEPT the
predicate (so canonicalizing the predicate would recover it); subject-drift likewise on the
subject. Predicate is checked first. PURE — stdlib-only.
"""
from __future__ import annotations

from typing import Callable, Iterable

from core.ses.recall import claim_key

_AXES = ("subject", "predicate", "object", "numeric")


def _norm(v) -> str:
    return "" if v is None else str(v).strip().lower()


def _fields(c: dict) -> dict:
    return {
        "subject": _norm(c.get("subject")),
        "predicate": _norm(c.get("predicate")),
        "object": _norm(c.get("object")),
        "numeric": _norm(c.get("numeric_value")),
    }


def _axis_sig(c: dict, drop: str) -> tuple:
    """The claim's field signature with one axis dropped (a near-match key on the other axes)."""
    f = _fields(c)
    f.pop(drop)
    return tuple(sorted(f.items()))


def missed_keys_decomposition(
    gold: Iterable[dict],
    extracted: Iterable[dict],
    *,
    key: Callable[[object], str] = claim_key,
) -> dict:
    """Categorize the recall misses (gold not matched under ``key``) by likely cause.

    Returns (stable keys)::

        {"n_gold": int, "n_missed": int,
         "counts": {"predicate": int, "subject": int, "unexplained": int},
         "pct":    {"predicate": float, "subject": float, "unexplained": float},
         "detail": [{"id": <gold id>, "category": <cause>}, ...]}
    """
    gold, extracted = list(gold), list(extracted)
    ext_keys = {key(e) for e in extracted}
    ext_by_axis = {
        axis: {_axis_sig(e, axis) for e in extracted} for axis in ("predicate", "subject")
    }
    counts = {"predicate": 0, "subject": 0, "unexplained": 0}
    detail = []
    missed = [g for g in gold if key(g) not in ext_keys]
    for g in missed:
        if _axis_sig(g, "predicate") in ext_by_axis["predicate"]:
            cat = "predicate"
        elif _axis_sig(g, "subject") in ext_by_axis["subject"]:
            cat = "subject"
        else:
            cat = "unexplained"
        counts[cat] += 1
        detail.append({"id": g.get("id"), "category": cat})
    n = len(missed)
    pct = {k: (v / n if n else 0.0) for k, v in counts.items()}
    return {"n_gold": len(gold), "n_missed": n, "counts": counts, "pct": pct, "detail": detail}


__all__ = ["missed_keys_decomposition"]
