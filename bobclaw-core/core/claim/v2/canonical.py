"""BoBClaw Core — V1 claim-model v2 canonicalization (MS#4 · Slice 1).

The object-aware, predicate-lemmatized claim key that lifts ``bid_key`` recall WITHOUT the
false-merge the object-blind baseline suffers (SPEC §0/§5). Canonicalization is a PURE,
content-addressed function of (claim fields + a versioned, hash-pinned normalization table)
— F1: the table's hash is folded into every key, so a table change is loud (keys are never
compared across ``norm_version``), never a silent drift.

Slice 1 scope = predicate lemmatization + object-aware keys (numeric canonicalized). The
5-type taxonomy (Slice 2) and subject entity-linking (Slice 3, gated on its own precision
test) are later slices; subject/object are surface-normalized only here.
"""
from __future__ import annotations

import hashlib
from typing import Mapping

# ── F1: the hash-pinned predicate normalization table (surface form → lemma) ──
# A curated table (deterministic + content-addressed). A morphological analyzer / dictionary
# is the refinement; the suffix fallback in :func:`lemmatize_predicate` handles only simple
# unseen plurals so an out-of-table predicate still normalizes conservatively.
PREDICATE_LEMMAS: dict[str, str] = {
    "reduces": "reduce", "reducing": "reduce", "reduced": "reduce",
    "prevents": "prevent", "preventing": "prevent", "prevented": "prevent",
    "stabilizes": "stabilize", "stabilizing": "stabilize", "stabilized": "stabilize",
    "improves": "improve", "improving": "improve", "improved": "improve",
    "charges": "charge", "charging": "charge", "charged": "charge",
    "converts": "convert", "converting": "convert", "converted": "convert",
    "increases": "increase", "increasing": "increase", "increased": "increase",
    "requires": "require", "requiring": "require", "required": "require",
    "protects": "protect", "protecting": "protect", "protected": "protect",
    "weighs": "weigh", "weighing": "weigh", "weighed": "weigh",
    "joins": "join", "joining": "join", "joined": "join",
    "withstands": "withstand", "withstanding": "withstand", "withstood": "withstand",
    "causes": "cause", "causing": "cause", "caused": "cause",
}


def norm_version() -> str:
    """Content address (short sha256) of the normalization table — folded into every key so a
    table edit changes the key space (F1: loud, never a silent cross-version comparison)."""
    blob = repr(sorted(PREDICATE_LEMMAS.items())).encode("utf-8")
    return "nv1-" + hashlib.sha256(blob).hexdigest()[:12]


def _norm_text(v) -> str:
    return "" if v is None else str(v).strip().lower()


def _norm_num(v) -> str:
    """Canonical numeric string so ``5.40 == 5.4`` (matches the recall bucket's numeric norm)."""
    if v is None:
        return ""
    s = str(v).strip()
    try:
        return str(float(s))
    except (ValueError, TypeError):
        return s.lower()


def lemmatize_predicate(pred) -> str:
    """PURE predicate lemma: table lookup, else a light suffix fallback (drop -es/-s). NEVER raises."""
    w = _norm_text(pred)
    if w in PREDICATE_LEMMAS:
        return PREDICATE_LEMMAS[w]
    if w.endswith("es") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and len(w) > 3:
        return w[:-1]
    return w


def _get(claim, field):
    if isinstance(claim, Mapping):
        return claim.get(field)
    return getattr(claim, field, None)


def canonical_key(claim, *, tag_version: bool = True) -> str:
    """The v2 claim key: object-AWARE + predicate-lemmatized + numeric-canonicalized, prefixed
    with ``norm_version`` (F1). PURE. ``claim`` is a Mapping or an attr-bearing object."""
    subject = _norm_text(_get(claim, "subject"))
    predicate = lemmatize_predicate(_get(claim, "predicate"))
    obj = _norm_text(_get(claim, "object"))
    numeric = _norm_num(_get(claim, "numeric_value"))
    body = f"{subject}|{predicate}|{obj}|{numeric}"
    return f"{norm_version()}|{body}" if tag_version else body


__all__ = ["PREDICATE_LEMMAS", "norm_version", "lemmatize_predicate", "canonical_key"]
