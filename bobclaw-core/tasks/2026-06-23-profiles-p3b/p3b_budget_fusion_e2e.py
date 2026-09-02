"""
BoBClaw — P3b live E2E: per-profile bounds + grounding BIND at runtime.

Drives the SAME compiled graph the HTTP `/api/chat` path uses (build_graph +
the production AgentState seed from api/server.py), with a real council profile
and REAL backends (deepseek_v4_flash). No mocks. Proves the three P3b claims:

  (a) ROLE PROMPTS  — each fusion seat's answer reflects its distinct role prompt
                      (we read council_spec-driven panel_results and check each
                      seat opens with its expected marker).
  (b) BUDGET BINDS  — the per-profile protocol_bounds.max_usd (0.10) trips the
                      cost ceiling, NOT the global $5: synthesize defers, the
                      always-wired ground node ceiling-breaches BEFORE spending a
                      grounding spawn (projected 0.25 > 0.10), and the ceiling
                      notice rides the error frame.
  (c) ONE ANSWER    — exactly one final answer lands (one custom "token" emit AND
                      one assistant message), never double-emitted or dropped
                      (the streaming-drop class P3b had to keep aligned).

Plus a CONTROL run with NO profile to prove a plain turn routes byte-identically
(no council_spec → normal dispatch/execute, no panel).

Run live (needs DEEPSEEK_API_KEY in the env / .secrets):
    cd bobclaw-core
    set PYTHONPATH=.
    run the venv python on tasks/2026-06-23-profiles-p3b/p3b_budget_fusion_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import sys

# Real model output (and the seat snippets) can carry unicode; the Windows
# console default is cp1252, so force UTF-8 so printing never crashes the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from langgraph.checkpoint.memory import MemorySaver

from core import teams
from core.graph import build_graph

PROFILE_NAME = "budget-fusion-e2e"

# Distinct, recognizable role prompts: each seat must open with its marker so we
# can prove the per-seat role_prompt actually steered the answer.
PROFILE = {
    "shape": "fusion",
    "synth_backend": "deepseek_v4_flash",
    "seats": [
        {
            "posture": "framer",
            "backend": "deepseek_v4_flash",
            "role_prompt": (
                "You are the FRAMER. In 3 sentences max, restate the problem "
                "crisply and lay out the solution space as 2-3 distinct options. "
                "Be structural; do NOT pick a winner. Begin your reply with "
                "'FRAMER:' and nothing before it."
            ),
        },
        {
            "posture": "stress",
            "backend": "deepseek_v4_flash",
            "role_prompt": (
                "You are the STRESS-TESTER. Attack the framing: name the top 2 "
                "risks or failure modes and say which bites first. Be skeptical "
                "and concrete. Begin your reply with 'STRESS:' and nothing before it."
            ),
        },
        {
            "posture": "wildcard",
            "backend": "deepseek_v4_flash",
            "role_prompt": (
                "You are the WILDCARD. Offer ONE non-obvious, contrarian or "
                "lateral angle the others will miss. Begin your reply with "
                "'WILDCARD:' and nothing before it."
            ),
        },
    ],
    # The HOW layer under test: grounding ON + a tiny per-run ceiling that trips
    # BEFORE the first grounding spawn (0.25 > 0.10), proving the budget binds.
    "protocol_bounds": {
        "grounding": "on",
        "max_usd": 0.10,
        "restart_budget": 1,
        "drift_threshold": 0.34,
    },
}

TASK = (
    "We have a 16GB-VRAM desktop and want to serve a 30B MoE model locally for an "
    "agent fleet without OOM. Should we offload experts to CPU, quantize harder, or "
    "cap concurrency? Give a recommendation."
)

EXPECTED_MARKERS = {"framer": "FRAMER", "stress": "STRESS", "wildcard": "WILDCARD"}


def _seed(task: str, *, profile_name: str | None, face_id: str = "assistant") -> dict:
    """The production AgentState seed (api/server.py:821), minimal but faithful."""
    return {
        "messages": [],
        "task": task,
        "conversation_id": f"p3b-e2e-{profile_name or 'control'}",
        "user_id": None,
        "face_id": face_id,
        "model_override": None,
        "backend_override": None,
        "team": None,
        "profile_name": profile_name,
        "backend": "local",
        "tools_allowed": [],
        "approval_required": False,
        "approval_response": None,
        "artifacts": [],
        "error": None,
        "project_instructions": None,
    }


async def _run(graph, seed: dict) -> tuple[dict, list[dict]]:
    """astream the turn; return (final_state, custom_emits)."""
    cfg = {"configurable": {"thread_id": seed["conversation_id"]}, "recursion_limit": 50}
    custom_emits: list[dict] = []
    final_state: dict = {}
    async for mode, chunk in graph.astream(seed, cfg, stream_mode=["values", "custom"]):
        if mode == "custom":
            custom_emits.append(chunk)
        elif mode == "values":
            final_state = chunk
    return final_state, custom_emits


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


async def main() -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("[!] DEEPSEEK_API_KEY not set - the panel/synth backends will fail. "
              "Set it (or .secrets) before running live.")

    # Persist the profile so route_node.load_profile finds it (idempotent).
    created = teams.create_profile(PROFILE_NAME, PROFILE, overwrite=True)
    print(f"profile {PROFILE_NAME!r} saved: shape={created.get('shape')} "
          f"bounds={created.get('protocol_bounds')}")

    graph = build_graph(MemorySaver())

    # ── Run 1: the council profile under test ────────────────────────────────
    _hr(f"RUN 1 — council profile {PROFILE_NAME!r} (grounding on, max_usd $0.10)")
    state, emits = await _run(graph, _seed(TASK, profile_name=PROFILE_NAME))

    spec = state.get("council_spec") or {}
    panel = sorted((state.get("panel_results") or []), key=lambda r: r.get("idx", 0))
    assistants = [m for m in (state.get("messages") or [])
                  if isinstance(m, dict) and m.get("role") == "assistant"]
    token_emits = [e for e in emits if isinstance(e, dict) and e.get("type") == "token"]

    print(f"\ncouncil_spec.mode = {spec.get('mode')!r}; "
          f"bounds = {spec.get('bounds')}")
    print(f"seats resolved: {[s.get('posture') for s in (spec.get('resolved_seats') or [])]}")

    print("\n--- (a) PER-SEAT ROLE-PROMPT ADHERENCE ---")
    role_ok = True
    for r in panel:
        posture = r.get("posture", "?")
        text = (r.get("text") or "").strip()
        snippet = text[:160].replace("\n", " ")
        marker = EXPECTED_MARKERS.get(posture, posture.upper())
        hit = marker in text[:80].upper()
        role_ok = role_ok and hit
        flag = "OK " if hit else "MISS"
        print(f"  [{flag}] seat {posture:9s} -> {snippet!r}")

    print("\n--- (b) BUDGET BINDS (per-profile ceiling, not global $5) ---")
    err = state.get("error") or ""
    budget_ok = ("ceiling" in err.lower()) and ("0.10" in err)
    print(f"  error frame: {err!r}")
    print(f"  council_cost_usd = {state.get('council_cost_usd')}; "
          f"grounding_verdict = {state.get('grounding_verdict')}")
    print(f"  [{'OK ' if budget_ok else 'MISS'}] ceiling notice present at $0.10")

    print("\n--- (c) EXACTLY ONE ANSWER LANDS ---")
    one_ok = len(assistants) == 1 and len(token_emits) == 1
    print(f"  assistant messages = {len(assistants)}; custom token emits = {len(token_emits)}")
    if assistants:
        print(f"  final answer (first 240 chars): "
              f"{assistants[0]['content'][:240]!r}")
    print(f"  [{'OK ' if one_ok else 'MISS'}] exactly one emit + one message")

    # ── Run 2: control — no profile, same task → plain route, no council ──────
    _hr("RUN 2 — CONTROL (no profile) — must route byte-identically (no council)")
    cstate, cemits = await _run(graph, _seed(TASK, profile_name=None))
    control_ok = (cstate.get("council_spec") is None) and not (cstate.get("panel_results"))
    print(f"  council_spec = {cstate.get('council_spec')}; "
          f"panel_results = {len(cstate.get('panel_results') or [])}")
    print(f"  backend chosen = {cstate.get('backend')!r}; "
          f"face = {cstate.get('face_id')!r}")
    print(f"  [{'OK ' if control_ok else 'MISS'}] no council_spec, no panel (plain path)")

    _hr("SUMMARY")
    print(f"  (a) role prompts inject : {'PASS' if role_ok else 'FAIL'}")
    print(f"  (b) budget binds        : {'PASS' if budget_ok else 'FAIL'}")
    print(f"  (c) exactly one answer  : {'PASS' if one_ok else 'FAIL'}")
    print(f"  control no-regression   : {'PASS' if control_ok else 'FAIL'}")
    all_ok = role_ok and budget_ok and one_ok and control_ok
    print(f"\n  OVERALL: {'[ALL PASS]' if all_ok else '[SEE MISSES ABOVE]'}")


if __name__ == "__main__":
    asyncio.run(main())
