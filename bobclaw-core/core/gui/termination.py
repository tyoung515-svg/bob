from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from enum import Enum
from typing import Awaitable, Callable, Iterable, Optional, Sequence  # noqa: F401 (kept for completeness)

from core.verify.termination import Criterion, is_complete, termination_decision
from core.ledger.types import EXHAUSTED_TAG
from core.verify.postcondition import (
    verify_post_condition,
    PostConditionResult,
    decorrelated_critic_backend,
    is_decorrelated,
    family_of,
    _run_blocking,
)
from core.gui.recovery import RecoveryDirective, RecoveryDecision
from core.gui.grounders.holo import HOLO_BACKEND
from core.gui.types import Subgoal


class CriterionSource(str, Enum):
    """Source of verification for a run-level criterion."""
    GRADER = "grader"
    EXTERNAL = "external"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class GuiCriterion:
    """A run-level GUI completion criterion (Default-FAIL).

    Starts unverified; the run-level evaluator requires it verified (by ≥1 independent
    source) OR exhausted-tagged before the run FAST_FORWARDs.
    """
    key: str
    subgoal: str
    verified: bool = False
    exhausted: bool = False
    tag: str = "U"
    sources: tuple[str, ...] = ()
    reason: str = ""

    def to_criterion(self) -> Criterion:
        """Project to the MS-3 Criterion the gate decides on."""
        return Criterion(key=self.key, verified=self.verified,
                         exhausted=self.exhausted, tag=self.tag)


# ---------------------------------------------------------------------------
# Builders + run-level evaluator (COMPOSE MS-3 core/verify/termination.py)
# ---------------------------------------------------------------------------

def criteria_for_subgoals(subgoals: Iterable[Subgoal | str]) -> list[GuiCriterion]:
    """One GuiCriterion per subgoal, all starting unverified/unexhausted/tag='U'.

    key = the subgoal text (a Subgoal's .text attribute, else str(subgoal)).
    A duplicate text is disambiguated with a deterministic "#<idx>" suffix
    (idx = its zero‑based position among duplicates).
    """
    result: list[GuiCriterion] = []
    seen: dict[str, int] = {}  # text -> count of occurrences so far
    for item in subgoals:
        raw_key = item.text if isinstance(item, Subgoal) else str(item)
        if raw_key in seen:
            seen[raw_key] += 1
            key = f"{raw_key}#{seen[raw_key]}"
        else:
            seen[raw_key] = 0
            key = raw_key
        result.append(GuiCriterion(key=key, subgoal=raw_key))
    return result


def _sanitize_source(source: CriterionSource | str) -> str:
    """Return the string representation for a source."""
    return source.value if isinstance(source, CriterionSource) else str(source)


def mark_verified(
    criteria: list[GuiCriterion],
    key: str,
    *,
    source: CriterionSource | str,
    reason: str = "",
) -> list[GuiCriterion]:
    """Return a new list with the matching criterion verified=True and source appended.

    If the source is already present, it is not duplicated.
    An unknown key returns the list unchanged.  Total, never raises.
    """
    new_criteria: list[GuiCriterion] = []
    source_str = _sanitize_source(source)
    for c in criteria:
        if c.key == key:
            new_sources = c.sources + (source_str,) if source_str not in c.sources else c.sources
            new_c = replace(
                c,
                verified=True,
                sources=new_sources,
                reason=reason if reason else c.reason,
            )
            new_criteria.append(new_c)
        else:
            new_criteria.append(c)
    return new_criteria


def mark_exhausted(
    criteria: list[GuiCriterion],
    key: str,
    *,
    reason: str = "",
    tag: str = EXHAUSTED_TAG,
) -> list[GuiCriterion]:
    """Return a new list with the matching criterion exhausted=True, tag=tag,
    and RECOVERY source appended (verified STAYS False).

    An unknown key returns the list unchanged.  Total, never raises.
    """
    new_criteria: list[GuiCriterion] = []
    source_str = CriterionSource.RECOVERY.value
    for c in criteria:
        if c.key == key:
            new_sources = c.sources + (source_str,) if source_str not in c.sources else c.sources
            new_c = replace(
                c,
                verified=False,
                exhausted=True,
                tag=tag,
                sources=new_sources,
                reason=reason if reason else c.reason,
            )
            new_criteria.append(new_c)
        else:
            new_criteria.append(c)
    return new_criteria


