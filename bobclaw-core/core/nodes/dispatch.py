"""
BoBClaw — Worker fan-out dispatch (handoff 006).

`dispatch_node` is a state-mutation node that inspects state and writes
`fanout_subtasks` (list of {idx, text}) if fan-out applies.  It also performs
a pre-flight approval check (handoff 006) — if any subtask requires approval,
it writes a combined approval message + ``approval_required = True`` and the
conditional edge routes to ``approval_node``.

``_route_after_dispatch`` is the conditional edge function that reads the
results of ``dispatch_node`` and returns either ``list[Send]`` (fan-out),
``"approval"`` (pre-flight approval needed), or ``"execute"`` (single-worker
fall-through).

In 007 cost-cap pre-flight and width-chunking land here.
"""
from __future__ import annotations

from typing import Any, Union

from langgraph.graph import END
from langgraph.types import Send

from core.backends._cost import remaining_budget
from core.config import (
    HARD_CONCURRENCY_CAP_BACKENDS,
    MAX_FANOUT_WIDTH_BY_BACKEND,
    MAX_FANOUT_WIDTH_GLOBAL,
    MAX_WORKER_USD_BY_BACKEND,
    _FANOUT_THRESHOLD,
)
import core.teams as teams
from core.faces.registry import get_default_registry
from core.nodes.budget_runtime import budget_config, plan_reservations
from core.permissions import task_requires_approval


def _build_worker_backend(state: dict) -> str:
    """The build fan-out's worker backend: the active team's worker role (e.g.
    demo-fleet → deepseek_v4_flash), else the turn's backend, else local."""
    return (teams.role_backend(state.get("team"), "worker")
            or state.get("backend") or "local")


def _build_dispatch(state: dict) -> dict:
    """Cost + width pre-flight for the build fan-out (Feature 2; one worker/contract).

    SINGLE-WAVE: the centerpiece demo proved 100 concurrent DeepSeek workers in one
    wave with no 429, so a build fans out up to the global cap in a single wave for
    spawn-unbounded (rate-limit-bounded) fleets. Fail-loud — never silently truncate
    or 429-degrade — on: the global width cap; an unmapped worker backend; a build
    that would exceed a HARD per-account concurrency cap (Kimi) in one wave (those
    fleets need multi-wave build chunking, a deferred enhancement); or a budget
    breach. The Sends themselves are built in ``_route_after_dispatch``.
    """
    contracts_list = state.get("build_contracts") or []
    n = len(contracts_list)
    worker_backend = _build_worker_backend(state)
    if n > MAX_FANOUT_WIDTH_GLOBAL:
        return {"error": (
            f"Build fan-out width {n} exceeds global cap {MAX_FANOUT_WIDTH_GLOBAL}; aborting"
        )}
    if worker_backend not in MAX_WORKER_USD_BY_BACKEND:
        return {"error": (
            f"Build worker backend {worker_backend!r} has no MAX_WORKER_USD_BY_BACKEND entry"
        )}
    # A single wave cannot exceed a backend's HARD per-account concurrency cap (Kimi):
    # fail loud instead of firing N>cap Sends that silently 429-degrade (each over-cap
    # worker would fail-soft to a kept stub with no error surfaced). Spawn-unbounded
    # fleets (deepseek/claude) are bounded only by the global cap above.
    if worker_backend in HARD_CONCURRENCY_CAP_BACKENDS:
        cap = MAX_FANOUT_WIDTH_BY_BACKEND.get(worker_backend, MAX_FANOUT_WIDTH_GLOBAL)
        if n > cap:
            return {"error": (
                f"Build fan-out width {n} exceeds the hard per-account concurrency cap "
                f"{cap} for {worker_backend!r}; this fleet needs multi-wave build chunking "
                f"(deferred) — use a higher-cap worker or fewer units."
            )}
    per_worker = MAX_WORKER_USD_BY_BACKEND[worker_backend]
    estimated_usd = n * per_worker
    remaining = remaining_budget(worker_backend)
    if estimated_usd > remaining:
        return {"error": (
            f"Build fan-out cost-cap pre-flight rejected: estimated worst-case "
            f"${estimated_usd:.4f} > remaining ${remaining:.4f} "
            f"({n} workers × ${per_worker:.4f} on {worker_backend})"
        )}
    return {}


