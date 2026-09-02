"""BoBClaw TUI — panel formatter tests (Lane 1c, the CI-testable data layer).

Routing + Approvals panes are REST-poll-fed; their JSON→lines shaping is pure, so it's
tested here without a terminal (same discipline as the monitor_state reducer)."""
from __future__ import annotations

from bobclaw_tui.monitor_state import MonitorState
from bobclaw_tui.panels import (
    approval_prompt_lines,
    approvals_lines,
    bot_rows,
    bots_lines,
    chat_cell,
    cost_line,
    decide_result_line,
    fleet_lines,
    fmt_tokens,
    header_line,
    health_line,
    orch_header,
    routing_lines,
    total_tokens,
)


def test_fmt_tokens():
    assert fmt_tokens(0) == "0"
    assert fmt_tokens(940) == "940"
    assert fmt_tokens(4200) == "4.2k"
    assert fmt_tokens(1_300_000) == "1.3M"
    assert fmt_tokens(None) == "0"


def test_flight_tokens_sum_and_orch_header_tick():
    st = MonitorState()
    st.apply({"type": "worker_state", "flight_id": "f", "idx": 0, "status": "running",
              "backend": "deepseek_v4_flash"})  # running → no tokens yet
    st.apply({"type": "worker_state", "flight_id": "f", "idx": 0, "status": "ok",
              "backend": "deepseek_v4_flash", "tokens": 1200})  # terminal supersedes
    st.apply({"type": "worker_state", "flight_id": "f", "idx": 1, "status": "ok",
              "backend": "deepseek_v4_flash", "tokens": 800})
    v = st.flight("f")
    assert v.tokens() == 2000
    assert "~2.0k tok" in orch_header(v)


def test_is_quiescent_and_evict():
    st = MonitorState()
    st.apply({"type": "worker_state", "flight_id": "f", "idx": 0, "status": "running",
              "backend": "d"})
    assert not st.flight("f").is_quiescent()      # a worker is mid-run
    st.apply({"type": "worker_state", "flight_id": "f", "idx": 0, "status": "ok",
              "backend": "d", "tokens": 10})
    assert st.flight("f").is_quiescent()           # all terminal now
    assert st.evict("f") is True
    assert st.flight("f") is None
    assert st.evict("f") is False                  # idempotent


def test_council_seat_running_is_not_quiescent():
    st = MonitorState()
    st.apply({"type": "council_seat", "flight_id": "cx", "idx": 0, "posture": "framer",
              "backend": "minimax", "status": "running", "round": 0})
    assert not st.flight("cx").is_quiescent()


def _face(fid, role, pref, resolved, tool=False):
    return {"id": fid, "role": role, "preferred_backend": pref,
            "resolved_backend": resolved, "tool_capable": tool}


def test_routing_header_counts_and_rerouted_first():
    view = {
        "active_team": "demo-fleet",
        "live_probe": True,
        "faces": [
            _face("assistant", "apex", "local", "local"),               # not rerouted
            _face("worker-deepseek", "worker", "kimi_code", "deepseek_v4_flash"),  # rerouted
        ],
    }
    lines = routing_lines(view)
    assert "team: demo-fleet" in lines[0]
    assert "live · health-walk" in lines[1]
    assert "faces: 2" in lines[2] and "rerouted: 1" in lines[2]
    # rerouted face is listed first (interesting rows on top) and marked
    body = [ln for ln in lines[4:] if ln.strip()]
    assert body[0].startswith("↝")
    assert "worker-deepseek" in body[0]


def test_routing_declared_probe_and_tool_mark():
    view = {"active_team": None, "live_probe": False,
            "faces": [_face("assistant-tools", "apex", "glm_5_2", "glm_5_2", tool=True)]}
    lines = routing_lines(view)
    assert "(default · per-face)" in lines[0]
    assert "declared · not-checked" in lines[1]
    assert any("🔧" in ln for ln in lines)


def test_routing_caps_faces():
    faces = [_face(f"f{i}", "worker", "local", "local") for i in range(20)]
    lines = routing_lines({"faces": faces, "active_team": "t", "live_probe": False}, max_faces=5)
    assert any("+15 more" in ln for ln in lines)


def test_routing_unavailable_on_bad_payload():
    assert routing_lines({"error": "502 bad gateway"}) == ["routing unavailable: 502 bad gateway"]
    assert routing_lines({}) == ["routing unavailable"]
    assert routing_lines("nonsense") == ["routing unavailable"]


