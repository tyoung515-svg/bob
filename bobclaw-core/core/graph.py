"""
BoBClaw Core — Main LangGraph StateGraph

Exports:
    AgentState  — TypedDict for the shared graph state
    build_graph — compile graph with an optional checkpointer (used in tests)
    create_graph — async factory that wires in the Postgres checkpointer
"""
from __future__ import annotations

import logging
import operator
from typing import Annotated, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from core.config import MAX_FANOUT_WIDTH_BY_BACKEND
from core.memory import Fact
from core.memory.exceptions import EmbedderUnavailable, RetrievalProviderError
from core.nodes.approval import approval_node
from core.nodes.build_plan import plan_contracts_node
from core.nodes.build_verify import (
    _route_after_verify,
    repair_node,
    verify_node,
)
from core.nodes.council import council_node
from core.nodes.debate import debate_converge_node
from core.nodes.decompose import decompose_node
from core.nodes.dispatch import dispatch_node, _route_after_dispatch
from core.nodes.execute import execute_node
from core.nodes.grounding import grounding_node
from core.nodes.hier import (
    _route_after_manager_dispatch,
    manager_dispatch_node,
    manager_join_node,
    mini_manager_node,
)
from core.nodes.join import join_node
from core.nodes.panel import (
    _route_after_panel,
    panel_dispatch_node,
    panel_worker_node,
)
from core.nodes.postcondition import postcondition_node
from core.nodes.recall import recall_node
from core.nodes.research_plan import (
    TIER_FANOUT,
    TIER_HIER,
    research_plan_node,
    select_research_tier,
)
from core.nodes.route import route_node
from core.nodes.synthesize import synthesize_node
from core.nodes.worker import worker_node

logger = logging.getLogger(__name__)


