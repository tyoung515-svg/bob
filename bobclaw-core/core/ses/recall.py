"""BoBClaw Core — SES eval harness (§2.8): claim-extraction RECALL.

The silent failure the verification spine (Default-FAIL) CANNOT catch (DESIGN-MS-D2 §5):
a claim the extractor MISSES never enters the citation gate and lands unverified in the
report. Default-FAIL guarantees no *extracted* claim slips unverified — but recall (did the
extractor find EVERY claim?) is the one place the spine does not protect. So this measures the
R4 extractor's recall against a LABELLED gold claim set, surfaces the missed claims explicitly,
and — per §3 MS2-R7(d) — treats a low recall as a CAPABILITY-eval regression (an improvement
target), NOT a pass.

PURE MEASUREMENT — stdlib-only, no model / network / git / clock / random / global mutable
state, at import or call. The extractor runs elsewhere; this compares its OUTPUT claim set
against the gold set by a stable key. It reads the ``.bid_key`` a ``Claim`` already computes,
so it never imports ``core.ledger`` and the ``core.ses`` package stays stdlib-only.
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping

from core.ses.types import EvalKind, EvalResult, SesError


class RecallError(SesError):
    """gold claim set empty (recall undefined)."""


DEFAULT_RECALL_THRESHOLD: float = 1.0
"""Strict: EVERY gold claim must be extracted (a miss = a silently-unverified claim, §5).

The raw recall is ALWAYS reported regardless of the threshold; the threshold only sets the
capability ``EvalResult``'s ``passed`` in :func:`recall_eval_result`.
"""


def _norm_num(v) -> str:
    """Canonical numeric string so ``'80.40' == '80.4'`` (and ``80.40 == '80.4'``). PURE, never raises.

    Tries ``float(str(v).strip())`` and returns its canonical ``str`` (drops trailing zeros); on a
    non-numeric value returns the lowered/stripped string. This keeps a dict-path key consistent
    with the numeric canonicalization a ``Claim.bid_key`` performs.
    """
    s = str(v).strip()
    try:
        return str(float(s))
    except (ValueError, TypeError):
        return s.lower()


def claim_key(obj) -> str:
    """PURE, duck-typed stable identity for a claim (gold or extracted). NEVER raises.

    * has a ``.bid_key`` attribute AND is not a Mapping (a ``Claim``) -> ``str(obj.bid_key)``
      (the canonical bid_key: ``80.40 == 80.4`` — read off the Claim, so no ``core.ledger`` import);
    * a Mapping with a truthy ``'bid_key'`` -> ``str(obj['bid_key'])``;
    * a Mapping with ``'subject'`` and ``'predicate'`` ->
      ``f"{subject}|{predicate}|{_norm_num(numeric_value)}"`` (subject/predicate lowered + stripped;
      a missing/None ``numeric_value`` -> ``''``);
    * else -> ``str(obj)``.

    A plain ``dict`` has no ``.bid_key`` *attribute* (only possibly a key), so the ``not
    isinstance(obj, Mapping)`` guard on the first branch routes every Mapping through the Mapping
    branches — a Mapping is only bid-keyed via its ``'bid_key'`` KEY, never a stray attribute.
    """
    # 1) a Claim-like (a .bid_key attribute), but explicitly NOT a Mapping.
    if hasattr(obj, "bid_key") and not isinstance(obj, Mapping):
        return str(obj.bid_key)

    # 2) a Mapping: prefer an explicit 'bid_key' key, else compose from subject/predicate/numeric.
    if isinstance(obj, Mapping):
        bid_key = obj.get("bid_key")
        if bid_key:
            return str(bid_key)
        if "subject" in obj and "predicate" in obj:
            subject = str(obj["subject"]).strip().lower()
            predicate = str(obj["predicate"]).strip().lower()
            raw_numeric = obj.get("numeric_value")
            numeric = "" if raw_numeric is None else _norm_num(raw_numeric)
            return f"{subject}|{predicate}|{numeric}"

    # 3) fallback: string conversion.
    return str(obj)


def extraction_recall(
    gold: Iterable[object],
    extracted: Iterable[object],
    *,
    key: Callable[[object], str] = claim_key,
) -> dict:
    """PURE recall of an extractor's output vs a gold claim set (by ``key``; each side de-duped to a key SET).

    **Recall ONLY** — over-extraction (precision) is NOT penalized; a MISSED gold claim is the silent
    failure (§5). Raises :class:`RecallError` iff the gold key-set is empty (recall undefined).

    Returns a breakdown (stable keys — do NOT rename)::

        {"recall": float,            # n_matched / n_gold  (0.0..1.0; the headline)
         "n_gold": int, "n_extracted": int, "n_matched": int, "n_missed": int,
         "matched_keys": list[str],  # sorted
         "missed_keys":  list[str]}  # sorted — SURFACE the silently-missed claims (never hidden)
    """
    gold_keys = {key(g) for g in gold}
    if not gold_keys:
        raise RecallError("gold claim set is empty (recall undefined)")

    extracted_keys = {key(e) for e in extracted}
    matched = gold_keys & extracted_keys
    missed = gold_keys - extracted_keys

    return {
        "recall": len(matched) / len(gold_keys),
        "n_gold": len(gold_keys),
        "n_extracted": len(extracted_keys),
        "n_matched": len(matched),
        "n_missed": len(missed),
        "matched_keys": sorted(matched),
        "missed_keys": sorted(missed),
    }


def recall_eval_result(
    breakdown: dict,
    *,
    id: str = "extraction_recall",
    threshold: float = DEFAULT_RECALL_THRESHOLD,
    kind: EvalKind = EvalKind.CAPABILITY,
) -> EvalResult:
    """Map an :func:`extraction_recall` breakdown to an :class:`EvalResult` in the CAPABILITY bucket.

    Research-quality / improvement target: ``passed = breakdown['recall'] >= threshold``. A low recall
    therefore surfaces as a capability-eval MISS (NOT a silent pass — §3 MS2-R7(d)); the CAPABILITY
    bucket is REPORTED, never gated to 100% (§2.8), so this never raises a false regression alarm.
    ``detail`` carries the recall + the missed keys so they stay visible.
    """
    passed = bool(breakdown["recall"] >= threshold)
    detail = (
        f"recall={breakdown['recall']:.3f} "
        f"matched={breakdown['n_matched']}/{breakdown['n_gold']} "
        f"missed={breakdown['missed_keys']}"
    )
    return EvalResult(id=id, kind=kind, passed=passed, detail=detail)


__all__ = [
    "RecallError",
    "DEFAULT_RECALL_THRESHOLD",
    "claim_key",
    "extraction_recall",
    "recall_eval_result",
]
