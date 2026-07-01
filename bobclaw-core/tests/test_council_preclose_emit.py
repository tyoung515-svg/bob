"""
BoBClaw Core — CoCouncil P2 "pre-close emit" tests (network-free).

The P2 bug: synthesize_node emitted the answer to the client + appended it to
``messages`` BEFORE the grounding gate ran, so a grounded restart leaked the
drifted round-0 answer (both the live stream AND persisted history got
round0+round1 concatenated). The fix DEFERS the single client-facing commit to
grounding_node's ``_converge`` chokepoint when grounding is ON, so only the FINAL
grounded answer ever surfaces/persists.

Covers (per the P2 fix spec's NEW/UPDATED TESTS + the brief's Refinement 3):
  * synthesize defers when grounding ON, commits in-node when OFF.
  * _converge / grounding_node commit the pending answer exactly once on EVERY
    converge reason; restart commits NOTHING; one-restart-then-converge emits
    exactly one (round-1) answer; budget-exhaustion emits the last round once.
  * ceiling breach: answer (if pending) commits once via custom channel; the
    ceiling NOTICE rides ``out["error"]`` (server relays it as a single error
    frame), NOT ``messages`` (Refinement 1, no double-emit).
  * synth-failure turn (no pending answer) → grounding commits nothing extra.
  * council_pending_answer is last-write-wins (no operator.add reducer).

All claude_code spawns are mocked (``core.backends.claude_code.ClaudeCodeClient``
is imported lazily in grounding_node); the stream writer is mocked via
``core.nodes.synthesize._get_stream_writer`` (emit_synthesis lives in
synthesize.py and is imported lazily inside _converge).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import COUNCIL_RESTART_BUDGET
from core.nodes.grounding import grounding_node


# ─── helpers ─────────────────────────────────────────────────────────────────

def _verdict_json(claims, research="findings here"):
    return json.dumps({"claims": claims, "research": research})


def _confirmed(n=1):
    return [{"claim": f"c{i}", "status": "confirmed", "note": "ok"} for i in range(n)]


def _contradicted_majority():
    """2 of 3 contradicted = 0.66 ≥ 0.34 default → restart."""
    return [
        {"claim": "a", "status": "contradicted", "note": ""},
        {"claim": "b", "status": "contradicted", "note": ""},
        {"claim": "c", "status": "confirmed", "note": ""},
    ]


def _mock_cc(text):
    """Patch ClaudeCodeClient so .chat returns {"text": text}."""
    client = MagicMock()
    client.chat = AsyncMock(return_value={"text": text, "session_id": "s1",
                                          "is_error": False})
    return MagicMock(return_value=client)


def _state(pending=None, **overrides):
    """A fusion-close state arriving at the grounding gate with grounding ON.

    When ``pending`` is given, the synthesized answer lives in
    council_pending_answer (the deferred P2 path) and messages holds only the
    user turn — exactly the shape grounding_node sees in production.
    """
    base = {
        "task": "Did the Fusion launch beat Fable 5 on cost?",
        "face_id": "council-max",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax",
                         "seats": ["framer", "stress", "wildcard"]},
        "council_restart": 0,
        "council_cost_usd": 0.0,
        "council_handoff": {"resolved": ["IDEA-01"], "active_debate": [],
                            "blocked": [], "corrections": [], "next_task": ""},
        "messages": [{"role": "user", "content": "the question"}],
        "council_pending_answer": pending,
        "conversation_id": "conv-1",
    }
    base.update(overrides)
    return base


class _FakeWriter:
    """Collects the message-level custom chunks emit_synthesis writes."""
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, chunk):
        self.calls.append(chunk)


# ─── synthesize: defer vs in-node commit (cadence-gated) ─────────────────────

async def test_synthesize_defers_commit_when_grounding_on(monkeypatch):
    """Grounding ON → synthesize returns NO answer messages, stashes pending,
    and the stream writer is NOT called (the commit is deferred to converge)."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    writer = _FakeWriter()
    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "panel_results": [
            {"idx": 0, "posture": "framer", "backend": "claude_api", "text": "A"},
        ],
        "messages": [],
    }
    with patch("core.nodes.synthesize._send_to_backend",
               AsyncMock(return_value="The council concludes: option A.")), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()) as l0, \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer):
        out = await synthesize_node(state)

    # No answer committed to messages, no writer call, no L0 — all deferred.
    assert "messages" not in out
    assert writer.calls == []
    l0.assert_not_called()
    # The answer + backend are stashed for grounding_node.
    assert out["council_pending_answer"] == {
        "content": "The council concludes: option A.", "backend": "minimax",
    }
    assert out["council_handoff"] is not None


