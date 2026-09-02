"""BoBClaw — Flight supervisor logic (Lane 1a, slices 2-3).

Two control-plane concerns over the Flight registry (:mod:`core.flight.store`):

  * **Budget enforcement** — a flight's ``budget_usd`` vs its live spend from the L0.4
    meter (``core.telemetry.spend.flight_spend``). ``budget_status`` is the read the
    dispatch path / TUI / supervisor consult; over-budget is a SURFACE + a policy the
    caller acts on (pause the flight), never a hard mid-call kill.

  * **Fair-share over provider windows** — the concurrency enabler. The hard constraint:
    **GLM caps concurrency ~1** across ALL flights (Z.AI coding plan → 429/1305 on
    parallel — ``[[glm-coding-plan-concurrency-ceiling]]``). So the supervisor SERIALIZES
    the serial providers across flights (grant the single slot to the highest-priority
    contender; defer or SPILL the rest to PAYG ``paas/v4`` which is parallel-safe), with
    priority PREEMPTION (a low-pri flight yields the GLM slot to a high-pri one).
    ``plan_provider_grants`` is the pure, deterministic allocator; ``ProviderSlots`` is the
    thin cross-process (Redis) enforcement, composed with — not reinventing — the
    escalation-pin Redis coordination.
"""
from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis

from core.config import MAX_FANOUT_WIDTH_BY_BACKEND, MAX_FANOUT_WIDTH_GLOBAL, config
from core.flight.store import STATUS_PAUSED, Flight, FlightStore
from core.telemetry.spend import flight_spend

logger = logging.getLogger(__name__)

# Providers that cap concurrency at ~1 across ALL flights (serial). GLM coding plan (Z.AI)
# returns 429/1305 on any parallel call — the supervisor MUST serialize it across flights.
SERIAL_PROVIDERS = {"glm", "glm_5_2"}

# Where a serial provider's OVERFLOW spills: the PAYG `paas/v4` endpoint variant is
# parallel-safe (separate from the concurrency-1 coding plan). A deferred flight either
# waits for the single slot or routes its GLM work here.
SERIAL_PROVIDER_SPILL = {"glm": "glm_payg", "glm_5_2": "glm_payg"}

# TTL on a held provider slot: a crashed holder's slot auto-frees so the resource never
# wedges (mirrors the escalation-pin TTL policy).
_SLOT_TTL_SECONDS = 900


# ── Budget enforcement ────────────────────────────────────────────────────────

async def budget_status(store: FlightStore, flight_id: str) -> dict:
    """The flight's live budget picture: ``{flight_id, budget_usd, spent_usd,
    remaining_usd, over_budget, exists}``. ``budget_usd=None`` (unbudgeted) ⇒ never
    over budget. Reads the durable Flight + the L0.4 Redis spend meter."""
    flight = await store.get(flight_id)
    spent = (await flight_spend(flight_id))["usd"]
    budget = flight.budget_usd if flight else None
    over = budget is not None and spent >= budget
    remaining = None if budget is None else round(budget - spent, 6)
    return {
        "flight_id": flight_id,
        "exists": flight is not None,
        "budget_usd": budget,
        "spent_usd": spent,
        "remaining_usd": remaining,
        "over_budget": over,
    }


async def enforce_budget(store: FlightStore, flight_id: str) -> dict:
    """Check the budget and, if breached, PAUSE the flight (status → paused) so the
    supervisor stops granting it new work. Idempotent — pausing an already-paused flight
    is a no-op. Returns the budget_status dict with a ``paused`` flag. Never raises."""
    status = await budget_status(store, flight_id)
    paused = False
    if status["over_budget"]:
        try:
            updated = await store.set_status(flight_id, STATUS_PAUSED)
            paused = updated is not None
            if paused:
                logger.warning(
                    "flight %s over budget ($%.4f >= $%.4f) — paused",
                    flight_id, status["spent_usd"], status["budget_usd"],
                )
        except Exception:  # noqa: BLE001 — enforcement must never break a turn
            logger.debug("enforce_budget pause failed (non-fatal)", exc_info=True)
    status["paused"] = paused
    return status


# ── Provider-window fair-share (pure allocator) ───────────────────────────────

def provider_capacity(provider: str) -> int:
    """Max concurrent slots for a provider across flights. Serial providers (GLM) → 1;
    others fall back to their fan-out width cap (rate-limit-bounded), then the global cap."""
    if provider in SERIAL_PROVIDERS:
        return 1
    return MAX_FANOUT_WIDTH_BY_BACKEND.get(provider, MAX_FANOUT_WIDTH_GLOBAL)


def spill_target(provider: str) -> Optional[str]:
    """The parallel-safe overflow backend for a serial provider (GLM → PAYG), else None."""
    return SERIAL_PROVIDER_SPILL.get(provider)


