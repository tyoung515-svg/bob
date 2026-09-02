"""events.py — Typed constructors and validators for additive FOREST events.

Part of the ``core.forest`` package. PURE, deterministic, stdlib-only. Every forest event is a dict
that gets appended (one JSON object per line) to a program ledger's ``events.jsonl`` in the
``core/ledger`` format — each object MUST carry a non-empty string ``id`` so
``core.ledger.project.read_ledger_at`` includes it.

Determinism (mega-sprint invariant 13): NO clock, NO uuid, NO random, NO I/O. Every timestamp is a
caller-supplied argument, and an unspecified ``event_id`` is derived as a content hash — so identical
inputs yield an identical dict (reproducible tests, content-stable ids).
"""

import hashlib
import json
from typing import Any, Optional, Union


class ForestError(RuntimeError):
    """Base error for the core.forest package."""
    pass


class ForestEventError(ForestError):
    """Raised on any invalid event construction/validation."""
    pass


EVENT_KINDS: frozenset[str] = frozenset({
    "measurement",
    "spend",
    "epoch_open",
    "epoch_close",
    "fork_proposed",
    "fork_approved",
    "pull_from_parent",
    "experiment_run",
    "archive_proposed",
})

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "measurement":      ("node_id", "metric", "value", "source"),
    "spend":            ("amount_usd", "label"),
    "epoch_open":       ("epoch_id", "trigger"),
    "epoch_close":      ("epoch_id",),
    "fork_proposed":    ("fork_id", "parent_program", "rationale"),
    "fork_approved":    ("fork_id", "child_program"),
    "pull_from_parent": ("parent_program", "from_ref"),
    "experiment_run":   ("experiment_id", "node_id", "runner"),
    "archive_proposed": ("program_id", "rationale"),
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _event_id(payload_without_id: dict, event_id: Optional[str]) -> str:
    """Return the event id: the caller's ``event_id`` (validated non-empty str) or a deterministic
    content-addressed hash of the id-less payload."""
    if event_id is not None:
        if not isinstance(event_id, str) or event_id == "":
            raise ForestEventError("event_id must be a non-empty string")
        return event_id
    canonical = json.dumps(
        payload_without_id,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return "evt:" + digest


def _build(kind: str, *, ts, event_id: Optional[str] = None, **fields) -> dict:
    """Assemble, validate, and stamp an id onto an event dict. Fields whose value is ``None`` are
    dropped so optionals never pollute the JSON line."""
    payload: dict[str, Any] = {"kind": kind, "ts": ts}
    for k, v in fields.items():
        if v is not None:
            payload[k] = v

    # Validate the id-less payload (kind / ts / required fields / numeric fields).
    validate_event(payload)

    payload_without_id = {k: v for k, v in payload.items() if k != "id"}
    payload["id"] = _event_id(payload_without_id, event_id)
    return payload


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------

def measurement(
    *,
    node_id: str,
    metric: str,
    value: Union[int, float],
    source: str,
    ts,
    unit: Optional[str] = None,
    weight: Union[int, float] = 1,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Create a ``measurement`` event (one observation on a hypothesis node's measurement stream)."""
    return _build(
        "measurement",
        ts=ts,
        event_id=event_id,
        node_id=node_id,
        metric=metric,
        value=value,
        source=source,
        unit=unit,
        weight=weight,
        **extra,
    )


def spend(
    *,
    amount_usd: Union[int, float],
    label: str,
    ts,
    est: bool = True,
    node_id: Optional[str] = None,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Create a ``spend`` event (an EST-badged cost; COST-2 — budget derives from ledger truth)."""
    return _build(
        "spend",
        ts=ts,
        event_id=event_id,
        amount_usd=amount_usd,
        label=label,
        est=est,
        node_id=node_id,
        **extra,
    )


def epoch_open(
    *,
    epoch_id: str,
    trigger: str,
    ts,
    base_commit: Optional[str] = None,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Create an ``epoch_open`` event (a hypothesize epoch begins; carries its trigger verb)."""
    return _build(
        "epoch_open",
        ts=ts,
        event_id=event_id,
        epoch_id=epoch_id,
        trigger=trigger,
        base_commit=base_commit,
        **extra,
    )


def epoch_close(
    *,
    epoch_id: str,
    ts,
    halted: bool = False,
    verified_claims: Optional[Any] = None,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Create an ``epoch_close`` event (a hypothesize epoch ends; halted flag + verified claims)."""
    return _build(
        "epoch_close",
        ts=ts,
        event_id=event_id,
        epoch_id=epoch_id,
        halted=halted,
        verified_claims=verified_claims,
        **extra,
    )


def fork_proposed(
    *,
    fork_id: str,
    parent_program: str,
    rationale: str,
    ts,
    node_id: Optional[str] = None,
    subtree_ref: Optional[str] = None,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Create a ``fork_proposed`` event (proposal-only; carries the "why it can't stay a subtree")."""
    return _build(
        "fork_proposed",
        ts=ts,
        event_id=event_id,
        fork_id=fork_id,
        parent_program=parent_program,
        rationale=rationale,
        node_id=node_id,
        subtree_ref=subtree_ref,
        **extra,
    )


def fork_approved(
    *,
    fork_id: str,
    child_program: str,
    ts,
    parent_program: Optional[str] = None,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Create a ``fork_approved`` event (a fork was approved; a child program was seeded)."""
    return _build(
        "fork_approved",
        ts=ts,
        event_id=event_id,
        fork_id=fork_id,
        child_program=child_program,
        parent_program=parent_program,
        **extra,
    )


def pull_from_parent(
    *,
    parent_program: str,
    from_ref: str,
    ts,
    notes: Optional[str] = None,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Create a ``pull_from_parent`` event (explicit knowledge transfer from a parent program)."""
    return _build(
        "pull_from_parent",
        ts=ts,
        event_id=event_id,
        parent_program=parent_program,
        from_ref=from_ref,
        notes=notes,
        **extra,
    )


def experiment_run(
    *,
    experiment_id: str,
    node_id: str,
    runner: str,
    ts,
    config: Optional[Any] = None,
    est_cost: Optional[Union[int, float]] = None,
    result: Optional[Any] = None,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Create an ``experiment_run`` event (an experiment node's runner executed for one config)."""
    return _build(
        "experiment_run",
        ts=ts,
        event_id=event_id,
        experiment_id=experiment_id,
        node_id=node_id,
        runner=runner,
        config=config,
        est_cost=est_cost,
        result=result,
        **extra,
    )


def archive_proposed(
    *,
    program_id: str,
    rationale: str,
    ts,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Create an ``archive_proposed`` event (a tree is all dormant/resolved; archive proposal)."""
    return _build(
        "archive_proposed",
        ts=ts,
        event_id=event_id,
        program_id=program_id,
        rationale=rationale,
        **extra,
    )


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def validate_event(event: dict) -> None:
    """Raise ``ForestEventError`` if *event* is not a valid forest event.

    Accepts an event with OR without ``id`` (the builder validates mid-construction, before the id is
    stamped). A present ``id`` must be a non-empty str. Numeric fields (measurement ``value``, spend
    ``amount_usd``, experiment_run ``est_cost``), when present, must be a real int/float — a ``bool``
    (an int subclass) is rejected.
    """
    if not isinstance(event, dict):
        raise ForestEventError("Event must be a dict")

    kind = event.get("kind")
    if kind is None or kind not in EVENT_KINDS:
        raise ForestEventError(f"Missing or invalid 'kind': {kind!r}")

    ts = event.get("ts")
    if ts is None:
        raise ForestEventError("'ts' is required and must not be None")

    for name in REQUIRED_FIELDS[kind]:
        if name not in event or event[name] is None:
            raise ForestEventError(
                f"Required field '{name}' missing or None for kind '{kind}'"
            )

    numeric_fields = {
        "measurement": "value",
        "spend": "amount_usd",
        "experiment_run": "est_cost",
    }
    if kind in numeric_fields:
        name = numeric_fields[kind]
        val = event.get(name)
        if val is not None and (not isinstance(val, (int, float)) or isinstance(val, bool)):
            raise ForestEventError(
                f"Field '{name}' must be a numeric type (int or float), got {type(val).__name__}"
            )

    id_val = event.get("id")
    if id_val is not None and (not isinstance(id_val, str) or id_val == ""):
        raise ForestEventError("'id' must be a non-empty string when present")


def is_forest_event(event) -> bool:
    """Return True iff *event* is a dict whose ``kind`` is in ``EVENT_KINDS``. Never raises."""
    return isinstance(event, dict) and event.get("kind") in EVENT_KINDS


__all__ = [
    "ForestError",
    "ForestEventError",
    "EVENT_KINDS",
    "REQUIRED_FIELDS",
    "validate_event",
    "is_forest_event",
    "measurement",
    "spend",
    "epoch_open",
    "epoch_close",
    "fork_proposed",
    "fork_approved",
    "pull_from_parent",
    "experiment_run",
    "archive_proposed",
]
