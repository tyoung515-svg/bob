"""BoBClaw Core — LKS git-DAG ledger primitives (Phase 1).

Pure, stdlib-only, JSON-I/O building blocks shared by the verification spine
(bidkey / erg / mergegate) and the durable ledger (budget / commits). No git,
filesystem, or corpus access — those are Phase 2.

The public surface is re-exported here once the impl modules land (assembly step).
"""
from __future__ import annotations

from core.ledger.types import (  # noqa: F401
    BID_NUMERIC_NDIGITS,
    EXHAUSTED_TAG,
    OVERSPEND_TRIGGER,
    RETRY_LIMIT,
    BoundaryKind,
    ClaimStatus,
    ErgAction,
    MergeDecision,
    RetryReason,
)
