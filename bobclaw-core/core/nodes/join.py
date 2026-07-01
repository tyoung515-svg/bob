"""
BoBClaw — Worker fan-out join (handoff 006, wave-aware in 007).

`join_node` is the only writer of `messages` under fan-out. It sorts
`worker_results` by `idx`, formats into a single assistant message, and sets
`error` per the best-effort policy (only when ALL workers fail).

When wave-chunking is active (handoff 007 Phase 2), intermediate waves
increment `fanout_wave` without writing messages. Only the final wave
produces the assistant message.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from core.config import MAX_FANOUT_WIDTH_BY_BACKEND
from core.nodes._l0_events import _append_agent_turn_event
from core.nodes.budget_runtime import budget_config, reconcile_branches

if TYPE_CHECKING:
    from core.graph import AgentState

logger = logging.getLogger(__name__)


def _reconcile_budget(state, results) -> "Optional[dict]":
    """MS-4 — reconcile-on-merge + the §2.7 contested-by-cost surface (guarded).

    Returns ``None`` when no budget is configured (byte-identical join). Else gathers the
    per-branch budget dicts the workers recorded in-branch (stamping each branch's idx),
    reconciles unspent reservation back to the parent pool, aggregates the run spend, and
    decides the THRESHOLD-GATED escalation (any branch past ~150%, or the total-run
    ceiling). The result is a SURFACE only — join never sets ``approval_required`` from
    it (the §2.7 interrupt is "human on the loop", never per-branch approval).
    """
    bcfg = budget_config(state.get("budget"))
    if bcfg is None:
        return None
    branches = []
    for r in results:
        b = r.get("budget")
        if isinstance(b, dict):
            # Shallow-copy with the branch idx stamped — never MUTATE the shared worker
            # entry's budget dict (it is canonical reducer state; audit r1 purity fix).
            branches.append({**b, "idx": b.get("idx", r.get("idx", 0))})
    return reconcile_branches(
        bcfg["pool"],
        branches,
        run_total_before=bcfg["run_total"],
        run_ceiling=bcfg["run_ceiling"],
        trigger=bcfg["trigger"],
    )


async def _audit_gate_results(
    results: list[dict],
    user_id: Optional[str],
    conversation_id: Optional[str],
) -> None:
    """Persist a gate-audit ``approvals`` row per worker carrying a gate verdict.

    For each worker result with a ``gate_destination``:
      * ``auto`` → ``status='approved'``, ``approved_by='gate'`` — a pure,
        non-blocking audit row recording that the Gate auto-cleared the subtask.
      * ``gate`` / ``human`` → ``status='pending'``, ``approved_by=NULL`` — surfaces
        the flagged subtask for human review.

    Mirrors ``create_project``'s asyncpg write, but **fail-OPEN**: a missing pool,
    missing user_id, or any write error is logged and swallowed. A failed audit
    must NEVER break or error a fan-out turn (the opposite of create_project,
    which fails closed on a missing identity).
    """
    gated = [r for r in results if r.get("gate_destination")]
    if not gated:
        return

    # Fail-open on a missing identity: without a user_id we can't attribute the
    # audit rows, so skip the audit rather than erroring the turn.
    if not user_id:
        logger.debug("Skipping gate audit — no user_id on the fan-out turn")
        return

    # Resolve the pool lazily and tolerantly: an uninitialised pool (tests, or a
    # core process without Postgres) must not raise out of the join.
    try:
        from core.db import get_pool

        pool = get_pool()
    except Exception as exc:
        logger.warning("Gate audit skipped — Postgres pool unavailable: %s", exc)
        return

    conv_uuid = None
    if conversation_id:
        try:
            conv_uuid = UUID(str(conversation_id))
        except (ValueError, TypeError):
            # A non-UUID conversation id just drops the FK link; the audit row
            # is still useful with conversation_id = NULL.
            conv_uuid = None

    for r in gated:
        destination = r.get("gate_destination")
        details = {
            "subtask_idx": r.get("idx", 0),
            "reasons": r.get("gate_reasons", []),
        }
        if destination == "auto":
            status, approved_by = "approved", "gate"
        else:  # "gate" or "human" — needs human review
            status, approved_by = "pending", None
        try:
            await pool.execute(
                """
                INSERT INTO approvals (
                    conversation_id, user_id, action_type, details,
                    status, approved_by
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                conv_uuid,
                user_id,
                "worker_scope_review",
                json.dumps(details),
                status,
                approved_by,
            )
        except Exception as exc:
            # Fail-open: a single failed audit write must never abort the turn.
            logger.warning(
                "Gate audit write failed (subtask %s, %s): %s",
                details["subtask_idx"], destination, exc,
            )


async def _build_join(state: "AgentState") -> dict:
    """Build pipeline (Feature 2): merge worker impls by name, re-write the app.

    Reads ``build_impls`` (the build worker's reducer field, not ``worker_results``),
    merges by name (last non-None source wins; a contract with no impl keeps its
    stub), and re-writes the sandbox app. Sets an interim ``verify_report`` (the
    implemented count). It is NOT terminal: ``_route_after_join`` routes the build
    branch to ``verify``, which runs the pytest/build/CLI gate and emits the single
    final assistant message (P2). It deliberately emits NO message here — the verify
    gate is the sole emitter, so the turn produces exactly one answer.
    """
    from pathlib import Path

    from core.build import skeleton

    contracts_list = state.get("build_contracts") or []
    workspace = state.get("build_workspace")
    impls = skeleton.merge_impls(state.get("build_impls") or [])

    if workspace:
        skeleton.write_app(Path(workspace), contracts_list, impls)

    out: dict = {
        "verify_report": {
            "phase": "built", "units": len(contracts_list),
            "implemented": len(impls), "workspace": workspace,
        },
    }
    # MS-4 reconcile-on-merge + §2.7 surface for the build fan-out (guarded). The build
    # path's surface IS the budget_report field — verify_node remains the sole message
    # emitter (untouched), so the build turn still produces exactly one answer.
    budget_report = _reconcile_budget(state, state.get("build_impls") or [])
    if budget_report is not None:
        out["budget_report"] = budget_report
    return out


async def join_node(state: "AgentState") -> dict:
    """Reduce worker_results into a single assistant message + error flag.

    For intermediate waves (wave-chunking active, more waves remain),
    increments fanout_wave without producing a message.
    """
    # ── Build pipeline branch (Feature 2): merge impls + re-write the app. ──
    if state.get("build_contracts") is not None:
        return await _build_join(state)

    # Wave-chunking: intermediate waves skip message production
    fanout_wave = state.get("fanout_wave")
    if fanout_wave is not None:
        backend = state.get("backend", "local")
        cap = MAX_FANOUT_WIDTH_BY_BACKEND.get(backend, 0)
        subtasks = state.get("subtasks") or []
        if cap > 0 and (fanout_wave + 1) * cap < len(subtasks):
            return {"fanout_wave": fanout_wave + 1}

    results = sorted(state.get("worker_results", []), key=lambda r: r.get("idx", 0))

    # MS-4 reconcile-on-merge + §2.7 surface (guarded; None ⇒ byte-identical join).
    budget_report = _reconcile_budget(state, results)

    # Gate audit trail (GR-P3-finish): record what the Gate auto-cleared vs
    # flagged into the approvals store. Fail-open — never breaks the turn.
    await _audit_gate_results(
        results, state.get("user_id"), state.get("conversation_id")
    )

    successes = [r for r in results if r.get("status") == "ok"]
    rejections = [r for r in results if r.get("status") == "rejected"]
    # "flagged" is non-fatal (scope drift surfaced for review); do not count as a
    # hard failure for the all-failed error check.
    failures = [r for r in results if r.get("status") not in ("ok", "rejected", "flagged")]

    sections: list[str] = []

    for r in results:
        idx = r.get("idx", 0)
        content = r.get("content", "")
        gate_dest = r.get("gate_destination")
        if gate_dest in {"gate", "human"}:
            label = "scope drift" if gate_dest == "gate" else "out of scope"
            reasons = r.get("gate_reasons", [])
            sections.append(
                f"### Subtask {idx + 1} ({label}: {', '.join(reasons)})\n{content}"
            )
            continue
        verdict = r.get("critic_verdict")
        if verdict == "flag":
            reasons = r.get("critic_reasons", [])
            sections.append(f"### Subtask {idx + 1} (flagged: {', '.join(reasons)})\n{content}")
        elif verdict == "reject":
            reasons = r.get("critic_reasons", [])
            error = r.get("error", "unknown error")
            sections.append(f"### Subtask {idx + 1} (rejected: {', '.join(reasons)})\n_{error}_")
        elif verdict == "none":
            sections.append(f"### Subtask {idx + 1} (critic unavailable)\n{content}")
        elif r.get("status") == "ok":
            sections.append(f"### Subtask {idx + 1}\n{content}")
        else:
            error = r.get("error", "unknown error")
            status = r.get("status", "failed")
            sections.append(f"### Subtask {idx + 1} ({status})\n_{error}_")

    drifts = [r for r in results if r.get("gate_destination") in {"gate", "human"}]
    if drifts:
        drift_parts = ", ".join(
            f"subtask {r['idx'] + 1} ({r['gate_destination']})" for r in drifts
        )
        sections.append(
            f"\n_{len(drifts)} subtask(s) flagged for scope review: {drift_parts}._"
        )

    all_failures = rejections + failures
    if all_failures:
        total = len(results)
        n_failed = len(all_failures)
        n_ok = len(successes)
        summary_parts = ", ".join(
            f"subtask {r['idx'] + 1} ({r.get('status', 'failed')})" for r in all_failures
        )
        sections.append(
            f"\n_{n_ok} of {total} subtasks completed; {n_failed} failed: {summary_parts}._"
        )

    # MS-4 §2.7: SURFACE a "contested by cost" annotation when the threshold-gated
    # interrupt fired (overspend past ~150% and/or the run ceiling). This is a surface,
    # NOT a gate — the turn still completes; approval_required is never set from budget.
    if budget_report is not None and budget_report["interrupt"]["surfaced"]:
        intr = budget_report["interrupt"]
        contested = intr["contested_branches"]
        bits = []
        if contested:
            bits.append(
                "branch(es) " + ", ".join(str(i + 1) for i in contested)
                + " over ~150% of reservation"
            )
        if intr["ceiling_hit"]:
            bits.append(
                f"run total {budget_report['run_total']} ≥ ceiling "
                f"{budget_report['run_ceiling']}"
            )
        sections.append(
            f"\n_⚠ contested by cost ({intr['reason']}): " + "; ".join(bits)
            + " — surfaced for human review (run not blocked)._"
        )

    body = "\n\n".join(sections)

    # Best-effort: error only when ALL workers failed (rejected counts as failure)
    error_msg: str | None = None
    if all_failures and not successes:
        detail = "; ".join(
            f"subtask {r['idx'] + 1} ({r.get('status', 'failed')}: {r.get('error', 'unknown')})"
            for r in all_failures
        )
        error_msg = f"All fan-out workers failed: {detail}"

    await _append_agent_turn_event(state, assistant_response=body, error_msg=error_msg)
    out: dict = {
        "messages": [{"role": "assistant", "content": body}],
        "error": error_msg,
    }
    if budget_report is not None:
        out["budget_report"] = budget_report
    return out