async def test_synthesize_commits_in_node_when_grounding_off(monkeypatch):
    """Grounding OFF → synthesize commits in-node: answer in messages,
    council_pending_answer None, writer called exactly once."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    writer = _FakeWriter()
    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "panel_results": [
            {"idx": 0, "posture": "framer", "backend": "claude_api", "text": "A"},
        ],
        "messages": [],
    }
    with patch("core.nodes.synthesize._send_to_backend",
               AsyncMock(return_value="committed answer")), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()) as l0, \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer):
        out = await synthesize_node(state)

    assert out["messages"][0]["role"] == "assistant"
    assert out["messages"][0]["content"] == "committed answer"
    assert out["council_pending_answer"] is None
    assert len(writer.calls) == 1
    assert writer.calls[0]["content"] == "committed answer"
    l0.assert_called_once()


async def test_synthesize_bounds_off_commits_in_node_despite_global_on(monkeypatch):
    """P3b: a profile's protocol_bounds.grounding='off' makes synthesize commit
    IN-NODE even when the GLOBAL cadence is ON — the per-run bound overrides the
    global. Proves the per-profile override path (not just the global cadence)."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    writer = _FakeWriter()
    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax",
                         "bounds": {"grounding": "off"}},
        "panel_results": [
            {"idx": 0, "posture": "framer", "backend": "claude_api", "text": "A"},
        ],
        "messages": [],
    }
    with patch("core.nodes.synthesize._send_to_backend",
               AsyncMock(return_value="committed answer")), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()) as l0, \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer):
        out = await synthesize_node(state)

    # Committed in-node despite the global cadence being ON.
    assert out["messages"][0]["content"] == "committed answer"
    assert out["council_pending_answer"] is None
    assert len(writer.calls) == 1
    l0.assert_called_once()


