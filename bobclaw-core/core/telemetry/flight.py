"""BoBClaw — Flight identity (Layer 0, L0.1).

A *flight* is the unit every emitted event, tool_trace, and spend datum rolls up
under. Two tiers:

  * **Named flight** — an explicit, budgeted, prioritized task-stream ("block of
    work") created via the supervisor. Its id is a caller-chosen string
    (``AgentState.flight_id``).
  * **Ambient flight** — the fallback when no named flight is in scope, so stray /
    mechanical work and the live face talking to the user are still metered +
    watchable (NOT untracked):
      - a live-face chat turn → ``chat:<conversation_id>`` (grouped per conversation);
      - un-conversationed / stray work → the shared ``ambient`` flight.

**Additive discipline.** ``AgentState.flight_id`` stays ``Optional[str]`` default
``None`` ⇒ a byte-identical delta (no key). The ambient *assignment* is a READ-time
concern: the emit layer and spend meter call :func:`resolve_flight_id` to map a
``None`` flight onto its ambient bucket. Nothing here mutates state, so an off-graph
/ test call with no flight is unchanged.

PURE — no I/O, no clock, no Redis. Safe to import from any node.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

# The shared catch-all flight for stray / mechanical work with no conversation.
AMBIENT_FLIGHT = "ambient"

# Prefix for the per-conversation ambient flight (the live face talking to a user).
_CHAT_PREFIX = "chat:"


def chat_flight_id(conversation_id: Optional[str]) -> str:
    """The ambient flight id for a live-face chat turn: ``chat:<conversation_id>``.

    Falls back to the shared :data:`AMBIENT_FLIGHT` when the conversation id is
    absent/blank (a chat turn with no stable conversation id is stray work).
    """
    cid = str(conversation_id).strip() if conversation_id is not None else ""
    if not cid:
        return AMBIENT_FLIGHT
    return f"{_CHAT_PREFIX}{cid}"


def resolve_flight_id(state: Optional[Mapping[str, Any]]) -> str:
    """Resolve the flight a unit of work rolls up under, at READ time.

    Precedence (never mutates ``state``):
      1. an explicit ``state['flight_id']`` (a named flight) — highest;
      2. else ``chat:<conversation_id>`` when the turn has a conversation id
         (the live face);
      3. else the shared :data:`AMBIENT_FLIGHT`.

    A ``None``/empty ``state`` resolves to :data:`AMBIENT_FLIGHT` so the meter/emit
    path can never key on ``None``.
    """
    if not state:
        return AMBIENT_FLIGHT
    explicit = state.get("flight_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    # A non-str truthy flight_id is a caller bug; coerce defensively rather than
    # keying telemetry on a repr.
    if explicit not in (None, "") and not isinstance(explicit, str):
        return str(explicit)
    return chat_flight_id(state.get("conversation_id"))


def is_ambient(flight_id: Optional[str]) -> bool:
    """True when ``flight_id`` denotes ambient (stray/live-face) work — i.e. it is
    absent, the shared ambient bucket, or a ``chat:`` conversation flight. Named
    "block of work" flights (anything else) return False."""
    if not flight_id:
        return True
    fid = str(flight_id).strip()
    return fid == AMBIENT_FLIGHT or fid.startswith(_CHAT_PREFIX)


__all__ = [
    "AMBIENT_FLIGHT",
    "chat_flight_id",
    "resolve_flight_id",
    "is_ambient",
]
