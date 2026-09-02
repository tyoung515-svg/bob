"""MS#4 · RS1 negative-space — Slice 0 tests (rewired-null significance + positive control).

Deterministic (seeded RNG). Degree-preserving rewiring keeps the degree sequence; the positive
control (a planted clique) scores high z vs the null; a sparse graph does not.
"""
import random

from core.research.negspace import (
    degree_sequence,
    rewire_degree_preserving,
    rewired_null_distribution,
    triangle_count,
    zscore,
)


def _clique(nodes):
    return [(a, b) for i, a in enumerate(nodes) for b in nodes[i + 1:]]


def test_triangle_count_known():
    assert triangle_count([(1, 2), (2, 3), (1, 3)]) == 1          # one triangle
    assert triangle_count(_clique([1, 2, 3, 4])) == 4            # K4 has 4 triangles
    assert triangle_count([(1, 2), (3, 4)]) == 0


def test_rewire_preserves_degree_sequence():
    edges = _clique([1, 2, 3, 4]) + [(4, 5), (5, 6), (6, 1)]
    rng = random.Random(42)
    rewired = rewire_degree_preserving(edges, rng=rng)
    assert degree_sequence(rewired) == degree_sequence(edges)     # degree-preserving invariant


def test_positive_control_planted_motif_is_significant():
    # a dense clique of triangles embedded in an otherwise sparse ring
    ring = [(i, i + 1) for i in range(6, 30)]
    planted = _clique([1, 2, 3, 4, 5]) + ring
    rng = random.Random(7)
    observed = triangle_count(planted)
    null = rewired_null_distribution(planted, triangle_count, rng=rng, n_samples=80)
    z = zscore(observed, null)
    assert observed == 10                                          # K5 → 10 triangles
    assert z > 2.0                                                 # significant vs the rewired null


def test_negative_control_sparse_graph_not_significant():
    ring = [(i, i + 1) for i in range(0, 30)] + [(29, 0)]         # a pure cycle: 0 triangles
    rng = random.Random(11)
    observed = triangle_count(ring)
    null = rewired_null_distribution(ring, triangle_count, rng=rng, n_samples=80)
    assert observed == 0
    assert abs(zscore(observed, null)) < 2.0                       # no planted signal → not significant


def test_zscore_degenerate_null_is_zero():
    assert zscore(5.0, []) == 0.0
    assert zscore(5.0, [3.0, 3.0, 3.0]) == 0.0                     # zero variance → 0