def test_orch_header_shows_dispatch_join_and_counts():
    st = MonitorState()
    st.apply({"type": "fleet_start", "flight_id": "ms-5", "n_workers": 100, "wave": 0,
              "backend": "deepseek_v4_flash", "kind": "chat"})
    st.apply({"type": "worker_state", "flight_id": "ms-5", "idx": 0, "status": "running",
              "backend": "deepseek_v4_flash"})
    st.apply({"type": "worker_state", "flight_id": "ms-5", "idx": 1, "status": "ok",
              "backend": "deepseek_v4_flash"})
    st.apply({"type": "fleet_join", "flight_id": "ms-5", "ok": 99, "failed": 1, "total": 100})
    header = orch_header(st.flight("ms-5"))
    assert "ms-5" in header
    assert "dispatched 100" in header
    assert "deepseek_v4_flash" in header
    assert "join 99/100" in header and "1 failed" in header
    assert "1 run" in header and "1 done" in header


def test_fleet_lines_two_tier_tree_and_council_seats():
    st = MonitorState()
    st.apply({"type": "fleet_start", "flight_id": "ms-5", "n_workers": 2, "wave": 0,
              "backend": "deepseek_v4_flash", "kind": "chat"})
    st.apply({"type": "worker_state", "flight_id": "ms-5", "idx": 0, "status": "running",
              "backend": "deepseek_v4_flash", "role": "worker-deepseek"})
    st.apply({"type": "council_seat", "flight_id": "cx", "idx": 0, "posture": "risk",
              "backend": "minimax", "status": "answered", "round": 1})
    st.apply({"type": "council_synth", "flight_id": "cx", "backend": "minimax",
              "status": "committed", "shape": "debate"})
    lines = fleet_lines(st.flights())
    # a worker row is indented beneath its flight's header
    assert any(ln.startswith("    w0") and "worker-deepseek" in ln for ln in lines)
    # the council flight surfaces its synthesizer (the council "manager") + its seat
    assert any("synth[debate]" in ln for ln in lines)
    assert any(ln.startswith("    seat0") and "risk" in ln for ln in lines)


def test_fleet_lines_empty():
    assert fleet_lines(MonitorState().flights()) == ["no fleet activity yet"]


# ── T6: fleet truncation is no longer silent ──

def test_fleet_lines_truncation_marker():
    st = MonitorState()
    # 30 workers on one flight → header(1) + 30 rows = 31 lines; cap at 10.
    for i in range(30):
        st.apply({"type": "worker_state", "flight_id": "big", "idx": i,
                  "status": "ok", "backend": "d"})
    lines = fleet_lines(st.flights(), max_lines=10)
    assert lines[0] == "… +21 earlier lines"          # 31 - 10 dropped, marked at top
    assert len(lines) == 11                             # marker + the 10 tailed rows
    # the LATEST rows are what survive (fleet tails)
    assert any("w29" in ln for ln in lines)
    # ascii mode swaps the ellipsis glyph
    a = fleet_lines(st.flights(), max_lines=10, ascii_mode=True)
    assert a[0] == "... +21 earlier lines"


# ── T3: honest cost line (EST badge, never a bare $) ──

def test_cost_line_est_badge_and_sum():
    st = MonitorState()
    st.apply({"type": "cost", "flight_id": "a", "usd": 0.10, "by_backend": {}})
    st.apply({"type": "cost", "flight_id": "b", "usd": 0.014, "by_backend": {}})
    line = cost_line(st.flights())
    assert line == "$0.114 EST"                        # summed across flights
    assert "EST" in line                               # provenance badge mandatory
    # empty fleet is still honest ($0 EST), never a bare $ or blank
    assert cost_line([]) == "$0.000 EST"


# ── T4: header token aggregate ──

def test_total_tokens_and_header_line():
    st = MonitorState()
    st.apply({"type": "worker_state", "flight_id": "a", "idx": 0, "status": "ok",
              "backend": "d", "tokens": 1200})
    st.apply({"type": "worker_state", "flight_id": "b", "idx": 0, "status": "ok",
              "backend": "d", "tokens": 3000})
    st.apply({"type": "cost", "flight_id": "a", "usd": 0.05})
    assert total_tokens(st.flights()) == 4200
    row = header_line(st.flights(), st.health(), "live", 0)
    assert "● monitor: live" in row
    assert "$0.050 EST" in row
    assert "~4.2k tok" in row


# ── T2: the combined header health cell (reducer health() + ConnState) ──

