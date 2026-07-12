"""BoBClaw — Flight substrate emit layer (Layer 0, L0.2).

ONE instrumentation point per orchestration node. **Emit once, fork twice:**

  (a) LIVE cross-process → Redis pub/sub channel :data:`MONITOR_CHANNEL`
      (``bobclaw:monitor``). The gateway ``/ws/monitor`` router (L0.3) subscribes and
      forwards frames to the TUI. This is the REAL transport for the (separate-process)
      monitor consumer.
  (b) LIVE in-turn → the LangGraph custom stream writer (``execute._get_stream_writer``),
      when inside a graph run. Safe: the chat SSE relay only forwards ``type == "token"``
      custom frames (``api/server.py``), so a monitor frame (``worker_state`` etc.) is
      IGNORED by chat — it never pollutes the chat stream. Gives same-process / test
      subscribers (and the live-fan-out E2E gate) a Redis-free path.

**FAIL-SAFE.** Every fork is best-effort — a dead Redis, no stream context, or a raising
writer NEVER breaks the orchestration turn (telemetry is observational). Mirrors the
escalation-pin degradation policy (``execute._pin_escalation``).

**Frame shape** (GATEWAY-CONTRACT: snake_case, top-level ``type``, FLAT): the ``payload``
dict is merged flat with three reserved top-level keys — ``type`` (kind), ``flight_id``,
``ts`` (ISO-8601).

Two entry points because ``dispatch_node`` is sync while ``worker``/``join``/``council``
are async:
  * :func:`emit_event` (async) — awaits the Redis publish. The reliable path; carries
    the mid-run ``worker_state`` / ``fleet_join`` events the gate asserts on.
  * :func:`emit_event_sync` (sync) — stream-writer fork + best-effort scheduled Redis
    publish (a sync node runs in a LangGraph executor thread with no running loop, so its
    Redis fork is skipped there; the stream-writer fork still delivers in-turn). Used for
    ``dispatch``'s ``fleet_start`` wave marker.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import redis.asyncio as aioredis

from core.config import config

logger = logging.getLogger(__name__)

# Single monitor pub/sub channel (Bob is effectively single-tenant). Frames
# carry ``flight_id`` (+ ``user_id`` when known) so the gateway can filter/group. The
# TUI groups by ``flight_id`` client-side.
MONITOR_CHANNEL = "bobclaw:monitor"

# Reserved top-level keys — set last so a payload can never shadow them.
_RESERVED = ("type", "flight_id", "ts")

# Well-known event kinds (documentation + the wire contract). Not enforced — an
# unknown kind still emits (forward-compatible), but the fleet path uses these.
KIND_FLEET_START = "fleet_start"       # dispatch: a wave began  {n_workers, wave, backend}
KIND_WORKER_STATE = "worker_state"     # worker: a state change  {idx, role, backend, status, tokens?, duration_ms?, name?}
KIND_FLEET_JOIN = "fleet_join"         # join: the wave reduced  {ok, failed, total}
KIND_COUNCIL_SEAT = "council_seat"     # panel_worker: a seat answered  {idx, posture, backend, round, status, tokens}
KIND_COUNCIL_SYNTH = "council_synth"   # council/synthesize: the synthesis committed
KIND_COST = "cost"                     # spend meter (L0.4): a per-flight cost delta

_redis_client: "aioredis.Redis | None" = None
_redis_warned: bool = False
# Keep strong refs to fire-and-forget publish tasks so they are not GC'd mid-flight
# (mirrors core/nodes/_l0_events.py's pending-task set).
_pending: set = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_redis() -> "aioredis.Redis":
    """Process-wide async Redis client for monitor publishes (lazy singleton).

    Separate from ``execute._get_redis`` (escalation pins) by design — this is a
    pub/sub publisher, a distinct usage — so the two modules stay decoupled. Same
    ``config.REDIS_URL`` / ``decode_responses`` shape.
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis_client


