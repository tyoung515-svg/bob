from __future__ import annotations

from core.memory._rrf import rrf_fuse
from core.memory.models import Hit


def _h(id_: str, score: float) -> Hit:
    return Hit(id=id_, score=score, payload={})


def test_empty_input_returns_empty():
    assert rrf_fuse([]) == []


def test_single_ranking_identity_mapped():
    ranking = [_h("a", 0.9), _h("b", 0.8), _h("c", 0.7)]
    fused = rrf_fuse([ranking])

    assert len(fused) == 3
    assert fused[0].id == "a"
    assert fused[1].id == "b"
    assert fused[2].id == "c"
    assert abs(fused[0].score - 1.0 / 61.0) < 1e-10
    assert abs(fused[1].score - 1.0 / 62.0) < 1e-10
    assert abs(fused[2].score - 1.0 / 63.0) < 1e-10


def test_two_rankings_overlapping_fused():
    r1 = [_h("a", 0.9), _h("b", 0.8)]
    r2 = [_h("b", 0.85), _h("c", 0.75)]
    fused = rrf_fuse([r1, r2])

    assert len(fused) == 3
    assert fused[0].id == "b"

    score_b = 1.0 / 62.0 + 1.0 / 61.0
    score_a = 1.0 / 61.0
    score_c = 1.0 / 62.0
    assert abs(fused[0].score - score_b) < 1e-10
    assert fused[1].id == "a"
    assert abs(fused[1].score - score_a) < 1e-10
    assert fused[2].id == "c"
    assert abs(fused[2].score - score_c) < 1e-10


def test_property_bit_identical_across_runs():
    r1 = [_h("a", 0.9), _h("b", 0.8)]
    r2 = [_h("c", 0.85)]

    results = [rrf_fuse([r1, r2]) for _ in range(50)]
    first = results[0]
    for r in results[1:]:
        assert r == first


def test_k_constant_changes_scores_preserves_order():
    ranking = [_h("x", 0.95), _h("y", 0.85)]
    fused_k60 = rrf_fuse([ranking], k=60)
    fused_k10 = rrf_fuse([ranking], k=10)

    assert fused_k60[0].id == fused_k10[0].id == "x"
    assert fused_k60[1].id == fused_k10[1].id == "y"
    assert fused_k60[0].score != fused_k10[0].score
    assert fused_k10[0].score > fused_k60[0].score


def test_ties_resolved_by_id():
    r1 = [_h("a", 0.9), _h("b", 0.8)]
    r2 = [_h("b", 0.8), _h("a", 0.9)]
    fused = rrf_fuse([r1, r2])

    assert len(fused) == 2
    assert fused[0].id == "a"
    assert fused[1].id == "b"
