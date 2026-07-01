from __future__ import annotations

import hashlib
import json
from core.ledger.types import BoundaryKind


def _coerce_str(x) -> str:
    """None -> "" (a missing/None field is empty, never the literal 'None'); any other
    value -> str(x) so a real falsy value like 0 / False stays distinct from missing."""
    return "" if x is None else str(x)


def canonical_commit(record: dict) -> dict:
    """Return a new dict with keys in a canonical, order-independent form.

    Only the exact keys ``trajectory_id``, ``parents``, ``boundary_kind``,
    ``message``, ``claims`` are retained, normalised as per spec.
    """
    # Normalise each field independently.
    trajectory_id = _coerce_str(record.get("trajectory_id"))
    # None-safe: a None element in parents/claims is dropped, not str()-ed to "None" and not
    # crashing sorted() with a TypeError (None is unorderable against str).
    parents = sorted({str(p) for p in (record.get("parents") or []) if p is not None})
    boundary_kind = _coerce_str(record.get("boundary_kind"))
    message = " ".join(_coerce_str(record.get("message")).split())   # collapse whitespace
    claims = sorted({str(c) for c in (record.get("claims") or []) if c is not None})

    return {
        "trajectory_id": trajectory_id,
        "parents": parents,
        "boundary_kind": boundary_kind,
        "message": message,
        "claims": claims,
    }


def commit_hash(record: dict) -> str:
    """Return the SHA-256 hex digest of the JSON representation of the
    canonical commit dict (with sorted keys and compact separators)."""
    canon = canonical_commit(record)
    payload = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest


def should_commit(boundary_kind: str) -> bool:
    """Return True if the given boundary kind is committable.

    Every ``BoundaryKind`` value is committable except ``TOOL_CALL``.
    Unknown values are treated as committable (they are not TOOL_CALL).
    """
    return boundary_kind != BoundaryKind.TOOL_CALL.value


def squash_trajectory(
    micro_steps: list[dict],
    *,
    trajectory_id: str,
    boundary_kind: str = "ARTIFACT_COMPLETE",
) -> dict:
    """Collapse N micro‑step records into one commit‑ready dict.

    The ``message`` is the concatenation of all step messages (preserving
    order), joined with a single space. ``claims`` are the union of all
    step claims, deduplicated while preserving the first‑seen order.
    ``parents`` is taken from the first step (or an empty list if no steps).
    The remaining fields are taken from the function arguments.
    """
    # Collect messages in order, skipping steps without a message.
    messages: list[str] = []
    # Collect claims in order, deduplicating via seen set.
    seen_claims: set[str] = set()
    ordered_claims: list[str] = []
    parents: list = []

    for step in micro_steps:
        # message (None-safe: a None message is skipped, not joined as "None")
        step_msg = _coerce_str(step.get("message")).strip()
        if step_msg:
            messages.append(step_msg)
        # claims
        step_claims = step.get("claims") or []
        for c in step_claims:
            if c is None:
                continue
            s = str(c)
            if s not in seen_claims:
                seen_claims.add(s)
                ordered_claims.append(s)
        # parents – only from first step (None-safe)
        if not parents:
            raw_parents = step.get("parents") or []
            parents = sorted({str(p) for p in raw_parents if p is not None})

    joined_message = " ".join(messages)

    return {
        "trajectory_id": trajectory_id,
        "parents": parents,
        "boundary_kind": boundary_kind,
        "message": joined_message,
        "claims": ordered_claims,
    }
