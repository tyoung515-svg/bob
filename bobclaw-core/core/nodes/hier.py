"""BoBClaw — Hierarchical-managers fan-out (a real 2-level agent tree).

Adds a MANAGER tier ABOVE the flat fan-out (dispatch→worker→join):

    recall ─(hierarchical)→ manager_dispatch ─Send×K→ mini_manager → manager_join → END

* ``manager_dispatch`` — the top manager splits the job's ``subtasks`` into K
  sections (deterministic balanced chunk; an LLM section-planner is the obvious
  v1.1). Gated upstream so a non-hierarchical turn never reaches it.
* ``mini_manager``     — ONE section: fans its subtasks over ``worker_node``
  (REUSED verbatim — the tested per-worker path: timeout / 429 / critic /
  best-effort partial-failure) via ``asyncio.gather``, then synthesizes the
  section through the **apex** backend. Emits one ``section_results`` entry.
* ``manager_join``     — reduces ``section_results`` by idx, runs the FIRST final
  audit through the **critic** backend, and assembles the single assistant
  message (the sole emitter on the HM path).

Additive + gated on ``hierarchical``: a turn without the trigger never reaches
these nodes, so every existing path is byte-identical. Roles resolve through the
active team (built-in ``hier-fleet``: apex=kimi_code, worker=deepseek_v4_flash,
critic=glm_5_2) — mirroring the build pipeline's ``teams.role_backend`` split.

Realization note (WORKER_FANOUT_DESIGN OPEN-2's revisable-realization clause):
the mini-manager runs its workers by DIRECT REUSE of ``worker_node`` (gather),
NOT a nested compiled subgraph. Same 2-level tree, same tested per-worker
machinery, without nested-``Send`` / checkpointer friction — the "thin" pass.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Union

from langgraph.graph import END
from langgraph.types import Send

import core.teams as teams
from core.config import (
    MANAGER_MAX_SECTIONS,
    MANAGER_SECTION_SIZE,
    WORKER_TIMEOUT_SECONDS,
    config,
)
from core.nodes._l0_events import _append_agent_turn_event
from core.nodes.execute import _send_to_backend
from core.nodes.worker import worker_node

logger = logging.getLogger(__name__)


def _chunk_sections(subtasks: list[str], k: int) -> list[dict]:
    """Split *subtasks* into *k* balanced, contiguous sections.

    Returns ``[{idx, subtasks}]``. Extra items are spread across the first
    sections (so 9 over 4 ⇒ 3,2,2,2), never an empty trailing section.
    """
    n = len(subtasks)
    k = max(1, min(k, n))
    base, extra = divmod(n, k)
    sections: list[dict] = []
    start = 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        sections.append({"idx": i, "subtasks": subtasks[start:start + size]})
        start += size
    return sections


def manager_dispatch_node(state: dict) -> dict:
    """Top manager: split ``subtasks`` into K balanced sections.

    Reached only via the ``hierarchical`` arm of ``_route_after_recall`` so this
    assumes a hierarchical turn. K = ceil(N / MANAGER_SECTION_SIZE), capped at
    MANAGER_MAX_SECTIONS and N; a per-turn ``manager_max_sections`` overrides.
    Fails loud (``error``) on no subtasks — the edge then ENDs.
    """
    subtasks = state.get("subtasks") or []
    if not subtasks:
        return {"error": "hierarchical fan-out requested but no subtasks to dispatch"}
    override = state.get("manager_max_sections")
    if override and override > 0:
        k = override
    else:
        k = math.ceil(len(subtasks) / max(1, MANAGER_SECTION_SIZE))
    k = max(1, min(k, MANAGER_MAX_SECTIONS, len(subtasks)))
    return {"sections": _chunk_sections(subtasks, k)}


def _route_after_manager_dispatch(state: dict) -> Union[list[Send], str]:
    """Conditional edge: Send one section sub-state per ``mini_manager``.

    END on error / no sections (mirrors how ``Send`` targets bypass the edge map,
    so only the ``END`` string return needs the map entry in graph.py).
    """
    if state.get("error"):
        return END
    sections = state.get("sections") or []
    if not sections:
        return END
    worker_backend = (
        teams.role_backend(state.get("team"), "worker")
        or state.get("backend") or "local"
    )
    apex_backend = (
        teams.role_backend(state.get("team"), "apex")
        or state.get("backend") or "local"
    )
    critic_backend = teams.role_backend(state.get("team"), "critic") or ""
    return [
        Send(
            "mini_manager",
            {
                "section_idx": s["idx"],
                "section_subtasks": s["subtasks"],
                "worker_backend": worker_backend,
                "apex_backend": apex_backend,
                "critic_backend": critic_backend,
                "escalation_backend": state.get("escalation_backend"),
                "face_id": state.get("face_id", "assistant"),
                "scope": state.get("scope"),
                "recalled_facts": state.get("recalled_facts") or [],
                "messages": [],
            },
        )
        for s in sections
    ]


async def mini_manager_node(sub_state: dict) -> dict:
    """One section: fan its subtasks over ``worker_node``, synthesize via the apex.

    Returns one ``section_results`` entry. The workers' raw results are NESTED in
    the entry (not merged into the top-level ``worker_results`` reducer), so the
    manager tier never collides with the flat fan-out's state.
    """
    idx = sub_state.get("section_idx", 0)
    subtasks = sub_state.get("section_subtasks") or []
    worker_backend = sub_state.get("worker_backend", "local")
    apex_backend = sub_state.get("apex_backend") or worker_backend
    escalation = sub_state.get("escalation_backend")
    scope = sub_state.get("scope")
    recalled = sub_state.get("recalled_facts") or []

    # Fan the section's subtasks over the TESTED per-worker path (timeout / 429 /
    # critic / best-effort). worker_node is called directly (not as a graph node),
    # so its {"worker_results": [...]} delta is captured here, never graph-merged.
    worker_substates = [
        {
            "task": t,
            "backend": worker_backend,
            "escalation_backend": escalation,
            "subtask_idx": j,
            "messages": [],
            "phase": "dispatch",
            "scope": scope,
            "recalled_facts": recalled,
        }
        for j, t in enumerate(subtasks)
    ]
    results = await asyncio.gather(
        *(worker_node(ws) for ws in worker_substates), return_exceptions=True
    )
    entries: list[dict] = []
    for r in results:
        if isinstance(r, dict) and r.get("worker_results"):
            entries.extend(r["worker_results"])
        elif isinstance(r, BaseException):  # worker_node catches, but be defensive
            entries.append({"idx": len(entries), "status": "failed", "error": str(r)})
    entries.sort(key=lambda e: e.get("idx", 0))
    oks = [e for e in entries if e.get("status") == "ok"]

    parts: list[str] = []
    for e in entries:
        if e.get("status") == "ok":
            parts.append(f"- {e.get('content', '')}")
        else:
            parts.append(f"- [{e.get('status', 'failed')}] {e.get('error', '')}")
    worker_blob = "\n".join(parts)

    # Apex (mini-manager) synthesis of the section. Fail-open to the raw worker
    # blob — a synth failure must not lose the section's completed work.
    synthesis = worker_blob
    if oks:
        try:
            synth_prompt = (
                "You are a section manager in a hierarchical fan-out. Synthesize "
                "your workers' outputs below into ONE concise, coherent section "
                "result. Note any subtask that failed.\n\nWorker outputs:\n"
                + worker_blob
            )
            synthesis = await asyncio.wait_for(
                _send_to_backend(
                    [{"role": "user", "content": synth_prompt}], apex_backend
                ),
                timeout=WORKER_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open to the worker blob
            logger.warning(
                "mini_manager section %s apex synth failed (%s); using raw blob",
                idx, exc,
            )

    return {
        "section_results": [{
            "idx": idx,
            "status": "ok" if oks else "failed",
            "synthesis": str(synthesis),
            "n_workers": len(entries),
            "n_ok": len(oks),
            "worker_results": entries,
            "apex_backend": apex_backend,
        }],
    }


async def manager_join_node(state: dict) -> dict:
    """Reduce ``section_results`` by idx, run the FIRST final audit, emit the answer.

    The critic (e.g. glm_5_2) audits the assembled sections — fail-open: a failed
    audit must never break the turn (the assembled answer still surfaces). Sets
    ``error`` only when EVERY section failed (best-effort, mirrors join_node).
    """
    sections = sorted(state.get("section_results", []), key=lambda s: s.get("idx", 0))
    n_ok = sum(1 for s in sections if s.get("status") == "ok")

    body_parts: list[str] = []
    for s in sections:
        head = f"## Section {s.get('idx', 0) + 1}"
        if s.get("status") != "ok":
            head += " (failed)"
        body_parts.append(f"{head}\n{s.get('synthesis', '')}")
    assembled = "\n\n".join(body_parts)

    critic_backend = teams.role_backend(state.get("team"), "critic") or ""
    audit_note = ""
    if critic_backend and sections:
        audit_prompt = (
            "You are the FINAL auditor of a hierarchical fan-out. Review the "
            "assembled section results below for completeness, consistency, and "
            "gaps. Give a SHORT audit verdict (1-3 sentences).\n\n" + assembled
        )
        # Stand in a healthy backend when the primary critic hard-fails (e.g. the
        # glm_5_2 HTTP path is balance-dead) — mirrors run_critic so the final audit
        # is not silently lost. Still fail-open: if BOTH fail, skip (assembled answer
        # surfaces regardless).
        audit_backends = [critic_backend]
        fb = config.CRITIC_FALLBACK_BACKEND
        if fb and fb != critic_backend:
            audit_backends.append(fb)
        for ab in audit_backends:
            try:
                verdict = await asyncio.wait_for(
                    _send_to_backend([{"role": "user", "content": audit_prompt}], ab),
                    timeout=WORKER_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 — audit is best-effort
                logger.warning("manager_join final audit via %s failed (%s)", ab, exc)
                continue
            tag = critic_backend if ab == critic_backend else f"{ab}, stand-in"
            audit_note = f"\n\n---\n**Final audit ({tag}):** {str(verdict).strip()}"
            break

    total = len(sections)
    summary = f"\n\n_{n_ok} of {total} sections completed._" if total else ""
    final = assembled + summary + audit_note

    error_msg = "All hierarchical sections failed" if (sections and n_ok == 0) else None

    await _append_agent_turn_event(state, assistant_response=final, error_msg=error_msg)
    return {
        "messages": [{"role": "assistant", "content": final}],
        "error": error_msg,
    }
