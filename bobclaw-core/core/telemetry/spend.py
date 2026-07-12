"""BoBClaw — Per-flight spend meter (Layer 0, L0.4).

Aggregates per-call USD (``core/backends/_cost.usd_for``) UP TO THE FLIGHT LEVEL — the
one meter three consumers share (KICKOFF §4.3):
  * the **flight supervisor** (1a) — per-flight budget enforcement (``Flight.budget_usd``);
  * the **TUI** — the live cost tick per flight;
  * **E1 DEP-2** spend-metering (MS#4 BLOCKED #5).

**Cross-process** (Bob runs multi-process — the Topology note makes in-memory
process-local spend a correctness bug): the source of truth is a Redis hash
``bobclaw:spend:<flight_id>`` (field = backend, value = cumulative USD) written with
``HINCRBYFLOAT`` (atomic, exactly the concurrency-safe primitive the supervisor needs).
When Redis is unreachable it FAILS OPEN to a process-local dict (mirrors the escalation-pin
degradation policy) so a metered call never breaks — spend attribution just degrades to
per-process until Redis returns.

**What is metered.** USD is only *known* for metered backends (``kimi_platform`` today via
``_cost``); subscription backends (deepseek/claude via their own login) cost window-time,
not USD, so their per-flight USD stays 0 — which is correct (window-time fair-sharing is the
supervisor's scheduler, a separate axis from the USD budget). A consumer that DOES know a
call's USD (E1's API fetcher) records it directly via :func:`record_flight_spend`.

A ``current_flight`` contextvar lets the deep metered path attribute spend to the flight the
node is running without threading ``flight_id`` through every backend signature — the node
(worker/execute) sets it at entry; ``_send_to_backend``'s metered branch reads it.
"""
from __future__ import annotations

import contextvars
import logging
from typing import Optional

import redis.asyncio as aioredis

from core.config import config
from core.telemetry.emit import KIND_COST, emit_event
from core.telemetry.flight import AMBIENT_FLIGHT, resolve_flight_id

logger = logging.getLogger(__name__)

# The flight the current async task is running under. Set by the node (worker/execute);
# read at the metered-call site. Default None ⇒ ambient at record time.
_current_flight: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "bobclaw_current_flight", default=None
)

# Process-local fallback when Redis is down: {flight_id: {backend: usd}}.
_LOCAL_SPEND: dict[str, dict[str, float]] = {}

_redis_client: "aioredis.Redis | None" = None
_redis_warned: bool = False


def _spend_key(flight_id: str) -> str:
    return f"bobclaw:spend:{flight_id}"


def _get_redis() -> "aioredis.Redis":
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis_client


# ── current-flight contextvar ─────────────────────────────────────────────────

def set_current_flight(flight_id: Optional[str]) -> contextvars.Token:
    """Bind the flight for the current task; returns the reset Token."""
    return _current_flight.set(flight_id)


def reset_current_flight(token: contextvars.Token) -> None:
    try:
        _current_flight.reset(token)
    except (ValueError, LookupError):
        # Reset from a different context (e.g. token created in another task) — ignore.
        pass


def current_flight() -> Optional[str]:
    return _current_flight.get()


# ── record / read ─────────────────────────────────────────────────────────────

async def record_flight_spend(
    flight_id: Optional[str], backend: str, usd: float, *, emit: bool = True,
) -> dict:
    """Add ``usd`` to a flight's spend (keyed by backend) and return the new snapshot.

    Atomic cross-process via Redis ``HINCRBYFLOAT``; fails open to the process-local dict.
    Emits a ``cost`` monitor frame (a per-flight spend delta = a state-change) unless
    ``emit=False``. A zero/negative delta is ignored (no-op) so a no-cost call is silent.
    Never raises."""
    global _redis_warned
    fid = flight_id or AMBIENT_FLIGHT
    amount = float(usd or 0.0)
    if amount <= 0.0:
        return await flight_spend(fid)

    try:
        await _get_redis().hincrbyfloat(_spend_key(fid), backend, amount)
    except Exception as exc:  # noqa: BLE001 — fail open, never break a metered call
        if not _redis_warned:
            _redis_warned = True
            logger.warning(
                "flight-spend Redis write failed (%s); degrading to process-local: %s",
                type(exc).__name__, exc,
            )
        _LOCAL_SPEND.setdefault(fid, {}).setdefault(backend, 0.0)
        _LOCAL_SPEND[fid][backend] += amount

    snap = await flight_spend(fid)
    if emit:
        await emit_event(
            KIND_COST, fid,
            {"backend": backend, "delta_usd": amount,
             "usd": snap["usd"], "by_backend": snap["by_backend"]},
        )
    return snap


async def flight_spend(flight_id: Optional[str]) -> dict:
    """Return ``{"usd": float, "by_backend": {backend: usd}}`` for a flight.

    Reads Redis (source of truth); on Redis failure returns the process-local view. A
    flight with no recorded spend returns zeros. Never raises."""
    fid = flight_id or AMBIENT_FLIGHT
    by_backend: dict[str, float] = {}
    try:
        raw = await _get_redis().hgetall(_spend_key(fid))
        for k, v in (raw or {}).items():
            try:
                by_backend[k] = float(v)
            except (TypeError, ValueError):
                continue
    except Exception:
        by_backend = dict(_LOCAL_SPEND.get(fid, {}))
    # Round per-backend (and the sum) to 6dp so float-dust from repeated adds never
    # surfaces on the cost tick / budget check.
    by_backend = {k: round(v, 6) for k, v in by_backend.items()}
    return {"usd": round(sum(by_backend.values()), 6), "by_backend": by_backend}


async def record_current_flight_spend(backend: str, usd: float) -> None:
    """Attribute a metered call's USD to whatever flight the current task is bound to
    (the contextvar). Best-effort — a telemetry failure never breaks the call."""
    try:
        await record_flight_spend(current_flight(), backend, usd)
    except Exception:  # noqa: BLE001
        logger.debug("record_current_flight_spend failed (non-fatal)", exc_info=True)


async def reset_flight_spend(flight_id: Optional[str]) -> None:
    """Clear a flight's spend (flight lifecycle / tests). Best-effort both stores."""
    fid = flight_id or AMBIENT_FLIGHT
    _LOCAL_SPEND.pop(fid, None)
    try:
        await _get_redis().delete(_spend_key(fid))
    except Exception:
        logger.debug("reset_flight_spend Redis delete failed (non-fatal)", exc_info=True)


def bind_flight_from_state(state) -> contextvars.Token:
    """Convenience for a node: resolve the two-tier flight from ``state`` and bind the
    contextvar. Returns the Token for :func:`reset_current_flight`."""
    return set_current_flight(resolve_flight_id(state))


__all__ = [
    "set_current_flight",
    "reset_current_flight",
    "current_flight",
    "bind_flight_from_state",
    "record_flight_spend",
    "record_current_flight_spend",
    "flight_spend",
    "reset_flight_spend",
]