def dispatch_node(state: dict) -> dict:
    # ── Build pipeline branch (Feature 2): fan out over contracts, not subtasks.
    # Reached only when plan_contracts set build_contracts (a build turn); the
    # existing subtask path below is byte-identical for every non-build turn. ──
    if state.get("build_contracts") is not None:
        return _build_dispatch(state)

    """State-mutation node: decide fan-out parameters, run approval pre-check.

    Sets:
        fanout_subtasks — list[dict] | None  ({idx, text}) for fan-out, or None.
        approval_required — bool, True if pre-flight check finds any subtask
                            needing approval.
        messages — combined approval message when approval_required is set.
        approval_response — cleared on first pass; preserved on re-entry.
    """
    subtasks = state.get("subtasks") or []
    fanout_width = state.get("fanout_width")
    face_id = state.get("face_id", "assistant")
    backend = state.get("backend", "local")
    approval_response = state.get("approval_response")

    result: dict[str, Any] = {"fanout_subtasks": None}

    # 1. Workspace-bound bypass: workers bound to a single instance don't fan out.
    if backend == "opencode_serve" or face_id == "worker-opencode":
        return result

    # 2. Single-subtask turns never fan out.
    if len(subtasks) <= 1:
        return result

    # 3. Determine width
    if fanout_width is not None:
        if fanout_width <= 1:
            return result
        width = min(fanout_width, len(subtasks))
    else:
        if len(subtasks) < _FANOUT_THRESHOLD:
            return result
        width = len(subtasks)

    # 4. Global width cap (handoff 007)
    n = len(subtasks)
    if n > MAX_FANOUT_WIDTH_GLOBAL:
        return {"error": f"Fan-out width {n} exceeds global cap {MAX_FANOUT_WIDTH_GLOBAL}; aborting"}

    # 5. Unmapped backend check (handoff 007)
    if backend not in MAX_FANOUT_WIDTH_BY_BACKEND:
        raise ValueError(f"Backend {backend!r} has no MAX_FANOUT_WIDTH_BY_BACKEND entry")
    if backend not in MAX_WORKER_USD_BY_BACKEND:
        raise ValueError(f"Backend {backend!r} has no MAX_WORKER_USD_BY_BACKEND entry")

    # 6. Per-backend width cap & wave-chunking (handoff 007 Phase 2)
    per_backend_cap = MAX_FANOUT_WIDTH_BY_BACKEND[backend]
    wave_idx = state.get("fanout_wave", 0)
    if width > per_backend_cap:
        wave_start = wave_idx * per_backend_cap
        wave_end = min(wave_start + per_backend_cap, len(subtasks))
        wave_indices = range(wave_start, wave_end)
    else:
        wave_indices = range(width)

    # 7. Cost pre-flight (handoff 007) — includes critic cost when set (handoff 008)
    face = get_default_registry().get_face(face_id)
    per_worker = MAX_WORKER_USD_BY_BACKEND[backend]
    if face.critic_backend:
        if face.critic_backend not in MAX_WORKER_USD_BY_BACKEND:
            raise ValueError(f"Critic backend {face.critic_backend!r} has no MAX_WORKER_USD_BY_BACKEND entry")
        per_worker += MAX_WORKER_USD_BY_BACKEND[face.critic_backend]
    estimated_usd = len(wave_indices) * per_worker
    remaining = remaining_budget(backend)
    if estimated_usd > remaining:
        return {"error": (
            f"Fan-out cost-cap pre-flight rejected: estimated worst-case "
            f"${estimated_usd:.4f} > remaining ${remaining:.4f} "
            f"({len(wave_indices)} workers × ${per_worker:.4f} on {backend})"
        )}

    # 8. Build fan-out subtask list (handoff 005, wave-sliced in Phase 2)
    fanout_subtasks = [{"idx": i, "text": subtasks[i]} for i in wave_indices]
    result["fanout_subtasks"] = fanout_subtasks

    # 9. Rejection handling — stop if previous approval was rejected
    dec = (approval_response or "").strip().lower()
    if dec in {"reject", "rejected"}:
        result["fanout_subtasks"] = None
        result["approval_required"] = False
        return result

    # 9. Approval pre-check (skip if already approved on re-entry)
    if approval_response is None:
        needing = [(e["idx"], e["text"]) for e in fanout_subtasks if task_requires_approval(e["text"])]
        if needing:
            lines = [f"  - subtask {idx + 1}: '{text[:80]}'" for idx, text in needing]
            msg = (
                f"Fan-out wants to run {len(fanout_subtasks)} subtasks. "
                f"{len(needing)} require approval:\n" + "\n".join(lines)
                + "\n\nApprove all to proceed."
            )
            result["messages"] = [{"role": "system", "content": msg}]
            result["approval_required"] = True
            return result

    # 10. Fan-out proceeds — approval was already granted or not needed
    return result


