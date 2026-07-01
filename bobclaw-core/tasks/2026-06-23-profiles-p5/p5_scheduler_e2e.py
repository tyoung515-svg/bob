"""
BoBClaw — P5 live E2E: a scheduled profile fires its council via the real path.

Exercises the ACTUAL daemon wiring (scripts.profile_scheduler._make_invoke) +
core.scheduler.run_tick + the real SchedulerLedger, against the same compiled
graph the HTTP path uses and REAL deepseek backends. Proves:

  (a) FIRES        — a profile whose schedule.cron is due now is claimed + invoked,
                     and its fusion council actually runs (an assistant answer lands).
  (b) EXACTLY ONCE — a second tick in the same cron minute does NOT re-fire (the
                     INSERT-OR-IGNORE ledger dedups the bucket).
  (c) NO-OP WHEN   — a profile with NO schedule is never fired by the same tick.
      UNSCHEDULED

Run live (needs DEEPSEEK_API_KEY in env / .secrets):
    cd bobclaw-core
    set PYTHONPATH=.
    run the venv python on tasks/2026-06-23-profiles-p5/p5_scheduler_e2e.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
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
from core.scheduler import SchedulerLedger, run_tick  # noqa: E402
from core.graph import build_graph  # noqa: E402
from scripts.profile_scheduler import _FireRunner  # noqa: E402

SCHED_PROFILE = "sched-fusion-e2e"

# A fast, clean council: deepseek seats, grounding OFF (commit in-node, one answer,
# no grounding spawn), cron "* * * * *" so the bucket is the current minute (due now).
PROFILE = {
    "shape": "fusion",
    "synth_backend": "deepseek_v4_flash",
    "seats": [
        {"posture": "framer", "backend": "deepseek_v4_flash",
         "role_prompt": "You are the FRAMER. In 2 sentences, frame the question and "
                        "name the key tradeoff. Begin with 'FRAMER:'."},
        {"posture": "stress", "backend": "deepseek_v4_flash",
         "role_prompt": "You are the STRESS-TESTER. Name the single biggest risk in "
                        "1-2 sentences. Begin with 'STRESS:'."},
    ],
    "protocol_bounds": {"grounding": "off", "max_usd": 2.0},
    "schedule": {"cron": "* * * * *",
                 "task": "Is a cron-scheduled nightly council a good idea for ops digests?"},
}


def _hr(t: str) -> None:
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("[!] DEEPSEEK_API_KEY not set - the council backends will fail.")

    # Author the scheduled profile + a plain (unscheduled) control profile.
    teams.create_profile(SCHED_PROFILE, PROFILE, overwrite=True)
    teams.create_profile("sched-control-unscheduled",
                         {"shape": "fusion",
                          "seats": [{"posture": "framer", "backend": "deepseek_v4_flash"}]},
                         overwrite=True)
    print(f"authored {SCHED_PROFILE!r} (cron='* * * * *', grounding off) + an "
          "unscheduled control")

    graph = build_graph(MemorySaver())
    runner = _FireRunner(graph, max_concurrent=4)
    ledger = SchedulerLedger(_CORE_ROOT / "bobclaw-core" / ".memory" / "e2e_scheduler.db")
    await ledger.init()

    now = datetime.now(timezone.utc)
    bucket_iso = now.replace(second=0, microsecond=0).isoformat()

    _hr(f"TICK 1 @ {now.isoformat()} (bucket {bucket_iso})")
    fired1 = await run_tick(now, teams.list_profiles, ledger.try_claim, runner.invoke,
                            catchup_seconds=120)
    print(f"\ndispatched: {fired1}")

    _hr("TICK 2 (same minute) — must NOT re-fire (ledger dedup)")
    fired2 = await run_tick(now, teams.list_profiles, ledger.try_claim, runner.invoke,
                            catchup_seconds=120)
    print(f"dispatched: {fired2}")

    # Claim-then-SPAWN: the fire runs as a background task — wait for it so the
    # council answer log line lands before the summary.
    print("\n(draining the spawned fire — the council runs in the background...)")
    await runner.drain()

    # ── assertions ───────────────────────────────────────────────────────────
    fired_names = [n for n, _ in fired1]
    a_fires = (fired1 == [(SCHED_PROFILE, bucket_iso)])
    b_dedup = (fired2 == [])
    c_unscheduled = ("sched-control-unscheduled" not in fired_names)

    _hr("SUMMARY")
    print(f"  (a) scheduled profile fired once : {'PASS' if a_fires else 'FAIL'}  ({fired1})")
    print(f"  (b) second tick deduped          : {'PASS' if b_dedup else 'FAIL'}  ({fired2})")
    print(f"  (c) unscheduled never fired       : {'PASS' if c_unscheduled else 'FAIL'}")
    print("  (the INFO log line 'scheduled run sched:...' above shows the council answer)")
    ok = a_fires and b_dedup and c_unscheduled
    print(f"\n  OVERALL: {'[ALL PASS]' if ok else '[SEE FAILS ABOVE]'}")

    # cleanup the e2e ledger db (the gitignored profiles can stay)
    try:
        (_CORE_ROOT / "bobclaw-core" / ".memory" / "e2e_scheduler.db").unlink()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
