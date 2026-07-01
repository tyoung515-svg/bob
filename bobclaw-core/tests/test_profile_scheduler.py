"""
BoBClaw Core — Profiles cron scheduler (P5) tests (network-free, clock-injected).

Covers (per the plan §Phase-5 test list):
  * fire_bucket_for: a due bucket within the catch-up window fires; a stale bucket
    (daemon-just-started) does NOT back-fire; an invalid cron is skipped (no crash).
  * SchedulerLedger: the real SQLite INSERT-OR-IGNORE dedup — two claims of the same
    (profile, bucket) → True then False (exactly-once across ticks/processes).
  * run_tick: a due profile fires exactly once across repeated ticks; an unscheduled
    profile never fires; a stale bucket is skipped; an invoke failure consumes the
    bucket (no retry) and does not stop the other profiles.
  * build_schedule_seed shape matches the /api/chat initial_state contract.

No live scheduler/daemon, no graph, no network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.scheduler import (
    SchedulerLedger,
    build_schedule_seed,
    fire_bucket_for,
    persist_run,
    run_tick,
    summarize_run,
)

UTC = timezone.utc


def _profile(name: str, *, cron: str | None = None, task: str = "do the thing",
             face_hint: str | None = None) -> dict:
    env: dict = {"name": name, "builtin": False, "shape": "fusion"}
    if cron is not None:
        sched: dict = {"cron": cron, "task": task}
        if face_hint is not None:
            sched["face_hint"] = face_hint
        env["schedule"] = sched
    return env


# ── fire_bucket_for ──────────────────────────────────────────────────────────

def test_fire_bucket_due_within_catchup():
    now = datetime(2026, 6, 23, 14, 30, 3, tzinfo=UTC)
    bucket = fire_bucket_for("*/5 * * * *", now, catchup_seconds=120)
    assert bucket == datetime(2026, 6, 23, 14, 30, 0, tzinfo=UTC)


def test_fire_bucket_stale_is_not_backfired():
    # Daily 02:00; at 14:30 the most recent bucket is ~12.5h old → not due.
    now = datetime(2026, 6, 23, 14, 30, 0, tzinfo=UTC)
    assert fire_bucket_for("0 2 * * *", now, catchup_seconds=120) is None


def test_fire_bucket_just_inside_and_outside_window():
    now = datetime(2026, 6, 23, 14, 31, 0, tzinfo=UTC)
    # hourly at :30 → bucket 14:30:00, 60s old. Inside a 120s window, outside a 30s.
    assert fire_bucket_for("30 * * * *", now, catchup_seconds=120) == \
        datetime(2026, 6, 23, 14, 30, 0, tzinfo=UTC)
    assert fire_bucket_for("30 * * * *", now, catchup_seconds=30) is None


def test_fire_bucket_invalid_cron_returns_none():
    now = datetime(2026, 6, 23, 14, 30, 0, tzinfo=UTC)
    assert fire_bucket_for("not a cron", now, catchup_seconds=120) is None
    assert fire_bucket_for("", now, catchup_seconds=120) is None


# ── SchedulerLedger (real SQLite exactly-once) ───────────────────────────────

async def test_ledger_try_claim_dedups(tmp_path):
    ledger = SchedulerLedger(tmp_path / "sched.db")
    await ledger.init()
    first = await ledger.try_claim("p1", "2026-06-23T14:30:00+00:00", fired_at="t0")
    second = await ledger.try_claim("p1", "2026-06-23T14:30:00+00:00", fired_at="t1")
    assert first is True       # this caller claimed the bucket
    assert second is False     # already fired — no double claim
    # A different bucket for the same profile is a fresh claim.
    other = await ledger.try_claim("p1", "2026-06-23T14:35:00+00:00", fired_at="t2")
    assert other is True
    # A different profile, same bucket, is also independent.
    p2 = await ledger.try_claim("p2", "2026-06-23T14:30:00+00:00", fired_at="t3")
    assert p2 is True


async def test_ledger_init_is_idempotent(tmp_path):
    ledger = SchedulerLedger(tmp_path / "sched.db")
    await ledger.init()
    await ledger.init()  # second init must not error / wipe prior claims
    assert await ledger.try_claim("p", "b", fired_at="t") is True
    await ledger.init()
    assert await ledger.try_claim("p", "b", fired_at="t") is False


async def test_ledger_prune_removes_old_keeps_recent(tmp_path):
    ledger = SchedulerLedger(tmp_path / "sched.db")
    await ledger.init()
    await ledger.try_claim("p", "b-old", fired_at="2026-06-01T00:00:00+00:00")
    await ledger.try_claim("p", "b-new", fired_at="2026-06-23T00:00:00+00:00")
    deleted = await ledger.prune("2026-06-10T00:00:00+00:00")
    assert deleted == 1
    # The pruned (old) bucket can be claimed afresh; the recent one is still held.
    assert await ledger.try_claim("p", "b-old", fired_at="t") is True
    assert await ledger.try_claim("p", "b-new", fired_at="t") is False


# ── run_tick ─────────────────────────────────────────────────────────────────

class _Recorder:
    def __init__(self, *, fail_for: set[str] | None = None):
        self.calls: list[tuple[str, str]] = []
        self._fail_for = fail_for or set()

    async def __call__(self, profile: dict, schedule: dict, bucket_iso: str):
        self.calls.append((profile["name"], bucket_iso))
        if profile["name"] in self._fail_for:
            raise RuntimeError("boom")
        return {"ok": True}


async def test_run_tick_fires_due_profile_once_across_ticks(tmp_path):
    ledger = SchedulerLedger(tmp_path / "sched.db")
    await ledger.init()
    rec = _Recorder()
    profiles = [_profile("nightly", cron="*/5 * * * *")]
    now = datetime(2026, 6, 23, 14, 30, 3, tzinfo=UTC)

    fired1 = await run_tick(now, lambda: profiles, ledger.try_claim, rec, catchup_seconds=120)
    # Two more ticks inside the SAME 5-min window (same bucket) must NOT re-fire.
    fired2 = await run_tick(now + timedelta(seconds=30), lambda: profiles,
                            ledger.try_claim, rec, catchup_seconds=120)
    fired3 = await run_tick(now + timedelta(seconds=59), lambda: profiles,
                            ledger.try_claim, rec, catchup_seconds=120)

    assert fired1 == [("nightly", "2026-06-23T14:30:00+00:00")]
    assert fired2 == [] and fired3 == []
    assert rec.calls == [("nightly", "2026-06-23T14:30:00+00:00")]  # exactly once


async def test_run_tick_next_window_fires_again(tmp_path):
    ledger = SchedulerLedger(tmp_path / "sched.db")
    await ledger.init()
    rec = _Recorder()
    profiles = [_profile("five", cron="*/5 * * * *")]

    await run_tick(datetime(2026, 6, 23, 14, 30, 3, tzinfo=UTC),
                   lambda: profiles, ledger.try_claim, rec, catchup_seconds=120)
    # Next window (14:35) is a new bucket → fires again.
    fired = await run_tick(datetime(2026, 6, 23, 14, 35, 2, tzinfo=UTC),
                           lambda: profiles, ledger.try_claim, rec, catchup_seconds=120)
    assert fired == [("five", "2026-06-23T14:35:00+00:00")]
    assert len(rec.calls) == 2


async def test_run_tick_skips_unscheduled_profile(tmp_path):
    ledger = SchedulerLedger(tmp_path / "sched.db")
    await ledger.init()
    rec = _Recorder()
    # No `schedule` key at all, and one with a schedule but no cron.
    profiles = [
        _profile("plain"),                       # no schedule
        {"name": "half", "schedule": {"task": "x"}},  # schedule, no cron
    ]
    fired = await run_tick(datetime(2026, 6, 23, 14, 30, 3, tzinfo=UTC),
                           lambda: profiles, ledger.try_claim, rec, catchup_seconds=120)
    assert fired == []
    assert rec.calls == []


async def test_run_tick_skips_empty_task(tmp_path):
    """A cron with no task burns a fire on a vacuous turn — skip it (nit fix)."""
    ledger = SchedulerLedger(tmp_path / "sched.db")
    await ledger.init()
    rec = _Recorder()
    profiles = [
        {"name": "no-task", "schedule": {"cron": "*/5 * * * *", "task": ""}},
        {"name": "missing-task", "schedule": {"cron": "*/5 * * * *"}},
    ]
    fired = await run_tick(datetime(2026, 6, 23, 14, 30, 3, tzinfo=UTC),
                           lambda: profiles, ledger.try_claim, rec, catchup_seconds=120)
    assert fired == []
    assert rec.calls == []


async def test_run_tick_skips_stale_bucket(tmp_path):
    ledger = SchedulerLedger(tmp_path / "sched.db")
    await ledger.init()
    rec = _Recorder()
    profiles = [_profile("daily2am", cron="0 2 * * *")]
    # Daemon starts at 14:30 — the 02:00 bucket is stale and must not back-fire.
    fired = await run_tick(datetime(2026, 6, 23, 14, 30, 0, tzinfo=UTC),
                           lambda: profiles, ledger.try_claim, rec, catchup_seconds=120)
    assert fired == []
    assert rec.calls == []


async def test_run_tick_invoke_failure_consumes_bucket_and_isolates(tmp_path):
    """A failing invoke is logged, does NOT stop other profiles, and the bucket is
    consumed (claimed) so it is not retried on the next tick."""
    ledger = SchedulerLedger(tmp_path / "sched.db")
    await ledger.init()
    rec = _Recorder(fail_for={"bad"})
    profiles = [
        _profile("bad", cron="*/5 * * * *"),
        _profile("good", cron="*/5 * * * *"),
    ]
    now = datetime(2026, 6, 23, 14, 30, 3, tzinfo=UTC)
    fired = await run_tick(now, lambda: profiles, ledger.try_claim, rec, catchup_seconds=120)
    # 'good' fired; 'bad' raised so it's not in `fired`, but it WAS claimed.
    assert fired == [("good", "2026-06-23T14:30:00+00:00")]
    assert ("bad", "2026-06-23T14:30:00+00:00") in rec.calls

    # Next tick in the same window: 'bad' is NOT retried (bucket consumed); 'good'
    # already fired. No new invokes.
    rec.calls.clear()
    fired2 = await run_tick(now + timedelta(seconds=20), lambda: profiles,
                            ledger.try_claim, rec, catchup_seconds=120)
    assert fired2 == []
    assert rec.calls == []


# ── build_schedule_seed ──────────────────────────────────────────────────────

# The /api/chat initial_state keys (api/server.py) — the contract a scheduled seed
# must match so a scheduled turn routes identically to an HTTP turn.
_HTTP_SEED_KEYS = {
    "messages", "task", "conversation_id", "user_id", "face_id", "model_override",
    "backend_override", "team", "profile_name", "backend", "tools_allowed",
    "approval_required", "approval_response", "artifacts", "error",
    "project_instructions",
}


def test_build_schedule_seed_matches_http_shape():
    prof = _profile("nightly-audit", cron="0 2 * * *", task="audit the repo",
                    face_hint="reviewer")
    seed = build_schedule_seed(prof, prof["schedule"], "2026-06-23T02:00:00+00:00")
    assert set(seed.keys()) == _HTTP_SEED_KEYS
    assert seed["profile_name"] == "nightly-audit"
    assert seed["task"] == "audit the repo"
    assert seed["face_id"] == "reviewer"
    assert seed["conversation_id"] == "sched:nightly-audit:2026-06-23T02:00:00+00:00"
    assert isinstance(seed["tools_allowed"], list)
    assert seed["messages"] == [] and seed["backend"] == "local"


def test_build_schedule_seed_defaults_face_to_assistant():
    prof = _profile("p", cron="*/5 * * * *")  # no face_hint
    seed = build_schedule_seed(prof, prof["schedule"], "b")
    assert seed["face_id"] == "assistant"
    assert seed["team"] is None and seed["model_override"] is None


# ── summarize_run (observable outcome classifier) ────────────────────────────

def test_summarize_run_ok():
    final = {"messages": [{"role": "user", "content": "q"},
                          {"role": "assistant", "content": "the council answer"}]}
    out = summarize_run(final)
    assert out["status"] == "ok"
    assert "the council answer" in out["detail"]


def test_summarize_run_needs_approval_takes_precedence():
    # An approval interrupt: the dangerous action was gated (not executed). Must be
    # surfaced as needs_approval even if a system message is present.
    final = {"approval_required": True,
             "messages": [{"role": "system", "content": "approval needed"}]}
    out = summarize_run(final)
    assert out["status"] == "needs_approval"
    assert "not executed" in out["detail"].lower()


def test_summarize_run_error():
    out = summarize_run({"error": "minimax timed out", "messages": []})
    assert out["status"] == "error" and "minimax" in out["detail"]


def test_summarize_run_empty_variants():
    assert summarize_run({"messages": [{"role": "user", "content": "q"}]})["status"] == "empty"
    assert summarize_run({})["status"] == "empty"
    assert summarize_run(None)["status"] == "empty"


# ── persist_run (surface scheduled output to a conversation) ─────────────────

class _FakeDB:
    """Records create_conversation / save_message calls (mocks core.db)."""
    def __init__(self):
        self.conversations: list[dict] = []
        self.messages: list[dict] = []

    async def create_conversation(self, *, user_id, title, face_id):
        cid = f"conv-{len(self.conversations) + 1}"
        self.conversations.append(
            {"id": cid, "user_id": user_id, "title": title, "face_id": face_id})
        return {"id": cid}

    async def save_message(self, conversation_id, role, content, metadata=None):
        self.messages.append({"conversation_id": conversation_id, "role": role,
                              "content": content, "metadata": metadata})


def _final_with_answer(answer: str) -> dict:
    return {"messages": [{"role": "user", "content": "q"},
                         {"role": "assistant", "content": answer}]}


async def test_persist_run_ok_persists_full_answer():
    db = _FakeDB()
    prof = _profile("nightly", cron="0 2 * * *", task="audit the repo",
                    face_hint="reviewer")
    long_answer = "Full audit:\n- finding one\n- finding two\n- finding three (full text)"
    conv_id = await persist_run(
        prof, prof["schedule"], "2026-06-23T02:00:00+00:00", _final_with_answer(long_answer),
        owner="admin", create_conversation=db.create_conversation,
        save_message=db.save_message)
    assert conv_id == "conv-1"
    conv = db.conversations[0]
    assert conv["user_id"] == "admin" and conv["face_id"] == "reviewer"
    assert "nightly" in conv["title"] and "2026-06-23T02:00:00" in conv["title"]
    # user task message, then the FULL (untruncated) assistant answer.
    assert db.messages[0] == {"conversation_id": "conv-1", "role": "user",
                              "content": "audit the repo", "metadata": None}
    assert db.messages[1]["role"] == "assistant"
    assert db.messages[1]["content"] == long_answer
    md = db.messages[1]["metadata"]
    assert md["status"] == "ok" and md["scheduled"] is True and md["profile"] == "nightly"


async def test_persist_run_needs_approval_persists_note():
    db = _FakeDB()
    prof = _profile("danger", cron="0 2 * * *", task="delete file tmp.log")
    final = {"approval_required": True,
             "messages": [{"role": "system", "content": "approval needed"}]}
    conv_id = await persist_run(prof, prof["schedule"], "b", final, owner="admin",
                                create_conversation=db.create_conversation,
                                save_message=db.save_message)
    assert conv_id is not None
    assert db.messages[-1]["role"] == "assistant"
    assert "needs_approval" in db.messages[-1]["content"]
    assert db.messages[-1]["metadata"]["status"] == "needs_approval"


async def test_persist_run_error_persists_note():
    db = _FakeDB()
    prof = _profile("p", cron="0 2 * * *", task="t")
    conv_id = await persist_run(prof, prof["schedule"], "b",
                                {"error": "minimax timed out", "messages": []},
                                owner="admin", create_conversation=db.create_conversation,
                                save_message=db.save_message)
    assert conv_id is not None
    assert "error" in db.messages[-1]["content"] and "minimax" in db.messages[-1]["content"]
    assert db.messages[-1]["metadata"]["status"] == "error"


async def test_persist_run_empty_returns_none_and_writes_nothing():
    db = _FakeDB()
    prof = _profile("p", cron="0 2 * * *", task="t")
    conv_id = await persist_run(prof, prof["schedule"], "b",
                                {"messages": [{"role": "user", "content": "q"}]},
                                owner="admin", create_conversation=db.create_conversation,
                                save_message=db.save_message)
    assert conv_id is None
    assert db.conversations == [] and db.messages == []


async def test_persist_run_honors_owner():
    db = _FakeDB()
    prof = _profile("p", cron="0 2 * * *", task="t")
    await persist_run(prof, prof["schedule"], "b", _final_with_answer("a"),
                      owner="admin", create_conversation=db.create_conversation,
                      save_message=db.save_message)
    assert db.conversations[0]["user_id"] == "admin"