async def test_synthesize_bounds_on_defers_despite_global_off(monkeypatch):
    """The converse: global cadence OFF but the profile sets grounding='on' →
    synthesize DEFERS, so the always-wired ground node owns the single commit."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    writer = _FakeWriter()
    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax",
                         "bounds": {"grounding": "on"}},
        "panel_results": [
            {"idx": 0, "posture": "framer", "backend": "claude_api", "text": "A"},
        ],
        "messages": [],
    }
    with patch("core.nodes.synthesize._send_to_backend",
               AsyncMock(return_value="deferred answer")), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()) as l0, \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer):
        out = await synthesize_node(state)

    assert "messages" not in out
    assert writer.calls == []
    l0.assert_not_called()
    assert out["council_pending_answer"]["content"] == "deferred answer"


async def test_bounds_off_synthesize_then_ground_emits_exactly_once(monkeypatch):
    """End-to-end alignment for the NEW per-profile grounding-off path: GLOBAL
    cadence ON but the profile bounds grounding OFF → synthesize commits in-node
    and the (always-wired) ground node must NO-OP. Exactly one emit total, never a
    double-emit or drop (the streaming-drop class). Pins that synthesize_node and
    grounding_node read the SAME runtime signal."""
    from langgraph.graph import END

    from core.graph import _route_after_ground
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    writer = _FakeWriter()
    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax",
                         "bounds": {"grounding": "off"}},
        "council_restart": 0,
        "council_cost_usd": 0.0,
        "council_handoff": {"active_debate": [], "next_task": ""},
        "messages": [{"role": "user", "content": "q"}],
        "panel_results": [{"idx": 0, "posture": "framer", "backend": "claude_api",
                           "text": "v", "round": 0}],
        "council_pending_answer": None,
    }

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("a grounding-off run must not spawn the verifier")

    def _apply(delta):
        for k, v in delta.items():
            if k == "messages":
                state["messages"] = (state.get("messages") or []) + list(v)
            else:
                state[k] = v

    with patch("core.nodes.synthesize._send_to_backend",
               AsyncMock(return_value="THE ONLY ANSWER")), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.backends.claude_code.ClaudeCodeClient", _Boom):
        _apply(await synthesize_node(state))   # commits in-node (bounds off)
        _apply(await grounding_node(state))    # no-op converge, no spawn

    assert _route_after_ground(state) == END
    assistants = [m for m in state["messages"]
                  if isinstance(m, dict) and m.get("role") == "assistant"]
    assert assistants == [{"role": "assistant", "content": "THE ONLY ANSWER"}]
    assert len(writer.calls) == 1
    assert writer.calls[0]["content"] == "THE ONLY ANSWER"
    assert state["council_pending_answer"] is None


async def test_synthesize_reads_only_latest_round_panel_results(monkeypatch):
    """The max-round superseding filter (the OTHER half of grounded restart):
    panel_results is operator.add-accumulated, so after a restart it holds BOTH
    the round-0 and round-1 seats. synthesize_node must build its synthesis prompt
    from ONLY the latest round's seats, so the re-run round supersedes the drifted
    one. The multi-restart tests fake the round delta via the synth iterator and
    keep a single round-0 panel entry, so this filter is otherwise unguarded —
    if it regressed (read all rounds / min-round), a restart would re-feed the
    drifted seats. Here we feed a real round-0 + round-1 entry and assert the
    synth prompt carries ONLY round-1's seat content."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "off", raising=False)
    captured: dict = {}

    async def _capture(messages, backend):
        captured["messages"] = messages
        return "final answer"

    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "panel_results": [
            {"idx": 0, "posture": "framer", "backend": "claude_api",
             "text": "OLD_SEAT_ROUND0_CONTENT", "round": 0},
            {"idx": 0, "posture": "framer", "backend": "claude_api",
             "text": "NEW_SEAT_ROUND1_CONTENT", "round": 1},
        ],
        "messages": [],
    }
    with patch("core.nodes.synthesize._send_to_backend", _capture), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        await synthesize_node(state)

    user_prompt = next(m["content"] for m in captured["messages"]
                       if m.get("role") == "user")
    assert "NEW_SEAT_ROUND1_CONTENT" in user_prompt
    assert "OLD_SEAT_ROUND0_CONTENT" not in user_prompt


# ─── grounding converge: commit pending exactly once ─────────────────────────

async def test_grounding_converge_commits_pending_answer_once():
    """A converging verdict commits the pending answer exactly once and clears it."""
    writer = _FakeWriter()
    st = _state(pending={"content": "the final grounded answer", "backend": "minimax"})
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(_confirmed(1)))), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await grounding_node(st)

    assert "council_restart" not in out                       # converge
    assert out["messages"] == [
        {"role": "assistant", "content": "the final grounded answer"}
    ]
    assert out["council_pending_answer"] is None              # carrier cleared
    assert len(writer.calls) == 1
    assert writer.calls[0]["content"] == "the final grounded answer"
    assert writer.calls[0]["backend"] == "minimax"


async def test_grounding_restart_does_not_commit_pending_answer():
    """A restarting verdict commits NOTHING: no messages, pending untouched,
    council_restart incremented, reseed written, writer NOT called."""
    writer = _FakeWriter()
    pending = {"content": "round-0 drifted answer", "backend": "minimax"}
    st = _state(pending=pending, council_restart=0)
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(_contradicted_majority()))), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await grounding_node(st)

    assert "messages" not in out                              # nothing committed
    assert "council_pending_answer" not in out                # left untouched
    assert out["council_restart"] == 1
    assert out["council_spec"]["reseed_context"]
    assert writer.calls == []                                 # no emit on restart


