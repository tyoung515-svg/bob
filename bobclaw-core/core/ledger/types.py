"""BoBClaw Core — LKS git-DAG ledger: shared types (Phase 1, manager-authored contract).

The single source of truth every ``core.ledger.*`` primitive imports from. PURE: enums
and constants only, no I/O, no state. All enums subclass ``str`` so values are
JSON-serializable verbatim (``RetryReason.STALE_SOURCE`` serializes as ``"STALE_SOURCE"``).

Grounded in the unified architecture spec: §2.6 (Externalized Retry-Gate),
§2.7 F2/F3 (threshold-gated budget / action tiers), §2.9 (git-DAG + BIND-01/02).
"""
from __future__ import annotations

from enum import Enum

# ── Constants (tunable defaults; see CONTRACTS.md) ───────────────────────────────
RETRY_LIMIT: int = 2
"""ERG re-branch ceiling: retry_count < RETRY_LIMIT re-branches, >= gives up."""

OVERSPEND_TRIGGER: float = 1.5
"""Branch overspend escalation ratio (§2.9 F2, '~150%')."""

BID_NUMERIC_NDIGITS: int = 4
"""Significant figures for numeric claim identity (so 80.40 == 80.4)."""

EXHAUSTED_TAG: str = "[UNVERIFIED: EXHAUSTED_SEARCH]"
"""First-class 'known unknown' tag committed past the retry limit (never a silent drop)."""


class RetryReason(str, Enum):
    """Bounded typed-reason channel (§2.6, Decision 1). The critic may attach ONE of these
    enum codes; free text is rejected at the firewall to avoid cross-firewall bias."""

    TEMPORAL_SCOPE_MISMATCH = "TEMPORAL_SCOPE_MISMATCH"
    WRONG_ENTITY = "WRONG_ENTITY"
    STALE_SOURCE = "STALE_SOURCE"
    NUMERIC_MISMATCH = "NUMERIC_MISMATCH"
    UNSUPPORTED = "UNSUPPORTED"


class ErgAction(str, Enum):
    """The Externalized Retry-Gate's decision for a rejected claim."""

    RE_BRANCH = "RE_BRANCH"
    EXHAUSTED_SEARCH = "EXHAUSTED_SEARCH"


class ClaimStatus(str, Enum):
    """Lifecycle of a claim's verification state in the gate metadata."""

    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    UNVERIFIED_EXHAUSTED = "UNVERIFIED_EXHAUSTED"


class MergeDecision(str, Enum):
    """The merge-gate verdict (§2.9 'verification at the merge gate')."""

    FAST_FORWARD = "FAST_FORWARD"
    REVERT = "REVERT"
    ESCALATE = "ESCALATE"


class BoundaryKind(str, Enum):
    """Commit boundaries (§2.9 discipline: commit at meaningful boundaries, squash micro-steps).
    Every kind is committable EXCEPT ``TOOL_CALL`` (the over-granular micro-step)."""

    BRANCH_START = "BRANCH_START"
    ARTIFACT_COMPLETE = "ARTIFACT_COMPLETE"
    MERGE = "MERGE"
    CORRECTION = "CORRECTION"
    TOOL_CALL = "TOOL_CALL"