def build_frame(
    kind: str,
    flight_id: Optional[str],
    payload: Optional[Mapping[str, Any]] = None,
    *,
    ts: Optional[str] = None,
) -> dict:
    """Build a flat monitor frame. Reserved keys (``type``/``flight_id``/``ts``) win over
    any same-named payload key. ``ts`` is injectable so tests are deterministic."""
    frame: dict[str, Any] = {}
    if payload:
        for k, v in payload.items():
            if k not in _RESERVED:
                frame[k] = v
    frame["type"] = kind
    frame["flight_id"] = flight_id
    frame["ts"] = ts or _now_iso()
    return frame


def _emit_to_stream(frame: dict) -> None:
    """Best-effort in-turn fork via the LangGraph custom stream writer."""
    try:
        from core.nodes.execute import _get_stream_writer

        writer = _get_stream_writer()
        if writer is not None:
            writer(dict(frame))
    except Exception:
        logger.debug("monitor stream emit failed (non-fatal)", exc_info=True)


async def _publish_to_redis(frame: dict) -> None:
    """Best-effort cross-process fork via Redis pub/sub. Never raises."""
    global _redis_warned
    try:
        await _get_redis().publish(MONITOR_CHANNEL, json.dumps(frame))
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks the turn
        if not _redis_warned:
            _redis_warned = True
            logger.warning(
                "monitor Redis publish failed (%s); live monitor degraded, run continues: %s",
                type(exc).__name__, exc,
            )
        else:
            logger.debug("monitor Redis publish failed (non-fatal)", exc_info=True)


def _schedule_redis_publish(frame: dict) -> None:
    """Schedule the Redis fork from a SYNC caller.

    When a running loop exists (async caller in a loop) create a tracked task; from a
    sync LangGraph node executing in an executor thread there is NO running loop, so the
    Redis fork is skipped (the stream-writer fork already delivered in-turn). This keeps
    the sync path non-blocking — dispatch's ``fleet_start`` is a bonus wave marker; the
    gate rides ``worker_state``/``fleet_join`` which are awaited on the async path.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("no running loop for monitor emit; redis fork skipped (stream fork stands)")
        return
    task = loop.create_task(_publish_to_redis(frame))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def emit_event(
    kind: str,
    flight_id: Optional[str],
    payload: Optional[Mapping[str, Any]] = None,
    *,
    ts: Optional[str] = None,
) -> dict:
    """Emit a monitor frame from an ASYNC node (worker/join/council). Awaits the Redis
    fork so the mid-run event is actually on the wire before the node returns. Returns
    the frame (for tests). Best-effort throughout — never raises."""
    frame = build_frame(kind, flight_id, payload, ts=ts)
    _emit_to_stream(frame)
    await _publish_to_redis(frame)
    return frame


def emit_event_sync(
    kind: str,
    flight_id: Optional[str],
    payload: Optional[Mapping[str, Any]] = None,
    *,
    ts: Optional[str] = None,
) -> dict:
    """Emit a monitor frame from a SYNC node (dispatch). Stream-writer fork now + a
    best-effort scheduled Redis fork. Returns the frame. Never raises."""
    frame = build_frame(kind, flight_id, payload, ts=ts)
    _emit_to_stream(frame)
    _schedule_redis_publish(frame)
    return frame


async def aclose() -> None:
    """Close the Redis client (test teardown / shutdown). Best-effort."""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:
            logger.debug("monitor Redis close failed (non-fatal)", exc_info=True)
        _redis_client = None


__all__ = [
    "MONITOR_CHANNEL",
    "KIND_FLEET_START",
    "KIND_WORKER_STATE",
    "KIND_FLEET_JOIN",
    "KIND_COUNCIL_SEAT",
    "KIND_COUNCIL_SYNTH",
    "KIND_COST",
    "build_frame",
    "emit_event",
    "emit_event_sync",
    "aclose",
]
