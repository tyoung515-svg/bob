"""BoBClaw — Flight substrate telemetry package (Layer 0).

The data plane of the flight substrate: flight identity (``flight``), the emit
layer (``emit``, added in L0.2), and the per-flight spend meter (``spend``, L0.4).

Import surface is kept flat so call sites read ``from core.telemetry.flight import
resolve_flight_id`` etc. — no heavy imports at package load (emit/spend pull Redis
lazily so a pure unit test of a node never spins a client).
"""
from __future__ import annotations

from core.telemetry.flight import (
    AMBIENT_FLIGHT,
    chat_flight_id,
    is_ambient,
    resolve_flight_id,
)
from core.telemetry.spend import (
    flight_spend,
    record_flight_spend,
    reset_flight_spend,
)

__all__ = [
    "AMBIENT_FLIGHT",
    "chat_flight_id",
    "is_ambient",
    "resolve_flight_id",
    "flight_spend",
    "record_flight_spend",
    "reset_flight_spend",
]
