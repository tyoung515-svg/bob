"""V1 claim-model v2 — the 5-type taxonomy + deterministic classification (MS#4 · Slice 2, F4).

Rule-anchored, DETERMINISTIC dispatch (no model): a numeric value → NUMERIC; a closed causal
lemma → CAUSAL; an is-a predicate → DEFINITIONAL; a temporal relation → TEMPORAL; else the
bare entity-presence → EXISTENTIAL. A claim may carry MULTIPLE types (F4) and ALL must pass
(``checks.py``). EXISTENTIAL is the loosest and is only the FALLBACK when nothing more specific
applies — never chosen over a stricter applicable type. PURE.
"""
from __future__ import annotations

from enum import Enum

from core.claim.v2.canonical import _get, lemmatize_predicate


class ClaimType(str, Enum):
    NUMERIC = "numeric"
    CAUSAL = "causal"
    TEMPORAL = "temporal"
    DEFINITIONAL = "definitional"
    EXISTENTIAL = "existential"


# Closed, deterministic dispatch anchors (lemmatized predicates).
CAUSAL_LEMMAS = frozenset({
    "cause", "prevent", "reduce", "increase", "improve", "stabilize", "protect",
    "enable", "trigger", "inhibit", "charge", "convert", "withstand", "require", "join",
})
DEFINITIONAL_LEMMAS = frozenset({"be", "is", "define", "denote", "mean", "equal"})
TEMPORAL_LEMMAS = frozenset({"precede", "follow", "occur", "happen", "predate", "postdate"})


def classify_claim(claim) -> list["ClaimType"]:
    """Deterministic multi-type classification (F4). Returns ≥1 type; EXISTENTIAL only as the
    fallback when no stricter type applies (never the loosest-by-default)."""
    types: list[ClaimType] = []
    if _get(claim, "numeric_value") is not None:
        types.append(ClaimType.NUMERIC)
    lemma = lemmatize_predicate(_get(claim, "predicate"))
    if lemma in CAUSAL_LEMMAS:
        types.append(ClaimType.CAUSAL)
    if lemma in DEFINITIONAL_LEMMAS:
        types.append(ClaimType.DEFINITIONAL)
    if lemma in TEMPORAL_LEMMAS:
        types.append(ClaimType.TEMPORAL)
    if not types:
        types.append(ClaimType.EXISTENTIAL)
    return types


__all__ = [
    "ClaimType",
    "CAUSAL_LEMMAS",
    "DEFINITIONAL_LEMMAS",
    "TEMPORAL_LEMMAS",
    "classify_claim",
]
