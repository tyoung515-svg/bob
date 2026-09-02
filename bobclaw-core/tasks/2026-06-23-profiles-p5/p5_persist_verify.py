"""
BoBClaw — verify scheduled-run output is SURFACED to a conversation (the same
Postgres conversations/messages the web UI / KMM read).

Runs a real one-tick scheduled fire with persistence enabled, then queries the
conversations table to confirm the run landed as a conversation a user can open.
Needs Postgres up + DEEPSEEK_API_KEY.

    cd bobclaw-core; set PYTHONPATH=.; run the venv python on this file.
"""
from __future__ import annotations

import asyncio
import logging
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

from core import db as core_db  # noqa: E402
from core import teams  # noqa: E402
from core.graph import build_graph  # noqa: E402
from core.scheduler import SchedulerLedger, run_tick  # noqa: E402
from scripts.profile_scheduler import _FireRunner  # noqa: E402

PROFILE = "sched-persist-verify"


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        await core_db.init_postgres()
    except Exception as e:  # noqa: BLE001
        print(f"[!] Postgres unavailable ({type(e).__name__}: {e}); cannot verify "
              "persistence. Bring up the stack first.")
        return

    teams.create_profile(PROFILE, {
        "shape": "fusion",
        "synth_backend": "deepseek_v4_flash",
        "seats": [{"posture": "framer", "backend": "deepseek_v4_flash",
                   "role_prompt": "Answer in 2 sentences. Begin with 'FRAMER:'."}],
        "protocol_bounds": {"grounding": "off"},
        "schedule": {"cron": "* * * * *", "face_hint": "assistant",
                     "task": "One sentence: why schedule a council digest?"},
    }, overwrite=True)

    graph = build_graph(MemorySaver())
    runner = _FireRunner(graph, max_concurrent=2, persist=True, default_owner="admin")
    ledger = SchedulerLedger(_CORE_ROOT / "bobclaw-core" / ".memory" / "persist_verify.db")
    await ledger.init()

    now = datetime.now(timezone.utc)
    print(f"\nfiring scheduled profile {PROFILE!r} (persist=True)...")
    fired = await run_tick(now, lambda: [teams.load_profile(PROFILE)],
                           ledger.try_claim, runner.invoke, catchup_seconds=120)
    print(f"dispatched: {fired}")
    await runner.drain()

    # Now read it back the way the UI does: list conversations + their messages.
    convs = await core_db.list_conversations(user_id="admin", limit=10)
    sched = [c for c in convs if PROFILE in (c.get("title") or "")]
    print("\n" + "=" * 78 + "\nVERIFY — scheduled run visible as a conversation\n" + "=" * 78)
    if not sched:
        print("  [FAIL] no conversation found for the scheduled run")
    else:
        c = sched[0]
        print(f"  conversation: {c['id']}")
        print(f"  title       : {c['title']}")
        print(f"  user_id     : {c['user_id']}  | face_id: {c.get('face_id')}")
        msgs = await core_db.get_conversation_messages(str(c["id"]), limit=10)
        for m in msgs:
            snippet = (m["content"] or "")[:120].replace("\n", " ")
            md = m.get("metadata")
            print(f"    [{m['role']:9s}] {snippet!r}" + (f"  meta={md}" if md else ""))
        roles = [m["role"] for m in msgs]
        ok = "user" in roles and "assistant" in roles
        print(f"\n  [{'PASS' if ok else 'FAIL'}] conversation has the task + the answer "
              "(what the UI renders)")

    try:
        (_CORE_ROOT / "bobclaw-core" / ".memory" / "persist_verify.db").unlink()
    except Exception:
        pass
    await core_db.get_pool().close()


if __name__ == "__main__":
    asyncio.run(main())
