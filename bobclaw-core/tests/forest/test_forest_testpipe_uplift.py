"""MS9-F6 — tests for core.forest.adapters.testpipe_uplift (the seed uplift runner, SPEC §5).

Pins the F6 accept criterion #2 (adapter fixture-run emits well-formed uplift ``measurement`` events),
OFFLINE + deterministic via a MOCKED testpipe ``run_one`` (inv. 13 — no live model calls here; the ONE
live run is separate). Also proves:
  * the emitted events (measurement + spend + experiment_run) all validate + append to an F1 store;
  * a baseline yields ``uplift`` + ``uplift_per_dollar`` measurements;
  * the gated entry point auto-runs UNDER budget and does NOT run (proposal-only) ABOVE budget;
  * the emitted ``pass_at_1`` stream feeds F5 measurement-entailment (the forest→verify wiring).
"""
from __future__ import annotations

import pytest

from core.forest import events as fe
from core.forest.adapters.testpipe_uplift import (
    METRIC_COST_USD,
    METRIC_PASS_AT_1,
    METRIC_UPLIFT,
    METRIC_UPLIFT_PER_DOLLAR,
    METRIC_VISIBLE_RATE,
    RUNNER_NAME,
    measurements_from_config,
    run_config,
    run_experiment_gated,
    run_uplift_experiment,
    slot_config_from,
)
from core.forest.experiment import ExperimentNode, spend_in_epoch, spend_on_day
from core.forest.store import create_program
from core.testpipe.fixtures import fixture_items
from core.testpipe.types import ConfigResult, ItemResult, SlotConfig

# asyncio_mode = auto (pytest.ini) — bare ``async def test_*`` are collected + run.


# ---------------------------------------------------------------------------
# mock run_one — a synthetic testpipe spine (offline, deterministic)
# ---------------------------------------------------------------------------
def make_mock_run_one(*, pass_ratio=1.0, cost_per_item=0.02, tokens_per_item=200, calls=None):
    """A run_one that passes the first ``pass_ratio`` fraction of items (by stable order)."""
    seen = {}

    async def run_one(config: SlotConfig, item):
        if calls is not None:
            calls.append((config.name, item.id))
        idx = seen.setdefault(item.id, len(seen))
        # deterministic: pass items whose index is within the pass fraction
        total = len(fixture_items())
        passed = idx < round(pass_ratio * total)
        return ItemResult(
            item_id=item.id, config_name=config.name,
            hidden_pass=passed, visible_pass=passed,
            cost_usd=cost_per_item, tokens=tokens_per_item,
            provenance=config.provenance(),
        )
    return run_one


def _exp(est_cost=0.1, *, state_mutating=False, worker="deepseek_v4_flash"):
    return ExperimentNode(
        id="exp:uplift1", node_id="h_uplift", runner=RUNNER_NAME,
        config={"name": "B0", "worker": worker, "audit": "off", "repair": 0},
        est_cost=est_cost, state_mutating=state_mutating,
    )


# ---------------------------------------------------------------------------
# slot_config coercion
# ---------------------------------------------------------------------------
def test_slot_config_from_dict_ignores_extra_keys():
    cfg = slot_config_from({"name": "B0", "worker": "deepseek_v4_flash", "not_a_slot": 1})
    assert isinstance(cfg, SlotConfig) and cfg.worker == "deepseek_v4_flash" and cfg.name == "B0"


def test_slot_config_from_passthrough_existing():
    sc = SlotConfig(name="X", worker="local")
    assert slot_config_from(sc) is sc


# ---------------------------------------------------------------------------
# measurements_from_config (PURE)
# ---------------------------------------------------------------------------
def test_measurements_from_config_emits_core_metrics():
    cr = ConfigResult(name="B0", n_items=4, n_hidden_pass=3, n_visible_pass=3,
                      cost_usd=0.08, tokens=800)
    ms = measurements_from_config("h_uplift", cr, ts=1, source="exp:uplift1")
    by = {m["metric"]: m for m in ms}
    assert set(by) == {METRIC_PASS_AT_1, METRIC_VISIBLE_RATE, METRIC_COST_USD}
    assert by[METRIC_PASS_AT_1]["value"] == pytest.approx(0.75)
    assert by[METRIC_COST_USD]["value"] == pytest.approx(0.08)
    for m in ms:
        fe.validate_event(m)                       # well-formed measurement events
        assert m["node_id"] == "h_uplift" and m["source"] == "exp:uplift1"
        assert m["arm"] == "B0"                     # tagged for comparison claims


def test_measurements_with_baseline_add_uplift_metrics():
    base = ConfigResult(name="B0", n_items=4, n_hidden_pass=2, cost_usd=0.04)
    variant = ConfigResult(name="audit_S", n_items=4, n_hidden_pass=4, cost_usd=0.10)
    ms = measurements_from_config("h_uplift", variant, ts=1, source="s", baseline=base)
    by = {m["metric"]: m for m in ms}
    assert by[METRIC_UPLIFT]["value"] == pytest.approx(0.5)         # 1.0 - 0.5 pass@1
    # delta pass 0.5 / delta cost 0.06 = 8.333...
    assert by[METRIC_UPLIFT_PER_DOLLAR]["value"] == pytest.approx(0.5 / 0.06, abs=1e-4)
    assert by[METRIC_UPLIFT]["baseline"] == "B0"