def test_health_line_states_and_ascii():
    # idle-but-healthy socket → live (success)
    assert health_line(None, "live", 0) == "● monitor: live"
    # socket dropped → reconnecting(n) (warn); attempt count surfaced
    assert health_line(None, "reconnecting", 2) == "● monitor: reconnecting(2)"
    # never-connected yet → connecting
    assert health_line(None, "connecting", 0) == "● monitor: connecting"
    # a transport error frame (Redis down) wins even over a live socket → DOWN:<code>
    down = health_line({"code": "redis_unavailable"}, "live", 0)
    assert down == "● monitor: DOWN:redis_unavailable"
    # ASCII fallback swaps the dot, keeps the word (VOCABULARY §4)
    assert health_line(None, "live", 0, ascii_mode=True) == "o monitor: live"


def test_approvals_empty_is_clean():
    assert approvals_lines({"items": []}) == ["no pending approvals ✓"]


def test_approvals_lists_pending():
    payload = {"items": [
        {"action_type": "email_send", "conversation_id": "abcd1234ef", "status": "pending"},
        {"action_type": "cc_edit", "conversation_id": "9999", "status": "pending"},
    ]}
    lines = approvals_lines(payload)
    assert "2 pending" in lines[0]
    assert any("email_send" in ln and "abcd1234" in ln for ln in lines)
    assert any("cc_edit" in ln for ln in lines)


def test_approvals_caps_items():
    payload = {"items": [{"action_type": "x", "conversation_id": "c"} for _ in range(30)]}
    lines = approvals_lines(payload, max_items=10)
    assert any("+20 more" in ln for ln in lines)


def test_approvals_unavailable_on_bad_payload():
    assert approvals_lines({"error": "Postgres unavailable"}) == [
        "approvals unavailable: Postgres unavailable"
    ]
    assert approvals_lines({}) == ["approvals unavailable"]


# ── Wave 1B Task 4: the approvals pane is actionable now (/ui is gone) ──
def test_approvals_header_advertises_tui_actions():
    # the stale "approve/deny in /ui" pointer is gone — the TUI IS the gate surface
    lines = approvals_lines({"items": [{"action_type": "x", "conversation_id": "c"}]})
    assert "/ui" not in lines[0]
    assert "a approve" in lines[0] and "d deny" in lines[0]


def test_approvals_selected_row_marked():
    payload = {"items": [
        {"action_type": "email_send", "conversation_id": "abcd1234ef"},
        {"action_type": "cc_edit", "conversation_id": "9999"},
    ]}
    lines = approvals_lines(payload, selected=1)
    rows = [ln for ln in lines if ln.startswith((">", "•"))]
    assert rows[0].startswith("•") and "email_send" in rows[0]
    assert rows[1].startswith(">") and "cc_edit" in rows[1]
    # selected=None (untracked callers) is byte-identical to the old read-only render
    assert approvals_lines(payload) == approvals_lines(payload, selected=None)


def test_approval_prompt_lines_render_action_and_details():
    frame = {"type": "approval_request", "approval_id": "abc123",
             "action": "email_send", "details": {"to": "travis@x.z", "subject": "hi"}}
    lines = approval_prompt_lines(frame)
    assert lines[0].startswith("⚠ approval requested: email_send")
    assert "to=travis@x.z" in lines[0] and "subject=hi" in lines[0]
    assert "y" in lines[1] and "n" in lines[1] and "Esc" in lines[1]


def test_approval_prompt_lines_tolerate_missing_details_and_ascii():
    lines = approval_prompt_lines({"type": "approval_request"}, ascii_mode=True)
    assert lines[0] == "! approval requested: ?"   # no details → no summary dash
    # details=None / non-dict never raises
    assert approval_prompt_lines({"action": "x", "details": None})[0].endswith(": x")


def test_decide_result_line_variants():
    ok = {"status": "approved", "decision": "recorded", "agent_resume": "ok"}
    assert decide_result_line(ok) == "approval approved · agent resumed"
    failed = {"status": "rejected", "agent_resume": "failed",
              "agent_resume_message": "core unreachable"}
    assert decide_result_line(failed) == \
        "approval rejected · agent resume FAILED: core unreachable"
    # a bare row (no resume fields) still renders the recorded status
    assert decide_result_line({"status": "approved"}) == "approval approved"


def test_chat_cell_blanks_when_unbound():
    # no conversation bound yet → empty cell, status row byte-identical to before
    assert chat_cell(None) == ""
    assert chat_cell("") == ""
    assert chat_cell("   ") == ""


def test_chat_cell_truncates_long_titles():
    assert chat_cell("short chat") == "short chat"
    cell = chat_cell("x" * 40)
    assert len(cell) == 24 and cell.endswith("…")


