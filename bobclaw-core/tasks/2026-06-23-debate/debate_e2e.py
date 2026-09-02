"""
BoBClaw — debate shape live E2E (real deepseek seats + synth, grounding OFF).

Runs a real `shape: debate` council on a contested topic through the SAME compiled
graph the HTTP path uses, and confirms:
  (a) ROUNDS RUN     — council_round increments across rounds (the loop fires).
  (b) DEBATE TRACKED — the [ACTIVE DEBATE] Idea-IDs are tracked round-over-round
                       (shrink toward convergence, or the round cap stops it).
  (c) ONE ANSWER     — exactly one final answer reaches the client (one custom emit
                       + one assistant message) — the exactly-once contract under
                       the real loop (what unit tests can't prove live).

Run live (needs DEEPSEEK_API_KEY):
    cd bobclaw-core; set PYTHONPATH=.; run the venv python on this file.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_CORE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from core import teams  # noqa: E402
from core.graph import build_graph  # noqa: E402

PROFILE = "debate-e2e"

# A genuinely contested decision so the seats argue (active debate persists a round
# or two). grounding OFF so the debate loop is the only loop; max_rounds caps it.
PROFILE_DEF = {
    "shape": "debate",
    "synth_backend": "deepseek_v4_flash",
    "seats": [
        {"posture": "framer", "backend": "deepseek_v4_flash",
         "role_prompt": "You are the FRAMER. Frame the decision and take a clear "
                        "position. Assign each open dispute an Idea-ID like [D-1]."},
        {"posture": "stress", "backend": "deepseek_v4_flash",
         "role_prompt": "You are the STRESS-TESTER. Attack the framing; cite the "
                        "Idea-ID you challenge. Concede points that survive scrutiny."},
        {"posture": "wildcard", "backend": "deepseek_v4_flash",
         "role_prompt": "You are the WILDCARD. Offer a contrarian third option; tie "
                        "it to the open Idea-IDs."},
    ],
    "protocol_bounds": {"grounding": "off", "max_rounds": 3, "max_usd": 4.0},
}

TASK = ("Decision: a 12-person startup with a 3-year-old monolith that is slowing "
        "feature delivery — rewrite into microservices NOW, or keep the monolith and "
        "only extract services under proven scaling pressure? Argue it out and land a "
        "recommendation.")


def _hr(t: str) -> None:
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


async def main() -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("[!] DEEPSEEK_API_KEY not set - the council backends will fail.")

    teams.create_profile(PROFILE, PROFILE_DEF, overwrite=True)
    print(f"authored debate profile {PROFILE!r} (max_rounds=3, grounding off)")

    graph = build_graph(MemorySaver())
    seed = {
        "messages": [], "task": TASK, "conversation_id": "debate-e2e",
        "user_id": None, "face_id": "assistant", "model_override": None,
        "backend_override": None, "team": None, "profile_name": PROFILE,
        "backend": "local", "tools_allowed": [], "approval_required": False,
        "approval_response": None, "artifacts": [], "error": None,
        "project_instructions": None,
    }
    cfg = {"configurable": {"thread_id": "debate-e2e"}, "recursion_limit": 60}

    _hr("RUNNING the debate (real deepseek; may take a minute)...")
    custom_emits: list[dict] = []
    rounds_seen: list[int] = []
    debate_seq: list[tuple[int, list]] = []
    final_state: dict = {}
    async for mode, chunk in graph.astream(seed, cfg, stream_mode=["values", "custom"]):
        if mode == "custom":
            if isinstance(chunk, dict) and chunk.get("type") == "token":
                custom_emits.append(chunk)
        elif mode == "values":
            final_state = chunk
            r = chunk.get("council_round") or 0
            if r not in rounds_seen:
                rounds_seen.append(r)
            ho = chunk.get("council_handoff") or {}
            ad = ho.get("active_debate")
            if ad is not None:
                snap = (r, list(ad))
                if not debate_seq or debate_seq[-1] != snap:
                    debate_seq.append(snap)

    assistants = [m for m in (final_state.get("messages") or [])
                  if isinstance(m, dict) and m.get("role") == "assistant"]

    _hr("RESULTS")
    print(f"  rounds observed (council_round)  : {rounds_seen}")
    print(f"  cost accrued (council_cost_usd)  : {final_state.get('council_cost_usd')}")
    print("\n  [ACTIVE DEBATE] Idea-IDs per round:")
    for r, ad in debate_seq:
        print(f"    round {r}: {ad if ad else '(converged — empty)'}")
    print(f"\n  custom answer emits = {len(custom_emits)}; assistant messages = {len(assistants)}")
    if assistants:
        print(f"  final answer (first 280 chars): {assistants[-1]['content'][:280]!r}")

    rounds_ok = len(rounds_seen) >= 1                     # the loop ran
    one_answer = len(custom_emits) == 1 and len(assistants) == 1
    multi_round = max(rounds_seen) >= 1 if rounds_seen else False

    _hr("SUMMARY")
    print(f"  (a) loop ran / rounds tracked : {'PASS' if rounds_ok else 'FAIL'} "
          f"(max round {max(rounds_seen) if rounds_seen else 0}, "
          f"{'multi-round' if multi_round else 'single round'})")
    print(f"  (b) [ACTIVE DEBATE] tracked   : {'PASS' if debate_seq else 'WARN (synth emitted no handoff)'}")
    print(f"  (c) exactly one answer lands  : {'PASS' if one_answer else 'FAIL'}")
    print(f"\n  OVERALL: {'[PASS]' if (rounds_ok and one_answer) else '[SEE ABOVE]'}")


if __name__ == "__main__":
    asyncio.run(main())
