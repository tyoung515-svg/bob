"""
BoBClaw Core — MS9-W5 (finding B) council FINALIZATION robustness (network-free).

The live hang: a FUSION council whose 3 seats all finished ("done", real tok counts)
never finalized — ``synthesize_node``'s synth-backend call had NO timeout, so a synth
that HANGS (no exception) stalled the whole council forever. No completing
``council_synth`` / terminal ``council_event`` ever fired, so the app banner stuck on
"Deliberating… Round 0 · $0.0000" with cost never updating.

Proves the fix (all mocked — zero network):
  * the synth call is TIMEOUT-BOUNDED — a hanging backend can no longer wedge the node
    (the fallback loop only advanced on an Exception; a hang tripped nothing);
  * on total synth failure the node DEGRADES to the best answer so far (the raw panel
    seat positions) and GUARANTEES a terminal frame: the completing ``council_synth``
    ALWAYS, plus a ``blocked`` council_event in fusion (opt-in gated);
  * DEBATE turns do NOT get a synthesize-authored blocked event (debate_converge owns
    the debate terminal frame — no double-emit);
  * the HAPPY path (synth returns promptly) is byte-identical: the synth answer commits
    exactly as before and NO blocked frame is emitted.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from core.nodes.synthesize import synthesize_node
from core.telemetry.emit import KIND_COUNCIL_SYNTH


_HANDOFF = """\
Reconciled: option A.

