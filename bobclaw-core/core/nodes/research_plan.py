"""BoBClaw Core — Research orchestrator node (MS2-R2).

This module implements the MS2-R2 research orchestrator: it decomposes a
research question into sub-questions and then picks the execution effort tier
deterministically from the sub-question count.

Design references:
- DESIGN-MS-D2 §3 (MS2-R2 orchestration)
- DECISIONS-MS2 OD#4 (determinism is on the COUNT, not an LLM-declared tier)

The orchestrator is routing-only: it decides which already-landed arm runs:
- 1 sub-question  -> single execute node
- 2-4 sub-questions -> flat fan-out (dispatch/worker/join)
- 10+ sub-questions -> hierarchical managers (manager_dispatch/mini_manager/manager_join)

The 5-9 band is conservatively mapped to flat fan-out, because fan-out is the
landed arm for "a handful of parallel subagents" and the hierarchical tree is
reserved for the spec's explicit 10+ boundary.

R3 (subagent/IterResearch loop + condensed-return firewall) and R4
(CitationAgent) are separate modules that consume the routing decision produced
here.
"""

from __future__ import annotations

import core.nodes.decompose as decompose
from core.config import RESEARCH_FANOUT_MIN, RESEARCH_HIER_THRESHOLD
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.graph import AgentState


TIER_SINGLE = "single"
TIER_FANOUT = "fanout"
TIER_HIER = "hierarchical"


def select_research_tier(n: int) -> str:
    """Return the deterministic effort tier for a sub-question count.

    PURE arithmetic on the count (OD#4 — no model in the routing). Boundaries are
    the ``core.config`` constants, so the three spec tiers are config-sourced:
    ``n < RESEARCH_FANOUT_MIN`` (n<=1) -> single; ``RESEARCH_FANOUT_MIN <= n <
    RESEARCH_HIER_THRESHOLD`` (2..9) -> fan-out; ``n >= RESEARCH_HIER_THRESHOLD``
    (10+) -> hierarchical. The 5-9 band is the conservative interpolation onto the
    landed flat fan-out (the spec names only 1 / 2-4 / 10+).

    Args:
        n: Number of sub-questions produced by decomposition.

    Returns:
        One of ``TIER_SINGLE``, ``TIER_FANOUT``, or ``TIER_HIER``.
    """
    if n < RESEARCH_FANOUT_MIN:          # n <= 1
        return TIER_SINGLE
    if n < RESEARCH_HIER_THRESHOLD:      # 2 .. 9
        return TIER_FANOUT
    return TIER_HIER                     # 10+


async def research_plan_node(state: "AgentState") -> dict:
    """LangGraph node: plan a research turn and select its execution tier.

    Steps:
      1. Read ``task`` (the research question) and ``backend`` from state.
      2. Decompose the question into sub-questions via ``decompose._call_llm`` on
         the RESOLVED research backend (set by ``route_node``) — the orchestrator's
         authoritative plan. We deliberately do NOT reuse the upstream
         ``decompose_node`` output: that node runs BEFORE ``route`` (so it sees
         ``backend="local"`` and, with no local model, fail-opens to ``[task]``),
         which would wrongly collapse every research turn to the single tier. The
         module-attribute reference (``decompose._call_llm``) preserves the
         test-injection seam.
      3. Count sub-questions and select the tier via ``select_research_tier``.
      4. Return a state delta with subtasks, tier, a bookkeeping message, and
         ``fanout_width`` set only for the fan-out tier.

    The node never mutates ``task`` and never sets backend/face/escalation keys.
    It never raises: decomposition failures degrade to ``[question]`` (single tier).
    """
    question = state.get("task", "")
    backend = state.get("backend", "local")

    subtasks = await decompose._call_llm(question, backend)

    n = len(subtasks)
    tier = select_research_tier(n)

    if tier == TIER_SINGLE:
        arm = "execute"
    elif tier == TIER_FANOUT:
        arm = "dispatch"
    else:
        arm = "manager_dispatch"

    out: dict = {
        "subtasks": subtasks,
        "research_tier": tier,
        "messages": [
            {
                "role": "system",
                "content": f"research_plan: tier={tier}, count={n}, arm={arm}",
            }
        ],
    }

    if tier == TIER_FANOUT:
        out["fanout_width"] = n

    return out
