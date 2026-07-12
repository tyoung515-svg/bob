"""
BoBClaw Core — MS9-U7 council event tap tests (network-free).

Proves the ADDITIVE, OPT-IN ``council_event`` lifecycle frames (SPEC-UI-OVERHAUL
§5/§7) the U8 Council theater consumes:

  * ``core/council/events.py`` — the gate (``events_enabled``), the payload builder,
    and the async/sync emit helpers (NO-OP when opt-in absent; emit + correct payload
    when opted in).
  * ``core/nodes/panel.py`` — panel_dispatch emits ``panel_start``; panel_worker emits
    ``seat_start`` BEFORE its completion ``council_seat`` frame; ``_route_after_panel``
    threads the opt-in flag onto the per-seat Send.
  * ``core/nodes/debate.py`` — debate_converge emits ``round_converged`` / ``round_
    advanced`` / ``blocked``.

The two load-bearing invariants:
  1. A mocked council run emits ORDERED seat/round events when opted in.
  2. With the opt-in ABSENT, ZERO ``council_event`` frames are emitted and the
     final-answer-path deltas (panel_results entry, converge commit) are byte-identical.

Emit is captured by patching ``core.council.events.emit_event`` /
``emit_event_sync`` (the tap's transport) and ``core.nodes.panel.emit_event`` (the
pre-existing ``council_seat`` emit) — no Redis, no sockets.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from core.council.events import (
    COUNCIL_EVENT_PHASES,
    KIND_COUNCIL_EVENT,
    PHASE_BLOCKED,
    PHASE_PANEL_START,
    PHASE_ROUND_ADVANCED,
    PHASE_ROUND_CONVERGED,
    PHASE_SEAT_START,
    emit_council_event,
    emit_council_event_sync,
    events_enabled,
    _build_payload,
)


def _fake_send(text):
    async def _s(messages, backend, *a, **k):
        return text
    return _s


# ─── events_enabled gate ──────────────────────────────────────────────────────

def test_events_enabled_default_off():
    assert events_enabled(None) is False
    assert events_enabled({}) is False
    assert events_enabled({"emit_events": False}) is False
    assert events_enabled({"emit_events": 0}) is False
    assert events_enabled({"emit_events": None}) is False
    assert events_enabled("not-a-mapping") is False
    assert events_enabled(["emit_events"]) is False


def test_events_enabled_on_when_truthy():
    assert events_enabled({"emit_events": True}) is True
    assert events_enabled({"emit_events": 1}) is True
    assert events_enabled({"emit_events": "yes"}) is True


# ─── _build_payload ───────────────────────────────────────────────────────────

def test_build_payload_always_has_phase_and_round():
    p = _build_payload(PHASE_PANEL_START, 0, None, None, None)
    assert p == {"phase": "panel_start", "round": 0}


def test_build_payload_round_coerced_int_and_none_safe():
    assert _build_payload(PHASE_SEAT_START, None, None, None, None)["round"] == 0
    assert _build_payload(PHASE_SEAT_START, 3, None, None, None)["round"] == 3


def test_build_payload_conditional_seat_posture():
    p = _build_payload(PHASE_SEAT_START, 1, 2, "framer", None)
    assert p["seat"] == 2 and p["posture"] == "framer"
    # seat=0 is a valid index and must be kept (not treated as absent).
    assert _build_payload(PHASE_SEAT_START, 0, 0, "x", None)["seat"] == 0


def test_build_payload_extra_merged_but_reserved_protected():
    p = _build_payload(
        PHASE_PANEL_START, 5, 1, "framer",
        {"backend": "x", "phase": "HACK", "round": 99, "seat": 99, "posture": "HACK"},
    )
    # extra may add new keys...
    assert p["backend"] == "x"
    # ...but must NEVER overwrite the reserved keys.
    assert p["phase"] == "panel_start" and p["round"] == 5
    assert p["seat"] == 1 and p["posture"] == "framer"


def test_all_phases_registered():
    assert COUNCIL_EVENT_PHASES == {
        "panel_start", "seat_start", "round_converged", "round_advanced", "blocked",
    }


# ─── emit helpers: gate OFF ⇒ NO-OP; gate ON ⇒ emit council_event ─────────────

async def test_emit_council_event_gate_off_is_noop(monkeypatch):
    called = []
    async def _cap(kind, flight_id, payload=None, **kw):
        called.append(kind); return {}
    monkeypatch.setattr("core.council.events.emit_event", _cap)
    out = await emit_council_event({}, {"flight_id": "f"}, PHASE_PANEL_START)
    assert out is None
    assert called == []  # transport never touched when opt-in absent


async def test_emit_council_event_gate_on_emits_with_payload(monkeypatch):
    calls = []
    async def _cap(kind, flight_id, payload=None, **kw):
        calls.append((kind, flight_id, dict(payload or {}))); return {"type": kind}
    monkeypatch.setattr("core.council.events.emit_event", _cap)
    out = await emit_council_event(
        {"emit_events": True}, {"flight_id": "flight-9"}, PHASE_SEAT_START,
        round_idx=2, seat=1, posture="framer",
        extra={"backend": "deepseek_v4_flash", "phase": "SHOULD_NOT_WIN"},
    )
    assert out == {"type": KIND_COUNCIL_EVENT}
    assert len(calls) == 1
    kind, flight_id, payload = calls[0]
    assert kind == KIND_COUNCIL_EVENT and flight_id == "flight-9"
    assert payload["phase"] == "seat_start"  # extra can't clobber the reserved key
    assert payload["round"] == 2 and payload["seat"] == 1 and payload["posture"] == "framer"
    assert payload["backend"] == "deepseek_v4_flash"


def test_emit_council_event_sync_gate_off_is_noop(monkeypatch):
    called = []
    def _cap(kind, flight_id, payload=None, **kw):
        called.append(kind); return {}
    monkeypatch.setattr("core.council.events.emit_event_sync", _cap)
    assert emit_council_event_sync({}, {}, PHASE_PANEL_START) is None
    assert called == []


def test_emit_council_event_sync_gate_on_emits(monkeypatch):
    calls = []
    def _cap(kind, flight_id, payload=None, **kw):
        calls.append((kind, dict(payload or {}))); return {"type": kind}
    monkeypatch.setattr("core.council.events.emit_event_sync", _cap)
    out = emit_council_event_sync(
        {"emit_events": True}, {"flight_id": "f"}, PHASE_PANEL_START,
        round_idx=0, extra={"seats": ["framer", "stress"], "mode": "fusion"},
    )
    assert out == {"type": KIND_COUNCIL_EVENT}
    assert calls[0][1]["phase"] == "panel_start"
    assert calls[0][1]["seats"] == ["framer", "stress"]


async def test_emit_council_event_failsafe_never_raises(monkeypatch):
    async def _boom(kind, flight_id, payload=None, **kw):
        raise RuntimeError("transport down")
    monkeypatch.setattr("core.council.events.emit_event", _boom)
    # Best-effort: a raising transport must be swallowed (telemetry never breaks a turn).
    assert await emit_council_event({"emit_events": True}, {}, PHASE_SEAT_START) is None


# ─── panel_dispatch_node: panel_start (opt-in) ───────────────────────────────

def test_panel_dispatch_gate_on_emits_panel_start(monkeypatch):
    from core.nodes.panel import panel_dispatch_node

    frames = []
    def _cap(kind, flight_id, payload=None, **kw):
        frames.append((kind, dict(payload or {}))); return {}
    monkeypatch.setattr("core.council.events.emit_event_sync", _cap)

    spec = {"mode": "fusion", "seats": ["framer", "stress"], "synth_backend": "minimax",
            "emit_events": True}
    out = panel_dispatch_node({"council_spec": spec, "task": "the topic",
                               "council_restart": 0})

    assert len(frames) == 1
    kind, p = frames[0]
    assert kind == KIND_COUNCIL_EVENT
    assert p["phase"] == PHASE_PANEL_START and p["round"] == 0
    assert p["mode"] == "fusion" and p["seats"] == ["framer", "stress"]
    # Normal dispatch behavior intact.
    assert [s["posture"] for s in out["council_spec"]["resolved_seats"]] == ["framer", "stress"]
    assert out["council_spec"]["panel_task"]


def test_panel_dispatch_gate_off_emits_nothing_and_is_byte_identical(monkeypatch):
    from core.nodes.panel import panel_dispatch_node

    frames = []
    def _cap(kind, flight_id, payload=None, **kw):
        frames.append(kind); return {}
    monkeypatch.setattr("core.council.events.emit_event_sync", _cap)

    spec_off = {"mode": "fusion", "seats": ["framer", "stress"], "synth_backend": "minimax"}
    out_off = panel_dispatch_node({"council_spec": dict(spec_off), "task": "the topic",
                                   "council_restart": 0})
    assert frames == []  # no council_event when opt-in absent

    # The returned council_spec is identical to what a run WITH the (inert) flag off
    # produces, save for the flag key itself — i.e. the tap adds nothing to the spec.
    spec_flagfalse = {**spec_off, "emit_events": False}
    out_ff = panel_dispatch_node({"council_spec": dict(spec_flagfalse), "task": "the topic",
                                  "council_restart": 0})
    assert frames == []  # emit_events False also stays silent
    a, b = out_off["council_spec"], out_ff["council_spec"]
    assert a["resolved_seats"] == b["resolved_seats"]
    assert a["panel_task"] == b["panel_task"]


def test_panel_dispatch_debate_round_uses_council_round(monkeypatch):
    from core.nodes.panel import panel_dispatch_node

    frames = []
    def _cap(kind, flight_id, payload=None, **kw):
        frames.append(dict(payload or {})); return {}
    monkeypatch.setattr("core.council.events.emit_event_sync", _cap)

    spec = {"mode": "debate", "seats": ["framer"], "synth_backend": "minimax",
            "emit_events": True}
    panel_dispatch_node({"council_spec": spec, "task": "t",
                         "council_round": 3, "council_restart": 0})
    assert frames[0]["round"] == 3 and frames[0]["mode"] == "debate"


# ─── panel_worker_node: seat_start ordered before completion ──────────────────

async def test_panel_worker_gate_on_emits_seat_start_before_council_seat(monkeypatch):
    from core.nodes.panel import panel_worker_node

    order = []
    async def _ce(kind, flight_id, payload=None, **kw):
        order.append(("council_event", dict(payload or {}))); return {}
    async def _seat(kind, flight_id, payload=None, **kw):
        order.append(("council_seat", dict(payload or {}))); return {}
    monkeypatch.setattr("core.council.events.emit_event", _ce)
    monkeypatch.setattr("core.nodes.panel.emit_event", _seat)

    sub = {"seat_posture": "stress", "backend": "deepseek_v4_flash", "fallback_chain": [],
           "task": "shared prompt", "seat_idx": 1, "panel_round": 2, "flight_id": "f",
           "emit_events": True, "messages": []}
    with patch("core.nodes.panel._send_to_backend", _fake_send("a stress voice")):
        out = await panel_worker_node(sub)

    # seat_start (about-to-speak) MUST precede the completion council_seat frame.
    assert [k for k, _ in order] == ["council_event", "council_seat"]
    p = order[0][1]
    assert p["phase"] == PHASE_SEAT_START and p["round"] == 2 and p["seat"] == 1
    assert p["posture"] == "stress" and p["backend"] == "deepseek_v4_flash"
    # The seat still produced its normal result.
    assert out["panel_results"][0]["text"] == "a stress voice"


async def test_panel_worker_gate_off_no_council_event_and_byte_identical(monkeypatch):
    from core.nodes.panel import panel_worker_node

    ce_kinds = []
    async def _ce(kind, flight_id, payload=None, **kw):
        ce_kinds.append(kind); return {}
    monkeypatch.setattr("core.council.events.emit_event", _ce)
    monkeypatch.setattr("core.nodes.panel.emit_event", AsyncMock())  # silence council_seat

    base = {"seat_posture": "framer", "backend": "deepseek_v4_flash", "fallback_chain": [],
            "task": "t", "seat_idx": 0, "panel_round": 0, "flight_id": "f", "messages": []}
    with patch("core.nodes.panel._send_to_backend", _fake_send("framer voice")):
        out_absent = await panel_worker_node(dict(base))                       # no flag
        out_false = await panel_worker_node({**base, "emit_events": False})    # inert flag

    assert ce_kinds == []  # opt-in absent ⇒ zero council_event frames
    # The panel_results entry (the seat's contribution to the final answer) is
    # byte-identical with the flag absent vs. present-but-false.
    assert out_absent["panel_results"][0] == out_false["panel_results"][0]
    # And it carries none of the tap's fields.
    entry = out_absent["panel_results"][0]
    assert "phase" not in entry and "seat" not in entry
    assert entry["posture"] == "framer" and entry["text"] == "framer voice"


# ─── _route_after_panel threads the opt-in flag onto the Send ─────────────────

def test_route_after_panel_threads_emit_events_flag():
    from core.nodes.panel import _route_after_panel

    resolved = [{"idx": 0, "posture": "framer", "backend": "x",
                 "fallback_chain": [], "role_prompt": ""}]
    on = {"mode": "fusion", "panel_task": "t", "resolved_seats": resolved,
          "emit_events": True}
    sends = _route_after_panel({"council_spec": on, "council_restart": 0})
    assert sends[0].arg["emit_events"] is True

    off = {"mode": "fusion", "panel_task": "t", "resolved_seats": resolved}
    sends = _route_after_panel({"council_spec": off, "council_restart": 0})
    assert sends[0].arg["emit_events"] is False  # additive inert bool, never absent


# ─── ordered end-to-end: dispatch → workers (opt-in) ─────────────────────────

async def test_mocked_council_run_emits_ordered_seat_round_events(monkeypatch):
    """The headline accept: a mocked (opt-in) fusion run emits an ORDERED
    panel_start → seat_start(0) → seat_start(1) sequence with correct round/seat/phase."""
    from core.nodes.panel import (
        _route_after_panel,
        panel_dispatch_node,
        panel_worker_node,
    )

    events = []
    def _sync_cap(kind, flight_id, payload=None, **kw):
        events.append(dict(payload or {})); return {}
    async def _async_cap(kind, flight_id, payload=None, **kw):
        events.append(dict(payload or {})); return {}
    monkeypatch.setattr("core.council.events.emit_event_sync", _sync_cap)
    monkeypatch.setattr("core.council.events.emit_event", _async_cap)
    monkeypatch.setattr("core.nodes.panel.emit_event", AsyncMock())  # silence council_seat

    spec = {"mode": "fusion", "seats": ["framer", "stress"], "synth_backend": "minimax",
            "emit_events": True}
    state = {"council_spec": spec, "task": "the topic", "council_restart": 0}

    d = panel_dispatch_node(state)                       # → panel_start
    state = {**state, **d}
    sends = _route_after_panel(state)                    # threads emit_events onto each Send
    with patch("core.nodes.panel._send_to_backend", _fake_send("voice")):
        for s in sends:                                  # → seat_start per seat, in idx order
            await panel_worker_node(s.arg)

    phases = [(e["phase"], e.get("round"), e.get("seat")) for e in events]
    assert phases == [
        (PHASE_PANEL_START, 0, None),
        (PHASE_SEAT_START, 0, 0),
        (PHASE_SEAT_START, 0, 1),
    ]


# ─── debate_converge_node: converged / advanced / blocked ────────────────────

def _dstate(active_debate, *, council_round=0, pending="ANSWER", prev=None, cost=0.0,
            bounds=None, emit_events=False):
    spec = {"mode": "debate", "synth_backend": "minimax"}
    if prev is not None:
        spec["prev_active_debate"] = prev
    if bounds is not None:
        spec["bounds"] = bounds
    if emit_events:
        spec["emit_events"] = True
    return {
        "task": "t",
        "council_spec": spec,
        "council_round": council_round,
        "council_cost_usd": cost,
        "council_handoff": {"active_debate": active_debate, "resolved": [],
                            "blocked": [], "corrections": [], "next_task": ""},
        "council_pending_answer": ({"content": pending, "backend": "minimax"} if pending else None),
        "messages": [{"role": "user", "content": "q"}],
    }


async def _run_converge(st, ce_capture):
    from core.nodes.debate import debate_converge_node

    with patch("core.council.events.emit_event", ce_capture), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        return await debate_converge_node(st)


def _ce_collector():
    calls = []
    async def _cap(kind, flight_id, payload=None, **kw):
        calls.append(dict(payload or {})); return {}
    return calls, _cap


async def test_debate_converge_gate_on_emits_round_converged():
    calls, cap = _ce_collector()
    out = await _run_converge(_dstate([], emit_events=True), cap)
    assert "council_round" not in out                       # converge
    assert len(calls) == 1
    assert calls[0]["phase"] == PHASE_ROUND_CONVERGED
    assert calls[0]["reason"] == "no active debate" and calls[0]["round"] == 0


async def test_debate_converge_gate_on_emits_round_advanced_on_loop():
    calls, cap = _ce_collector()
    out = await _run_converge(
        _dstate(["IDEA-01", "IDEA-02"], prev=["IDEA-01"], council_round=0, emit_events=True),
        cap,
    )
    assert out["council_round"] == 1                        # loop
    assert len(calls) == 1
    assert calls[0]["phase"] == PHASE_ROUND_ADVANCED
    assert calls[0]["round"] == 0 and calls[0]["next_round"] == 1
    assert calls[0]["active_debate"] == ["IDEA-01", "IDEA-02"]


async def test_debate_converge_gate_on_emits_blocked_on_ceiling():
    calls, cap = _ce_collector()
    out = await _run_converge(
        _dstate(["IDEA-01"], prev=["IDEA-02"], council_round=0, cost=0.15,
                bounds={"max_usd": 0.2}, emit_events=True),
        cap,
    )
    assert out["error"] and "ceiling" in out["error"].lower()
    assert len(calls) == 1
    assert calls[0]["phase"] == PHASE_BLOCKED and calls[0]["reason"] == "cost_ceiling"


async def test_debate_converge_gate_off_emits_nothing_and_commits_normally():
    calls, cap = _ce_collector()
    out = await _run_converge(_dstate([], emit_events=False), cap)
    assert calls == []                                     # opt-in absent ⇒ silent
    # Final-answer path unchanged: the deferred answer still commits once on converge.
    assert out["messages"] == [{"role": "assistant", "content": "ANSWER"}]
    assert out["council_pending_answer"] is None
    assert "council_round" not in out
