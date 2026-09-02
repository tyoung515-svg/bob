"""P1 notebook-grounded surface — council escalation policy (MS#4 · Slice 5, §5).

When the centroid router ESCALATES (an edge / ambiguous query, Slice 4), this decides HOW MUCH
council to bring and WHICH notebooks the query spans — a deterministic tier from the routing
margin (margin → quorum → full fan-out). The spanned notebook set is the candidate scope handed
to the ForestOS synthesis grant (Slice 6). No model in the tiering. PURE.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.surface.router import RoutingDecision


class EscalationTier(str, Enum):
    GROUNDED = "grounded"          # no escalation — answer from the single bound notebook
    QUORUM = "quorum"              # a small council over the spanned notebooks
    FULL_FANOUT = "full_fanout"    # full council fan-out (far-from-all / very ambiguous)


@dataclass(frozen=True)
class EscalationRequest:
    tier: EscalationTier
    notebook_ids: list[str]        # the notebooks the query spans → the synthesis-grant scope
    reason: str


def plan_escalation(
    decision: RoutingDecision,
    *,
    span_band: float = 0.1,
    fanout_margin: float = 0.02,
) -> EscalationRequest:
    """Tier the escalation for a routing decision.

    Not escalating ⇒ GROUNDED (answer from the one notebook). Escalating ⇒ FULL_FANOUT when the
    query is far from every notebook or the top-1/top-2 margin is below ``fanout_margin`` (deeply
    ambiguous); otherwise QUORUM over the notebooks within ``span_band`` of the top similarity.
    """
    top3 = decision.provenance.get("top3") or []
    if not decision.escalate:
        ids = [decision.notebook_id] if decision.notebook_id else []
        return EscalationRequest(EscalationTier.GROUNDED, ids, "grounded")

    reason = decision.provenance.get("reason", "escalate")
    spanned = ([nb for nb, s in top3 if top3[0][1] - s <= span_band] if top3 else [])
    margin = decision.provenance.get("margin")

    if reason == "far_from_all" or reason == "no_centroids":
        return EscalationRequest(EscalationTier.FULL_FANOUT, [nb for nb, _ in top3], reason)
    if margin is not None and margin < fanout_margin:
        return EscalationRequest(EscalationTier.FULL_FANOUT, spanned or [nb for nb, _ in top3], reason)
    return EscalationRequest(EscalationTier.QUORUM, spanned, reason)


__all__ = ["EscalationTier", "EscalationRequest", "plan_escalation"]
