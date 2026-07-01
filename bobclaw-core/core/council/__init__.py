"""
BoBClaw Core — CoCouncil package.

Self-contained, backend-injected multi-voice deliberation engine (CoCouncil
P1a). Ported from ForestOS/CoCouncilHub. Pure stdlib; no Bob coupling. Bob
integration (graph wiring, real backends, fusion panel node, face) is P1b.
"""
from __future__ import annotations

from core.council.engine import (
    BackendFn,
    CostFn,
    CouncilEngine,
    CouncilHandoff,
    CouncilSession,
    CouncilVoice,
)
from core.council.protocol import (
    DEFAULT_PROTOCOL_PATH,
    load_protocols,
)

__all__ = [
    "BackendFn",
    "CostFn",
    "CouncilEngine",
    "CouncilHandoff",
    "CouncilSession",
    "CouncilVoice",
    "DEFAULT_PROTOCOL_PATH",
    "load_protocols",
]