# ─── Shared state ─────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    # Chat history — new messages are appended (operator.add reducer)
    messages: Annotated[list, operator.add]
    # Current task description
    task: str
    # Stable BoBClaw conversation id from the HTTP request.
    conversation_id: Optional[str]
    # the response-language locale (BCP-47-ish: "en" | "zh-Hans" | "zh-Hant"); optional, default "en"; absent/"en" => no directive => byte-identical.
    locale: Optional[str]
    # Gateway-derived user identity (JWT subject). Threaded into tool contextvars.
    user_id: Optional[str]
    # Active face / persona
    face_id: str
    # Optional model name override supplied by the user
    model_override: Optional[str]
    # Optional backend override supplied by the user (hard route override,
    # e.g. "deepseek_v4_flash"). When set, route_node skips face resolution.
    backend_override: Optional[str]
    # ── JOAT v0: per-conversation team pin ──
    # Optional named team (e.g. "cloud-heavy") selecting role→backend resolution
    # for this turn. route_node threads it into teams.resolve. Precedence:
    # this pin > BOBCLAW_TEAM env > default (per-face). None ⇒ today's behaviour.
    team: Optional[str]
    # ── Profiles (HOW layer): per-conversation profile name. route_node loads it;
    # a council-shaped profile compiles to council_spec, a plain roster is a team.
    profile_name: Optional[str]
    # Headless contract: honor the explicit face pin and SKIP the intent heuristic.
    # Set for an agent-token turn (the gateway) or by a profile that opts in. The
    # heuristic can never select an explicitly-pinned face (e.g. planner-cc-edit).
    pin_authoritative: Optional[bool]
    # Resolved backend name or URL (set by route_node)
    backend: str
    # Tools the active face is allowed to use
    tools_allowed: list[str]
    # True when the next action requires human sign-off
    approval_required: bool
    # Human's approval response ("approve" / "reject" / None)
    approval_response: Optional[str]
    # Files, documents, screenshots produced during execution
    artifacts: Annotated[list[dict], operator.add]
    # Last error message, if any
    error: Optional[str]
    # Optional dispatch subtask from planner decomposition
    dispatch_subtask: Optional[dict]
    # Subtasks produced by decompose_node (used by route_node for bulk-dispatch heuristic)
    subtasks: Optional[list[str]]
    # Optional phase marker for routing (e.g. "dispatch", "execute", "build")
    phase: Optional[str]
    # ── NEW for fan-out (handoff 005) ──
    # Populated by dispatch_node when fan-out fires; consumed by worker_node via Send.
    fanout_subtasks: Optional[list[dict]]   # [{idx: int, text: str}, ...]
    # Per-worker results, accumulated via reducer. Order non-deterministic across
    # parallel workers — each entry carries `idx` so `join_node` can sort.
    worker_results: Annotated[list[dict], operator.add]
    # Optional explicit width override; falls back to len(subtasks).
    fanout_width: Optional[int]
    # ── Hierarchical-managers (2-level agent tree) ──
    # Trigger: when truthy, recall routes to manager_dispatch (its own arm, never
    # shared with the council/build/flat-fan-out). Absent ⇒ byte-identical to today.
    hierarchical: Optional[bool]
    # Sections produced by manager_dispatch ([{idx, subtasks:[...]}]); one per mini_manager.
    sections: Optional[list[dict]]
    # Per-section results, accumulated via reducer. Order non-deterministic across
    # parallel mini-managers — each entry carries `idx` so manager_join sorts.
    section_results: Annotated[list[dict], operator.add]
    # Optional per-turn override of the number of sections (mini-managers).
    manager_max_sections: Optional[int]
    # Escalation backend for 429 fallback (set by route_node)
    escalation_backend: Optional[str]
    # Claude Code posture (set by route_node from the active face's cc_posture).
    # execute_node's claude_code block reads this and threads it to the CLI.
    cc_posture: Optional[dict]
    # Claude Code session id to resume for this conversation, when known.
    cc_resume_session_id: Optional[str]
    # Antigravity (agy) posture. Unlike cc_posture, route_node does NOT thread
    # this — execute_node's agy_code block reads it from the active face registry
    # (or an explicit override here), and the fan-out path threads the worker
    # face's model via the Send sub_state.
    agy_posture: Optional[dict]
    # agy conversation UUID to resume for this conversation, when known.
    agy_resume_session_id: Optional[str]
    # Codex (codex_code) posture. Like agy_posture, route_node does NOT thread it —
    # execute_node's codex_code block reads it from the face registry (or an explicit
    # override here); the fan-out threads the worker model via the Send sub_state.
    codex_posture: Optional[dict]
    # codex thread_id to resume for this conversation, when known.
    codex_resume_session_id: Optional[str]
    # Workspace directory for workspace-bound workers (e.g. OpenCode)
    workspace_dir: Optional[str]
    # ── Wave-chunking (handoff 007 Phase 2) ──
    # Current wave index for per-backend width cap re-entry.
    fanout_wave: Optional[int]
    # ── Memory integration (Sprint INT-1) ──
    # L1 facts retrieved by recall_node on each agent turn.
    recalled_facts: Optional[list[Fact]]
    # ── Projects (server-side workspaces) ──
    # Project-level instructions for the conversation's project, resolved by the
    # gateway and passed in the /api/chat payload. execute_node splices it into
    # the system prompt (mirrors the recalled_facts splice). None when the
    # conversation belongs to no project.
    project_instructions: Optional[str]
    # ── Gate Router scope (GR-P2/P4) ──
    # Declared blast radius/intent for the job. Threaded to the cc_edit Gate
    # path and to fan-out workers for scope-drift review. None/{} when absent.
    scope: Optional[dict]
    # ── CC approved-edit path (C4) ──
    # The proposed unified diff a planner-cc-edit turn captured (parked across
    # the approval interrupt; applied by execute_node on approve).
    cc_pending_edit: Optional[dict]
    # Approval surface: when a node raises approval_required it sets these so
    # the SSE layer emits the right action_type + details (default action is
    # "task_approval" when unset, preserving pre-C4 behaviour).
    approval_action_type: Optional[str]
    approval_details: Optional[dict]
    # Optional human-edited diff supplied on approve (cc_edit edit_content
    # override). Threaded by /api/chat/approval from the gateway decide proxy.
    approval_edit_content: Optional[str]
    # ── CoCouncil (P1b) ──
    # Set by route_node ONLY for the `council-max` face. Carries the
    # deliberation spec: {mode: "fusion"|"sequential", seats: [postures],
    # synth_backend, resolved_seats (added by panel_dispatch), panel_task}.
    # When None/absent the graph behaves exactly as today (additive branch).
    council_spec: Optional[dict]
    # Reserved for P3 debate loop / P2 grounded restart. Unused in P1b but
    # declared now so state is stable across phases.
    council_round: Optional[int]
    council_restart: Optional[int]
    # Per-seat fusion-panel results, accumulated via reducer. Order is
    # non-deterministic across parallel seats — each entry carries `idx` so
    # synthesize_node sorts deterministically (mirrors worker_results).
    panel_results: Annotated[list[dict], operator.add]
    # The parsed COUNCIL HANDOFF block (dataclass-as-dict) set by
    # synthesize_node / council_node.
    council_handoff: Optional[dict]
    # Reserved for P5 LKS question/project branch scoping. Unused in P1b.
    branch_id: Optional[str]
    # ── CoCouncil (P2) — pre-close grounding gate ──
    # The grounding node's parsed verdict ({claims, research, drift} | parse-error
    # | None). Set by grounding_node; carried for observability / the handoff.
    grounding_verdict: Optional[dict]
    # Cumulative council-run cost (USD) across panel + synth + grounding spawns.
    # Enforced against COUNCIL_MAX_USD by grounding_node (fail-loud on breach).
    council_cost_usd: Optional[float]
    # When grounding is ON, synthesize_node defers its client emit + messages
    # append to converge-time (grounding_node) so an intermediate (restarted)
    # round never surfaces/persists. It stashes the pending answer here:
    # {"content": <synthesis>, "backend": <used_synth>}. Last-write-wins
    # (no reducer) so a re-run round overwrites the drifted round's value;
    # grounding_node consumes it on EVERY converge path and ignores it on a
    # restart. None when grounding is OFF (synthesize commits in-node) or when
    # there is nothing pending (synth-failure path clears it).
    council_pending_answer: Optional[dict]
    # ── Build pipeline (Feature 2) — agentic plan→build→test→repair loop ──────
    # ON the agentic path (writes files + runs pytest), DISTINCT from the council.
    # The whole build path is gated on ``build_contracts``: a turn that never plans
    # contracts leaves these absent and is byte-identical to today. Set by
    # plan_contracts_node (P0); consumed by the build fan-out (P1) + verify/repair
    # loop (P2). Wiring (the routing arm) lands in P1; P0 only declares the state.
    #
    # The ENTRY trigger: when truthy, recall routes to plan_contracts (its own arm,
    # never shared with the council). Set by the caller / a build face/profile (the
    # E2E harness drives it directly). Absent ⇒ today's behaviour, byte-identical.
    build_request: Optional[bool]
    # The validated contract list ({name, signature, doc, cases}) the apex emitted —
    # the "skeleton". Its presence is the downstream build-path gate
    # (dispatch/worker/join branch on it).
    build_contracts: Optional[list[dict]]
    # Per-turn sandbox dir (under BUILD_WORKSPACE_ROOT, outside the repo) where the
    # skeleton + impls are written and the build/test subprocess runs.
    build_workspace: Optional[str]
    # Worker-produced implementations, accumulated via reducer. Order is
    # non-deterministic across parallel build workers — each entry is
    # {name, source}; the build join merges by name (mirrors worker_results/idx).
    build_impls: Annotated[list[dict], operator.add]
    # The latest verify-gate report: the skeleton build-empty result from P0, then
    # the pytest/build/CLI gate from P2. Last-write-wins (observability + routing).
    verify_report: Optional[dict]
    # Repair-loop counter, bounded by BUILD_REPAIR_BUDGET (P2). 0 after planning.
    repair_round: Optional[int]
    # Optional per-turn override of how many contracts plan_contracts requests
    # (the live E2E drives a small N through the graph). Falls back to
    # BUILD_DEFAULT_UNITS when unset.
    build_units: Optional[int]
    # ── Verification spine §2.6 tier-1 (MS-2) — post-condition critic ──────────
    # The ENTRY trigger: when truthy, recall routes to the postcondition node (its
    # own arm). The declared post-condition spec for this turn:
    #   {step, statement (or "post_condition"), result, actor_backend?, critic_backend?}.
    # Absent ⇒ today's behaviour, byte-identical (the arm only fires when set).
    post_condition: Optional[dict]
    # The postcondition node's verdict ({verdict, passed, reasons, actor_backend,
    # critic_backend, decorrelated}). Reusable by MS-3 + both lanes.
    post_condition_verdict: Optional[dict]
    # ── Budget BIND-01/02 + §2.7 threshold interrupt (§2.3/2.9 [v1.2/F2], MS-4) ──────
    # The ENTRY trigger (a dict): when present, the fan-out reserves a per-branch token
    # sub-budget at dispatch (BIND-01), each branch meters its own spend IN-BRANCH
    # (BIND-02, O(0), no shared poll), join reconciles unspent back to the pool, and a
    # ~150%-overspend / total-run-ceiling crossing SURFACES a "contested by cost" flag
    # (§2.7, NOT a per-branch approval gate). Shape:
    #   {pool, per_branch?, run_ceiling?, run_total?, trigger?}.
    # Absent/None ⇒ byte-identical to today (the budget machinery only fires when set).
    # Per-branch spend rides the existing worker_results / build_impls reducers (each
    # entry gains a "budget" sub-dict) — no new reducer field is introduced.
    budget: Optional[dict]
    # join's reconcile + escalation surface (last-write-wins; observability + the §2.7
    # contested-by-cost interrupt). None on every non-budgeted turn.
    budget_report: Optional[dict]
    # ── Research orchestrator (§3 research lane, MS2-R2) ───────────────────────
    # The ENTRY trigger: when truthy, recall routes to research_plan (its OWN arm),
    # which decomposes the question into sub-questions and sizes the effort tier
    # deterministically from the count (1 / 2-4 / 10+ → execute / flat fan-out /
    # hierarchical-managers, OD#4). Absent/None/False ⇒ today's behaviour,
    # byte-identical (the arm only fires when set — guard-at-top).
    research_request: Optional[bool]
    # The tier research_plan picked ("single"|"fanout"|"hierarchical"); read by
    # _route_after_research_plan to pick the landed arm. None on non-research turns.
    research_tier: Optional[str]


