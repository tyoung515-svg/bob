from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from core.ledger.mergegate import merge_decision, is_fast_forwardable
from core.ledger.types import MergeDecision, EXHAUSTED_TAG


@dataclass(frozen=True)
class Criterion:
    """A single termination criterion for a claim (bid_key)."""
    key: str
    verified: bool = False
    exhausted: bool = False
    tag: str = "U"

    def to_verdict(self) -> dict:
        """Convert to a verdict dict compatible with merge_decision."""
        return {"bid_key": self.key, "verified": self.verified, "exhausted": self.exhausted}


def default_fail_criteria(keys: Iterable[str]) -> list[Criterion]:
    """One Criterion per key, all starting unverified/unexhausted with tag 'U'."""
    return [Criterion(key=k) for k in keys]


def criterion_from_outcome(outcome: Mapping) -> Criterion:
    """Bridge from a GateOutcome.as_dict()-shaped mapping to a Criterion."""
    return Criterion(
        key=outcome["bid_key"],
        verified=bool(outcome["verified"]),
        exhausted=bool(outcome.get("exhausted", False)),
        tag=outcome.get("final_tag", "U"),
    )


def termination_decision(
    criteria: Iterable[Criterion], *, budget_escalated: bool = False
) -> dict:
    """Default-FAIL termination: any unverified, non-exhausted criterion → REVERT."""
    verdicts = [c.to_verdict() for c in criteria]
    return merge_decision(verdicts, budget_escalated)


def is_complete(
    criteria: Iterable[Criterion], *, budget_escalated: bool = False
) -> bool:
    """True iff every criterion is verified OR exhausted (empty set → False)."""
    verdicts = [c.to_verdict() for c in criteria]
    return is_fast_forwardable(verdicts, budget_escalated)


def could_not_verify(criteria: Iterable[Criterion]) -> list[Criterion]:
    """Return criteria that are NOT verified (U + exhausted known-unknowns)."""
    return [c for c in criteria if not c.verified]
