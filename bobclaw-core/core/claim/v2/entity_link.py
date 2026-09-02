"""V1 claim-model v2 — subject entity-linking (MS#4 · Slice 3).

Maps subject surface variants to a canonical entity id (a hash-pinned alias table), recovering
the subject-drift recall misses the predicate-lemma slice (Slice 1) cannot. GATED on its OWN
precision test (:func:`link_precision_ok`): entity-linking that MERGES two known-distinct
subjects is a precision regression and must stay DISABLED until fixed — enable it only when it
passes on the frozen distinct set. PURE (stdlib-only).

Per-type ``EXHAUSTED_SEARCH`` generalization (SPEC §3/§5) is carried-open — the honest limit
the SPEC declined to force-close; not implemented here.
"""
from __future__ import annotations

import hashlib
from typing import Mapping

from core.claim.v2.canonical import _get, _norm_text, canonical_key

# F1-style hash-pinned alias table: subject surface form → canonical entity id.
# A curated table (a learned/embedding linker is the refinement); unknown subjects pass through
# normalized, so linking never invents a merge it wasn't told about.
ENTITY_ALIASES: dict[str, str] = {
    "d3400": "xs_power_d3400",
    "the_d3400": "xs_power_d3400",
    "the_bms": "bms",
    "amp": "sundown_amp",
    "the_amp": "sundown_amp",
}


def entity_norm_version() -> str:
    """Content address of the alias table (F1: a table edit changes the linked key space)."""
    blob = repr(sorted(ENTITY_ALIASES.items())).encode("utf-8")
    return "ev1-" + hashlib.sha256(blob).hexdigest()[:12]


def link_subject(subject) -> str:
    """Canonical entity id for a subject surface form (alias table → else normalized surface)."""
    s = _norm_text(subject)
    return ENTITY_ALIASES.get(s, s)


def linked_key(claim, *, tag_version: bool = True) -> str:
    """The Slice-3 key: entity-LINK the subject, then :func:`canonical_key` (predicate lemma +
    object-aware + numeric). PURE."""
    base = {
        "subject": link_subject(_get(claim, "subject")),
        "predicate": _get(claim, "predicate"),
        "object": _get(claim, "object"),
        "numeric_value": _get(claim, "numeric_value"),
    }
    return canonical_key(base, tag_version=tag_version)


def link_precision_ok(distinct, *, threshold: float = 0.0) -> bool:
    """The Slice-3 GATE: entity-linking must NOT collapse known-distinct subjects. True iff the
    linked key's merge_rate over ``distinct`` stays ``<= threshold`` (ship linking only if True)."""
    from core.ses.precision import merge_rate_breakdown

    return merge_rate_breakdown(distinct, key=linked_key)["merge_rate"] <= threshold


__all__ = ["ENTITY_ALIASES", "entity_norm_version", "link_subject", "linked_key",
           "link_precision_ok"]