# ─── Conditional routing helper ───────────────────────────────────────────────

def _route_from_execute(state: AgentState) -> str:
    """After execute: go to approval if waiting for sign-off, else END."""
    if state.get("approval_required") and state.get("approval_response") is None:
        return "approval"
    return END


def _route_after_recall(state: AgentState) -> str:
    """After recall: divert to the CoCouncil subgraph for the council-max face,
    else fall through to the existing dispatch path (byte-for-byte unchanged).

    The council branch is ADDITIVE and fires ONLY when ``council_spec`` is set
    (route_node sets it solely for face_id == "council-max"). When absent, this
    returns "dispatch" — identical to the prior unconditional recall→dispatch
    edge — so the ~979 existing core tests stay green.
    """
    # Verification spine §2.6 tier-1 (MS-2): its OWN arm, gated on the explicit
    # post_condition trigger so non-postcondition turns are byte-identical. Taken
    # first; the post-condition critic is a leaf node (→ END).
    # Guard mirrors postcondition_node: a present-but-empty dict (or one with no
    # statement) must NOT divert from the default dispatch path.
    pc = state.get("post_condition")
    if isinstance(pc, dict) and str(pc.get("statement") or pc.get("post_condition") or "").strip():
        return "postcondition"
    # Research orchestrator (§3 research lane, MS2-R2): its OWN arm, gated on the
    # explicit research_request trigger so non-research turns are byte-identical. Taken
    # before build/HM/council so a (defensively) co-set research turn is the
    # orchestrator's; research_plan then decomposes + deterministically count→tier routes.
    if state.get("research_request"):
        return "research_plan"
    # Build pipeline (Feature 2): its OWN arm, taken before the council, gated on the
    # explicit build_request trigger so non-build turns are byte-identical and the
    # build loop never shares a routing arm with the council.
    if state.get("build_request"):
        return "plan_contracts"
    # Hierarchical-managers: its OWN arm (a 2-level agent tree), gated on the
    # explicit `hierarchical` trigger so non-HM turns are byte-identical.
    if state.get("hierarchical"):
        return "manager_dispatch"
    spec = state.get("council_spec")
    # Presence-based, not truthiness: a present-but-empty {} spec means "council
    # with default (fusion) mode"; only an absent/None spec falls through to dispatch.
    if spec is not None:
        mode = (spec.get("mode") or "fusion").strip().lower()
        if mode == "sequential":
            return "council"
        # fusion AND debate share panel_dispatch → panel_worker → synthesize; they
        # diverge only at the close gate (_route_after_synthesize picks `ground` for
        # fusion/grounding, `debate_converge` for debate).
        return "panel_dispatch"
    return "dispatch"


