"""experiment.py — Experiment nodes + the §7.4 auto-budget gate (MS9-F6).

An **Experiment node** (SPEC-RESEARCH-FOREST §1) is a hypothesis whose evidence requires *running*
something. It carries ``{runner, config, est_cost}`` and links the hypothesis ``node_id`` it produces
evidence for. Whether an experiment may auto-run is decided by the **auto-budget** (SPEC §7.4):

  * **$2 EST per tree per epoch**, **$5 EST per day forest-wide** — a run whose projected spend stays
    at/under BOTH caps auto-proceeds;
  * a run whose projected spend crosses EITHER cap, OR a **state-mutating** experiment (regardless of
    cost), surfaces an **approval item of kind ``forest_experiment``** — proposal-only (mega-sprint
    invariant 14): NOTHING auto-applies; a human decides.

Budget is **derived from ledger truth** (SPEC §3 / COST-2): the spend so far is summed from the
program ledger's ``spend`` events (``core.forest.events.spend``), never a side counter. This module is
PURE + deterministic (mega-sprint invariant 13): NO clock, NO uuid, NO random, NO I/O. The caller
supplies the (already time/epoch-scoped) event lists; every id here is caller-supplied or content-
addressed. Actually *driving* an experiment (the testpipe-uplift adapter, §5) is I/O and lives in
``core.forest.adapters`` — this file only classifies + emits proposal artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Union

from core.forest.events import (
    ForestError,
    experiment_run as _ev_experiment_run,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APPROVAL_KIND = "forest_experiment"
SPEND_KIND = "spend"

# SPEC §7.4 ratified auto-budget (constants live here; the gate reads them so a test pins them).
PER_TREE_EPOCH_USD = 2.0
PER_DAY_FOREST_USD = 5.0

# Float boundary tolerance so a projected spend of *exactly* the cap counts as UNDER (the cap is an
# inclusive ceiling: "$2 per tree per epoch" allows a run that brings the total to exactly $2).
_EPS = 1e-9


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ExperimentError(ForestError):
    """Raised on invalid experiment-node construction or budget-gate misuse."""
    pass


def _is_number(v: Any) -> bool:
    # bool is an int subclass — a cost/amount must be a real number, never True/False.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_finite_number(v: Any) -> bool:
    # A budget amount must be a FINITE real number: NaN/inf would poison the projection (``nan > cap``
    # is always False → a NaN-cost run would silently bypass the auto-budget gate).
    return _is_number(v) and math.isfinite(v)


# ---------------------------------------------------------------------------
# Experiment node (SPEC §1: {runner, config, est_cost})
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExperimentNode:
    """A hypothesis node whose evidence requires RUNNING a ``runner`` for one ``config``.

    ``node_id`` is the hypothesis node this experiment produces evidence for; ``runner`` names the
    adapter (v0: ``testpipe_uplift``, SPEC §5); ``config`` is the runner's config (e.g. a testpipe
    ``SlotConfig`` as a dict); ``est_cost`` is the EST USD the run is expected to spend; a
    ``state_mutating`` experiment ALWAYS requires approval (SPEC §7.4), regardless of cost.
    """

    id: str
    node_id: str
    runner: str
    config: Mapping[str, Any] = field(default_factory=dict)
    est_cost: float = 0.0
    state_mutating: bool = False
    question: str = ""
    hypothesis: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ExperimentError("Experiment id must be a non-empty string")
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ExperimentError("Experiment node_id must be a non-empty string")
        if not isinstance(self.runner, str) or not self.runner.strip():
            raise ExperimentError("Experiment runner must be a non-empty string")
        if not _is_number(self.est_cost):
            raise ExperimentError(
                f"est_cost must be a numeric type (int or float), got {type(self.est_cost).__name__}"
            )
        if not math.isfinite(self.est_cost):
            # NaN/inf would poison the budget projection and silently bypass the auto-budget gate.
            raise ExperimentError(f"est_cost must be a finite number, got {self.est_cost}")
        if float(self.est_cost) < 0:
            raise ExperimentError(f"est_cost must be >= 0, got {self.est_cost}")
        if not isinstance(self.state_mutating, bool):
            raise ExperimentError("state_mutating must be a bool")
        if not isinstance(self.config, Mapping):
            raise ExperimentError("config must be a mapping (dict)")
        # Freeze the config as a plain dict so the frozen node is not aliased to a caller's mutable map.
        object.__setattr__(self, "config", dict(self.config))

    def to_dict(self) -> dict:
        """A JSON-serializable dict of the experiment node (SPEC §1 shape)."""
        return {
            "id": self.id,
            "node_id": self.node_id,
            "runner": self.runner,
            "config": dict(self.config),
            "est_cost": self.est_cost,
            "state_mutating": self.state_mutating,
            "question": self.question or None,
            "hypothesis": self.hypothesis or None,
        }


# ---------------------------------------------------------------------------
# Spend derivation from ledger truth (COST-2 — budget derives from ledger, not a side counter)
# ---------------------------------------------------------------------------
def spend_events(events: Sequence[Mapping[str, Any]]) -> list:
    """Filter *events* to the ``spend`` events (a ``spend`` event carries ``amount_usd``)."""
    return [
        e for e in (events or [])
        if isinstance(e, Mapping) and e.get("kind") == SPEND_KIND
    ]


def sum_spend(events: Sequence[Mapping[str, Any]]) -> float:
    """Sum ``amount_usd`` over the ``spend`` events in *events* (non-numeric amounts are skipped).

    This is the ledger-truth spend total — the budget gate uses it in place of any side counter.
    """
    total = 0.0
    for e in spend_events(events):
        v = e.get("amount_usd")
        # Skip non-finite amounts (NaN/inf) — one poisoned spend event must not corrupt the whole
        # projection (``total + nan`` → NaN → every downstream cap comparison silently fails open).
        if _is_finite_number(v):
            total += float(v)
    return round(total, 6)


def _ts_day(ts: Any) -> Any:
    """Extract a ``YYYY-MM-DD`` day key from an ISO-8601 ``ts`` string; else return ``ts`` unchanged.

    Tolerant + deterministic: an ISO date/datetime string ``"2026-07-08..."`` → ``"2026-07-08"``; any
    other ts shape (a non-string, or a string that is not date-prefixed) is returned as-is so callers
    that key days by a non-ISO ts still match exactly.
    """
    if isinstance(ts, str) and len(ts) >= 10 and ts[4:5] == "-" and ts[7:8] == "-":
        return ts[:10]
    return ts


def spend_in_epoch(events: Sequence[Mapping[str, Any]], epoch_id: Any) -> list:
    """The ``spend`` events tagged with ``epoch_id`` (a per-tree-per-epoch scope).

    A ``spend`` event scopes to an epoch by carrying an ``epoch_id`` field (stamped by the runner that
    emits it). Events with no / a different ``epoch_id`` are excluded.
    """
    return [e for e in spend_events(events) if e.get("epoch_id") == epoch_id]


def spend_on_day(events: Sequence[Mapping[str, Any]], day: Any) -> list:
    """The ``spend`` events on *day* (a forest-wide daily scope).

    Matches either an explicit ``day`` field on the event or the ISO date extracted from its ``ts``.
    """
    out = []
    for e in spend_events(events):
        if e.get("day") == day or _ts_day(e.get("ts")) == day:
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# Budget assessment (SPEC §7.4)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BudgetAssessment:
    """The classification of ONE experiment against the §7.4 auto-budget (pure, derived from spend)."""

    experiment_id: str
    node_id: str
    est_cost: float
    state_mutating: bool
    tree_epoch_spent: float
    day_forest_spent: float
    per_tree_epoch: float
    per_day_forest: float
    projected_tree_epoch: float
    projected_day_forest: float
    over_tree_epoch: bool
    over_day_forest: bool
    over_budget: bool
    requires_approval: bool
    decision: str  # "auto" | "approval"
    reasons: tuple = ()

    @property
    def auto(self) -> bool:
        return self.decision == "auto"

    def as_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "node_id": self.node_id,
            "est_cost": self.est_cost,
            "state_mutating": self.state_mutating,
            "tree_epoch_spent": self.tree_epoch_spent,
            "day_forest_spent": self.day_forest_spent,
            "per_tree_epoch": self.per_tree_epoch,
            "per_day_forest": self.per_day_forest,
            "projected_tree_epoch": self.projected_tree_epoch,
            "projected_day_forest": self.projected_day_forest,
            "over_tree_epoch": self.over_tree_epoch,
            "over_day_forest": self.over_day_forest,
            "over_budget": self.over_budget,
            "requires_approval": self.requires_approval,
            "decision": self.decision,
            "reasons": list(self.reasons),
        }


def assess_experiment(
    experiment: ExperimentNode,
    *,
    tree_epoch_events: Sequence[Mapping[str, Any]] = (),
    day_forest_events: Sequence[Mapping[str, Any]] = (),
    per_tree_epoch: float = PER_TREE_EPOCH_USD,
    per_day_forest: float = PER_DAY_FOREST_USD,
) -> BudgetAssessment:
    """Classify *experiment* against the §7.4 auto-budget — PURE, derived from ledger ``spend`` events.

    ``tree_epoch_events`` are the events scoped to THIS tree+epoch (use :func:`spend_in_epoch`);
    ``day_forest_events`` are the events scoped to THIS day across the forest (use :func:`spend_on_day`).
    The spend so far is summed from those event lists (NOT a side counter). The experiment auto-runs iff
    its projected spend stays at/under BOTH caps AND it is not state-mutating; otherwise it requires an
    approval item (SPEC §7.4).
    """
    if not isinstance(experiment, ExperimentNode):
        raise ExperimentError("assess_experiment requires an ExperimentNode")

    est = float(experiment.est_cost)
    te_spent = sum_spend(tree_epoch_events)
    df_spent = sum_spend(day_forest_events)
    proj_te = round(te_spent + est, 6)
    proj_df = round(df_spent + est, 6)

    over_te = proj_te > per_tree_epoch + _EPS
    over_df = proj_df > per_day_forest + _EPS
    over_budget = over_te or over_df
    requires_approval = over_budget or bool(experiment.state_mutating)

    reasons: list[str] = []
    if experiment.state_mutating:
        reasons.append("state-mutating experiment ALWAYS requires approval (SPEC §7.4)")
    if over_te:
        reasons.append(
            f"projected tree/epoch spend ${proj_te} exceeds ${per_tree_epoch} cap (spent ${te_spent} "
            f"+ est ${est})"
        )
    if over_df:
        reasons.append(
            f"projected forest/day spend ${proj_df} exceeds ${per_day_forest} cap (spent ${df_spent} "
            f"+ est ${est})"
        )
    if not requires_approval:
        reasons.append(
            f"under budget (tree/epoch ${proj_te} <= ${per_tree_epoch}; forest/day ${proj_df} <= "
            f"${per_day_forest}) and non-mutating: auto-run"
        )

    return BudgetAssessment(
        experiment_id=experiment.id,
        node_id=experiment.node_id,
        est_cost=est,
        state_mutating=bool(experiment.state_mutating),
        tree_epoch_spent=te_spent,
        day_forest_spent=df_spent,
        per_tree_epoch=float(per_tree_epoch),
        per_day_forest=float(per_day_forest),
        projected_tree_epoch=proj_te,
        projected_day_forest=proj_df,
        over_tree_epoch=over_te,
        over_day_forest=over_df,
        over_budget=over_budget,
        requires_approval=requires_approval,
        decision="approval" if requires_approval else "auto",
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# The approval item (proposal-only — inv. 14) + the gate
# ---------------------------------------------------------------------------
def experiment_approval_item(experiment: ExperimentNode, assessment: BudgetAssessment) -> dict:
    """Build the gateway ``forest_experiment`` approval item (``action_type`` + ``details`` JSONB).

    Mirrors the fork proposal's :meth:`ForkProposal.approval_item` shape and the gateway approvals
    surface. Proposal-only: it is surfaced for a human decision; NOTHING is applied until the decision
    is 'approve'. The ``details`` carry the budget math so the pane can explain *why* approval is
    needed.
    """
    return {
        "action_type": APPROVAL_KIND,
        "proposal_only": True,
        "details": {
            "experiment_id": experiment.id,
            "node_id": experiment.node_id,
            "runner": experiment.runner,
            "config": dict(experiment.config),
            "est_cost": experiment.est_cost,
            "state_mutating": experiment.state_mutating,
            "reason": "; ".join(assessment.reasons),
            "tree_epoch_spent": assessment.tree_epoch_spent,
            "day_forest_spent": assessment.day_forest_spent,
            "projected_tree_epoch": assessment.projected_tree_epoch,
            "projected_day_forest": assessment.projected_day_forest,
            "per_tree_epoch": assessment.per_tree_epoch,
            "per_day_forest": assessment.per_day_forest,
            "question": experiment.question or None,
        },
    }


@dataclass(frozen=True)
class ExperimentGate:
    """The decision for one experiment: its :class:`BudgetAssessment` + (when gated) an approval item.

    Proposal-only (inv. 14): holding a gate actuates NOTHING. When ``requires_approval`` the
    ``approval_item`` is populated (surface it); otherwise it is ``None`` and the experiment may auto-run.
    """

    experiment: ExperimentNode
    assessment: BudgetAssessment
    approval_item: Optional[dict] = None

    @property
    def requires_approval(self) -> bool:
        return self.assessment.requires_approval

    @property
    def auto(self) -> bool:
        return self.assessment.auto


def gate_experiment(
    experiment: ExperimentNode,
    *,
    tree_epoch_events: Sequence[Mapping[str, Any]] = (),
    day_forest_events: Sequence[Mapping[str, Any]] = (),
    per_tree_epoch: float = PER_TREE_EPOCH_USD,
    per_day_forest: float = PER_DAY_FOREST_USD,
) -> ExperimentGate:
    """Gate *experiment* against the auto-budget → an :class:`ExperimentGate` (actuates NOTHING).

    Under budget + non-mutating ⇒ ``auto`` (``approval_item is None``). Over budget OR state-mutating ⇒
    an approval item of kind ``forest_experiment`` (proposal-only). The caller runs the experiment ONLY
    on ``gate.auto`` (or after a human approves the item) — this function never runs it.
    """
    assessment = assess_experiment(
        experiment,
        tree_epoch_events=tree_epoch_events,
        day_forest_events=day_forest_events,
        per_tree_epoch=per_tree_epoch,
        per_day_forest=per_day_forest,
    )
    item = experiment_approval_item(experiment, assessment) if assessment.requires_approval else None
    return ExperimentGate(experiment=experiment, assessment=assessment, approval_item=item)


def experiment_run_event(
    experiment: ExperimentNode,
    *,
    ts: Any,
    result: Optional[Any] = None,
    event_id: Optional[str] = None,
    **extra,
) -> dict:
    """Convenience: the ``experiment_run`` ledger event for *experiment* (wraps ``events.experiment_run``).

    Carries the node's ``runner`` / ``config`` / ``est_cost``; ``result`` is the runner's structured
    outcome (e.g. the uplift summary). Deterministic — ``ts`` is caller-supplied.
    """
    return _ev_experiment_run(
        experiment_id=experiment.id,
        node_id=experiment.node_id,
        runner=experiment.runner,
        ts=ts,
        config=dict(experiment.config),
        est_cost=experiment.est_cost,
        result=result,
        event_id=event_id,
        **extra,
    )


def experiment_id_for(node_id: str, runner: str, config: Mapping[str, Any]) -> str:
    """A deterministic, content-addressed experiment id (no uuid/clock) over its defining triple."""
    canonical = json.dumps(
        {"node_id": node_id, "runner": runner, "config": dict(config)},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return "exp:" + digest


__all__ = [
    "APPROVAL_KIND",
    "SPEND_KIND",
    "PER_TREE_EPOCH_USD",
    "PER_DAY_FOREST_USD",
    "ExperimentError",
    "ExperimentNode",
    "spend_events",
    "sum_spend",
    "spend_in_epoch",
    "spend_on_day",
    "BudgetAssessment",
    "assess_experiment",
    "experiment_approval_item",
    "ExperimentGate",
    "gate_experiment",
    "experiment_run_event",
    "experiment_id_for",
]