def apply_recovery(
    criteria: list[GuiCriterion],
    key: str,
    directive: RecoveryDirective,
    *,
    reason: str = "",
) -> list[GuiCriterion]:
    """Bridge G7: if directive.decision == SURFACE and either
    directive.status_tag == EXHAUSTED_TAG or directive.surfaced,
    call mark_exhausted; otherwise return unchanged.

    Total, never raises.
    """
    if directive.decision is RecoveryDecision.SURFACE and (
        directive.status_tag == EXHAUSTED_TAG or directive.surfaced
    ):
        return mark_exhausted(
            criteria,
            key,
            reason=reason or directive.reason,
            tag=directive.status_tag or EXHAUSTED_TAG,
        )
    return criteria  # unchanged


def is_run_complete(
    criteria: list[GuiCriterion],
    *,
    budget_escalated: bool = False,
) -> bool:
    """Delegate to MS-3 is_complete over projected Criteria.

    Empty set → False (Default‑FAIL).
    """
    return is_complete(
        [c.to_criterion() for c in criteria],
        budget_escalated=budget_escalated,
    )


def run_decision(
    criteria: list[GuiCriterion],
    *,
    budget_escalated: bool = False,
) -> dict:
    """Delegate to MS-3 termination_decision."""
    return termination_decision(
        [c.to_criterion() for c in criteria],
        budget_escalated=budget_escalated,
    )


def could_not_verify(criteria: list[GuiCriterion]) -> list[GuiCriterion]:
    """Return the not‑verified GuiCriterions (pending‑U AND exhausted).

    Mirrors MS-3 could_not_verify semantics.  Surfaced, never dropped.
    """
    return [c for c in criteria if not c.verified]


def pending(criteria: list[GuiCriterion]) -> list[GuiCriterion]:
    """Return the unverified AND not‑exhausted criteria (active Default‑FAIL blockers)."""
    return [c for c in criteria if not c.verified and not c.exhausted]


# ---------------------------------------------------------------------------
# Fresh‑context DECORRELATED grader (COMPOSE MS-2 verify_post_condition)
# ---------------------------------------------------------------------------

async def grade_criterion(
    *,
    subgoal: str,
    result_state: str,
    actor_backend: str = HOLO_BACKEND,
    team: str | None = None,
    critic_backend: str | None = None,
    send=None,
) -> tuple[bool, PostConditionResult]:
    """Verify a subgoal's post‑condition with a decorrelated cross‑family critic.

    Delegates to verify_post_condition with FRESH context (never the actor's reasoning).
    Returns (res.passed, res).  Fail‑safe: violated/unknown/unreachable → passed False.
    """
    res = await verify_post_condition(
        step=f"GUI subgoal attempted: {subgoal}",
        statement=subgoal,
        result=result_state,
        actor_backend=actor_backend,
        team=team,
        critic_backend=critic_backend,
        send=send,
    )
    return (res.passed, res)


def make_grader(
    *,
    actor_backend: str = HOLO_BACKEND,
    team: str | None = None,
    critic_backend: str | None = None,
    send=None,
) -> Callable[[str, str], bool]:
    """SYNC adapter: grade(subgoal, result_state) -> bool.

    Uses _run_blocking (loop‑safe).  Fail‑safe: any error / cancellation → False.
    """
    def grade(subgoal: str, result_state: str) -> bool:
        try:
            passed, _ = _run_blocking(
                lambda: grade_criterion(
                    subgoal=subgoal,
                    result_state=result_state,
                    actor_backend=actor_backend,
                    team=team,
                    critic_backend=critic_backend,
                    send=send,
                )
            )
            return passed
        except asyncio.CancelledError:
            return False
        except Exception:  # noqa: BLE001 – fail‑safe, never auto‑verify
            return False
    return grade
