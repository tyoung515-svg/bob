"""MS9-F5 — tests for core.verify.measurement (measurement-entailment, longitudinal claims).

Primary convergence evidence for F5 (SPEC-RESEARCH-FOREST §3, §7.1, §7.2). These are golden-fixture
property pins written to FAIL against a wrong implementation:

  * GOLDEN FIXTURES per §7.1 claim kind (level / trend / comparison / causal-candidate) → the exact
    expected VerificationTag (PV / VS / U);
  * the SAME tag vocabulary as core.verify.entailment (PV/VS/U) — imported, not re-declared;
  * the §7.2 observation FLOOR (>= 5) as a DEFAULT-FAIL structural gate: below the floor the tag is U
    and **PV is UNREACHABLE** — proven by taking a case that IS PV at >= 5 obs and showing the SAME
    evidence/rule at < 5 obs is U (never PV);
  * verdict→tag mapping identical to entailment.tag_for (entailed+primary→PV, entailed+vendor→VS,
    not_entailed/unknown→U);
  * PURITY (AST scan: no I/O / clock / random imports) + determinism.

Evidence is built with the REAL F1 constructor (core.forest.events.measurement) so the module is
tested against the actual ledger event shape it consumes.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import core.verify.measurement as meas
from core.verify.measurement import (
    OBSERVATION_FLOOR,
    MeasurementEntailment,
    entail_measurement,
    entail_node,
)
from core.verify.entailment import EntailmentVerdict, SourceKind, VerificationTag, tag_for
from core.forest import events as ev
from core.forest import hypothesis as hyp


# ---------------------------------------------------------------------------
# Builders — real measurement events (F1 constructor)
# ---------------------------------------------------------------------------

def _obs(values, *, node_id="H1", metric="uplift", source="testpipe-uplift", ts0=0, **extra):
    """One measurement event per value, ts increasing from ts0."""
    return [
        ev.measurement(node_id=node_id, metric=metric, value=v, source=source, ts=ts0 + i, **extra)
        for i, v in enumerate(values)
    ]


# ---------------------------------------------------------------------------
# Vocabulary + floor constant
# ---------------------------------------------------------------------------

def test_shared_vocabulary_and_floor_constant():
    # Same VerificationTag object as the existing entailment module (no re-declared vocabulary).
    from core.verify import entailment as ent
    assert meas.VerificationTag is ent.VerificationTag
    assert {t.value for t in VerificationTag} == {"PV", "VS", "U"}
    # Floor is the F2 ratified §7.2 observation floor, exactly 5.
    assert OBSERVATION_FLOOR == hyp.SUPPORTED_MIN_OBSERVATIONS == 5


# ---------------------------------------------------------------------------
# §7.1 GOLDEN FIXTURES — level
# ---------------------------------------------------------------------------

def test_level_in_range_primary_is_PV():
    events = _obs([0.80, 0.82, 0.85, 0.83, 0.81])
    rule = {"metric": "uplift", "lo": 0.7, "hi": 0.9, "agg": "mean"}
    r = entail_measurement(claim_kind="level", decision_rule=rule, events=events, node_id="H1")
    assert r.tag is VerificationTag.PV
    assert r.entailed is True
    assert r.verdict is EntailmentVerdict.ENTAILED
    assert r.n_observations == 5 and r.floor_met is True


def test_level_out_of_range_is_U():
    events = _obs([0.50, 0.55, 0.52, 0.48, 0.51])  # mean ~0.51, below lo=0.7
    rule = {"metric": "uplift", "lo": 0.7, "hi": 0.9, "agg": "mean"}
    r = entail_measurement(claim_kind="level", decision_rule=rule, events=events, node_id="H1")
    assert r.tag is VerificationTag.U
    assert r.entailed is False
    assert r.verdict is EntailmentVerdict.NOT_ENTAILED


def test_level_vendor_source_is_VS():
    # Entailed, but the evidence provenance is vendor-stated → VS, not PV.
    events = _obs([0.80, 0.82, 0.85, 0.83, 0.81], source="flight/spend-telemetry")
    rule = {"metric": "uplift", "lo": 0.7, "hi": 0.9, "agg": "mean", "source_kind": "vendor"}
    r = entail_measurement(claim_kind="level", decision_rule=rule, events=events, node_id="H1")
    assert r.entailed is True
    assert r.tag is VerificationTag.VS
    assert r.source_kind is SourceKind.VENDOR


def test_level_one_sided_ranges():
    events = _obs([0.80, 0.82, 0.85, 0.83, 0.81])
    lo_only = entail_measurement(claim_kind="level", decision_rule={"metric": "uplift", "lo": 0.75},
                                 events=events, node_id="H1")
    hi_only = entail_measurement(claim_kind="level", decision_rule={"metric": "uplift", "hi": 0.75},
                                 events=events, node_id="H1")
    assert lo_only.tag is VerificationTag.PV   # mean ~0.822 >= 0.75
    assert hi_only.tag is VerificationTag.U    # mean ~0.822 NOT <= 0.75


# ---------------------------------------------------------------------------
# §7.1 GOLDEN FIXTURES — trend
# ---------------------------------------------------------------------------

def test_trend_up_is_PV():
    events = _obs([0.10, 0.20, 0.30, 0.40, 0.50])
    rule = {"metric": "uplift", "direction": "up"}
    r = entail_measurement(claim_kind="trend", decision_rule=rule, events=events, node_id="H1")
    assert r.tag is VerificationTag.PV
    assert r.observed["slope"] > 0


def test_trend_wrong_direction_is_U():
    events = _obs([0.10, 0.20, 0.30, 0.40, 0.50])  # clearly rising
    rule = {"metric": "uplift", "direction": "down"}
    r = entail_measurement(claim_kind="trend", decision_rule=rule, events=events, node_id="H1")
    assert r.tag is VerificationTag.U
    assert r.verdict is EntailmentVerdict.NOT_ENTAILED


def test_trend_flat_within_eps_is_PV():
    events = _obs([0.50, 0.50, 0.50, 0.50, 0.50])
    rule = {"metric": "uplift", "direction": "flat", "eps": 1e-9}
    r = entail_measurement(claim_kind="trend", decision_rule=rule, events=events, node_id="H1")
    assert r.tag is VerificationTag.PV


def test_trend_down_is_PV():
    events = _obs([0.50, 0.40, 0.30, 0.20, 0.10])
    rule = {"metric": "uplift", "direction": "down"}
    r = entail_measurement(claim_kind="trend", decision_rule=rule, events=events, node_id="H1")
    assert r.tag is VerificationTag.PV
    assert r.observed["slope"] < 0


# ---------------------------------------------------------------------------
# §7.1 GOLDEN FIXTURES — comparison
# ---------------------------------------------------------------------------

def test_comparison_effect_is_PV():
    a = _obs([0.90, 0.90, 0.90], arm="A", ts0=0)
    b = _obs([0.50, 0.50], arm="B", ts0=10)
    rule = {"metric": "uplift", "arm_field": "arm", "arm_a": "A", "arm_b": "B",
            "delta": 0.2, "op": ">="}
    r = entail_measurement(claim_kind="comparison", decision_rule=rule, events=a + b, node_id="H1")
    assert r.tag is VerificationTag.PV
    assert r.observed["effect"] == pytest.approx(0.4)
    assert r.n_observations == 5


def test_comparison_effect_too_small_is_U():
    a = _obs([0.55, 0.55, 0.55], arm="A", ts0=0)
    b = _obs([0.50, 0.50], arm="B", ts0=10)
    rule = {"metric": "uplift", "arm_a": "A", "arm_b": "B", "delta": 0.2, "op": ">="}
    r = entail_measurement(claim_kind="comparison", decision_rule=rule, events=a + b, node_id="H1")
    assert r.tag is VerificationTag.U  # effect 0.05 < delta 0.2


def test_comparison_missing_arm_is_U():
    # Floor met (5 obs of the metric) but arm B has zero observations → unknown → U (never PV).
    a = _obs([0.90, 0.90, 0.90, 0.90, 0.90], arm="A")
    rule = {"metric": "uplift", "arm_a": "A", "arm_b": "B", "delta": 0.2, "op": ">="}
    r = entail_measurement(claim_kind="comparison", decision_rule=rule, events=a, node_id="H1")
    assert r.tag is VerificationTag.U
    assert r.verdict is EntailmentVerdict.UNKNOWN


# ---------------------------------------------------------------------------
# §7.1 GOLDEN FIXTURES — causal-candidate
# ---------------------------------------------------------------------------

def test_causal_candidate_split_field_is_PV():
    pre = _obs([0.20, 0.20], phase="pre", ts0=0)
    post = _obs([0.80, 0.80, 0.80], phase="post", ts0=10)
    rule = {"metric": "uplift", "split_field": "phase", "pre": "pre", "post": "post",
            "min_effect": 0.3, "direction": "increase"}
    r = entail_measurement(claim_kind="causal-candidate", decision_rule=rule,
                           events=pre + post, node_id="H1")
    assert r.tag is VerificationTag.PV
    assert r.observed["effect"] == pytest.approx(0.6)


def test_causal_candidate_ts_threshold_is_PV():
    pre = _obs([0.20, 0.20], ts0=0)      # ts 0,1
    post = _obs([0.80, 0.80, 0.80], ts0=100)  # ts 100,101,102
    rule = {"metric": "uplift", "ts_threshold": 50, "min_effect": 0.3, "direction": "increase"}
    r = entail_measurement(claim_kind="causal-candidate", decision_rule=rule,
                           events=pre + post, node_id="H1")
    assert r.tag is VerificationTag.PV


def test_causal_candidate_no_movement_is_U():
    pre = _obs([0.50, 0.50], phase="pre", ts0=0)
    post = _obs([0.51, 0.50, 0.49], phase="post", ts0=10)
    rule = {"metric": "uplift", "split_field": "phase", "min_effect": 0.3, "direction": "increase"}
    r = entail_measurement(claim_kind="causal-candidate", decision_rule=rule,
                           events=pre + post, node_id="H1")
    assert r.tag is VerificationTag.U


# ---------------------------------------------------------------------------
# THE U-FLOOR TEST — PV is UNREACHABLE below the §7.2 observation floor
# ---------------------------------------------------------------------------

# Each fixture is (claim_kind, rule, events-that-are-PV-at-5-obs, events-that-are-<5-obs-but-same-rule).
_FLOOR_CASES = {
    "level": (
        {"metric": "uplift", "lo": 0.7, "hi": 0.9, "agg": "mean"},
        _obs([0.80, 0.82, 0.85, 0.83, 0.81]),
        _obs([0.80, 0.82, 0.85, 0.83]),  # 4 obs, same in-range values
    ),
    "trend": (
        {"metric": "uplift", "direction": "up"},
        _obs([0.10, 0.20, 0.30, 0.40, 0.50]),
        _obs([0.10, 0.20, 0.30, 0.40]),  # 4 obs, still clearly rising
    ),
    "comparison": (
        {"metric": "uplift", "arm_a": "A", "arm_b": "B", "delta": 0.2, "op": ">="},
        _obs([0.90, 0.90, 0.90], arm="A") + _obs([0.50, 0.50], arm="B", ts0=10),
        _obs([0.90, 0.90], arm="A") + _obs([0.50, 0.50], arm="B", ts0=10),  # 4 obs
    ),
    "causal-candidate": (
        {"metric": "uplift", "split_field": "phase", "min_effect": 0.3, "direction": "increase"},
        _obs([0.20, 0.20], phase="pre") + _obs([0.80, 0.80, 0.80], phase="post", ts0=10),
        _obs([0.20, 0.20], phase="pre") + _obs([0.80, 0.80], phase="post", ts0=10),  # 4 obs
    ),
}


@pytest.mark.parametrize("kind", list(_FLOOR_CASES))
def test_pv_reachable_at_floor(kind):
    rule, pv_events, _ = _FLOOR_CASES[kind]
    r = entail_measurement(claim_kind=kind, decision_rule=rule, events=pv_events, node_id="H1")
    assert r.n_observations == 5 and r.floor_met is True
    assert r.tag is VerificationTag.PV, f"{kind} should be PV with 5 obs"


@pytest.mark.parametrize("kind", list(_FLOOR_CASES))
def test_pv_UNREACHABLE_below_floor(kind):
    """The core default-FAIL pin: the SAME rule + rule-satisfying evidence that yields PV at 5 obs
    MUST yield U (never PV) at < 5 obs. Insufficient observations ⇒ U."""
    rule, _, below_events = _FLOOR_CASES[kind]
    assert len(below_events) < OBSERVATION_FLOOR
    r = entail_measurement(claim_kind=kind, decision_rule=rule, events=below_events, node_id="H1")
    assert r.floor_met is False
    assert r.n_observations < OBSERVATION_FLOOR
    assert r.entailed is False
    assert r.verdict is EntailmentVerdict.UNKNOWN
    assert r.tag is VerificationTag.U
    assert r.tag is not VerificationTag.PV


def test_floor_exact_boundary_5_meets_but_4_does_not():
    rule = {"metric": "uplift", "lo": 0.0, "hi": 1.0, "agg": "mean"}
    five = entail_measurement(claim_kind="level", decision_rule=rule, events=_obs([0.5] * 5), node_id="H1")
    four = entail_measurement(claim_kind="level", decision_rule=rule, events=_obs([0.5] * 4), node_id="H1")
    assert five.floor_met is True and five.tag is VerificationTag.PV
    assert four.floor_met is False and four.tag is VerificationTag.U


def test_custom_min_observations_gate():
    rule = {"metric": "uplift", "lo": 0.0, "hi": 1.0}
    r = entail_measurement(claim_kind="level", decision_rule=rule, events=_obs([0.5] * 8),
                           node_id="H1", min_observations=10)
    assert r.floor_met is False and r.tag is VerificationTag.U  # 8 < custom floor 10


# ---------------------------------------------------------------------------
# Metric / node scoping of the floor + aggregation
# ---------------------------------------------------------------------------

def test_floor_and_agg_are_metric_scoped():
    # 5 in-range obs of the RULE's metric + noise on another metric that must be ignored.
    target = _obs([0.80, 0.82, 0.85, 0.83, 0.81], metric="uplift")
    noise = _obs([9.0, 9.0, 9.0, 9.0], metric="latency", ts0=100)
    rule = {"metric": "uplift", "lo": 0.7, "hi": 0.9, "agg": "mean"}
    r = entail_measurement(claim_kind="level", decision_rule=rule, events=target + noise, node_id="H1")
    assert r.n_observations == 5  # latency events not counted
    assert r.tag is VerificationTag.PV


def test_floor_counts_only_rule_metric():
    # Only 4 obs of the target metric → floor NOT met even though other metrics abound.
    target = _obs([0.80, 0.82, 0.85, 0.83], metric="uplift")
    noise = _obs([0.1] * 20, metric="latency", ts0=100)
    rule = {"metric": "uplift", "lo": 0.7, "hi": 0.9}
    r = entail_measurement(claim_kind="level", decision_rule=rule, events=target + noise, node_id="H1")
    assert r.n_observations == 4 and r.tag is VerificationTag.U


def test_node_scoping():
    mine = _obs([0.80, 0.82, 0.85, 0.83, 0.81], node_id="H1")
    other = _obs([0.10] * 10, node_id="H2", ts0=100)
    rule = {"metric": "uplift", "lo": 0.7, "hi": 0.9}
    r = entail_measurement(claim_kind="level", decision_rule=rule, events=mine + other, node_id="H1")
    assert r.n_observations == 5 and r.tag is VerificationTag.PV


# ---------------------------------------------------------------------------
# Verdict→tag mapping matches entailment.tag_for
# ---------------------------------------------------------------------------

def test_tag_mapping_matches_tag_for():
    assert tag_for(EntailmentVerdict.ENTAILED, SourceKind.PRIMARY) is VerificationTag.PV
    assert tag_for(EntailmentVerdict.ENTAILED, SourceKind.VENDOR) is VerificationTag.VS
    assert tag_for(EntailmentVerdict.NOT_ENTAILED, SourceKind.PRIMARY) is VerificationTag.U
    assert tag_for(EntailmentVerdict.NOT_ENTAILED, SourceKind.VENDOR) is VerificationTag.U
    assert tag_for(EntailmentVerdict.UNKNOWN, SourceKind.PRIMARY) is VerificationTag.U
    # And the module honours it: entailed+vendor via per-event provenance → VS.
    events = _obs([0.80, 0.82, 0.85, 0.83, 0.81], source_kind="vendor")
    r = entail_measurement(claim_kind="level", decision_rule={"metric": "uplift", "lo": 0.7, "hi": 0.9},
                           events=events, node_id="H1")
    assert r.entailed is True and r.tag is VerificationTag.VS


def test_explicit_source_kind_arg_wins():
    events = _obs([0.80, 0.82, 0.85, 0.83, 0.81])
    r = entail_measurement(claim_kind="level", decision_rule={"metric": "uplift", "lo": 0.7, "hi": 0.9},
                           events=events, node_id="H1", source_kind="vendor")
    assert r.tag is VerificationTag.VS


# ---------------------------------------------------------------------------
# Fail-safe edge cases (never PV, never raise)
# ---------------------------------------------------------------------------

def test_empty_evidence_is_U():
    r = entail_measurement(claim_kind="level", decision_rule={"metric": "uplift", "lo": 0, "hi": 1},
                           events=[], node_id="H1")
    assert r.n_observations == 0 and r.tag is VerificationTag.U and r.floor_met is False


def test_unknown_claim_kind_is_U():
    r = entail_measurement(claim_kind="bogus", decision_rule={"metric": "uplift"},
                           events=_obs([0.5] * 5), node_id="H1")
    assert r.tag is VerificationTag.U and r.verdict is EntailmentVerdict.UNKNOWN


def test_no_decision_rule_is_U():
    r = entail_measurement(claim_kind="level", decision_rule=None, events=_obs([0.5] * 5), node_id="H1")
    assert r.tag is VerificationTag.U


def test_unparseable_rule_string_is_U():
    r = entail_measurement(claim_kind="level", decision_rule="{not json", events=_obs([0.5] * 5), node_id="H1")
    assert r.tag is VerificationTag.U


def test_level_rule_without_range_is_U():
    r = entail_measurement(claim_kind="level", decision_rule={"metric": "uplift", "agg": "mean"},
                           events=_obs([0.5] * 5), node_id="H1")
    assert r.tag is VerificationTag.U and r.verdict is EntailmentVerdict.UNKNOWN


def test_all_non_numeric_values_is_U():
    # Raw events (bypassing the constructor which forbids bool value) — 5 obs meet the floor by COUNT,
    # but there are no numeric values to aggregate → unknown → U (never PV).
    raw = [{"kind": "measurement", "node_id": "H1", "metric": "uplift", "value": True,
            "source": "s", "ts": i, "id": f"e{i}"} for i in range(5)]
    r = entail_measurement(claim_kind="level", decision_rule={"metric": "uplift", "lo": 0, "hi": 1},
                           events=raw, node_id="H1")
    assert r.n_observations == 5 and r.tag is VerificationTag.U


# ---------------------------------------------------------------------------
# JSON-string rule + entail_node bridge (F2 node consumption)
# ---------------------------------------------------------------------------

def test_json_string_rule_parses():
    import json
    events = _obs([0.80, 0.82, 0.85, 0.83, 0.81])
    rule = json.dumps({"metric": "uplift", "lo": 0.7, "hi": 0.9, "agg": "mean"})
    r = entail_measurement(claim_kind="level", decision_rule=rule, events=events, node_id="H1")
    assert r.tag is VerificationTag.PV


def test_entail_node_reads_node_fields():
    import json
    node = hyp.make_node(
        id="H1",
        claim_kind="trend",
        decision_rule=json.dumps({"metric": "uplift", "direction": "up"}),
    )
    events = _obs([0.10, 0.20, 0.30, 0.40, 0.50], node_id="H1")
    r = entail_node(node, events)
    assert r.node_id == "H1"
    assert r.tag is VerificationTag.PV


def test_entail_node_below_floor_is_U():
    import json
    node = hyp.make_node(
        id="H1",
        claim_kind="level",
        decision_rule=json.dumps({"metric": "uplift", "lo": 0.7, "hi": 0.9}),
    )
    events = _obs([0.80, 0.82, 0.85], node_id="H1")  # 3 obs
    r = entail_node(node, events)
    assert r.tag is VerificationTag.U and r.floor_met is False


# ---------------------------------------------------------------------------
# Determinism + result serialization
# ---------------------------------------------------------------------------

def test_determinism_same_inputs_same_result():
    events = _obs([0.80, 0.82, 0.85, 0.83, 0.81])
    rule = {"metric": "uplift", "lo": 0.7, "hi": 0.9, "agg": "mean"}
    a = entail_measurement(claim_kind="level", decision_rule=rule, events=events, node_id="H1")
    b = entail_measurement(claim_kind="level", decision_rule=rule, events=events, node_id="H1")
    assert a.as_dict() == b.as_dict()


def test_as_dict_shape():
    events = _obs([0.80, 0.82, 0.85, 0.83, 0.81])
    r = entail_measurement(claim_kind="level", decision_rule={"metric": "uplift", "lo": 0.7, "hi": 0.9},
                           events=events, node_id="H1")
    d = r.as_dict()
    assert d["tag"] == "PV" and d["verdict"] == "entailed"
    assert d["claim_kind"] == "level" and d["metric"] == "uplift"
    assert d["floor"] == 5 and d["floor_met"] is True
    assert d["source_kind"] == "primary"
    assert isinstance(d["reasons"], list) and d["reasons"]


# ---------------------------------------------------------------------------
# PURITY — no I/O / clock / random imports (SPEC §3: PURE, no I/O)
# ---------------------------------------------------------------------------

def test_module_has_no_io_or_nondeterministic_imports():
    src = pathlib.Path(meas.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {
        "os", "io", "sys", "socket", "subprocess", "pathlib", "shutil", "tempfile",
        "requests", "httpx", "aiohttp", "urllib", "http", "sqlite3", "asyncio",
        "random", "time", "datetime", "threading", "multiprocessing", "secrets",
    }
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                seen.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                seen.add(node.module.split(".")[0])
    bad = seen & forbidden
    assert not bad, f"measurement.py imports I/O/nondeterministic modules: {bad}"
