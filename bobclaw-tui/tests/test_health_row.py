"""BoBClaw TUI — connection-health row E2E-equivalent smoke (Lane 1c, T2).

No live gateway (invariant §5): we inject monitor frames straight into the reducer and
drive ``ConnState`` by hand, then render the header exactly as ``app.py`` does. This IS
the "kill Redis mid-run → header shows DOWN + recovery" acceptance — the whole health
path (reducer ``error`` branch → ``health()`` → ``panels.header_line`` ← ``ConnState``)
is exercised without a terminal or socket.
"""
from __future__ import annotations

from bobclaw_tui.monitor_client import ConnState
from bobclaw_tui.monitor_state import MonitorState
from bobclaw_tui.panels import header_line, health_line


def _row(st: MonitorState, conn: ConnState) -> str:
    """Render the header exactly as BobCockpit._render does."""
    return header_line(st.flights(), st.health(), conn.status, conn.attempts)


def test_conn_state_transitions():
    conn = ConnState()
    assert conn.status == "connecting" and conn.attempts == 0
    conn.note_connected()
    assert conn.status == "live" and conn.attempts == 0
    conn.note_disconnected()
    conn.note_disconnected()
    assert conn.status == "reconnecting" and conn.attempts == 2
    conn.note_connected()                                  # recovery resets the count
    assert conn.status == "live" and conn.attempts == 0


def test_kill_redis_midrun_then_recovery():
    """Redis dies mid-run then recovers — driven entirely by injected frames."""
    st = MonitorState()
    conn = ConnState()
    conn.note_connected()

    # 1. a live run with some work → header shows live + honest cost/token totals
    st.apply({"type": "worker_state", "flight_id": "run-1", "idx": 0,
              "status": "ok", "backend": "deepseek_v4_flash", "tokens": 900})
    st.apply({"type": "cost", "flight_id": "run-1", "usd": 0.05, "by_backend": {}})
    row = _row(st, conn)
    assert "● monitor: live" in row
    assert "$0.050 EST" in row and "~900 tok" in row

    # 2. Redis goes down mid-run → the socket forwards an error frame → header DOWN
    st.apply({"type": "error", "code": "redis_unavailable", "message": "conn refused"})
    row = _row(st, conn)
    assert "● monitor: DOWN:redis_unavailable" in row
    # the DOWN state wins even though ConnState is still "live" (socket up, Redis down)
    assert conn.status == "live"
    # cost/token totals are preserved (the error frame created no phantom flight)
    assert "$0.050 EST" in row and st.flight("ambient") is None

    # 3. Redis recovers → the next healthy frame clears health → header back to live
    st.apply({"type": "worker_state", "flight_id": "run-1", "idx": 1,
              "status": "ok", "backend": "deepseek_v4_flash", "tokens": 600})
    row = _row(st, conn)
    assert "● monitor: live" in row
    assert "~1.5k tok" in row                              # 900 + 600 aggregated


def test_socket_dead_distinct_from_redis_down_and_idle():
    """User must tell idle (live) from socket-dead (reconnecting) from Redis-down (DOWN)."""
    st = MonitorState()
    # idle-but-healthy
    assert health_line(st.health(), "live", 0) == "● monitor: live"
    # socket dead → the WS client is backing off
    assert health_line(st.health(), "reconnecting", 3) == "● monitor: reconnecting(3)"
    # Redis down (error frame present, socket may still be live)
    st.apply({"type": "error", "code": "redis_unavailable", "message": "x"})
    assert health_line(st.health(), "live", 0) == "● monitor: DOWN:redis_unavailable"