def _route_after_research_plan(state: AgentState) -> str:
    """After the research orchestrator (MS2-R2): route to the landed arm sized
    deterministically from the sub-question count (OD#4) — ``execute`` (single,
    n<=1), ``dispatch`` (flat fan-out, 2-9), or ``manager_dispatch``
    (hierarchical-managers, 10+).

    Fail-loud: an ``error`` or an empty ``subtasks`` list ENDs the turn (never fan
    out an empty plan). The tier is recomputed from ``len(subtasks)`` when
    ``research_tier`` is absent, so this edge is a pure function of state.
    """
    if state.get("error"):
        return END
    subtasks = state.get("subtasks") or []
    if not subtasks:
        return END
    # `or` (not `is not None`) is intentional: recompute the tier from the
    # authoritative sub-question COUNT on ANY falsy/missing research_tier. This is
    # strictly more robust than honoring a set-but-empty tier (which would mis-route
    # to `execute` regardless of count). research_plan_node always sets a valid
    # non-empty tier, so in practice the recompute branch only fires when absent.
    tier = state.get("research_tier") or select_research_tier(len(subtasks))
    if tier == TIER_HIER:
        return "manager_dispatch"
    if tier == TIER_FANOUT:
        return "dispatch"
    return "execute"


def _route_after_plan(state: AgentState) -> str:
    """After plan_contracts: proceed to the build fan-out, else END (fail-loud).

    ``plan_contracts_node`` fails loud by setting ``error`` (no valid contracts, or a
    deterministic skeleton that won't build). On any error, or if no contracts were
    produced, END the turn (the error surfaces) rather than fan out over a broken/
    empty skeleton. On success → ``dispatch`` (the build branch fans out)."""
    if state.get("error") or not state.get("build_contracts"):
        return END
    return "dispatch"


