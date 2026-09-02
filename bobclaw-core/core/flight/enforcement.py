"""BoBClaw — Flight substrate FU1: live serial-provider (GLM) enforcement gate.

Wires the BUILT control-plane primitive :class:`core.flight.supervisor.ProviderSlots` (the
cross-flight GLM-serial mutex + priority preemption) into the LIVE backend dispatch. Everything
here is **INERT** unless ``config.FLIGHT_ENFORCE_ENABLED`` — a serial-provider call then acquires
the single cross-flight slot before running; on contention it backs off to a parallel-safe
escalation backend (Decision D1) rather than blocking. Ambient / live-face work is priority 0
and (being non-serial-contended by construction) rides re-entrant with the holder.

Imported LAZILY from ``execute.py``'s serial-provider branches to avoid an import cycle.

**FAIL-OPEN throughout:** a dead Redis / store hiccup / any error ⇒ run the original backend
with no slot. Enforcement is a fairness optimization, never a correctness gate on the turn.

SERIAL-1 DOCTRINE (Travis 2026-07-04): ``ProviderSlots`` serializes GLM ACROSS flights (its
point — two mega-sprints don't both hammer the serial GLM window). WITHIN one flight it is
re-entrant (same-holder refresh), so same-flight PARALLEL GLM would slip past — but that only
happens when GLM is a FAN-OUT auditor/worker (e.g. the 1:10 chunk-audit). The resolution is a
roster policy, not a slot change: **GLM is single-lane-only work** — pull it out of the fan-out
critic/worker spots (``core/teams.py`` `critic`/`worker` = `glm_5_2` at ~:61/:78/:87;
``core/nodes/hier.py`` section critic) and use a parallel-safe auditor there (minimax /
deepseek_v4). With no same-flight parallel GLM, at most one GLM call runs per flight and the
cross-flight mutex gives TRUE serial-1 — re-entrancy moot. The roster swap is a FOLLOW-UP
(doesn't change this module). See memory [[glm-single-lane-only]].
"""
from __future__ import annotations

import logging
from typing import Optional

from core.config import config
from core.flight.supervisor import SERIAL_PROVIDERS, ProviderSlots
from core.telemetry.flight import is_ambient
from core.telemetry.spend import current_flight

logger = logging.getLogger(__name__)

# D1 (LOCKED): on serial-slot contention a deferred flight backs off to a parallel-safe
# escalation backend instead of blocking or building glm_payg. Tunable; deepseek_v4_flash is
# the always-available, uncapped worker that can stand in for the GLM audit/critic role.
SERIAL_ESCALATION: dict[str, str] = {
    "glm_5_2": "deepseek_v4_flash",
    "glm": "deepseek_v4_flash",
}

_slots: Optional[ProviderSlots] = None


def get_provider_slots() -> ProviderSlots:
    global _slots
    if _slots is None:
        _slots = ProviderSlots()
    return _slots


def set_provider_slots(slots: Optional[ProviderSlots]) -> None:
    """Test seam — inject a ProviderSlots over a fake KV (mirrors redis_client.set_redis_client)."""
    global _slots
    _slots = slots


async def _flight_priority(flight_id: Optional[str]) -> int:
    """Named-flight priority for slot arbitration; ambient/live-face ⇒ 0 (not in the store)."""
    if not flight_id or is_ambient(flight_id):
        return 0
    try:
        from core.flight.service import _ensure_inited

        store = await _ensure_inited()
        flight = await store.get(flight_id)
        return int(flight.priority) if flight is not None else 0
    except Exception:  # fail-open: a store hiccup must never break the turn
        logger.debug("flight priority lookup failed for %r; using 0", flight_id, exc_info=True)
        return 0


async def acquire_or_escalate(backend: str) -> tuple[str, Optional[str]]:
    """FU1 serial-provider gate. Returns ``(run_backend, holder)``:

    * non-serial provider OR flag OFF ⇒ ``(backend, None)`` — byte-identical, no slot.
    * slot acquired ⇒ ``(backend, holder_flight)`` — caller MUST ``await release(backend, holder)``.
    * slot contended ⇒ ``(escalation_backend, None)`` — caller runs there instead (D1 back-off).

    Fail-OPEN: any error ⇒ ``(backend, None)`` (run the original backend, no slot).
    """
    if not config.FLIGHT_ENFORCE_ENABLED or backend not in SERIAL_PROVIDERS:
        return backend, None
    try:
        flight = current_flight() or "ambient"
        priority = await _flight_priority(flight)
        acquired = await get_provider_slots().try_acquire(backend, flight, priority)
        if acquired:
            return backend, flight
        escalation = SERIAL_ESCALATION.get(backend)
        if escalation:
            logger.info(
                "serial slot %s contended by flight=%r (pri=%d); backing off to %s",
                backend, flight, priority, escalation,
            )
            return escalation, None
        return backend, None  # no escalation mapped ⇒ run anyway (fail-open)
    except Exception:
        logger.debug("serial-slot acquire failed for %s; running unslotted", backend, exc_info=True)
        return backend, None


async def paused_flight_refusal(state) -> Optional[dict]:
    """FU1 D2 pause-with-teeth: if the turn's NAMED flight is paused (auto-paused over
    budget), return a refusal state-update that ENDs the turn with a surfaced message.

    Returns ``None`` (proceed) when: the flag is off; the flight is ambient / ``chat:<conv>``
    / live-face (NEVER blocked); the flight is unknown or not paused; or any error (fail-open).
    The caller (route_node) merges the dict and sets ``flight_refused`` so ``_route_after_recall``
    routes to END. Ambient/live-face refusal is impossible by construction (is_ambient short-circuit).
    """
    if not config.FLIGHT_ENFORCE_ENABLED:
        return None
    from core.telemetry.flight import resolve_flight_id

    flight = resolve_flight_id(state)
    if is_ambient(flight):
        return None
    try:
        from core.flight.service import _ensure_inited
        from core.flight.store import STATUS_PAUSED

        store = await _ensure_inited()
        f = await store.get(flight)
    except Exception:  # fail-open: never block a turn on an enforcement infra hiccup
        logger.debug("flight pause check failed for %r; not refusing", flight, exc_info=True)
        return None
    if f is None or f.status != STATUS_PAUSED:
        return None
    budget = f"${f.budget_usd:.2f}" if f.budget_usd is not None else "budget"
    msg = (
        f"⚠ Flight '{f.name}' ({flight}) is PAUSED — over {budget}. New work is refused. "
        f"Raise the budget and resume via PATCH /api/flights/{flight} (status=active)."
    )
    logger.info("refusing turn for paused flight %r (over budget)", flight)
    return {
        "messages": [{"role": "assistant", "content": msg}],
        "error": f"flight_paused:{flight}",
        "flight_refused": True,
    }


async def release(backend: str, holder: Optional[str]) -> None:
    """Release a slot from :func:`acquire_or_escalate`. Best-effort; no-op when holder is None."""
    if holder is None:
        return
    try:
        await get_provider_slots().release(backend, holder)
    except Exception:
        logger.debug("serial-slot release failed for %s", backend, exc_info=True)


def _reset_for_tests() -> None:
    global _slots
    _slots = None


__all__ = [
    "SERIAL_ESCALATION",
    "acquire_or_escalate",
    "release",
    "paused_flight_refusal",
    "get_provider_slots",
    "set_provider_slots",
]
