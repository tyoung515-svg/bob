"""core.forest.adapters — experiment RUNNERS that drive existing spines for one config (MS9-F6).

An adapter wraps an EXISTING BoB spine for ONE ``config`` and emits well-formed ``measurement`` (+
``spend`` + ``experiment_run``) events into a program ledger (via the F1 store). v0 runner: the
testpipe-uplift adapter (SPEC §5), which drives ``core.testpipe`` (front→worker→audit→verify→repair).
Adapters READ-ONLY import the spine they drive — they never edit it.
"""
from __future__ import annotations

from core.forest.adapters.testpipe_uplift import (  # noqa: F401
    METRIC_COST_USD,
    METRIC_PASS_AT_1,
    METRIC_UPLIFT,
    METRIC_UPLIFT_PER_DOLLAR,
    METRIC_VISIBLE_RATE,
    RUNNER_NAME,
    UpliftResult,
    make_testpipe_runner,
    measurements_from_config,
    run_config,
    run_experiment_gated,
    run_uplift_experiment,
    slot_config_from,
)

__all__ = [
    "RUNNER_NAME",
    "METRIC_PASS_AT_1",
    "METRIC_VISIBLE_RATE",
    "METRIC_COST_USD",
    "METRIC_UPLIFT",
    "METRIC_UPLIFT_PER_DOLLAR",
    "UpliftResult",
    "slot_config_from",
    "run_config",
    "measurements_from_config",
    "run_uplift_experiment",
    "run_experiment_gated",
    "make_testpipe_runner",
]