### 📋 COUNCIL HANDOFF
- **[RESOLVED]:** IDEA-01
- **[ACTIVE DEBATE]:** None
- **[BLOCKED]:** None
- **[CORRECTION]:** None
- **[NEXT TASK]:** @Human: proceed
"""


class _FakeWriter:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, chunk):
        self.calls.append(chunk)


def _fusion_state(seat_text="a substantive framer position", *, emit_events=True, mode="fusion"):
    spec = {"mode": mode, "synth_backend": "minimax"}
    if emit_events:
        spec["emit_events"] = True
    return {
        "task": "should we adopt X?",
        "council_spec": spec,
        "council_cost_usd": 0.0,
        "council_restart": 0,
        "panel_results": [
            {"idx": 0, "posture": "framer", "backend": "claude_api",
             "text": seat_text, "round": 0, "tokens": 100, "cost_usd": 0.001},
            {"idx": 1, "posture": "stress", "backend": "gemini_flash",
             "text": "a stress objection", "round": 0, "tokens": 80, "cost_usd": 0.001},
        ],
        "messages": [],
    }


async def _hang(messages, backend):
    """A synth backend that never responds (open socket) — the finding-B failure mode."""
    await asyncio.sleep(30)
    return "unreachable"  # pragma: no cover


def _synth_capture():
    frames: list = []

    async def _emit(kind, flight_id, payload=None, **kw):
        frames.append((kind, dict(payload or {})))
        return {}

    return frames, _emit


def _council_event_capture():
    events: list = []

    async def _emit(kind, flight_id, payload=None, **kw):
        events.append(dict(payload or {}))
        return {}

    return events, _emit


# ─── anti-hang: the synth call is timeout-bounded ────────────────────────────

async def test_synth_hang_is_timeout_bounded_and_does_not_wedge_the_node(monkeypatch):
    """Every synth candidate HANGS, yet synthesize_node returns quickly (well under a
    hard 5s wall-clock cap) instead of stalling forever. The 5s wait_for makes a
    regression a LOUD failure rather than a frozen suite."""
    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    monkeypatch.setattr("core.nodes.synthesize.COUNCIL_SYNTH_TIMEOUT_SECONDS", 0.02)

    state = _fusion_state()
    with patch("core.nodes.synthesize._send_to_backend", _hang), \
         patch("core.nodes.synthesize.emit_event", AsyncMock()), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        out = await asyncio.wait_for(synthesize_node(state), timeout=5)

    assert out["error"]                             # surfaced, not swallowed
    assert out["council_pending_answer"] is None


# ─── degrade + guaranteed terminal frame (fusion, opt-in on) ─────────────────

async def test_synth_failure_degrades_to_best_answer_and_emits_terminal_frames(monkeypatch):
    """Fusion + emit_events: total synth failure → the degraded answer carries the raw
    seat positions (best answer so far); the completing council_synth fires (the app's
    fusion banner resolver); AND a terminal `blocked` council_event fires."""
    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    monkeypatch.setattr("core.nodes.synthesize.COUNCIL_SYNTH_TIMEOUT_SECONDS", 0.02)

    synth_frames, synth_emit = _synth_capture()
    council_events, ce_emit = _council_event_capture()
    writer = _FakeWriter()

    state = _fusion_state(seat_text="FRAMER_SEAT_TEXT")
    with patch("core.nodes.synthesize._send_to_backend", _hang), \
         patch("core.nodes.synthesize.emit_event", synth_emit), \
         patch("core.council.events.emit_event", ce_emit), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer):
        out = await asyncio.wait_for(synthesize_node(state), timeout=5)

    # Degraded to the best answer so far: the raw seat positions.
    ans = out["messages"][0]["content"]
    assert "FRAMER_SEAT_TEXT" in ans
    assert "raw seat positions" in ans.lower()
    # The degraded answer streamed to the client as a message-level custom chunk.
    assert any("FRAMER_SEAT_TEXT" in (c.get("content") or "") for c in writer.calls)
    # Terminal frame #1: the completing council_synth (banner resolves for fusion).
    assert any(kind == KIND_COUNCIL_SYNTH for kind, _ in synth_frames)
    # Terminal frame #2: a `blocked` council_event, exactly once, with the honest reason.
    blocked = [e for e in council_events if e.get("phase") == "blocked"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "synth_unavailable"
    # No hang, error surfaced, carrier cleared.
    assert out["error"] and out["council_pending_answer"] is None
    assert out["council_handoff"] is None


async def test_synth_failure_without_optin_still_resolves_via_council_synth(monkeypatch):
    """emit_events ABSENT: no `blocked` council_event (byte-identical tap behavior), but
    the completing council_synth STILL fires — so even without the U7 tap the fusion
    banner resolves via council_synth (never a permanent hang)."""
    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    monkeypatch.setattr("core.nodes.synthesize.COUNCIL_SYNTH_TIMEOUT_SECONDS", 0.02)

    synth_frames, synth_emit = _synth_capture()
    council_events, ce_emit = _council_event_capture()

    state = _fusion_state(emit_events=False)
    with patch("core.nodes.synthesize._send_to_backend", _hang), \
         patch("core.nodes.synthesize.emit_event", synth_emit), \
         patch("core.council.events.emit_event", ce_emit), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        out = await asyncio.wait_for(synthesize_node(state), timeout=5)

    assert council_events == []                              # tap off ⇒ no council_event
    assert any(k == KIND_COUNCIL_SYNTH for k, _ in synth_frames)  # council_synth still fires
    assert out["error"]


async def test_synth_failure_with_no_seat_text_still_emits_terminal_frame(monkeypatch):
    """Even when NO seat produced usable text, total synth failure still emits the
    completing council_synth + a blocked council_event (terminal frame guaranteed) and
    returns the error notice — never a hang."""
    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    monkeypatch.setattr("core.nodes.synthesize.COUNCIL_SYNTH_TIMEOUT_SECONDS", 0.02)

    synth_frames, synth_emit = _synth_capture()
    council_events, ce_emit = _council_event_capture()

    state = _fusion_state()
    for r in state["panel_results"]:  # every seat failed (empty text) too
        r["text"] = ""
    with patch("core.nodes.synthesize._send_to_backend", _hang), \
         patch("core.nodes.synthesize.emit_event", synth_emit), \
         patch("core.council.events.emit_event", ce_emit), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        out = await asyncio.wait_for(synthesize_node(state), timeout=5)

    assert "failed on all synth backends" in out["messages"][0]["content"].lower()
    assert any(k == KIND_COUNCIL_SYNTH for k, _ in synth_frames)
    assert any(e.get("phase") == "blocked" for e in council_events)


# ─── debate mode: no synthesize-authored blocked (debate_converge owns it) ───

async def test_synth_failure_debate_mode_no_blocked_frame_from_synthesize(monkeypatch):
    """DEBATE: on synth failure synthesize commits the degraded answer + council_synth,
    but does NOT author a `blocked` council_event — debate_converge_node owns the debate
    terminal frame, so synthesize must not double-emit it."""
    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    monkeypatch.setattr("core.nodes.synthesize.COUNCIL_SYNTH_TIMEOUT_SECONDS", 0.02)

    synth_frames, synth_emit = _synth_capture()
    council_events, ce_emit = _council_event_capture()

    state = _fusion_state(mode="debate")
    with patch("core.nodes.synthesize._send_to_backend", _hang), \
         patch("core.nodes.synthesize.emit_event", synth_emit), \
         patch("core.council.events.emit_event", ce_emit), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        out = await asyncio.wait_for(synthesize_node(state), timeout=5)

    assert any(k == KIND_COUNCIL_SYNTH for k, _ in synth_frames)      # still resolves banner
    assert all(e.get("phase") != "blocked" for e in council_events)   # debate_converge owns it
    assert out["error"]


# ─── happy path: byte-identical (the timeout only fires on a genuine hang) ───

async def test_happy_path_unchanged_no_blocked_frame_and_commits(monkeypatch):
    """The happy-path invariant: a synth that RETURNS promptly commits exactly as before
    — the synth answer in messages, council_synth fires once, the answer streams once,
    and NO `blocked` council_event is emitted (the timeout path never triggers)."""
    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)

    synth_frames, synth_emit = _synth_capture()
    council_events, ce_emit = _council_event_capture()
    writer = _FakeWriter()

    async def _ok(messages, backend):
        return "THE COUNCIL ANSWER" + _HANDOFF

    state = _fusion_state()
    with patch("core.nodes.synthesize._send_to_backend", _ok), \
         patch("core.nodes.synthesize.emit_event", synth_emit), \
         patch("core.council.events.emit_event", ce_emit), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer):
        out = await synthesize_node(state)

    assert out["messages"][0]["content"].startswith("THE COUNCIL ANSWER")
    assert out["council_pending_answer"] is None
    assert out["council_handoff"] is not None
    # council_synth committed exactly once (the fusion terminal frame).
    assert [k for k, _ in synth_frames].count(KIND_COUNCIL_SYNTH) == 1
    # NO blocked council_event on the happy path (synthesize authors it only on failure).
    assert all(e.get("phase") != "blocked" for e in council_events)
    # The answer streamed exactly once via the custom channel.
    assert len(writer.calls) == 1
    assert writer.calls[0]["content"].startswith("THE COUNCIL ANSWER")