async def test_grounding_one_restart_then_converge_single_emit(monkeypatch):
    """synth(round0,drift) → ground(restart) → synth(round1,clean) →
    ground(converge): the writer fires exactly ONCE total, and the committed
    answer is round1's (never round0's). Simulates the carrier overwrite +
    deferred-commit chain end-to-end across two synthesize+ground passes."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    writer = _FakeWriter()

    # Accumulated state we thread through the chain.
    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "council_restart": 0,
        "council_cost_usd": 0.0,
        "council_handoff": {"active_debate": [], "next_task": ""},
        "messages": [{"role": "user", "content": "q"}],
        "panel_results": [{"idx": 0, "posture": "framer", "backend": "claude_api",
                           "text": "v", "round": 0}],
        "council_pending_answer": None,
    }

    synth_outputs = iter(["ROUND0 drifted answer", "ROUND1 corrected answer"])

    async def _synth(messages, backend):
        return next(synth_outputs)

    with patch("core.nodes.synthesize._send_to_backend", _synth), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer):
        # round0 synthesize → defers
        state.update(await synthesize_node(state))
        assert state["council_pending_answer"]["content"] == "ROUND0 drifted answer"

        # round0 ground → restart (drift), commits nothing
        with patch("core.backends.claude_code.ClaudeCodeClient",
                   _mock_cc(_verdict_json(_contradicted_majority()))):
            state.update(await grounding_node(state))
        assert state["council_restart"] == 1
        assert writer.calls == []  # nothing emitted yet
        # pending still holds round0 (untouched on restart) — round1 will overwrite.
        assert state["council_pending_answer"]["content"] == "ROUND0 drifted answer"

        # round1 synthesize → overwrites the carrier (last-write-wins)
        state.update(await synthesize_node(state))
        assert state["council_pending_answer"]["content"] == "ROUND1 corrected answer"

        # round1 ground → converge (clean), commits round1 ONCE
        with patch("core.backends.claude_code.ClaudeCodeClient",
                   _mock_cc(_verdict_json(_confirmed(1)))):
            state.update(await grounding_node(state))

    assert len(writer.calls) == 1
    assert writer.calls[0]["content"] == "ROUND1 corrected answer"
    # The committed messages contain exactly one assistant turn == round1.
    assistants = [m for m in state["messages"]
                  if isinstance(m, dict) and m.get("role") == "assistant"]
    assert assistants == [{"role": "assistant", "content": "ROUND1 corrected answer"}]
    assert state["council_pending_answer"] is None


async def test_grounding_two_restart_budget_then_converge_single_emit(monkeypatch):
    """With budget==2: round0(drift)→round1(drift)→round2 budget-exhausted
    converge. Writer fires exactly once; the final committed answer is round2's;
    no intermediate content ever emitted/appended."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    monkeypatch.setattr("core.nodes.grounding.COUNCIL_RESTART_BUDGET", 2, raising=False)
    writer = _FakeWriter()

    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "council_restart": 0,
        "council_cost_usd": 0.0,
        "council_handoff": {"active_debate": [], "next_task": ""},
        "messages": [{"role": "user", "content": "q"}],
        "panel_results": [{"idx": 0, "posture": "framer", "backend": "claude_api",
                           "text": "v", "round": 0}],
        "council_pending_answer": None,
    }
    synth_outputs = iter(["ROUND0", "ROUND1", "ROUND2"])

    async def _synth(messages, backend):
        return next(synth_outputs)

    drift = _contradicted_majority()

    with patch("core.nodes.synthesize._send_to_backend", _synth), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer):
        # round0: synth defers, ground restarts (restart 0 -> 1)
        state.update(await synthesize_node(state))
        with patch("core.backends.claude_code.ClaudeCodeClient",
                   _mock_cc(_verdict_json(drift))):
            state.update(await grounding_node(state))
        assert state["council_restart"] == 1

        # round1: synth overwrites, ground restarts (restart 1 -> 2)
        state.update(await synthesize_node(state))
        with patch("core.backends.claude_code.ClaudeCodeClient",
                   _mock_cc(_verdict_json(drift))):
            state.update(await grounding_node(state))
        assert state["council_restart"] == 2

        # round2: synth overwrites, ground sees drift but budget exhausted → converge
        state.update(await synthesize_node(state))
        assert state["council_pending_answer"]["content"] == "ROUND2"
        with patch("core.backends.claude_code.ClaudeCodeClient",
                   _mock_cc(_verdict_json(drift))):
            state.update(await grounding_node(state))

    assert len(writer.calls) == 1
    assert writer.calls[0]["content"] == "ROUND2"
    assistants = [m for m in state["messages"]
                  if isinstance(m, dict) and m.get("role") == "assistant"]
    assert assistants == [{"role": "assistant", "content": "ROUND2"}]
    assert state["council_pending_answer"] is None