def _route_after_synthesize(state: AgentState) -> str:
    """After synthesize: route to the ONE close gate for this run — `debate_converge`
    for a debate-shaped council, else `ground` (fusion/grounding). Exactly one gate
    runs per turn so the deferred-answer commit chokepoint stays single."""
    spec = state.get("council_spec") or {}
    if (spec.get("mode") or "fusion").strip().lower() == "debate":
        return "debate_converge"
    return "ground"


def _route_after_debate(state: AgentState) -> str:
    """After the debate convergence gate: loop to ``panel_dispatch`` for the next
    round iff ``debate_converge_node`` set ``council_spec["debate_continue"]``, else
    converge to END (mirrors ``_route_after_ground``)."""
    spec = state.get("council_spec") or {}
    if spec.get("debate_continue"):
        return "panel_dispatch"
    return END


def _route_after_ground(state: AgentState) -> str:
    """After the pre-close grounding gate (P2): re-seed round 1 on a grounded
    restart, else converge to END.

    ``grounding_node`` signals a restart by INCREMENTING ``council_restart`` and
    writing ``council_spec["reseed_context"]``. We route back to ``panel_dispatch``
    iff the reseed context is present (a restart was decided AND the restart
    budget + cost ceiling allowed it). On converge / budget-spent / ceiling
    breach, the node leaves no reseed_context (or sets ``error``) → END with the
    best handoff so far.
    """
    spec = state.get("council_spec") or {}
    if spec.get("reseed_context"):
        return "panel_dispatch"
    return END


