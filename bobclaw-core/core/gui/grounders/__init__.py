"""GUI grounding-head adapters (MS2-G4).

Concrete :class:`~core.gui.loop.Grounder` implementations backed by a real model head.
The first head is Holo-3.1 (served on local llama.cpp, the OD#1-resolved Q4 quant); the
adapter is head-agnostic — the model/quant live in :class:`~core.gui.grounders.holo.HoloClient`
so the head is swappable behind the same Protocol.
"""
from __future__ import annotations

from core.gui.grounders.holo import (
    HOLO_BACKEND,
    GroundOutcome,
    HoloClient,
    HoloError,
    HoloGrounder,
    parse_coord,
)

__all__ = [
    "HOLO_BACKEND",
    "GroundOutcome",
    "HoloClient",
    "HoloError",
    "HoloGrounder",
    "parse_coord",
]
