"""V1 claim-model v2 — provenance fields + the F6 guard (MS#4 · Slice 2).

Every v2 claim carries ``claim_schema_version`` + ``claim_path`` (``numeric-v2`` /
``causal-v2`` / …). The ERG REFUSES a v2 claim without them (F6) — a claim whose identity
discipline is unstated must never enter the retry-gate, else EXHAUSTED_SEARCH could be set on
a claim that was never properly typed. Legacy numeric-v1 entries (no v2 marker) pass through
untouched, so the numeric path stays byte-identical (additive invariant). PURE.
"""
from __future__ import annotations

from typing import Mapping

from core.claim.v2.taxonomy import classify_claim

CLAIM_SCHEMA_VERSION = "claim-v2"


class ProvenanceError(ValueError):
    """A v2 claim is missing ``claim_schema_version`` / ``claim_path`` (F6)."""


def _get(claim, k):
    return claim.get(k) if isinstance(claim, Mapping) else getattr(claim, k, None)


def claim_path_for(claim) -> str:
    """The claim_path tag: ``<primary-type>-v2`` (e.g. ``numeric-v2``, ``causal-v2``)."""
    return f"{classify_claim(claim)[0].value}-v2"


def is_v2_claim(claim) -> bool:
    """True iff the claim OPTS INTO the v2 schema (declares a version or an explicit type).
    A legacy numeric-v1 claim carries none of these and is left alone."""
    return bool(
        _get(claim, "claim_schema_version")
        or _get(claim, "claim_type")
        or _get(claim, "claim_types")
    )


def has_provenance(claim) -> bool:
    return bool(_get(claim, "claim_schema_version")) and bool(_get(claim, "claim_path"))


def require_provenance(claim) -> None:
    """F6 guard: raise :class:`ProvenanceError` iff a v2 claim lacks the provenance fields.
    A non-v2 (legacy) claim is allowed through unchanged."""
    if is_v2_claim(claim) and not has_provenance(claim):
        raise ProvenanceError(
            "v2 claim missing claim_schema_version/claim_path (F6): stamp "
            "CLAIM_SCHEMA_VERSION + claim_path_for(claim) before ERG admission"
        )


def stamp_provenance(claim: dict) -> dict:
    """Return a COPY stamped with claim_schema_version + claim_path + claim_types (idempotent)."""
    out = dict(claim)
    out.setdefault("claim_schema_version", CLAIM_SCHEMA_VERSION)
    out.setdefault("claim_path", claim_path_for(claim))
    out.setdefault("claim_types", [t.value for t in classify_claim(claim)])
    return out


__all__ = [
    "CLAIM_SCHEMA_VERSION", "ProvenanceError", "claim_path_for", "is_v2_claim",
    "has_provenance", "require_provenance", "stamp_provenance",
]