async def test_final_state_messages_contains_only_final_synthesis(monkeypatch):
    """Composition-level E2E of the synthesize↔ground loop using the REAL
    routing helper (_route_after_ground), with messages merged via operator.add
    exactly like the compiled graph: grounding ON, one forced restart, mocked
    backends/verifier. The final accumulated messages must contain exactly ONE
    assistant turn == the round-1 (corrected) synthesis, NEVER the round-0
    drifted answer.

    (We drive the council sub-loop directly rather than ainvoke()-ing from START
    because the from-START path runs decompose/route/recall LLM nodes whose
    mocking is brittle and is what the manager's live E2E covers. This test pins
    the exact converge/restart topology the graph wires between synthesize and
    ground.)"""
    from core.graph import _route_after_ground
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    writer = _FakeWriter()

    state = {
        "task": "the topic",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "council_restart": 0,
        "council_cost_usd": 0.0,
        "council_handoff": {"active_debate": [], "next_task": ""},
        "messages": [{"role": "user", "content": "q"}],
        "panel_results": [{"idx": 0, "posture": "framer", "backend": "claude_api",
                           "text": "v", "round": 0}],
        "council_pending_answer": None,
    }

    synth_outputs = iter(["ROUND0 drifted", "ROUND1 corrected"])

    async def _synth(messages, backend):
        return next(synth_outputs)

    def _apply(delta):
        """Merge a node delta the way LangGraph would: messages via operator.add
        (extend), everything else last-write-wins."""
        for k, v in delta.items():
            if k == "messages":
                state["messages"] = (state.get("messages") or []) + list(v)
            else:
                state[k] = v

    routes: list[str] = []

    with patch("core.nodes.synthesize._send_to_backend", _synth), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer):
        # round 0: synthesize (defer) → ground (restart)
        _apply(await synthesize_node(state))
        with patch("core.backends.claude_code.ClaudeCodeClient",
                   _mock_cc(_verdict_json(_contradicted_majority()))):
            _apply(await grounding_node(state))
        routes.append(_route_after_ground(state))

        # round 1: synthesize (defer, overwrites) → ground (converge)
        _apply(await synthesize_node(state))
        with patch("core.backends.claude_code.ClaudeCodeClient",
                   _mock_cc(_verdict_json(_confirmed(1)))):
            _apply(await grounding_node(state))
        routes.append(_route_after_ground(state))

    # The loop ran exactly: restart → END.
    assert routes[0] == "panel_dispatch"
    from langgraph.graph import END
    assert routes[1] == END

    # Final committed state: exactly one assistant turn == the corrected round.
    assistants = [m for m in state["messages"]
                  if isinstance(m, dict) and m.get("role") == "assistant"]
    assert assistants == [{"role": "assistant", "content": "ROUND1 corrected"}]
    # And the drifted round never surfaced (stream OR persisted).
    assert len(writer.calls) == 1
    assert writer.calls[0]["content"] == "ROUND1 corrected"
    assert all("ROUND0" not in (m.get("content") or "") for m in state["messages"])


# ─── every converge reason emits exactly once ────────────────────────────────

