"""BoBClaw — Flight supervisor (Lane 1a): the control plane.

Makes "two mega-sprints at once" a real, isolated, budgeted primitive:
  * ``store`` — the durable Flight registry (``FlightStore``, SQLite).
  * ``supervisor`` — budget enforcement (over the L0.4 spend meter) + the GLM-serial
    fair-share provider scheduler + priority preemption.

Consumes Layer 0: a Flight's id IS the ``flight_id`` threaded through the substrate;
its budget is enforced against ``core.telemetry.spend.flight_spend``.
"""
from __future__ import annotations

from core.flight.store import (
    Flight,
    FlightStore,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_PAUSED,
)

__all__ = [
    "Flight",
    "FlightStore",
    "STATUS_ACTIVE",
    "STATUS_PAUSED",
    "STATUS_DONE",
    "STATUS_CANCELLED",
]
