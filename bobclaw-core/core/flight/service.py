"""BoBClaw — Flight service (Lane 1a): process-wide store singleton + high-level ops.

A thin layer over :class:`core.flight.store.FlightStore` that the API (``/api/flights``),
the scheduler, and the TUI share: one lazily-inited store on ``config.FLIGHT_DB`` (init is
idempotent CREATE-IF-NOT-EXISTS, guarded once), plus ``create_flight`` (stamps ``created``)
and ``flight_detail`` (the Flight + its live L0.4 budget status).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from core.config import config
from core.flight.store import Flight, FlightStore
from core.flight.supervisor import budget_status

_store: Optional[FlightStore] = None
_init_lock = asyncio.Lock()
_inited = False


def get_flight_store() -> FlightStore:
    global _store
    if _store is None:
        _store = FlightStore(config.FLIGHT_DB)
    return _store


async def _ensure_inited() -> FlightStore:
    global _inited
    store = get_flight_store()
    if not _inited:
        async with _init_lock:
            if not _inited:
                await store.init()
                _inited = True
    return store


async def create_flight(
    flight_id: str,
    name: str,
    *,
    project: Optional[str] = None,
    budget_usd: Optional[float] = None,
    priority: int = 0,
) -> Flight:
    """Create a flight, stamping ``created`` = now (UTC). Raises ``ValueError`` on a
    duplicate id / invalid field (the API maps that to 400)."""
    store = await _ensure_inited()
    return await store.create(
        flight_id, name, created=datetime.now(timezone.utc).isoformat(),
        project=project, budget_usd=budget_usd, priority=priority,
    )


async def list_flights(*, status: Optional[str] = None) -> list[dict]:
    store = await _ensure_inited()
    return [f.to_dict() for f in await store.list(status=status)]


async def flight_detail(flight_id: str) -> Optional[dict]:
    """The Flight record + its live budget status (spend vs budget). None if absent."""
    store = await _ensure_inited()
    flight = await store.get(flight_id)
    if flight is None:
        return None
    detail = flight.to_dict()
    detail["budget"] = await budget_status(store, flight_id)
    return detail


async def update_flight(flight_id: str, **fields) -> Optional[dict]:
    store = await _ensure_inited()
    updated = await store.update(flight_id, **fields)
    return updated.to_dict() if updated else None


async def delete_flight(flight_id: str) -> bool:
    store = await _ensure_inited()
    return await store.delete(flight_id)


def _reset_for_tests() -> None:
    """Drop the singleton + init flag so a test can point at a fresh DB path."""
    global _store, _inited
    _store = None
    _inited = False


__all__ = [
    "get_flight_store",
    "create_flight",
    "list_flights",
    "flight_detail",
    "update_flight",
    "delete_flight",
]