def plan_provider_grants(provider: str, requests: list[dict]) -> dict:
    """Deterministically allocate a provider's slots across contending flights.

    ``requests``: ``[{"flight_id", "priority"}, ...]`` (one per flight wanting the
    provider). Grants go to the highest ``priority`` first; ties break by request ORDER
    (FIFO fairness), up to :func:`provider_capacity`. Overflow flights are ``deferred``,
    and — for a serial provider — offered a ``spill`` backend (PAYG). Pure: no I/O, no
    clock. This is the priority-preemption policy in one place (a low-pri flight is the one
    that ends up deferred when a high-pri flight contends for the single GLM slot).
    """
    cap = provider_capacity(provider)
    ordered = sorted(
        enumerate(requests), key=lambda t: (-int(t[1].get("priority", 0)), t[0])
    )
    granted = [r["flight_id"] for _, r in ordered[:cap]]
    deferred = [r["flight_id"] for _, r in ordered[cap:]]
    return {
        "provider": provider,
        "capacity": cap,
        "granted": granted,
        "deferred": deferred,
        "spill": spill_target(provider),
    }


# ── Provider-window fair-share (live cross-process slot) ──────────────────────

_redis_client: "aioredis.Redis | None" = None


def _get_redis() -> "aioredis.Redis":
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis_client


def _slot_key(provider: str) -> str:
    return f"bobclaw:provider:{provider}"


def _parse_holder(value: Optional[str]) -> tuple[Optional[str], int]:
    """Parse a slot value ``"flight_id:priority"`` → ``(flight_id, priority)``."""
    if not value:
        return None, -1
    fid, _, pri = str(value).rpartition(":")
    if not fid:
        return value, 0
    try:
        return fid, int(pri)
    except ValueError:
        return value, 0


class ProviderSlots:
    """Cross-process single-holder locks for SERIAL providers (GLM), with best-effort
    priority preemption. Composes with — does not reinvent — the escalation-pin Redis
    coordination (same client shape / TTL policy).

    FAIL-OPEN: on Redis failure ``try_acquire`` returns True (proceed) — a serial-lock
    blip must not halt work; a resulting GLM 429/1305 is already handled by the existing
    throttle/escalation path. Only serial providers are gated here; a non-serial provider
    (capacity > 1) is always granted (rate-limit-bounded, not a hard mutex).
    """

    def __init__(self, redis_getter=_get_redis, ttl_seconds: int = _SLOT_TTL_SECONDS):
        self._redis = redis_getter
        self._ttl = ttl_seconds

    async def try_acquire(self, provider: str, flight_id: str, priority: int = 0) -> bool:
        """Acquire ``provider``'s single slot for ``flight_id``. True iff acquired (freshly,
        re-entrantly by the same holder, or by PREEMPTING a strictly-lower-priority holder)."""
        if provider not in SERIAL_PROVIDERS:
            return True  # not a hard-serial resource — always allowed
        key = _slot_key(provider)
        val = f"{flight_id}:{int(priority)}"
        try:
            r = self._redis()
            if await r.set(key, val, nx=True, ex=self._ttl):
                return True
            holder, holder_pri = _parse_holder(await r.get(key))
            if holder == flight_id:
                await r.set(key, val, ex=self._ttl)   # refresh own hold
                return True
            if int(priority) > holder_pri:
                # Preempt a strictly-lower-priority holder (best-effort; a race just means
                # a transient double-hold → a GLM 429 the throttle path already absorbs).
                await r.set(key, val, ex=self._ttl)
                logger.info("flight %s preempted %s on serial provider %s (pri %d > %d)",
                            flight_id, holder, provider, priority, holder_pri)
                return True
            return False
        except Exception:  # noqa: BLE001 — fail OPEN; never wedge work on a Redis blip
            logger.debug("provider-slot acquire failed for %s; failing open", provider,
                         exc_info=True)
            return True

    async def release(self, provider: str, flight_id: str) -> None:
        """Release ``provider``'s slot iff still held by ``flight_id`` (never steal another
        flight's freshly-acquired slot). Best-effort."""
        if provider not in SERIAL_PROVIDERS:
            return
        try:
            r = self._redis()
            holder, _ = _parse_holder(await r.get(_slot_key(provider)))
            if holder == flight_id:
                await r.delete(_slot_key(provider))
        except Exception:  # noqa: BLE001
            logger.debug("provider-slot release failed for %s (non-fatal)", provider,
                         exc_info=True)

    async def holder(self, provider: str) -> Optional[str]:
        """The flight currently holding ``provider``'s slot, or None. Best-effort."""
        try:
            holder, _ = _parse_holder(await self._redis().get(_slot_key(provider)))
            return holder
        except Exception:  # noqa: BLE001
            return None


__all__ = [
    "SERIAL_PROVIDERS",
    "SERIAL_PROVIDER_SPILL",
    "budget_status",
    "enforce_budget",
    "provider_capacity",
    "spill_target",
    "plan_provider_grants",
    "ProviderSlots",
]