# ── Wave 2 Task 3: the Bots pane (teammate roster + unread watermark) ──

def _binding(slug, face_id, *, name=None, avatar=None, conv="conv-1",
             updated="2026-08-18T10:00:00"):
    return {"slug": slug, "display_name": name or slug, "face_id": face_id,
            "avatar": avatar, "conversation_id": conv, "updated_at": updated}


_FACES = [
    {"id": "assistant", "avatar": "🤖", "simple_slot": "quick"},       # teammate via simple_slot
    {"id": "reviewer", "avatar": "🧐", "bot": True},                   # teammate via bot: true
    {"id": "worker-deepseek", "avatar": "🔧"},                         # NOT a teammate
]


def test_bots_lines_renders_teammate_roster():
    agents = {"items": [
        _binding("helper", "assistant", name="Assistant", avatar="🤖"),
        _binding("review", "reviewer", name="Reviewer", avatar="🧐"),
        _binding("drone", "worker-deepseek", name="Drone"),            # filtered out
    ]}
    lines = bots_lines(agents, _FACES, {})
    assert lines[0] == "2 bots (Enter opens chat):"
    assert any("🤖 Assistant (helper) · 2026-08-18T10:00" in ln for ln in lines)
    assert any("🧐 Reviewer (review)" in ln for ln in lines)
    assert not any("drone" in ln for ln in lines)      # non-teammate face filtered


def test_bots_lines_fail_open_when_faces_unavailable():
    """A garbled /faces payload must not empty the roster — the binding row is the bot
    identity, so the roster renders unfiltered (defensive teammate filter)."""
    agents = {"items": [_binding("helper", "assistant", name="Assistant", avatar="🤖")]}
    lines = bots_lines(agents, {"error": "core unreachable"}, {})
    assert any("Assistant (helper)" in ln for ln in lines)
    # a face MISSING from the registry payload keeps its binding too
    lines = bots_lines(agents, [], {})
    assert any("Assistant (helper)" in ln for ln in lines)


def test_bots_lines_unavailable_on_bad_agents_payload():
    assert bots_lines({"error": "Postgres unavailable"}, _FACES) == [
        "bots unavailable: Postgres unavailable"
    ]
    assert bots_lines({}, _FACES) == ["bots unavailable"]
    assert bots_lines("nonsense", _FACES) == ["bots unavailable"]


def test_bots_lines_empty_roster():
    assert bots_lines({"items": []}, _FACES) == [
        "no bots yet — a teammate appears once its binding is created"
    ]


def test_bots_lines_unread_watermark_marks_newer_activity():
    agents = {"items": [
        _binding("helper", "assistant", name="Assistant", avatar="🤖",
                 updated="2026-08-18T10:00:00"),
        _binding("review", "reviewer", name="Reviewer", avatar="🧐",
                 updated="2026-08-18T09:00:00"),
    ]}
    seen = {"helper": "2026-08-18T09:30:00",   # activity NEWER than the watermark → mark
            "review": "2026-08-18T09:30:00"}   # older than the watermark → no mark
    lines = bots_lines(agents, _FACES, seen)
    assert lines[1].startswith("●") and "helper" in lines[1]
    assert lines[2].startswith(" ") and "review" in lines[2]
    # never-seen bots (no watermark) with activity render as unread
    fresh = bots_lines(agents, _FACES, {})
    assert all(ln.startswith("●") for ln in fresh[1:])
    # ascii mode swaps the glyph, keeps the row
    ascii_lines = bots_lines(agents, _FACES, seen, ascii_mode=True)
    assert ascii_lines[1].startswith("*")


def test_bots_lines_no_timestamp_without_bound_conversation():
    agents = {"items": [_binding("helper", "assistant", name="A", avatar="🤖", conv=None)]}
    lines = bots_lines(agents, _FACES, {})
    assert lines[1].strip().endswith("(helper)")      # no " · <ts>" tail
    assert not lines[1].startswith("●")               # no activity ⇒ never unread


def test_bot_rows_matches_bots_lines_order():
    agents = {"items": [
        _binding("helper", "assistant"), _binding("review", "reviewer"),
        _binding("drone", "worker-deepseek"),
    ]}
    rows = bot_rows(agents, _FACES)
    assert [r["slug"] for r in rows] == ["helper", "review"]
    lines = bots_lines(agents, _FACES, {})
    assert len(lines) == len(rows) + 1                 # header + one row per binding
    assert bot_rows({"error": "x"}, _FACES) is None
