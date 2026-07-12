"""BoBClaw — Council event tap (MS9-U7). ADDITIVE · OPT-IN · emit-only.

The council/debate/panel nodes already emit per-seat COMPLETION frames (kind
``council_seat``) and a synth-commit frame (kind ``council_synth``) via
``core.telemetry.emit``. Those fire only AFTER the fact and carry no phase /
"who is speaking now" / round-lifecycle signal — which the U8 Council theater
(SPEC-UI-OVERHAUL §5 Live view: current round, who's speaking, converged/blocked
banner) needs. This module adds a NEW ``council_event`` frame (payload = seat /
round / phase) marking those lifecycle transitions, WITHOUT touching the existing
frames or the transport.

OPT-IN GATE (fence): emission is OFF unless ``council_spec["emit_events"]`` is
truthy. Absent / falsy / non-mapping ⇒ NOTHING new is emitted ⇒ every existing
frame + the final-answer path are byte-identical (proven by test). The gate exists
because the two live transports treat an unknown frame ``type`` differently — the
chat SSE relay (``api/server.py``) forwards ONLY ``type == "token"`` (a
``council_event`` is silently ignored — chat-safe), while the ``/ws/monitor`` relay
(gateway) forwards every frame verbatim (generic passthrough — a ``council_event``
reaches the TUI). NEITHER would choke, but default-OFF guarantees byte-identical
behavior for every current client until U8 explicitly opts in.

Reuses the transport verbatim: ``emit_event`` / ``emit_event_sync`` each fork a
frame to BOTH the LangGraph custom stream writer AND Redis pub/sub, and accept any
kind string (forward-compatible) — so no change to ``core.telemetry.emit``.
"""
from __future__ import annotations

import collections.abc
import logging
from typing import Any, Mapping, Optional

from core.telemetry.emit import emit_event, emit_event_sync
from core.telemetry.flight import resolve_flight_id

logger = logging.getLogger(__name__)

# The additive frame kind (distinct from council_seat / council_synth).
KIND_COUNCIL_EVENT = "council_event"

# Phase markers — the lifecycle the theater renders (wire strings).
PHASE_PANEL_START = "panel_start"          # a (round of) panel dispatch began
PHASE_SEAT_START = "seat_start"            # a seat is about to speak (who's speaking)
PHASE_ROUND_CONVERGED = "round_converged"  # a debate round closed -> converge
PHASE_ROUND_ADVANCED = "round_advanced"    # a debate round closed -> loop to next round
PHASE_BLOCKED = "blocked"                  # a debate stopped on a bound (cost / round cap)

COUNCIL_EVENT_PHASES = frozenset({
    PHASE_PANEL_START,
    PHASE_SEAT_START,
    PHASE_ROUND_CONVERGED,
    PHASE_ROUND_ADVANCED,
    PHASE_BLOCKED,
})

# The opt-in key on council_spec (also threaded onto panel_worker's Send sub-state).
_EMIT_KEY = "emit_events"


def events_enabled(spec: Any) -> bool:
    """True iff the tap is opted in via ``spec["emit_events"]`` being truthy.

    ``spec`` may be any mapping or None. Absent / falsy / non-mapping ⇒ False
    (OFF ⇒ byte-identical behavior — nothing new emitted).
    """
    if not isinstance(spec, collections.abc.Mapping):
        return False
    return bool(spec.get(_EMIT_KEY))


def _build_payload(
    phase: str,
    round_idx: Optional[int],
    seat: Optional[int],
    posture: Optional[str],
    extra: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Flat council_event payload. Always carries ``phase`` + ``round``; adds ``seat``
    / ``posture`` when given; merges ``extra`` last but NEVER lets it overwrite the
    reserved keys. Pure."""
    payload: dict[str, Any] = {"phase": phase, "round": int(round_idx or 0)}
    if seat is not None:
        payload["seat"] = seat
    if posture is not None:
        payload["posture"] = posture
    if extra:
        for k, v in extra.items():
            if k not in ("phase", "round", "seat", "posture"):
                payload[k] = v
    return payload


async def emit_council_event(
    spec: Any,
    flight_source: Any,
    phase: str,
    *,
    round_idx: int = 0,
    seat: Optional[int] = None,
    posture: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Optional[dict]:
    """ASYNC council_event emit (panel_worker / debate_converge). NO-OP unless opted in.

    Gate FIRST: when ``events_enabled(spec)`` is False, calls NOTHING and returns None
    (byte-identical). Otherwise builds the payload and forks it via ``emit_event``.
    ``flight_source`` is the state / sub-state dict ``resolve_flight_id`` reads.
    Best-effort — telemetry never breaks the turn (any error ⇒ debug log ⇒ None).
    """
    if not events_enabled(spec):
        return None
    try:
        payload = _build_payload(phase, round_idx, seat, posture, extra)
        return await emit_event(KIND_COUNCIL_EVENT, resolve_flight_id(flight_source), payload)
    except Exception:  # noqa: BLE001 — telemetry is observational, never load-bearing
        logger.debug("emit_council_event failed (non-fatal)", exc_info=True)
        return None


def emit_council_event_sync(
    spec: Any,
    flight_source: Any,
    phase: str,
    *,
    round_idx: int = 0,
    seat: Optional[int] = None,
    posture: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Optional[dict]:
    """SYNC council_event emit (the sync panel_dispatch node). NO-OP unless opted in.

    Mirrors :func:`emit_council_event` but forks via ``emit_event_sync`` (stream fork
    in-turn; Redis fork scheduled only when a running loop exists — the same policy as
    dispatch's ``fleet_start``). Best-effort; returns the frame or None.
    """
    if not events_enabled(spec):
        return None
    try:
        payload = _build_payload(phase, round_idx, seat, posture, extra)
        return emit_event_sync(KIND_COUNCIL_EVENT, resolve_flight_id(flight_source), payload)
    except Exception:  # noqa: BLE001
        logger.debug("emit_council_event_sync failed (non-fatal)", exc_info=True)
        return None


__all__ = [
    "KIND_COUNCIL_EVENT",
    "PHASE_PANEL_START",
    "PHASE_SEAT_START",
    "PHASE_ROUND_CONVERGED",
    "PHASE_ROUND_ADVANCED",
    "PHASE_BLOCKED",
    "COUNCIL_EVENT_PHASES",
    "events_enabled",
    "emit_council_event",
    "emit_council_event_sync",
]
