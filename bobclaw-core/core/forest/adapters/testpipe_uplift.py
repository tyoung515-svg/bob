"""testpipe_uplift.py — the SEED experiment runner: bobpipe uplift (SPEC §5, MS9-F6).

The seed program's standing question (SPEC §5): *"What do BoB-pipe slots contribute to uplift, and
how does that move over time?"* This adapter drives the EXISTING ``core.testpipe`` spine
(front→worker→audit→verify-sandbox→repair) for ONE ``SlotConfig`` over a set of ``TestItem``s, then
emits well-formed uplift ``measurement`` events (+ an EST-badged ``spend`` event + an
``experiment_run`` marker) into a program ledger via the F1 store.

The forest is what the test-pipe spec was missing: MEMORY + hypothesis-driven selection of which OFAT
config to run next. The adapter is the thin bridge: it REUSES the testpipe ``run_sweep`` aggregation
(a single config → one ``ConfigResult``) and the ``uplift_per_dollar`` reducer — it never re-implements
the spine or the metering, and it makes NO edit to ``core.testpipe``.

Injectable runner (``run_one``): the unit tests inject a MOCK ``run_one`` returning synthetic
``ItemResult``s, so the whole measurement path is exercised OFFLINE + deterministically (mega-sprint
invariant 13). :func:`make_testpipe_runner` binds the REAL spine (a raw send seam + a subprocess
scorer) for the ONE sanctioned micro live run (inv. 13). Every ``ts`` / id is caller-supplied so the
emitted events are content-stable.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

from core.forest.events import (
    ForestError,
    measurement as _ev_measurement,
    spend as _ev_spend,
)
from core.forest.experiment import ExperimentNode, experiment_run_event
from core.testpipe.ledger import uplift_per_dollar
from core.testpipe.pipeline import run_item
from core.testpipe.scorer import Scorer, SubprocessScorer
from core.testpipe.sweep import run_sweep
from core.testpipe.types import ConfigResult, ItemResult, SlotConfig, TestItem

RUNNER_NAME = "testpipe_uplift"

# Well-formed uplift metric names (each a ``measurement`` event's ``metric``).
METRIC_PASS_AT_1 = "pass_at_1"          # primary: pass@1-of-final on hidden tests (SPEC §0)
METRIC_VISIBLE_RATE = "visible_rate"    # visible-gate pass rate
METRIC_COST_USD = "cost_usd"            # captured EST cost of the config over the item set
METRIC_UPLIFT = "uplift"                # delta pass@1 vs a baseline config (when supplied)
METRIC_UPLIFT_PER_DOLLAR = "uplift_per_dollar"  # the BoB thesis number (SPEC §0 secondary)

RunOne = Callable[[SlotConfig, TestItem], Awaitable[ItemResult]]


class UpliftAdapterError(ForestError):
    """Raised on adapter misuse (bad config, empty item set)."""
    pass


@dataclass
class UpliftResult:
    """The outcome of one uplift experiment run: the aggregate + the emitted ledger events."""

    node_id: str
    experiment_id: str
    config_name: str
    config_result: ConfigResult
    measurements: list = field(default_factory=list)   # the measurement events
    spend_event: Optional[dict] = None
    experiment_run_event: Optional[dict] = None
    events: list = field(default_factory=list)         # append order: [experiment_run, *measurements, spend]
    sha: Optional[str] = None                          # set iff appended to a store
    baseline_name: Optional[str] = None

    @property
    def pass_at_1(self) -> float:
        return round(self.config_result.pass_at_1, 6)

    @property
    def cost_usd(self) -> float:
        return round(self.config_result.cost_usd, 6)

    def summary(self) -> dict:
        """A small JSON-serializable outcome (the ``experiment_run`` event's ``result``)."""
        cr = self.config_result
        return {
            "config": cr.name,
            "n_items": cr.n_items,
            "pass_at_1": round(cr.pass_at_1, 6),
            "visible_rate": round(cr.visible_rate, 6),
            "cost_usd": round(cr.cost_usd, 6),
            "tokens": cr.tokens,
            "provenance": dict(cr.provenance),
            "n_measurements": len(self.measurements),
            "baseline": self.baseline_name,
        }


def slot_config_from(config: Any, *, default_name: str = "experiment") -> SlotConfig:
    """Coerce an experiment ``config`` (a dict or an existing ``SlotConfig``) into a ``SlotConfig``.

    Only keys that are real ``SlotConfig`` fields are threaded through (extra keys are ignored so an
    experiment config may carry runner-side metadata). A missing ``name`` defaults to *default_name*.
    """
    if isinstance(config, SlotConfig):
        return config
    if config is None:
        return SlotConfig(name=default_name)
    if not isinstance(config, dict):
        raise UpliftAdapterError(f"config must be a dict or SlotConfig, got {type(config).__name__}")
    field_names = {f.name for f in dataclasses.fields(SlotConfig)}
    kwargs = {k: v for k, v in config.items() if k in field_names}
    kwargs.setdefault("name", config.get("name", default_name))
    try:
        return SlotConfig(**kwargs)
    except TypeError as exc:
        raise UpliftAdapterError(f"invalid SlotConfig fields: {exc}") from exc


async def run_config(
    slot_config: SlotConfig,
    items: Sequence[TestItem],
    run_one: RunOne,
) -> ConfigResult:
    """Drive the testpipe spine for ONE config over *items* → one aggregated ``ConfigResult``.

    REUSES ``core.testpipe.sweep.run_sweep`` (a single-config sweep) so the aggregation (cost/token
    sums, pass counts) is the SAME proven path the full OFAT sweep uses — no re-implementation.
    """
    items = list(items or [])
    if not items:
        raise UpliftAdapterError("run_config requires a non-empty item set")
    ledger = await run_sweep([slot_config], items, run_one)
    return ledger.get(slot_config.name)


def measurements_from_config(
    node_id: str,
    cr: ConfigResult,
    ts: Any,
    *,
    source: str,
    weight: Any = 1,
    baseline: Optional[ConfigResult] = None,
) -> list:
    """Turn a ``ConfigResult`` into well-formed uplift ``measurement`` events (PURE).

    Always emits ``pass_at_1`` / ``visible_rate`` / ``cost_usd``. When a *baseline* ``ConfigResult`` is
    supplied, also emits ``uplift`` (delta pass@1 vs baseline) and ``uplift_per_dollar`` (the thesis
    number, via the testpipe reducer). Each event tags ``arm=<config name>`` so a ``comparison`` claim
    (config A vs B) can consume the stream (measurement-entailment §7.1).
    """
    ms: list = []

    def _emit(metric: str, value: float, unit: str, **extra) -> None:
        ms.append(
            _ev_measurement(
                node_id=node_id,
                metric=metric,
                value=round(float(value), 6),
                source=source,
                ts=ts,
                unit=unit,
                weight=weight,
                arm=cr.name,
                config=cr.name,
                **extra,
            )
        )

    _emit(METRIC_PASS_AT_1, cr.pass_at_1, "fraction")
    _emit(METRIC_VISIBLE_RATE, cr.visible_rate, "fraction")
    _emit(METRIC_COST_USD, cr.cost_usd, "usd")

    if baseline is not None:
        dpass = round(cr.pass_at_1 - baseline.pass_at_1, 6)
        dcost = round(cr.cost_usd - baseline.cost_usd, 6)
        _emit(METRIC_UPLIFT, dpass, "fraction", baseline=baseline.name)
        _emit(METRIC_UPLIFT_PER_DOLLAR, uplift_per_dollar(dpass, dcost), "pass_per_usd",
              baseline=baseline.name)

    return ms


async def run_uplift_experiment(
    experiment: ExperimentNode,
    items: Sequence[TestItem],
    *,
    ts: Any,
    run_one: RunOne,
    source: Optional[str] = None,
    baseline: Optional[ConfigResult] = None,
    weight: Any = 1,
    store: Any = None,
    epoch_id: Any = None,
    day: Any = None,
) -> UpliftResult:
    """Run *experiment*'s config over *items* → emit uplift measurement/spend/experiment_run events.

    Drives the testpipe spine via *run_one* (injected — mock in unit tests, the real spine for the live
    run), aggregates a ``ConfigResult``, and builds the ledger events. The captured EST cost lands as a
    ``spend`` event (COST-2 / SPEC §3), stamped with ``epoch_id`` / ``day`` so the auto-budget gate can
    scope it later. If *store* (an F1 ``ProgramStore``) is given, the events are appended (one commit)
    and the resulting sha is returned; otherwise the events are returned un-persisted.

    This runs the experiment UNCONDITIONALLY — the auto-budget gate is applied by the caller (see
    :func:`run_experiment_gated`), never bypassed here.
    """
    slot_config = slot_config_from(experiment.config, default_name=experiment.node_id or "experiment")
    source = source or experiment.id or RUNNER_NAME

    cr = await run_config(slot_config, items, run_one)

    measurements = measurements_from_config(
        experiment.node_id, cr, ts, source=source, weight=weight, baseline=baseline
    )

    result = UpliftResult(
        node_id=experiment.node_id,
        experiment_id=experiment.id,
        config_name=cr.name,
        config_result=cr,
        measurements=measurements,
        baseline_name=baseline.name if baseline is not None else None,
    )

    # EST-badged spend event (COST-2): budget derives from this ledger truth, not a side counter.
    spend_extra: dict = {"runner": RUNNER_NAME, "experiment_id": experiment.id, "config": cr.name}
    if epoch_id is not None:
        spend_extra["epoch_id"] = epoch_id
    if day is not None:
        spend_extra["day"] = day
    spend_ev = _ev_spend(
        amount_usd=round(cr.cost_usd, 6),
        label=f"experiment:{experiment.id}:{cr.name}",
        ts=ts,
        est=True,
        node_id=experiment.node_id,
        **spend_extra,
    )
    exp_ev = experiment_run_event(experiment, ts=ts, result=result.summary())

    result.spend_event = spend_ev
    result.experiment_run_event = exp_ev
    # Append order: the run marker first, then its measurements, then the spend it incurred.
    result.events = [exp_ev, *measurements, spend_ev]

    if store is not None:
        result.sha = store.append_events(
            result.events,
            message=f"forest: experiment {experiment.id} ({cr.name}) — "
                    f"{len(measurements)} measurement(s)",
        )

    return result


async def run_experiment_gated(
    experiment: ExperimentNode,
    items: Sequence[TestItem],
    *,
    ts: Any,
    run_one: RunOne,
    tree_epoch_events: Sequence[Any] = (),
    day_forest_events: Sequence[Any] = (),
    source: Optional[str] = None,
    baseline: Optional[ConfigResult] = None,
    weight: Any = 1,
    store: Any = None,
    epoch_id: Any = None,
    day: Any = None,
    per_tree_epoch: Optional[float] = None,
    per_day_forest: Optional[float] = None,
):
    """Gate THEN (only if auto) run — the auto-budget-honouring entry point (SPEC §7.4 + inv. 14).

    Returns ``(gate, result)``:

      * ABOVE budget OR state-mutating ⇒ ``result is None`` and ``gate.approval_item`` is the
        proposal-only ``forest_experiment`` approval item — the spine is NOT driven, NOTHING is
        appended (inv. 14: nothing auto-applies).
      * UNDER budget + non-mutating ⇒ the experiment auto-runs (via :func:`run_uplift_experiment`) and
        ``result`` is the :class:`UpliftResult`.

    Budget is derived from the passed ``tree_epoch_events`` / ``day_forest_events`` (ledger ``spend``
    events), never a side counter.
    """
    # Imported here to keep the module import graph shallow (gate lives in the pure sibling module).
    from core.forest.experiment import gate_experiment, PER_DAY_FOREST_USD, PER_TREE_EPOCH_USD

    gate = gate_experiment(
        experiment,
        tree_epoch_events=tree_epoch_events,
        day_forest_events=day_forest_events,
        per_tree_epoch=PER_TREE_EPOCH_USD if per_tree_epoch is None else per_tree_epoch,
        per_day_forest=PER_DAY_FOREST_USD if per_day_forest is None else per_day_forest,
    )
    if gate.requires_approval:
        return gate, None

    result = await run_uplift_experiment(
        experiment, items, ts=ts, run_one=run_one, source=source, baseline=baseline,
        weight=weight, store=store, epoch_id=epoch_id, day=day,
    )
    return gate, result


def make_testpipe_runner(
    *,
    raw_send: Callable[..., Awaitable[str]],
    workspace_root: Any,
    scorer: Optional[Scorer] = None,
    sandbox_mode: str = "subprocess",
) -> RunOne:
    """Bind the REAL testpipe spine into a ``run_one(config, item)`` (for the ONE micro live run).

    ``raw_send`` is the raw model seam (``core.nodes.execute._send_to_backend`` in the live path); the
    build spine needs a ``scorer`` holding the hidden tests (defaults to a subprocess scorer under
    *workspace_root*). Unit tests do NOT use this — they inject a synthetic ``run_one`` directly.
    """
    ws_root = Path(workspace_root)
    the_scorer = scorer if scorer is not None else SubprocessScorer(ws_root / "score", mode=sandbox_mode)

    async def run_one(config: SlotConfig, item: TestItem) -> ItemResult:
        return await run_item(
            item, config,
            raw_send=raw_send, scorer=the_scorer,
            workspace_root=ws_root / "ws", sandbox_mode=sandbox_mode,
        )

    return run_one


__all__ = [
    "RUNNER_NAME",
    "METRIC_PASS_AT_1",
    "METRIC_VISIBLE_RATE",
    "METRIC_COST_USD",
    "METRIC_UPLIFT",
    "METRIC_UPLIFT_PER_DOLLAR",
    "UpliftAdapterError",
    "UpliftResult",
    "slot_config_from",
    "run_config",
    "measurements_from_config",
    "run_uplift_experiment",
    "run_experiment_gated",
    "make_testpipe_runner",
]