def _route_after_join(state: AgentState) -> str:
    """After join: route to dispatch if more waves remain, else approval/END."""
    # Build pipeline (Feature 2): after the build join (re-wrote the app), run the
    # verify gate. verify → {repair → verify}* → END is the P2 verify/repair loop.
    if state.get("build_contracts") is not None:
        return "verify"
    fanout_wave = state.get("fanout_wave")
    if fanout_wave is not None:
        backend = state.get("backend", "local")
        cap = MAX_FANOUT_WIDTH_BY_BACKEND.get(backend, 0)
        subtasks = state.get("subtasks") or []
        if cap > 0 and (fanout_wave + 1) * cap < len(subtasks):
            return "dispatch"
    if state.get("approval_required") and state.get("approval_response") is None:
        return "approval"
    return END


# ─── recall_node wrapper (dependency injection) ───────────────────────────────

async def _recall_node_wrapper(state: AgentState) -> dict:
    """Wraps recall_node with retriever/fact_store injection from bootstrap.

    Ensures a user message is available for the recall query since
    ``execute_node`` adds the task to messages *after* this node runs.
    """
    from core.config import config as _cfg

    if not _cfg.MEMORY_ENABLED:
        return {"recalled_facts": []}
    from core.memory.bootstrap import get_memory

    # Build messages copy with a synthetic user message if needed
    msgs = list(state.get("messages") or [])
    task = state.get("task")
    if task and not any(
        isinstance(m, dict) and m.get("role") == "user" and task in (m.get("content") or "")
        for m in msgs
    ):
        msgs.append({"role": "user", "content": task})

    patched = dict(state)
    patched["messages"] = msgs
    mem = get_memory()

    # Fail open on recall-path *availability* failures (embedder or Qdrant
    # unreachable): degrade to empty recall so the turn keeps moving, and record
    # the error on the observable singleton field. The embedder is an on-demand
    # local process that can die under load / session cleanup during bring-up;
    # a recall blip must not sink the whole request.
    #
    # NOTE: only EmbedderUnavailable / RetrievalProviderError are caught here.
    # Other MemoryErrors (e.g. ACLViolation) are correctness/security signals,
    # not availability problems — they must still propagate, never fail open.
    try:
        result = await recall_node(
            patched, mem.retriever, mem.fact_store, enabled=True,
        )
    except (EmbedderUnavailable, RetrievalProviderError) as exc:
        logger.warning(
            "memory recall unavailable (%s: %s); failing open with empty "
            "recall for this turn",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        mem.last_recall_error = exc
        return {"recalled_facts": []}

    mem.last_recall_error = None
    return result


# ─── Graph factory ────────────────────────────────────────────────────────────

def build_graph(checkpointer=None):
    """Build and compile the StateGraph.

    Args:
        checkpointer: LangGraph checkpointer instance.  Pass MemorySaver() for
                      tests or leave None for a stateless graph.
    Returns:
        Compiled CompiledStateGraph ready for invoke/astream.

    The graph is compiled with ``interrupt_before=["approval"]`` so the
    runtime self-pauses at the checkpoint boundary before entering the
    approval node.  The HTTP layer (``/api/chat/approval``) resumes by
    calling ``graph.aupdate_state(config, {"approval_response": ...})``
    then re-running ``astream(None, config)``.  See
    ``core/nodes/approval.py`` for the rationale (Python 3.10
    ContextVar propagation bug in ``langgraph.types.interrupt``).
    """
    g = StateGraph(AgentState)

    g.add_node("decompose", decompose_node)
    g.add_node("route", route_node)
    g.add_node("recall", _recall_node_wrapper)
    # Research orchestrator (MS2-R2): the research-lane entry node. Reached only via the
    # research_request arm of _route_after_recall; absent triggers leave it unvisited. It
    # decomposes + count→tier routes to execute / dispatch / manager_dispatch.
    g.add_node("research_plan", research_plan_node)
    # Build pipeline (Feature 2): contract-planning entry node. Reached only via the
    # build_request arm of _route_after_recall; absent triggers leave it unvisited.
    g.add_node("plan_contracts", plan_contracts_node)
    # Build pipeline (P2): the verify gate + bounded repair loop. Reached only from
    # the build branch of join; unvisited on every non-build turn.
    g.add_node("verify", verify_node)
    g.add_node("repair", repair_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("execute", execute_node)
    g.add_node("worker", worker_node)
    g.add_node("join", join_node)
    # ── Hierarchical-managers — additive nodes, reached only via the `hierarchical`
    # arm (manager_dispatch → Send×K → mini_manager → manager_join). ──
    g.add_node("manager_dispatch", manager_dispatch_node)
    g.add_node("mini_manager", mini_manager_node)
    g.add_node("manager_join", manager_join_node)
    g.add_node("approval", approval_node)
    # ── Verification spine §2.6 tier-1 (MS-2) — post-condition critic node ──
    # Reached only via the `post_condition` arm of _route_after_recall; a leaf (→ END).
    # Reusable by MS-3 + both lanes (they import postcondition_node directly too).
    g.add_node("postcondition", postcondition_node)
    # ── CoCouncil (P1b) — additive nodes, only reached via the council branch ──
    g.add_node("panel_dispatch", panel_dispatch_node)
    g.add_node("panel_worker", panel_worker_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("council", council_node)
    # D3: debate convergence gate — the debate close gate (analogue of `ground`).
    g.add_node("debate_converge", debate_converge_node)
    # ── CoCouncil (P2/P3b) — pre-close grounding gate (ALWAYS wired) ──
    # P3b moved the grounding on/off decision from build-time topology to a
    # RUNTIME gate (grounding_enabled(spec): a profile's protocol_bounds.grounding
    # overriding the global COUNCIL_GROUND_CADENCE), so a profile can flip
    # grounding per-run. The node is therefore always registered; when grounding
    # is OFF for a given run, synthesize commits in-node and grounding_node is a
    # no-op converge → END (equivalent to the old P1 synthesize → END). For
    # non-council runs `ground` is simply never reached.
    g.add_node("ground", grounding_node)

    g.add_edge(START, "decompose")
    g.add_edge("decompose", "route")
    g.add_edge("route", "recall")
    # recall → (council branch | dispatch). When council_spec is absent this is
    # exactly the prior unconditional recall→dispatch edge.
    g.add_conditional_edges(
        "recall",
        _route_after_recall,
        {
            "dispatch": "dispatch",
            "panel_dispatch": "panel_dispatch",
            "council": "council",
            "plan_contracts": "plan_contracts",
            "manager_dispatch": "manager_dispatch",
            "postcondition": "postcondition",
            "research_plan": "research_plan",
        },
    )
    # Research orchestrator (MS2-R2): research_plan → the count-sized landed arm
    # (execute / dispatch / manager_dispatch), or END (fail-loud on no subtasks).
    # The reused arms (dispatch→worker→join, manager_dispatch→mini_manager→manager_join)
    # carry the turn from here exactly as on a non-research turn.
    g.add_conditional_edges(
        "research_plan",
        _route_after_research_plan,
        {
            "execute": "execute",
            "dispatch": "dispatch",
            "manager_dispatch": "manager_dispatch",
            END: END,
        },
    )
    # Verification spine §2.6 tier-1: the post-condition critic is a leaf node.
    g.add_edge("postcondition", END)
    # Build pipeline: plan_contracts → dispatch (fan out the build) | END (fail-loud
    # on no contracts / an unbuildable skeleton). The build branch of dispatch /
    # worker / join carries it from here to the written app.
    g.add_conditional_edges(
        "plan_contracts",
        _route_after_plan,
        {"dispatch": "dispatch", END: END},
    )
    g.add_conditional_edges(
        "dispatch",
        _route_after_dispatch,
        {
            "execute": "execute",
            "approval": "approval",
            END: END,
        },
    )
    # ── CoCouncil wiring (mirrors dispatch→worker→join) ──
    # fusion:      panel_dispatch → (N× Send → panel_worker) → synthesize →
    #              ground → {panel_dispatch (grounded restart) | END}
    #              (P3b: `ground` always wired; runtime-gated. Grounding OFF ⇒
    #               synthesize commits in-node + ground no-op converges → END.
    #               Grounded-restart loop bounded by per-run restart_budget/max_usd.)
    # sequential:  council → END
    g.add_conditional_edges(
        "panel_dispatch",
        _route_after_panel,
        {"panel_worker": "panel_worker", "synthesize": "synthesize"},
    )
    g.add_edge("panel_worker", "synthesize")
    # synthesize → ONE close gate (selected by mode), so the deferred-answer commit
    # chokepoint stays single: `ground` for fusion/grounding (P3b: runtime-gated, a
    # no-op converge → END when grounding is OFF), `debate_converge` for debate.
    # Each gate loops back to panel_dispatch (grounded restart / next debate round)
    # or converges to END.
    g.add_conditional_edges(
        "synthesize",
        _route_after_synthesize,
        {"ground": "ground", "debate_converge": "debate_converge"},
    )
    g.add_conditional_edges(
        "ground",
        _route_after_ground,
        {"panel_dispatch": "panel_dispatch", END: END},
    )
    g.add_conditional_edges(
        "debate_converge",
        _route_after_debate,
        {"panel_dispatch": "panel_dispatch", END: END},
    )
    g.add_edge("council", END)
    # ── Hierarchical-managers wiring (mirrors dispatch→worker→join one tier up) ──
    # manager_dispatch → (K× Send → mini_manager) → manager_join → END.
    # Send targets bypass the edge map (like dispatch→worker), so only END is mapped.
    g.add_conditional_edges(
        "manager_dispatch",
        _route_after_manager_dispatch,
        {END: END},
    )
    g.add_edge("mini_manager", "manager_join")
    g.add_edge("manager_join", END)
    g.add_edge("worker", "join")
    g.add_conditional_edges(
        "join",
        _route_after_join,
        {"dispatch": "dispatch", "approval": "approval", "verify": "verify", END: END},
    )
    # Build pipeline (P2): verify gate → {repair → verify}* → END, bounded by
    # BUILD_REPAIR_BUDGET (repair_round). repair re-writes the app then re-verifies.
    g.add_conditional_edges(
        "verify",
        _route_after_verify,
        {"repair": "repair", END: END},
    )
    g.add_edge("repair", "verify")
    g.add_conditional_edges(
        "execute",
        _route_from_execute,
        {"approval": "approval", END: END},
    )
    g.add_edge("approval", "route")

    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=["approval"],
    )


async def create_graph():
    """Production factory: compile graph with AsyncPostgresSaver when available.

    Falls back to MemorySaver if the Postgres checkpointer package or the
    database connection is not available.
    """
    from core.config import config

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        import asyncpg

        conn = await asyncpg.connect(config.POSTGRES_URL)
        saver = AsyncPostgresSaver(conn)
        await saver.setup()
        return build_graph(checkpointer=saver)
    except Exception:
        # psycopg3 / libpq not installed, or Postgres not reachable
        return build_graph(checkpointer=MemorySaver())
