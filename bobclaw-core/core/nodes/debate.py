"""
BoBClaw — CoCouncil P3 debate convergence gate.

The debate analogue of ``grounding_node``: it closes each debate round, decides
converge-vs-loop by a DETERMINISTIC Idea-ID convergence test (no LLM Chair — that
referee is a later refinement), enforces the per-run round + cost bounds, and owns
the SINGLE exactly-once commit of the final answer.

Loop topology (wired in graph.py, D3):
    panel_dispatch → panel_worker×N → synthesize → debate_converge
                                                    → {panel_dispatch (next round) | END}

``synthesize_node`` DEFERS its commit every round in debate mode (``_should_defer``
includes ``is_debate``), stashing the answer on ``council_pending_answer``
(last-write-wins). ``debate_converge_node`` is the SOLE emitter — it commits the
deferred answer exactly once on the converge path (mirrors grounding's ``_converge``
chokepoint), so an intermediate round never surfaces/persists. Debate counts rounds
on ``council_round`` (grounding owns ``council_restart``).

Convergence (deterministic): read the latest ``council_handoff["active_debate"]``
(the [ACTIVE DEBATE] Idea-IDs parsed by synthesize each round). Converge when it is
empty (all resolved), OR equals the prior round's set (no delta), OR the round cap
is reached. Else loop: increment ``council_round``, stash the current debate set as
``prev_active_debate``, clear ``resolved_seats``/``panel_task`` so panel_dispatch
rebuilds with the new prior-positions context, and set the ``debate_continue``
sentinel (``_route_after_debate`` loops on it). Bounded by ``protocol_bounds``
(``max_rounds`` / ``max_usd``) over the global defaults, fail-loud on a cost breach.
"""
from __future__ import annotations

import logging

from core.config import COUNCIL_MAX_ROUNDS, COUNCIL_MAX_USD, DEBATE_ROUND_USD
from core.council.events import (
    PHASE_BLOCKED,
    PHASE_ROUND_ADVANCED,
    PHASE_ROUND_CONVERGED,
    emit_council_event,
)
from core.nodes._bounds import bound_float, bound_int

logger = logging.getLogger(__name__)


def is_debate(spec) -> bool:
    """True when the run is a debate-shaped council (``mode == "debate"``). Read by
    BOTH ``synthesize_node`` (defer the commit) and this node (own the commit), so
    the defer/commit chokepoint stays single-sourced."""
    return (((spec or {}).get("mode")) or "").strip().lower() == "debate"


def _active_ids(handoff) -> set:
    """The handoff's [ACTIVE DEBATE] Idea-IDs as a set (order-independent)."""
    return {str(x).strip() for x in ((handoff or {}).get("active_debate") or []) if str(x).strip()}


async def _commit(state: dict, *, cost_usd: float, error: "str | None" = None) -> dict:
    """Converge: commit the deferred answer EXACTLY ONCE and clear the loop sentinel.

    synthesize stashed the answer on ``council_pending_answer`` (it deferred every
    round). We emit it once here via ``emit_synthesis`` (writer custom-chunk + L0)
    and clear the carrier. An intermediate (looping) round never reaches here, so it
    never commits. If there is no pending answer (synth-failure turn) we commit
    nothing. ``error`` (a cost-ceiling notice) rides ``out["error"]`` — one error
    frame, never the custom channel (no double-emit)."""
    out: dict = {"council_cost_usd": cost_usd}
    if error is not None:
        out["error"] = error
    pending = state.get("council_pending_answer") or {}
    if pending.get("content"):
        from core.nodes.synthesize import emit_synthesis
        out["messages"] = await emit_synthesis(
            state, pending["content"], pending.get("backend"))
        out["council_pending_answer"] = None
    # _route_after_debate loops iff debate_continue is set, so a converge MUST clear
    # any sentinel a prior loop round left (else a later converge would loop back).
    spec = state.get("council_spec") or {}
    if spec.get("debate_continue"):
        cleared = dict(spec)
        cleared.pop("debate_continue", None)
        out["council_spec"] = cleared
    return out


async def debate_converge_node(state: dict) -> dict:
    """Close a debate round: converge (commit once → END) or loop (next round).

    Returns a state delta. CONVERGE leaves no ``debate_continue`` (``_route_after_
    debate`` → END) and commits the deferred answer once. LOOP increments
    ``council_round`` + sets ``debate_continue`` (``_route_after_debate`` →
    panel_dispatch). A cost breach converges fail-loud with ``error=`` set.
    """
    spec = dict(state.get("council_spec") or {})
    bounds = spec.get("bounds") or {}
    round_idx = state.get("council_round") or 0
    prior_cost = state.get("council_cost_usd") or 0.0

    max_rounds = bound_int(bounds.get("max_rounds"), COUNCIL_MAX_ROUNDS)
    max_usd = bound_float(bounds.get("max_usd"), COUNCIL_MAX_USD)

    # This round's deliberation (N panel calls + the synth reconcile) is now spent.
    cost_after = prior_cost + DEBATE_ROUND_USD

    current = _active_ids(state.get("council_handoff") or {})
    prev = {str(x).strip() for x in (spec.get("prev_active_debate") or []) if str(x).strip()}

    # ── Deterministic convergence ────────────────────────────────────────────
    if not current:
        converged, reason = True, "no active debate"
    elif current == prev:
        converged, reason = True, "no-delta round"
    elif round_idx + 1 >= max_rounds:
        converged, reason = True, f"round cap {max_rounds}"
    else:
        converged, reason = False, ""

    # ── Cost ceiling — fail loud BEFORE spending another round ───────────────
    if not converged:
        projected = cost_after + DEBATE_ROUND_USD
        if cost_after >= max_usd or projected > max_usd:
            msg = (f"Council debate cost ceiling reached (${cost_after:.2f} spent, "
                   f"ceiling ${max_usd:.2f}); returning the best answer so far.")
            logger.warning("debate ceiling breach: %s", msg)
            # U7 (opt-in): the theater's blocked/ceiling banner. NO-OP + byte-identical
            # when opt-in absent (does not touch the commit / final-answer path).
            await emit_council_event(
                spec, state, PHASE_BLOCKED, round_idx=round_idx,
                extra={"reason": "cost_ceiling", "cost_usd": round(cost_after, 6),
                       "active_debate": sorted(current)},
            )
            return await _commit(state, cost_usd=cost_after, error=msg)

    if converged:
        logger.info("debate converged after round %d (%s); committing", round_idx, reason)
        # U7 (opt-in): the theater's converged banner (round closed → END).
        await emit_council_event(
            spec, state, PHASE_ROUND_CONVERGED, round_idx=round_idx,
            extra={"reason": reason, "active_debate": sorted(current)},
        )
        return await _commit(state, cost_usd=cost_after)

    # ── Loop: set up the next round ──────────────────────────────────────────
    spec["prev_active_debate"] = sorted(current)
    spec["debate_continue"] = True
    spec.pop("resolved_seats", None)
    spec.pop("panel_task", None)
    logger.info("debate round %d → %d (active debate: %s)", round_idx, round_idx + 1,
                sorted(current))
    # U7 (opt-in): the theater's "round advanced" transition (loop to next round).
    # NO-OP + byte-identical when opt-in absent (does not alter the loop delta).
    await emit_council_event(
        spec, state, PHASE_ROUND_ADVANCED, round_idx=round_idx,
        extra={"next_round": round_idx + 1, "active_debate": sorted(current)},
    )
    return {
        "council_spec": spec,
        "council_round": round_idx + 1,
        "council_cost_usd": cost_after,
    }