# ---------------------------------------------------------------------------
# Accept #2 — adapter fixture-run emits well-formed uplift measurement events (offline)
# ---------------------------------------------------------------------------
async def test_run_uplift_experiment_emits_well_formed_events():
    items = fixture_items()
    calls = []
    run_one = make_mock_run_one(pass_ratio=0.75, calls=calls)
    exp = _exp()
    res = await run_uplift_experiment(exp, items, ts=10, run_one=run_one)

    # ran the config over every item
    assert len(calls) == len(items) and all(c[0] == "B0" for c in calls)
    # measurement events well-formed + present
    metrics = {m["metric"] for m in res.measurements}
    assert {METRIC_PASS_AT_1, METRIC_VISIBLE_RATE, METRIC_COST_USD} <= metrics
    assert res.config_result.pass_at_1 == pytest.approx(0.75)
    # a spend event captured the cost (COST-2) + an experiment_run marker
    assert res.spend_event["kind"] == "spend" and res.spend_event["amount_usd"] == pytest.approx(0.08)
    assert res.experiment_run_event["kind"] == "experiment_run"
    assert res.experiment_run_event["result"]["pass_at_1"] == pytest.approx(0.75)
    # every emitted event validates
    for ev in res.events:
        fe.validate_event(ev)
    # append order: run marker → measurements → spend
    assert res.events[0]["kind"] == "experiment_run" and res.events[-1]["kind"] == "spend"


async def test_run_uplift_experiment_appends_to_f1_store(tmp_path):
    store = create_program("uplift_prog", root=tmp_path)
    items = fixture_items()
    exp = _exp()
    res = await run_uplift_experiment(exp, items, ts=11, run_one=make_mock_run_one(),
                                      store=store, epoch_id="ep1", day="2026-07-08")
    assert res.sha is not None
    persisted = store.events()
    kinds = [e["kind"] for e in persisted]
    assert "measurement" in kinds and "spend" in kinds and "experiment_run" in kinds
    # the spend event is scoped (epoch_id + day) so the budget gate can find it
    sp = next(e for e in persisted if e["kind"] == "spend")
    assert sp["epoch_id"] == "ep1" and sp["day"] == "2026-07-08"


async def test_empty_item_set_is_rejected():
    with pytest.raises(Exception):
        await run_config(SlotConfig(name="B0"), [], make_mock_run_one())


# ---------------------------------------------------------------------------
# gated entry point — auto UNDER budget, proposal-only ABOVE budget (inv. 14)
# ---------------------------------------------------------------------------
async def test_gated_auto_run_under_budget(tmp_path):
    store = create_program("gated_auto", root=tmp_path)
    items = fixture_items()
    calls = []
    run_one = make_mock_run_one(calls=calls)
    exp = _exp(est_cost=0.1)  # nothing spent yet → under both caps
    gate, res = await run_experiment_gated(
        exp, items, ts=1, run_one=run_one,
        tree_epoch_events=[], day_forest_events=[], store=store, epoch_id="ep1", day="2026-07-08",
    )
    assert gate.auto is True and res is not None
    assert len(calls) == len(items)                 # the spine WAS driven
    assert res.sha is not None                       # events persisted


async def test_gated_above_budget_is_proposal_only(tmp_path):
    """ABOVE budget → approval item + NOTHING runs / appends (inv. 14)."""
    store = create_program("gated_gated", root=tmp_path)
    prior = [fe.spend(amount_usd=1.9, label="prior", ts="2026-07-08T00:00:00",
                      est=True, node_id="h_uplift", epoch_id="ep1", day="2026-07-08")]
    items = fixture_items()
    calls = []
    run_one = make_mock_run_one(calls=calls)
    exp = _exp(est_cost=0.5)  # 1.9 + 0.5 = 2.4 > $2 tree/epoch cap
    gate, res = await run_experiment_gated(
        exp, items, ts=1, run_one=run_one,
        tree_epoch_events=spend_in_epoch(prior, "ep1"),
        day_forest_events=spend_on_day(prior, "2026-07-08"),
        store=store, epoch_id="ep1", day="2026-07-08",
    )
    assert gate.requires_approval is True and res is None
    assert gate.approval_item["action_type"] == "forest_experiment"
    assert calls == []                               # the spine was NOT driven
    # nothing appended beyond the program's init commit (no measurement/spend/experiment_run events)
    assert [e for e in store.events() if e["kind"] in ("measurement", "spend", "experiment_run")] == []


async def test_gated_state_mutating_is_proposal_only():
    calls = []
    run_one = make_mock_run_one(calls=calls)
    exp = _exp(est_cost=0.0, state_mutating=True)
    gate, res = await run_experiment_gated(exp, fixture_items(), ts=1, run_one=run_one)
    assert gate.requires_approval is True and res is None and calls == []


# ---------------------------------------------------------------------------
# forest → verify wiring: the pass_at_1 stream feeds F5 measurement-entailment
# ---------------------------------------------------------------------------
async def test_uplift_stream_feeds_measurement_entailment():
    from core.verify.measurement import entail_measurement
    from core.verify.entailment import EntailmentVerdict

    items = fixture_items()
    exp = _exp()
    all_events = []
    # 5 repeated observations (distinct ts) → clears the §7.2 observation floor (5)
    for t in range(5):
        res = await run_uplift_experiment(exp, items, ts=t, run_one=make_mock_run_one(pass_ratio=1.0))
        all_events.extend(res.measurements)

    pass_events = [m for m in all_events if m["metric"] == METRIC_PASS_AT_1]
    assert len(pass_events) == 5
    ent = entail_measurement(
        claim_kind="level",
        decision_rule={"metric": METRIC_PASS_AT_1, "agg": "mean", "lo": 0.9, "hi": 1.0},
        events=pass_events,
        node_id="h_uplift",
    )
    assert ent.floor_met is True and ent.n_observations == 5
    assert ent.verdict == EntailmentVerdict.ENTAILED   # mean pass@1 == 1.0 in [0.9, 1.0]
