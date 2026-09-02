"""MS#4 · P1 — Slice 5 tests (council escalation policy).

PURE. Deterministic tiering off a routing decision: grounded → quorum → full fan-out, with the
spanned notebook set carried forward as the synthesis-grant scope.
"""
from core.surface import EscalationTier, plan_escalation
from core.surface.router import RoutingDecision, route_query


def _decision(escalate, reason, margin, top3):
    return RoutingDecision(
        notebook_id=top3[0][0] if top3 else None,
        escalate=escalate,
        provenance={"top3": top3, "margin": margin, "reason": reason},
    )


def test_grounded_no_escalation():
    d = _decision(False, "grounded", 0.9, [("nb-A", 0.99), ("nb-B", 0.09)])
    req = plan_escalation(d)
    assert req.tier is EscalationTier.GROUNDED and req.notebook_ids == ["nb-A"]


def test_far_from_all_is_full_fanout():
    d = _decision(True, "far_from_all", 0.0, [("nb-A", 0.1), ("nb-B", 0.1)])
    req = plan_escalation(d)
    assert req.tier is EscalationTier.FULL_FANOUT
    assert set(req.notebook_ids) == {"nb-A", "nb-B"}


def test_deep_ambiguity_is_full_fanout():
    d = _decision(True, "ambiguous_span", 0.01, [("nb-A", 0.71), ("nb-B", 0.70)])  # margin < 0.02
    assert plan_escalation(d).tier is EscalationTier.FULL_FANOUT


def test_moderate_ambiguity_is_quorum_over_spanned():
    d = _decision(True, "ambiguous_span", 0.03, [("nb-A", 0.72), ("nb-B", 0.70), ("nb-C", 0.20)])
    req = plan_escalation(d)                     # margin 0.03 ≥ fanout_margin 0.02 → quorum
    assert req.tier is EscalationTier.QUORUM
    assert set(req.notebook_ids) == {"nb-A", "nb-B"}   # nb-C outside the span band


def test_end_to_end_from_router():
    centroids = {"nb-A": [1.0, 0.0, 0.0], "nb-B": [0.0, 1.0, 0.0]}
    grounded = plan_escalation(route_query([0.99, 0.01, 0.0], centroids))
    assert grounded.tier is EscalationTier.GROUNDED
    spanning = plan_escalation(route_query([1.0, 1.0, 0.0], centroids))   # equidistant
    assert spanning.tier is EscalationTier.FULL_FANOUT   # margin ~0 → deep ambiguity
