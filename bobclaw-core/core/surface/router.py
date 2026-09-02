"""P1 notebook-grounded surface — centroid coarse-router + F-2 routing eval (MS#4 · Slice 4).

Routes a query to its notebook by centroid similarity, and decides proceed-vs-ESCALATE: a query
that is far from every notebook, or ambiguous between two, hits the "edge" and should escalate to
a synthesis-grant request (Slice 6), not silently answer from one notebook.

F-2 (the red-team catch — centroid routing assumes a topical coherence that fails at scale):
- every decision carries PROVENANCE (top-3 centroid similarities + the margin), never opaque;
- ``routing_eval`` is a BLOCKING pre-deploy check over planted adversarial cases (cross-notebook
  that SHOULD escalate; within-notebook that should NOT) — false/missed escalation must stay
  under threshold before the router ships. PURE (stdlib math, no model).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Iterable, Optional


def encode_centroid(vec: list[float]) -> bytes:
    return json.dumps([float(x) for x in vec], separators=(",", ":")).encode("utf-8")


def decode_centroid(blob: Optional[bytes]) -> Optional[list[float]]:
    if not blob:
        return None
    return [float(x) for x in json.loads(blob.decode("utf-8"))]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]. 0.0 for a zero/mismatched-length vector (fail-safe, never raises)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass(frozen=True)
class RoutingDecision:
    notebook_id: Optional[str]        # the primary notebook (None if nothing is close enough)
    escalate: bool                    # True ⇒ request a synthesis grant (edge / ambiguous / far)
    provenance: dict                  # {top3: [(id, sim)], margin, reason}


def route_query(
    query_vec: list[float],
    centroids: dict[str, list[float]],
    *,
    min_similarity: float = 0.5,
    ambiguity_margin: float = 0.05,
) -> RoutingDecision:
    """Route ``query_vec`` to a notebook, escalating on an edge/ambiguous/far query.

    Escalate iff: the best similarity < ``min_similarity`` (far from all), OR the top-1/top-2 gap
    < ``ambiguity_margin`` (spans two notebooks). Otherwise proceed to the top-1 notebook.
    """
    sims = sorted(
        ((nb, cosine(query_vec, c)) for nb, c in centroids.items()),
        key=lambda t: (-t[1], t[0]),
    )
    top3 = [(nb, round(s, 6)) for nb, s in sims[:3]]
    if not sims:
        return RoutingDecision(None, True, {"top3": [], "margin": None, "reason": "no_centroids"})
    best_nb, best_sim = sims[0]
    margin = best_sim - sims[1][1] if len(sims) > 1 else best_sim
    if best_sim < min_similarity:
        return RoutingDecision(best_nb, True, {"top3": top3, "margin": round(margin, 6),
                                               "reason": "far_from_all"})
    if len(sims) > 1 and margin < ambiguity_margin:
        return RoutingDecision(best_nb, True, {"top3": top3, "margin": round(margin, 6),
                                               "reason": "ambiguous_span"})
    return RoutingDecision(best_nb, False, {"top3": top3, "margin": round(margin, 6),
                                            "reason": "grounded"})


def routing_eval(
    cases: Iterable[dict],
    centroids: dict[str, list[float]],
    *,
    min_similarity: float = 0.5,
    ambiguity_margin: float = 0.05,
    max_false_escalation: float = 0.2,
    max_missed_escalation: float = 0.0,
) -> dict:
    """F-2 blocking pre-deploy check. Each case = ``{query_vec, should_escalate: bool}``.

    Returns ``{n, false_escalation_rate, missed_escalation_rate, passed}``. ``passed`` gates deploy:
    missed escalations (answering from one notebook when the query spanned/left it) are the
    dangerous class → default threshold 0.0. Empty cases ⇒ not passed (never a vacuous green)."""
    cases = list(cases)
    if not cases:
        return {"n": 0, "false_escalation_rate": 0.0, "missed_escalation_rate": 0.0, "passed": False}
    false_esc = missed = should_esc = shouldnt_esc = 0
    for c in cases:
        d = route_query(c["query_vec"], centroids,
                        min_similarity=min_similarity, ambiguity_margin=ambiguity_margin)
        want = bool(c["should_escalate"])
        if want:
            should_esc += 1
            if not d.escalate:
                missed += 1
        else:
            shouldnt_esc += 1
            if d.escalate:
                false_esc += 1
    fer = false_esc / shouldnt_esc if shouldnt_esc else 0.0
    mer = missed / should_esc if should_esc else 0.0
    passed = fer <= max_false_escalation and mer <= max_missed_escalation
    return {"n": len(cases), "false_escalation_rate": fer, "missed_escalation_rate": mer,
            "passed": passed}


__all__ = [
    "encode_centroid", "decode_centroid", "cosine", "RoutingDecision",
    "route_query", "routing_eval",
]
