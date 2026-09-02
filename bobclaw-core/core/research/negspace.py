"""RS1 negative-space — rewired-null significance (MS#4 · Slice 0).

The stepped-leader hypothesis scores a candidate k-ary gap by a graph metric and asks whether it
is significant vs a DEGREE-PRESERVING rewired null — so a high score is not just a popularity /
degree artifact (`CONV-PRELIM-RANK` → z-score vs the rewired null is the significance filter).
Slice 0 lands the deterministic statistical core: degree-preserving rewiring, the null
distribution, the z-score, and a positive control (a planted convergence motif must score high; a
random graph ~0).

Runs on a SYNTHETIC graph here. The frozen eval-LKS git tag + the κ≥0.6 human calibration panel
are Travis-only inputs (BLOCKED — see LOOP-TRACKER), needed for the real-corpus run and the
epistemic-irreducibility ceiling (`GATE-CEILING-HUMAN`). PURE given an injected ``random.Random``.
"""
from __future__ import annotations

import statistics
from collections import Counter
from typing import Callable, Iterable

Edge = tuple


def _norm_edges(edges: Iterable[Edge]) -> list[Edge]:
    """Undirected edges as sorted 2-tuples, deduped, self-loops dropped."""
    out, seen = [], set()
    for u, v in edges:
        if u == v:
            continue
        e = (u, v) if u <= v else (v, u)
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def degree_sequence(edges: Iterable[Edge]) -> dict:
    c: Counter = Counter()
    for u, v in _norm_edges(edges):
        c[u] += 1
        c[v] += 1
    return dict(c)


def triangle_count(edges: Iterable[Edge]) -> int:
    """Number of triangles — a proxy for k-ary convergence motifs (three claims that co-close)."""
    adj: dict = {}
    for u, v in _norm_edges(edges):
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)
    nodes = sorted(adj)
    tri = 0
    for i, a in enumerate(nodes):
        na = adj[a]
        for b in na:
            if b <= a:
                continue
            for c in adj[b]:
                if c <= b:
                    continue
                if c in na:
                    tri += 1
    return tri


def rewire_degree_preserving(edges: Iterable[Edge], *, rng, swaps: int | None = None) -> list[Edge]:
    """Degree-preserving double-edge swap: same degree sequence, randomized topology.
    ``(a,b),(c,d) → (a,d),(c,b)`` when it introduces no self-loop or multi-edge."""
    es = _norm_edges(edges)
    m = len(es)
    if m < 2:
        return es
    eset = set(es)
    edges_l = list(es)
    for _ in range(swaps if swaps is not None else 10 * m):
        i, j = rng.randrange(m), rng.randrange(m)
        if i == j:
            continue
        a, b = edges_l[i]
        c, d = edges_l[j]
        if len({a, b, c, d}) < 4:
            continue
        na = (a, d) if a <= d else (d, a)
        nb = (c, b) if c <= b else (b, c)
        if na in eset or nb in eset:
            continue
        eset.discard(edges_l[i])
        eset.discard(edges_l[j])
        eset.add(na)
        eset.add(nb)
        edges_l[i], edges_l[j] = na, nb
    return edges_l


def rewired_null_distribution(
    edges: Iterable[Edge], metric: Callable[[list], float], *, rng, n_samples: int = 100
) -> list[float]:
    """The null distribution of ``metric`` over ``n_samples`` degree-preserving rewirings."""
    es = _norm_edges(edges)
    return [float(metric(rewire_degree_preserving(es, rng=rng))) for _ in range(n_samples)]


def zscore(observed: float, null_dist: list[float]) -> float:
    """z of ``observed`` vs the null distribution. 0.0 for a degenerate (n<2 or zero-variance) null."""
    if len(null_dist) < 2:
        return 0.0
    sd = statistics.pstdev(null_dist)
    if sd == 0.0:
        return 0.0
    return (observed - statistics.fmean(null_dist)) / sd


__all__ = [
    "degree_sequence", "triangle_count", "rewire_degree_preserving",
    "rewired_null_distribution", "zscore",
]
