"""MS#4 · E1 earning layer — Slice 1 tests (critic seat-pinning D-1/FM-3 · PoC gate FM-1 · D-2).

Deterministic: family-divergence critic config, the compound PoC gate (entailment AND
observed==claimed severity, Default-FAIL), and the triage rule that a bounty without a
reproduced PoC can never fast-approve.
"""
import pytest

from core.earn import (
    CriticConfigError,
    assert_family_divergence,
    critic_backend_for,
    evaluate_poc,
    resolve_critic_backend,
)
from core.earn.contract import Lane, TriageBucket
from core.verify.entailment import EntailmentVerdict
from core.verify.postcondition import family_of


# ── D-1 / FM-3: lane-pinned critic + family divergence ───────────────────────

def test_lane_pinned_critic_backends():
    assert critic_backend_for(Lane.BUG_BOUNTY) == "minimax"
    assert critic_backend_for(Lane.DATASET) == "glm_5_2"


def test_family_divergence_enforced():
    # same family (both deepseek) → correlated-failure-blind → raise
    with pytest.raises(CriticConfigError):
        assert_family_divergence("deepseek_v4_flash", "deepseek_v4")   # both 'deepseek'
    # different families → ok
    assert family_of("deepseek_v4_flash") != family_of("minimax")
    assert_family_divergence("deepseek_v4_flash", "minimax")


def test_resolve_critic_backend_rejects_same_family_proposer():
    # bounty critic is minimax; a minimax-family proposer collides → raise
    with pytest.raises(CriticConfigError):
        resolve_critic_backend(Lane.BUG_BOUNTY, "minimax")
    # a deepseek proposer diverges from the minimax critic → resolves
    assert resolve_critic_backend(Lane.BUG_BOUNTY, "deepseek_v4_flash") == "minimax"


# ── FM-1: PoC compound gate (Default-FAIL) ───────────────────────────────────

def test_poc_gate_compound_and_default_fail():
    assert evaluate_poc(EntailmentVerdict.ENTAILED, "high", "high") is True
    assert evaluate_poc(EntailmentVerdict.ENTAILED, "high", "medium") is False   # severity mismatch
    assert evaluate_poc(EntailmentVerdict.NOT_ENTAILED, "high", "high") is False # not entailed
    assert evaluate_poc(EntailmentVerdict.ENTAILED, None, "high") is False       # no repro log
    assert evaluate_poc("entailed", "High", "high") is True                      # string + case-insensitive


# ── D-2: a bounty without a reproduced PoC can never fast-approve ─────────────

def test_bounty_without_poc_cannot_fast_approve(bounty):
    assert bounty(poc_reproduced=None).run_triage() is TriageBucket.NEEDS_REVIEW   # no PoC → review
    assert bounty(poc_reproduced=False).run_triage() is TriageBucket.NEEDS_REVIEW
    assert bounty(poc_reproduced=True).run_triage() is TriageBucket.FAST_APPROVE   # reproduced + clean