@pytest.mark.parametrize("verdict_text,setup", [
    # parse-fail: unparseable verdict (fails open → converge)
    ("the model rambled, no JSON", {}),
    # below-threshold: 1 of 10 contradicted < 0.34 → converge
    (_verdict_json(
        [{"claim": f"c{i}", "status": "confirmed", "note": ""} for i in range(9)]
        + [{"claim": "bad", "status": "contradicted", "note": ""}]
    ), {}),
    # all-unverifiable → converge + flag
    (_verdict_json([{"claim": "u", "status": "unverifiable", "note": ""}]), {}),
    # all-confirmed → converge
    (_verdict_json([{"claim": "c", "status": "confirmed", "note": ""}]), {}),
    # budget-exhausted: drift but restart budget spent → converge
    (_verdict_json([
        {"claim": "a", "status": "contradicted", "note": ""},
        {"claim": "b", "status": "contradicted", "note": ""},
    ]), {"council_restart": COUNCIL_RESTART_BUDGET}),
])
async def test_grounding_each_converge_reason_emits_exactly_once(verdict_text, setup):
    """Every spawn-based converge reason commits the pending answer exactly once."""
    writer = _FakeWriter()
    st = _state(pending={"content": "the answer", "backend": "minimax"}, **setup)
    with patch("core.backends.claude_code.ClaudeCodeClient", _mock_cc(verdict_text)), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await grounding_node(st)

    assert "council_restart" not in out                  # all are converges
    assert out["messages"] == [{"role": "assistant", "content": "the answer"}]
    assert out["council_pending_answer"] is None
    assert len(writer.calls) == 1
    assert writer.calls[0]["content"] == "the answer"


async def test_grounding_backend_not_wired_converge_emits_once(monkeypatch):
    """backend-not-wired converge (no spawn) still commits the pending answer once."""
    monkeypatch.setattr("core.nodes.grounding.COUNCIL_GROUND_BACKEND", "gemini",
                        raising=False)
    writer = _FakeWriter()
    st = _state(pending={"content": "the answer", "backend": "minimax"})
    with patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await grounding_node(st)

    assert "council_restart" not in out
    assert out["messages"] == [{"role": "assistant", "content": "the answer"}]
    assert out["council_pending_answer"] is None
    assert len(writer.calls) == 1


async def test_grounding_spawn_failure_converge_emits_once():
    """A spawn exception fails open → converge, still commits the pending once."""
    class _Fail:
        def __init__(self, *a, **k):
            pass
        async def chat(self, *a, **k):
            raise RuntimeError("claude CLI timed out")

    writer = _FakeWriter()
    st = _state(pending={"content": "the answer", "backend": "minimax"})
    with patch("core.backends.claude_code.ClaudeCodeClient", _Fail), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await grounding_node(st)

    assert "council_restart" not in out
    assert out["messages"] == [{"role": "assistant", "content": "the answer"}]
    assert out["council_pending_answer"] is None
    assert len(writer.calls) == 1


async def test_grounding_no_answer_converge_emits_nothing():
    """No-answer defensive path (no pending, no assistant message) → converge,
    _converge commits nothing (documents the zero-emit defensive path)."""
    writer = _FakeWriter()
    st = _state(pending=None)  # messages holds only the user turn
    with patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await grounding_node(st)

    assert "council_restart" not in out
    assert "messages" not in out
    assert writer.calls == []


# ─── ceiling breach (Refinement 1: notice rides the error frame) ─────────────

async def test_grounding_ceiling_breach_with_pending_emits_answer_once():
    """Ceiling breach WITH a pending answer: the answer commits ONCE via the
    custom channel, the ceiling notice rides out['error'] (NOT messages), and
    council_pending_answer is cleared. The notice is NOT in messages (it now
    rides the error frame — server.py relays out['error'] as one error frame)."""
    from core.config import COUNCIL_MAX_USD

    writer = _FakeWriter()
    st = _state(pending={"content": "best answer so far", "backend": "minimax"},
                council_cost_usd=COUNCIL_MAX_USD + 1.0)

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("must not spawn after a ceiling breach")

    with patch("core.backends.claude_code.ClaudeCodeClient", _Boom), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await grounding_node(st)

    # The notice rides the error frame, exactly once, never in messages.
    assert out["error"]
    assert "ceiling" in out["error"].lower()
    # The best answer committed ONCE via the custom channel.
    assert out["messages"] == [{"role": "assistant", "content": "best answer so far"}]
    assert len(writer.calls) == 1
    assert writer.calls[0]["content"] == "best answer so far"
    # The notice did NOT ride the custom channel (no double-emit) nor messages.
    assert all(c["content"] != out["error"] for c in writer.calls)
    assert all(m["content"] != out["error"] for m in out["messages"])
    assert out["council_pending_answer"] is None
    assert "council_restart" not in out


