"""MS9-F2 — tests for core.forest.hypothesis (the PURE hypothesis-node status machine).

Primary convergence evidence for F2 (SPEC-RESEARCH-FOREST §1, §7.1, §7.2). Property-style pins over:
  * the §7.2 RATIFIED thresholds as named constants (0.9, 5, 0.1, 3, default weight 1);
  * §7.1 claim kinds (level/trend/comparison/causal-candidate) as an enum + frozenset;
  * the SPEC §1 node schema fields + validation;
  * Beta(α,β) update (α += w supporting, β += w contradicting) with w-independent observation count;
  * the status machine edges — supported (posterior AND observation floor), refuted (posterior floor
    OR falsifier), dormant (3 zero-evidence epochs) — with precedence (falsifier/refute override Beta);
  * PURITY: the module imports no I/O.

Every test is written to FAIL against a wrong implementation (wrong constant, dropped observation
floor, wrong precedence, un-latched falsifier, etc.).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import core.forest.hypothesis as hyp
from core.forest.events import ForestError


# ---------------------------------------------------------------------------
# §7.2 ratified constants — pinned to the SPEC values EXACTLY
# ---------------------------------------------------------------------------

def test_ratified_constants_match_spec_exactly():
    assert hyp.SUPPORTED_POSTERIOR_MIN == 0.9
    assert hyp.SUPPORTED_MIN_OBSERVATIONS == 5
    assert hyp.REFUTED_POSTERIOR_MAX == 0.1
    assert hyp.DORMANT_EPOCHS == 3
    assert hyp.DEFAULT_WEIGHT == 1


def test_claim_kinds_are_exactly_the_four():
    assert hyp.CLAIM_KINDS == {"level", "trend", "comparison", "causal-candidate"}
    # the enum's values are exactly the frozenset
    assert {k.value for k in hyp.ClaimKind} == hyp.CLAIM_KINDS
    assert hyp.ClaimKind.CAUSAL_CANDIDATE.value == "causal-candidate"


def test_node_statuses_and_tags():
    assert hyp.NODE_STATUSES == {"open", "supported", "refuted", "dormant"}
    assert {s.value for s in hyp.NodeStatus} == hyp.NODE_STATUSES
    assert hyp.TAGS == {"PV", "VS", "U"}
    assert (hyp.STATUS_OPEN, hyp.STATUS_SUPPORTED, hyp.STATUS_REFUTED, hyp.STATUS_DORMANT) == (
        "open", "supported", "refuted", "dormant",
    )


# ---------------------------------------------------------------------------
# Beta posterior
# ---------------------------------------------------------------------------

def test_posterior_mean_over_beta_node_and_dict():
    assert hyp.posterior_mean(hyp.Beta(9, 1)) == pytest.approx(0.9)
    assert hyp.posterior_mean(hyp.Beta(1, 9)) == pytest.approx(0.1)
    assert hyp.posterior_mean(hyp.Beta(1, 1)) == pytest.approx(0.5)
    assert hyp.posterior_mean(hyp.Beta(0, 0)) == 0.0  # degenerate guard, never div-by-zero
    assert hyp.posterior_mean({"alpha": 3, "beta": 1}) == pytest.approx(0.75)
    n = hyp.make_node(id="H", claim_kind="level", beta=hyp.Beta(3, 1))
    assert hyp.posterior_mean(n) == pytest.approx(0.75)


def test_posterior_mean_rejects_wrong_type():
    with pytest.raises(TypeError):
        hyp.posterior_mean(42)


# ---------------------------------------------------------------------------
# Node schema + validation (SPEC §1)
# ---------------------------------------------------------------------------

def test_fresh_node_is_open_and_defaults():
    n = hyp.make_node(id="H1", claim_kind="level")
    assert n.status == "open"
    assert n.observations == 0
    assert n.epochs_without_evidence == 0
    assert n.falsified is False
    assert n.tag == "U"                     # default-FAIL verification tag
    assert isinstance(n.beta, hyp.Beta)


def test_node_carries_full_spec_schema():
    n = hyp.make_node(
        id="H1", question="q?", hypothesis="h", claim_kind="comparison",
        measurement_source="testpipe", decision_rule="rule", evidence_refs=["e1"],
        parent="P", children=["C"],
    )
    for f in (
        "id", "question", "hypothesis", "claim_kind", "falsifiers", "measurement_source",
        "decision_rule", "evidence_refs", "tag", "beta", "status", "parent", "children",
    ):
        assert hasattr(n, f), f"schema field missing: {f}"
    assert n.claim_kind == "comparison"
    assert n.parent == "P" and n.children == ["C"]


def test_invalid_claim_kind_tag_status_rejected():
    with pytest.raises(hyp.HypothesisError):
        hyp.HypothesisNode(id="H", claim_kind="bogus")
    with pytest.raises(hyp.HypothesisError):
        hyp.HypothesisNode(id="H", tag="XX")
    with pytest.raises(hyp.HypothesisError):
        hyp.HypothesisNode(id="H", status="weird")


def test_enum_and_dict_beta_coercion():
    n = hyp.HypothesisNode(
        id="H", claim_kind=hyp.ClaimKind.TREND, status=hyp.NodeStatus.OPEN,
        beta={"alpha": 2, "beta": 3},
    )
    assert n.claim_kind == "trend"          # enum coerced to its str value
    assert n.status == "open"
    assert isinstance(n.beta, hyp.Beta) and n.beta.alpha == 2 and n.beta.beta == 3


def test_hypothesis_error_is_a_forest_error():
    assert issubclass(hyp.HypothesisError, ForestError)


# ---------------------------------------------------------------------------
# Beta update — weight handling (w != 1) and w-independent observation count
# ---------------------------------------------------------------------------

def test_beta_weight_handling_and_observation_count():
    n = hyp.make_node(id="H", claim_kind="level")     # Beta(1, 1)
    hyp.apply_evidence(n, supporting=True, weight=4)
    assert n.beta.alpha == 5                          # 1 + 4 (α += w on support)
    assert n.beta.beta == 1
    assert n.observations == 1                        # weight-INDEPENDENT count
    hyp.apply_evidence(n, supporting=False, weight=3)
    assert n.beta.beta == 4                           # 1 + 3 (β += w on contradiction)
    assert n.observations == 2
    hyp.apply_evidence(n, supporting=True)            # default weight
    assert n.beta.alpha == 6                          # +1 (DEFAULT_WEIGHT)
    assert n.observations == 3


def test_apply_evidence_does_not_touch_dormancy_counter():
    n = hyp.make_node(id="H", claim_kind="level")
    hyp.advance_epoch(n, new_evidence=0)
    hyp.advance_epoch(n, new_evidence=0)
    assert n.epochs_without_evidence == 2
    hyp.apply_evidence(n, supporting=True)            # within-epoch evidence, not an epoch boundary
    assert n.epochs_without_evidence == 2             # must NOT be reset by apply_evidence


def test_apply_evidence_is_deterministic():
    def run():
        n = hyp.make_node(id="H", claim_kind="level")
        for supp in (True, True, False, True):
            hyp.apply_evidence(n, supporting=supp, weight=2)
        return (n.beta.alpha, n.beta.beta, n.observations, n.status)
    assert run() == run()


# ---------------------------------------------------------------------------
# Status machine — SUPPORTED (posterior AND observation floor)
# ---------------------------------------------------------------------------

def test_supported_requires_posterior_and_obs_floor_via_evidence():
    n = hyp.make_node(id="H", claim_kind="level")
    for _ in range(18):                               # (1+18)/(2+18) = 0.95
        hyp.apply_evidence(n, supporting=True)
    assert hyp.posterior_mean(n) >= hyp.SUPPORTED_POSTERIOR_MIN
    assert n.observations >= hyp.SUPPORTED_MIN_OBSERVATIONS
    assert n.status == "supported"


def test_supported_exact_boundary_09_and_5obs():
    n = hyp.make_node(id="H", claim_kind="level", beta=hyp.Beta(9, 1), observations=5)
    assert hyp.posterior_mean(n) == pytest.approx(0.9)
    assert n.status == "supported"                    # 0.9 is inclusive, 5 obs is inclusive


def test_observation_floor_blocks_support_below_five_obs():
    # posterior driven WAY above 0.9 with heavy weights but only 4 observations -> NOT supported.
    n = hyp.make_node(id="H", claim_kind="level")
    for _ in range(4):
        hyp.apply_evidence(n, supporting=True, weight=100)
    assert hyp.posterior_mean(n) >= hyp.SUPPORTED_POSTERIOR_MIN
    assert n.observations == 4
    assert n.status == "open"                          # high posterior alone must NOT support
    hyp.apply_evidence(n, supporting=True, weight=100)  # crosses the 5-obs floor
    assert n.observations == 5
    assert n.status == "supported"


def test_one_below_obs_floor_is_open_not_supported():
    n = hyp.make_node(id="H", claim_kind="level", beta=hyp.Beta(20, 1), observations=4)
    assert hyp.posterior_mean(n) >= hyp.SUPPORTED_POSTERIOR_MIN
    assert n.status == "open"


# ---------------------------------------------------------------------------
# Status machine — REFUTED (posterior floor OR falsifier)
# ---------------------------------------------------------------------------

def test_refuted_at_or_below_posterior_floor():
    n = hyp.make_node(id="H", claim_kind="level", beta=hyp.Beta(1, 9))
    assert hyp.posterior_mean(n) == pytest.approx(0.1)
    assert n.status == "refuted"                       # 0.1 is inclusive


def test_just_above_refute_floor_is_not_refuted():
    n = hyp.make_node(id="H", claim_kind="level", beta=hyp.Beta(2, 9))
    assert hyp.posterior_mean(n) > hyp.REFUTED_POSTERIOR_MAX
    assert n.status != "refuted"


def test_contradicting_evidence_drives_refuted():
    n = hyp.make_node(id="H", claim_kind="level")
    for _ in range(20):                                # 1/(1+21) ≈ 0.045
        hyp.apply_evidence(n, supporting=False)
    assert hyp.posterior_mean(n) <= hyp.REFUTED_POSTERIOR_MAX
    assert n.status == "refuted"


# ---------------------------------------------------------------------------
# Falsifier override — refutes even at a high posterior, and latches
# ---------------------------------------------------------------------------

def test_falsifier_overrides_high_posterior_and_latches():
    negative = lambda ev: ev.get("value", 0) < 0
    n = hyp.make_node(id="H", claim_kind="level", falsifiers=[negative])
    for _ in range(20):
        hyp.apply_evidence(n, supporting=True)
    assert n.status == "supported"                     # high posterior + enough obs
    hyp.apply_falsifier_check(n, {"value": -3})        # a falsifier fires
    assert n.falsified is True
    assert n.status == "refuted"                       # overrides Beta / a high posterior
    hyp.apply_evidence(n, supporting=True)             # latched: cannot un-refute
    assert n.status == "refuted"


def test_falsifier_not_firing_leaves_status_unchanged():
    negative = lambda ev: ev.get("value", 0) < 0
    n = hyp.make_node(id="H", claim_kind="level", falsifiers=[negative])
    hyp.apply_falsifier_check(n, {"value": 5})
    assert n.falsified is False
    assert n.status == "open"


def test_evaluate_falsifiers_any_of_and_exception_safe():
    p_true = lambda ev: True
    p_false = lambda ev: False
    p_raise = lambda ev: 1 / 0
    n = hyp.make_node(id="H", claim_kind="level", falsifiers=[p_false, p_true])
    assert hyp.evaluate_falsifiers(n, {}) is True      # any-of
    n2 = hyp.make_node(id="H2", claim_kind="level", falsifiers=[p_raise, p_false])
    assert hyp.evaluate_falsifiers(n2, {}) is False    # a raising predicate is treated as not-firing
    assert n2.falsified is False                       # pure — no mutation


# ---------------------------------------------------------------------------
# Status machine — DORMANT (3 consecutive zero-evidence epochs) + counter reset
# ---------------------------------------------------------------------------

def test_dormant_after_three_zero_evidence_epochs():
    n = hyp.make_node(id="H", claim_kind="level")
    hyp.advance_epoch(n, new_evidence=0)
    assert n.status == "open"                          # 1
    hyp.advance_epoch(n, new_evidence=0)
    assert n.status == "open"                          # 2
    hyp.advance_epoch(n, new_evidence=0)
    assert n.epochs_without_evidence == 3
    assert n.status == "dormant"                       # 3 -> dormant


def test_dormancy_counter_resets_on_new_evidence():
    n = hyp.make_node(id="H", claim_kind="level")
    hyp.advance_epoch(n, 0)
    hyp.advance_epoch(n, 0)
    assert n.epochs_without_evidence == 2
    hyp.advance_epoch(n, new_evidence=1)               # evidence arrived -> reset
    assert n.epochs_without_evidence == 0
    assert n.status == "open"
    hyp.advance_epoch(n, 0)
    hyp.advance_epoch(n, 0)
    assert n.status == "open"                          # only 2 quiet epochs
    hyp.advance_epoch(n, 0)
    assert n.status == "dormant"                       # 3rd quiet epoch


# ---------------------------------------------------------------------------
# Precedence — refuted/supported (resolved) outrank dormant
# ---------------------------------------------------------------------------

def test_refuted_outranks_dormant():
    n = hyp.make_node(id="H", claim_kind="level", beta=hyp.Beta(1, 9))  # refuted by posterior
    for _ in range(hyp.DORMANT_EPOCHS):
        hyp.advance_epoch(n, 0)
    assert n.epochs_without_evidence >= hyp.DORMANT_EPOCHS
    assert n.status == "refuted"                       # refuted wins over dormant


def test_supported_outranks_dormant():
    n = hyp.make_node(id="H", claim_kind="level", beta=hyp.Beta(20, 1), observations=6)  # supported
    for _ in range(5):
        hyp.advance_epoch(n, 0)
    assert n.status == "supported"                     # a resolved node stays supported when quiet


def test_falsifier_outranks_dormant_and_open():
    negative = lambda ev: ev.get("v", 0) < 0
    n = hyp.make_node(id="H", claim_kind="level", falsifiers=[negative])
    hyp.apply_falsifier_check(n, {"v": -1})
    for _ in range(hyp.DORMANT_EPOCHS):
        hyp.advance_epoch(n, 0)
    assert n.status == "refuted"


# ---------------------------------------------------------------------------
# compute_status is pure (does not mutate); refresh_status writes it
# ---------------------------------------------------------------------------

def test_compute_status_is_pure_refresh_writes():
    n = hyp.make_node(id="H", claim_kind="level")
    n.beta = hyp.Beta(1, 9)                             # low posterior, but status not yet refreshed
    assert n.status == "open"                           # compute has not been re-run
    assert hyp.compute_status(n) == "refuted"           # pure derivation
    assert n.status == "open"                           # compute_status did NOT mutate
    hyp.refresh_status(n)
    assert n.status == "refuted"                         # refresh writes it


# ---------------------------------------------------------------------------
# PURITY — the module imports no I/O (SPEC: PURE, mega-sprint inv. 13)
# ---------------------------------------------------------------------------

def test_module_imports_no_io():
    src = pathlib.Path(hyp.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    forbidden = {
        "os", "random", "time", "datetime", "pathlib", "socket", "sqlite3",
        "requests", "subprocess", "urllib", "http", "aiohttp",
    }
    assert not (imported & forbidden), f"impure imports found: {sorted(imported & forbidden)}"
