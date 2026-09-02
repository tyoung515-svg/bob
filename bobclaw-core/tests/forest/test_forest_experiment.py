"""MS9-F6 — tests for core.forest.experiment (experiment nodes + §7.4 auto-budget gate).

Pins the F6 accept criterion #1 (budget math unit-proven), derived from ledger ``spend`` events:
  * a run UNDER both §7.4 caps + non-mutating auto-runs;
  * a run ABOVE the tree/epoch cap OR the forest/day cap emits a ``forest_experiment`` approval item;
  * a state-mutating experiment ALWAYS emits an approval item, regardless of cost;
  * the spend so far is SUMMED from ``spend`` events (COST-2), never a side counter;
  * inv. 14 (proposal-only): the gate/approval item actuates NOTHING.
"""
from __future__ import annotations

import pytest

from core.forest import events as fe
from core.forest.experiment import (
    APPROVAL_KIND,
    PER_DAY_FOREST_USD,
    PER_TREE_EPOCH_USD,
    BudgetAssessment,
    ExperimentError,
    ExperimentGate,
    ExperimentNode,
    assess_experiment,
    experiment_approval_item,
    experiment_id_for,
    experiment_run_event,
    gate_experiment,
    spend_in_epoch,
    spend_on_day,
    sum_spend,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _exp(est_cost=0.5, *, state_mutating=False, node_id="h1", eid="exp:1"):
    return ExperimentNode(
        id=eid, node_id=node_id, runner="testpipe_uplift",
        config={"name": "B0", "worker": "deepseek_v4_flash"},
        est_cost=est_cost, state_mutating=state_mutating,
    )


def _spend(amount, *, epoch_id=None, day=None, ts="2026-07-08T00:00:00"):
    extra = {}
    if epoch_id is not None:
        extra["epoch_id"] = epoch_id
    if day is not None:
        extra["day"] = day
    return fe.spend(amount_usd=amount, label="prior", ts=ts, est=True, node_id="h1", **extra)


# ---------------------------------------------------------------------------
# Node schema + validation (SPEC §1)
# ---------------------------------------------------------------------------
def test_experiment_node_carries_runner_config_est_cost():
    n = _exp(est_cost=1.25)
    assert n.runner == "testpipe_uplift"
    assert n.config["worker"] == "deepseek_v4_flash"
    assert n.est_cost == 1.25
    assert n.state_mutating is False
    d = n.to_dict()
    assert d["runner"] == "testpipe_uplift" and d["est_cost"] == 1.25


@pytest.mark.parametrize("bad", [
    dict(id=""), dict(node_id=""), dict(runner=""),
    dict(est_cost="1.0"), dict(est_cost=True), dict(est_cost=-0.5),
    dict(state_mutating="yes"),
])
def test_experiment_node_rejects_bad_fields(bad):
    kw = dict(id="e", node_id="h1", runner="r", config={}, est_cost=0.1)
    kw.update(bad)
    with pytest.raises(ExperimentError):
        ExperimentNode(**kw)


@pytest.mark.parametrize("bad_cost", [float("nan"), float("inf"), float("-inf")])
def test_experiment_node_rejects_non_finite_est_cost(bad_cost):
    # audit F6-r1 (HIGH): a NaN/inf cost would make ``nan > cap`` always False → silently bypass the
    # auto-budget gate. Validation must reject it up front.
    with pytest.raises(ExperimentError):
        ExperimentNode(id="e", node_id="h1", runner="r", config={}, est_cost=bad_cost)


def test_sum_spend_skips_non_finite_amounts():
    # audit F6-r1 (HIGH): one poisoned spend event must not corrupt the whole projection.
    events = [_spend(0.5), _spend(float("nan")), _spend(float("inf")), _spend(0.7)]
    assert sum_spend(events) == pytest.approx(1.2)  # NaN/inf skipped, not poisoning the total


def test_experiment_node_config_is_defensively_copied():
    cfg = {"name": "B0"}
    n = ExperimentNode(id="e", node_id="h1", runner="r", config=cfg, est_cost=0.0)
    cfg["name"] = "MUTATED"
    assert n.config["name"] == "B0"  # frozen node not aliased to the caller's dict


# ---------------------------------------------------------------------------
# Spend derivation from ledger truth (COST-2)
# ---------------------------------------------------------------------------
def test_sum_spend_derives_from_spend_events_only():
    events = [
        fe.measurement(node_id="h1", metric="pass_at_1", value=0.4, source="tp", ts=1),
        _spend(0.3), _spend(0.2),
        fe.experiment_run(experiment_id="x", node_id="h1", runner="testpipe_uplift", ts=2),
    ]
    # only the two spend events count; measurement/experiment_run ignored
    assert sum_spend(events) == 0.5


def test_spend_scoping_by_epoch_and_day():
    events = [
        _spend(0.5, epoch_id="ep1", day="2026-07-08", ts="2026-07-08T01:00:00"),
        _spend(0.7, epoch_id="ep2", day="2026-07-08", ts="2026-07-08T02:00:00"),
        _spend(0.9, epoch_id="ep1", day="2026-07-09", ts="2026-07-09T01:00:00"),
    ]
    assert sum_spend(spend_in_epoch(events, "ep1")) == pytest.approx(1.4)   # 0.5 + 0.9
    assert sum_spend(spend_on_day(events, "2026-07-08")) == pytest.approx(1.2)  # 0.5 + 0.7


def test_spend_on_day_extracts_iso_day_from_ts_without_explicit_day():
    events = [_spend(0.4, ts="2026-07-08T09:30:00"), _spend(0.6, ts="2026-07-09T09:30:00")]
    assert sum_spend(spend_on_day(events, "2026-07-08")) == 0.4


# ---------------------------------------------------------------------------
# Accept #1 — budget math (auto under cap; approval above cap; mutating always approval)
# ---------------------------------------------------------------------------
def test_auto_run_under_both_caps():
    # $1.4 spent this tree/epoch + $0.5 est = $1.9 <= $2 cap; forest/day $1.9 <= $5 → auto.
    prior = [_spend(1.4, epoch_id="ep1", day="2026-07-08")]
    a = assess_experiment(_exp(0.5), tree_epoch_events=spend_in_epoch(prior, "ep1"),
                          day_forest_events=spend_on_day(prior, "2026-07-08"))
    assert a.decision == "auto" and a.auto is True
    assert a.requires_approval is False and a.over_budget is False
    assert a.projected_tree_epoch == pytest.approx(1.9)
    gate = gate_experiment(_exp(0.5), tree_epoch_events=spend_in_epoch(prior, "ep1"),
                           day_forest_events=spend_on_day(prior, "2026-07-08"))
    assert gate.auto is True and gate.approval_item is None  # inv. 14: nothing to actuate


def test_exactly_at_tree_epoch_cap_is_auto():
    # $1.5 + $0.5 = exactly $2.0 → inclusive ceiling, still auto.
    prior = [_spend(1.5, epoch_id="ep1", day="2026-07-08")]
    a = assess_experiment(_exp(0.5), tree_epoch_events=spend_in_epoch(prior, "ep1"),
                          day_forest_events=spend_on_day(prior, "2026-07-08"))
    assert a.projected_tree_epoch == pytest.approx(2.0)
    assert a.decision == "auto" and a.over_tree_epoch is False


def test_above_tree_epoch_cap_requires_approval():
    # $1.8 + $0.5 = $2.3 > $2 tree/epoch cap → approval, even though forest/day ($2.3) is under $5.
    prior = [_spend(1.8, epoch_id="ep1", day="2026-07-08")]
    exp = _exp(0.5)
    a = assess_experiment(exp, tree_epoch_events=spend_in_epoch(prior, "ep1"),
                          day_forest_events=spend_on_day(prior, "2026-07-08"))
    assert a.decision == "approval" and a.over_tree_epoch is True and a.over_day_forest is False
    gate = gate_experiment(exp, tree_epoch_events=spend_in_epoch(prior, "ep1"),
                           day_forest_events=spend_on_day(prior, "2026-07-08"))
    assert gate.requires_approval is True
    item = gate.approval_item
    assert item["action_type"] == APPROVAL_KIND and item["proposal_only"] is True
    assert item["details"]["est_cost"] == 0.5
    assert "exceeds" in item["details"]["reason"]


def test_above_forest_day_cap_requires_approval_even_when_tree_epoch_ok():
    # tree/epoch fine ($0.5) but forest-wide day already $4.8 + $0.5 = $5.3 > $5 → approval.
    tree_epoch = []  # nothing spent in this tree/epoch yet
    day_forest = [_spend(4.8, day="2026-07-08")]
    exp = _exp(0.5)
    a = assess_experiment(exp, tree_epoch_events=tree_epoch,
                          day_forest_events=spend_on_day(day_forest, "2026-07-08"))
    assert a.decision == "approval" and a.over_day_forest is True and a.over_tree_epoch is False
    assert a.projected_day_forest == pytest.approx(5.3)


def test_state_mutating_always_requires_approval_regardless_of_cost():
    exp = _exp(est_cost=0.0, state_mutating=True)  # zero cost, but state-mutating
    a = assess_experiment(exp, tree_epoch_events=[], day_forest_events=[])
    assert a.decision == "approval" and a.requires_approval is True
    assert a.over_budget is False  # not a budget breach — the mutation is the reason
    gate = gate_experiment(exp)
    assert gate.approval_item["action_type"] == APPROVAL_KIND
    assert any("state-mutating" in r for r in a.reasons)
    assert gate.approval_item["details"]["state_mutating"] is True


def test_caps_match_spec_7_4_defaults():
    assert PER_TREE_EPOCH_USD == 2.0
    assert PER_DAY_FOREST_USD == 5.0


def test_approval_item_carries_budget_math():
    prior = [_spend(1.9, epoch_id="ep1", day="2026-07-08")]
    exp = _exp(0.5)
    a = assess_experiment(exp, tree_epoch_events=spend_in_epoch(prior, "ep1"),
                          day_forest_events=spend_on_day(prior, "2026-07-08"))
    item = experiment_approval_item(exp, a)
    d = item["details"]
    assert d["per_tree_epoch"] == 2.0 and d["per_day_forest"] == 5.0
    assert d["tree_epoch_spent"] == pytest.approx(1.9)
    assert d["projected_tree_epoch"] == pytest.approx(2.4)
    assert d["runner"] == "testpipe_uplift" and d["config"]["name"] == "B0"


# ---------------------------------------------------------------------------
# experiment_run event + deterministic id
# ---------------------------------------------------------------------------
def test_experiment_run_event_is_well_formed_and_validates():
    exp = _exp(0.5)
    ev = experiment_run_event(exp, ts=7, result={"pass_at_1": 0.6})
    fe.validate_event(ev)  # raises if malformed
    assert ev["kind"] == "experiment_run" and ev["runner"] == "testpipe_uplift"
    assert ev["result"]["pass_at_1"] == 0.6 and ev["est_cost"] == 0.5


def test_experiment_id_for_is_deterministic():
    a = experiment_id_for("h1", "testpipe_uplift", {"name": "B0"})
    b = experiment_id_for("h1", "testpipe_uplift", {"name": "B0"})
    c = experiment_id_for("h1", "testpipe_uplift", {"name": "B1"})
    assert a == b and a != c and a.startswith("exp:")