async def test_grounding_ceiling_breach_no_pending_error_only():
    """Ceiling breach with NO pending answer: error set, no messages committed,
    writer never called (matches the Refinement-3 update to the existing test)."""
    from core.config import COUNCIL_MAX_USD

    writer = _FakeWriter()
    st = _state(pending=None, council_cost_usd=COUNCIL_MAX_USD + 1.0)
    with patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await grounding_node(st)

    assert out["error"] and "ceiling" in out["error"].lower()
    assert "messages" not in out
    assert writer.calls == []
    assert "council_restart" not in out


# ─── synth-failure turn: grounding commits nothing extra ─────────────────────

async def test_grounding_synth_failure_turn_commits_nothing_extra():
    """When synthesize hit the synth-failure path (no pending; error already in
    messages), grounding converges and commits NO new messages — the failure
    message stays exactly once."""
    writer = _FakeWriter()
    # Synth-failure left the error as the last assistant message; no pending.
    st = _state(
        pending=None,
        messages=[
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "Council synthesis failed: minimax timed out"},
        ],
    )
    with patch("core.backends.claude_code.ClaudeCodeClient",
               _mock_cc(_verdict_json(_confirmed(1)))), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=writer), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()):
        out = await grounding_node(st)

    assert "council_restart" not in out          # converge
    assert "messages" not in out                 # nothing re-committed
    assert writer.calls == []                     # no extra emit
    # The pre-existing failure message in state.messages stays exactly once
    # (grounding adds nothing to it).


# ─── overwrite-semantics guard (no operator.add reducer on the carrier) ──────

def test_council_pending_answer_has_no_reducer():
    """council_pending_answer must be last-write-wins (plain Optional[dict], no
    Annotated[..., operator.add] reducer) — else a restart round's value would
    ACCUMULATE with the drifted round's instead of cleanly superseding it. Guard
    against a future reducer regression by inspecting the AgentState annotation.

    An Annotated[...] reducer field exposes ``__metadata__`` (the reducer is the
    extra arg, e.g. panel_results below); a plain Optional[dict] does not. We pin
    that contrast so a future Annotated[..., operator.add] on the carrier fails."""
    import typing

    from core.graph import AgentState

    hints = typing.get_type_hints(AgentState, include_extras=True)
    pending_ann = hints["council_pending_answer"]
    # A reducer rides Annotated metadata (__metadata__). The carrier must NOT.
    assert not hasattr(pending_ann, "__metadata__"), (
        "council_pending_answer must NOT carry an Annotated reducer "
        "(it must overwrite last-write-wins so a restart supersedes the prior round)"
    )
    # Sanity contrast: panel_results IS a reducer field, so the check is meaningful.
    assert hasattr(hints["panel_results"], "__metadata__")


async def test_council_pending_answer_overwrites_last_write_wins(monkeypatch):
    """Two successive synthesize passes (grounding ON) write the carrier; the
    SECOND value must REPLACE the first (no accumulation), proving the field has
    overwrite semantics at the node-return level."""
    from core.nodes.synthesize import synthesize_node

    monkeypatch.setattr("core.config.COUNCIL_GROUND_CADENCE", "preclose", raising=False)
    base = {
        "task": "t",
        "council_spec": {"mode": "fusion", "synth_backend": "minimax"},
        "panel_results": [{"idx": 0, "posture": "framer", "backend": "claude_api",
                           "text": "v", "round": 0}],
        "messages": [],
    }
    outs = iter(["FIRST", "SECOND"])

    async def _synth(messages, backend):
        return next(outs)

    with patch("core.nodes.synthesize._send_to_backend", _synth), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=None):
        out1 = await synthesize_node(dict(base))
        out2 = await synthesize_node(dict(base))

    # Each return carries a SINGLE dict (not a growing list) — overwrite, not add.
    assert out1["council_pending_answer"] == {"content": "FIRST", "backend": "minimax"}
    assert out2["council_pending_answer"] == {"content": "SECOND", "backend": "minimax"}
    assert isinstance(out2["council_pending_answer"], dict)
