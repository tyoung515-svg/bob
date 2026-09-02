"""BoBClaw test-pipe (Phase-1 scaffold, MS5-P1 — BUILD ONLY).

Measures the WHOLE BoB pipeline on a code benchmark and attributes the uplift to
individual slots (front / worker / audit / shape) — see ``tasks/2026-07-02-test-pipe/
SPEC.md``. This package is the network-free SCAFFOLD: two spines (build/council), swept
slots, the honesty fence (hidden tests never enter a prompt), chunk+resume+aggregate,
the NEW in-loop LLM audit, the external-scorer bridge, the sweep runner + results ledger.

SCOPE FENCE (MS5-P1): BUILD ONLY. No live benchmark sweep, no live dataset download, no
spend wiring, no merge — those (SPEC §10 acceptance: B0-floor / uplift-triple / real-cost
capture) require the LIVE sweep = Travis's, deferred. This package proves the PLUMBING.

Additive by construction: it reuses the existing build-pipeline building blocks and the
council nodes (by reference), and touches NO existing module — so ``audit=off`` is
byte-identical to today and the core suite is unchanged.
"""
from __future__ import annotations

from core.testpipe.types import (  # noqa: F401
    AUDIT_OFF,
    ConfigResult,
    ItemResult,
    SlotConfig,
    TestItem,
)
