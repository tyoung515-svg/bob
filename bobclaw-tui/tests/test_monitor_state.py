"""BoBClaw TUI — monitor-state reducer tests (Lane 1c, the CI-testable data layer)."""
from __future__ import annotations

from bobclaw_tui.monitor_state import MonitorState


def _ws(flight, idx, status, backend="deepseek_v4_flash", **extra):
    return {"type": "worker_state", "flight_id": flight, "idx": idx,
            "status": status, "backend": backend, "ts": "T", **extra}


def test_worker_state_running_then_terminal():
    st = MonitorState()
    st.apply({"type": "fleet_start", "flight_id": "ms-5", "n_workers": 2, "wave": 0,
              "backend": "deepseek_v4_flash", "kind": "chat"})
    st.apply(_ws("ms-5", 0, "running"))
    st.apply(_ws("ms-5", 1, "running"))
    v = st.flight("ms-5")
    assert v.running() == 2 and v.done() == 0
    assert v.fleet["n_workers"] == 2
    # worker 0 completes → latest state wins
    st.apply(_ws("ms-5", 0, "ok", duration_ms=12))
    assert v.running() == 1 and v.done() == 1
    assert v.workers[0]["duration_ms"] == 12


def test_failed_counts():
    st = MonitorState()
    st.apply(_ws("ms-5", 0, "failed"))
    st.apply(_ws("ms-5", 1, "timeout"))
    st.apply(_ws("ms-5", 2, "ok"))
    v = st.flight("ms-5")
    assert v.failed() == 2 and v.done() == 3


def test_fleet_join_and_cost():
    st = MonitorState()
    st.apply({"type": "fleet_join", "flight_id": "ms-5", "ok": 18, "failed": 2, "total": 20})
    st.apply({"type": "cost", "flight_id": "ms-5", "usd": 0.42,
              "by_backend": {"kimi_platform": 0.42}})
    v = st.flight("ms-5")
    assert v.join == {"ok": 18, "failed": 2, "total": 20, "kind": None}
    assert v.cost["usd"] == 0.42 and v.cost["by_backend"]["kimi_platform"] == 0.42


def test_council_frames():
    st = MonitorState()
    st.apply({"type": "council_seat", "flight_id": "c1", "idx": 0, "posture": "framer",
              "backend": "claude_api", "status": "ok", "round": 0})
    st.apply({"type": "council_synth", "flight_id": "c1", "backend": "minimax",
              "status": "ok", "shape": "sequential"})
    v = st.flight("c1")
    assert v.council["seats"][0]["posture"] == "framer"
    assert v.council["synth"]["shape"] == "sequential"


def test_grouping_named_before_ambient():
    st = MonitorState()
    st.apply(_ws("ambient", 0, "running"))
    st.apply(_ws("chat:conv1", 0, "running"))
    st.apply(_ws("ms-5", 0, "running"))       # named block of work
    order = [v.flight_id for v in st.flights()]
    # named flight first, then ambient (chat:* / ambient), each alpha
    assert order[0] == "ms-5"
    assert set(order[1:]) == {"ambient", "chat:conv1"}
    assert st.flight("ms-5").is_ambient() is False
    assert st.flight("chat:conv1").is_ambient() is True


def test_none_flight_buckets_to_ambient():
    st = MonitorState()
    st.apply(_ws(None, 0, "running"))
    assert st.flight("ambient") is not None
    assert st.total_running() == 1


def test_unknown_and_malformed_frames_ignored():
    st = MonitorState()
    st.apply({"type": "brand_new_frame", "flight_id": "ms-5", "x": 1})  # counted, not crashing
    st.apply("not a dict")            # ignored entirely
    st.apply(None)
    v = st.flight("ms-5")
    assert v is not None and v.events == 1 and v.workers == {}


def test_total_running_across_flights():
    st = MonitorState()
    st.apply(_ws("a", 0, "running"))
    st.apply(_ws("a", 1, "running"))
    st.apply(_ws("b", 0, "running"))
    st.apply(_ws("b", 1, "ok"))
    assert st.total_running() == 3


# ── T2: connection-level health (error frames no longer silently swallowed) ──

def test_error_frame_sets_connection_health():
    st = MonitorState()
    assert st.health() is None                      # clean by default
    st.apply({"type": "error", "code": "redis_unavailable",
              "message": "redis down", "ts": "T1"})
    h = st.health()
    assert h is not None
    assert h["code"] == "redis_unavailable"
    assert h["message"] == "redis down" and h["ts"] == "T1"


def test_error_frame_is_connection_level_not_per_flight():
    """An error frame is a transport signal — it must NOT create a phantom flight lane
    nor tick a flight's events (else Redis-down heartbeats spawn/churn an `ambient`)."""
    st = MonitorState()
    st.apply({"type": "error", "code": "forbidden", "message": "no"})
    assert st.flights() == []                        # no FlightView created
    assert st.flight("ambient") is None


def test_health_clears_on_next_healthy_frame():
    """The reducer-injection health smoke (E2E-equiv): DOWN:redis_unavailable → cleared
    once a real good frame arrives (recovery is observed, never guessed)."""
    st = MonitorState()
    st.apply({"type": "error", "code": "redis_unavailable", "message": "x"})
    assert st.health()["code"] == "redis_unavailable"
    # a healthy worker_state frame proves the stream delivers again → health clears
    st.apply(_ws("ms-5", 0, "ok", tokens=100))
    assert st.health() is None
    assert st.flight("ms-5").done() == 1             # and the good frame still applied
    # a cost frame is likewise a healthy frame that clears a fresh error
    st.apply({"type": "error", "code": "decode_error", "message": "y"})
    assert st.health()["code"] == "decode_error"
    st.apply({"type": "cost", "flight_id": "ms-5", "usd": 0.02})
    assert st.health() is None


def test_terminal_worker_tokens_lights_flight_tokens():
    """G0(b) wire end-to-end on the TUI side: a terminal worker_state carrying int
    `tokens` lights FlightView.tokens() (the running frame contributes none)."""
    st = MonitorState()
    st.apply(_ws("f", 0, "running"))                 # no tokens yet
    assert st.flight("f").tokens() == 0
    st.apply(_ws("f", 0, "ok", tokens=1500))         # terminal frame supersedes
    assert st.flight("f").tokens() == 1500