def _route_after_dispatch(state: dict) -> Union[list[Send], str]:
    """Conditional edge: read dispatch_node's output and route accordingly.

    Returns:
        list[Send] — fan-out to worker_node with per-subtask sub-states.
        "approval" — pre-flight approval needed.
        "execute" — fall through to the existing single-worker execute path.
        END — abort on error (no fan-out, skip execute).
    """
    if state.get("error"):
        return END
    if state.get("approval_required"):
        return "approval"

    # ── Build pipeline: one Send per contract → worker_node's build branch.
    # Always fans out (even for a single contract — never the chat `execute` path). ──
    if state.get("build_contracts") is not None:
        contracts_list = state["build_contracts"]
        worker_backend = _build_worker_backend(state)
        workspace = state.get("build_workspace")
        args = [
            {
                "build_contract": c,
                "build_workspace": workspace,
                "backend": worker_backend,
                "escalation_backend": state.get("escalation_backend"),
                "subtask_idx": i,
                "messages": [],
                "phase": "build",
                # P3: the build blast radius (sandbox) — for the Gate-Router audit
                # trail + a future scope-aware impl critic.
                "scope": state.get("scope"),
            }
            for i, c in enumerate(contracts_list)
        ]
        # ── MS-4 BIND-01: reserve a per-branch token sub-budget at fan-out, guarded so a
        # non-budgeted build turn is byte-identical (no key added). ──
        _attach_reservations(args, state.get("budget"))
        return [Send("worker", a) for a in args]

    fanout = state.get("fanout_subtasks")
    if fanout and len(fanout) > 1:
        face_id = state.get("face_id", "assistant")
        backend = state.get("backend", "local")
        face = get_default_registry().get_face(face_id)
        args = [
            {
                "task": entry["text"],
                "face_id": face_id,
                "backend": backend,
                "escalation_backend": state.get("escalation_backend"),
                "subtask_idx": entry["idx"],
                "messages": [],
                "phase": "dispatch",
                "critic_backend": face.critic_backend,
                "critic_prompt_template": face.critic_prompt_template,
                "recalled_facts": state.get("recalled_facts") or [],
                "scope": state.get("scope"),
                # Worker posture for backends that need it (agy_code / codex_code
                # read the model here; empty for other faces).
                "agy_posture": dict(face.agy_posture or {}),
                "codex_posture": dict(face.codex_posture or {}),
            }
            for entry in fanout
        ]
        # ── MS-4 BIND-01: reserve a per-branch token sub-budget at fan-out, guarded so a
        # non-budgeted fan-out is byte-identical (no key added). ──
        _attach_reservations(args, state.get("budget"))
        return [Send("worker", a) for a in args]

    return "execute"


def _attach_reservations(args: list[dict], raw_budget: Any) -> None:
    """MS-4 BIND-01 — guarded per-branch token reservation at fan-out.

    Reserves a per-branch sub-budget for each Send arg from the parent reserve-pool and
    bakes ``branch_budget = {reservation, trigger}`` onto each arg. When no budget is
    configured (``budget_config(raw_budget) is None``) this is a NO-OP, so every
    non-budgeted fan-out stays byte-identical (no ``branch_budget`` key is added). The
    reservations are computed ONCE here (single-threaded) and carried in each Send
    payload — there is no shared mutable state, so BIND-02 in-branch metering reads only
    its own payload (no cross-branch poll; §2.9 BIND-02).
    """
    bcfg = budget_config(raw_budget)
    if bcfg is None:
        return
    plan = plan_reservations(bcfg["pool"], len(args), bcfg["per_branch"])
    for arg, p in zip(args, plan):
        arg["branch_budget"] = {"reservation": p["reservation"], "trigger": bcfg["trigger"]}
