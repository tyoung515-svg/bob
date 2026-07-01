"""
BoBClaw Core — SSE relay per-node suppression for the `ground` node (P2).

EDIT 9 added "ground" to the per-node ``messages`` suppression tuple in
``api.server._stream_graph_turn``: grounding_node now commits its converged
answer via the "custom" channel (emit_synthesis's message-level token chunk),
AND returns it in ``messages`` for checkpoint state. Without suppression the
``messages`` entry would ALSO be relayed off the "updates" channel → the answer
double-emits (the inverse 4f7d8f4 streaming-drop class).

This test drives ``_stream_graph_turn`` directly with a fake graph whose
``astream`` yields controlled (mode, chunk) tuples, and a capturing fake
StreamResponse, so it asserts the relay behavior without standing up a full
council graph turn:
  * a "custom" token chunk from the ground node → exactly ONE chunk SSE frame.
  * an "updates" chunk {'ground': {'messages':[assistant]}} → NO chunk frame
    (suppressed), mirroring execute/synthesize/council.
  * a non-suppressed node's "updates" messages → DOES relay (control).
  * an "updates" {'ground': {'error': ...}} → exactly one error SSE frame
    (the ceiling notice path, Refinement 1).
"""
from __future__ import annotations

import json

import pytest

from api import server as server_mod
from api.server import _stream_graph_turn


# ─── fakes ───────────────────────────────────────────────────────────────────

class _FakeResponse:
    """Capture every byte written; never raise (no disconnect)."""
    def __init__(self):
        self.chunks: list[bytes] = []
        self.prepared = False
        self.eof = False

    async def prepare(self, request):
        self.prepared = True

    async def write(self, data: bytes):
        self.chunks.append(data)

    async def write_eof(self):
        self.eof = True

    def events(self) -> list[dict]:
        events: list[dict] = []
        body = b"".join(self.chunks).decode("utf-8")
        for block in body.split("\n\n"):
            block = block.strip()
            if not block.startswith("data:"):
                continue
            try:
                events.append(json.loads(block[5:].strip()))
            except json.JSONDecodeError:
                continue
        return events


class _FakeGraph:
    """A graph whose astream yields a fixed list of (mode, chunk) tuples."""
    def __init__(self, items):
        self._items = items

    async def astream(self, graph_input, config, stream_mode=None):
        for item in self._items:
            yield item


class _FakeRequest:
    pass


async def _run_relay(monkeypatch, items) -> _FakeResponse:
    """Drive _stream_graph_turn with a fake graph + capturing response."""
    resp = _FakeResponse()

    # Replace StreamResponse construction with our capturing fake.
    monkeypatch.setattr(server_mod.web, "StreamResponse", lambda *a, **k: resp)

    await _stream_graph_turn(
        request=_FakeRequest(),
        graph=_FakeGraph(items),
        graph_input={},
        thread_id="t1",
        approvals={},
        face_id="council-max",
        user_content="the question",
        model_override=None,
        backend_override=None,
    )
    return resp


# ─── tests ───────────────────────────────────────────────────────────────────

async def test_server_ground_custom_token_emits_one_chunk(monkeypatch):
    """A ground-node 'custom' token chunk relays as exactly one chunk frame."""
    items = [
        ("custom", {"type": "token", "content": "the grounded answer",
                    "backend": "minimax", "model": None}),
    ]
    resp = await _run_relay(monkeypatch, items)
    chunks = [e for e in resp.events() if e.get("type") == "chunk"]
    assert len(chunks) == 1
    assert chunks[0]["content"] == "the grounded answer"


async def test_server_suppresses_ground_messages_on_updates_channel(monkeypatch):
    """A ground-node 'updates' {messages:[assistant]} chunk is SUPPRESSED
    (no chunk frame) — the answer already rode the 'custom' channel."""
    items = [
        ("updates", {"ground": {"messages": [
            {"role": "assistant", "content": "the grounded answer"}
        ]}}),
    ]
    resp = await _run_relay(monkeypatch, items)
    chunks = [e for e in resp.events() if e.get("type") == "chunk"]
    assert chunks == []  # suppressed, no double-emit


async def test_server_ground_custom_then_updates_emits_exactly_once(monkeypatch):
    """The production shape: ground emits BOTH a custom token AND a messages
    entry for checkpoint state → the client sees the answer EXACTLY once."""
    items = [
        ("custom", {"type": "token", "content": "answer X",
                    "backend": "minimax", "model": None}),
        ("updates", {"ground": {"messages": [
            {"role": "assistant", "content": "answer X"}
        ]}}),
    ]
    resp = await _run_relay(monkeypatch, items)
    chunks = [e for e in resp.events() if e.get("type") == "chunk"]
    assert len(chunks) == 1
    assert chunks[0]["content"] == "answer X"


async def test_server_non_suppressed_node_messages_still_relay(monkeypatch):
    """Control: a node NOT in the suppression tuple still relays its messages
    off 'updates' (proves the suppression is node-scoped, not blanket)."""
    items = [
        ("updates", {"dispatch": {"messages": [
            {"role": "assistant", "content": "fanout summary"}
        ]}}),
    ]
    resp = await _run_relay(monkeypatch, items)
    chunks = [e for e in resp.events() if e.get("type") == "chunk"]
    assert len(chunks) == 1
    assert chunks[0]["content"] == "fanout summary"


async def test_server_suppresses_debate_converge_messages(monkeypatch):
    """The debate close gate (debate_converge) commits the same way as ground, so
    its 'updates' messages must be suppressed too (else the converged debate answer
    double-emits). Pins the tuple entry against a future refactor."""
    items = [
        ("updates", {"debate_converge": {"messages": [
            {"role": "assistant", "content": "the debated answer"}
        ]}}),
    ]
    resp = await _run_relay(monkeypatch, items)
    chunks = [e for e in resp.events() if e.get("type") == "chunk"]
    assert chunks == []  # suppressed, no double-emit


async def test_server_debate_converge_custom_then_updates_emits_once(monkeypatch):
    """Production shape: debate_converge emits a custom token AND a messages entry →
    the client sees the converged debate answer EXACTLY once."""
    items = [
        ("custom", {"type": "token", "content": "debated X",
                    "backend": "minimax", "model": None}),
        ("updates", {"debate_converge": {"messages": [
            {"role": "assistant", "content": "debated X"}
        ]}}),
    ]
    resp = await _run_relay(monkeypatch, items)
    chunks = [e for e in resp.events() if e.get("type") == "chunk"]
    assert len(chunks) == 1
    assert chunks[0]["content"] == "debated X"


async def test_server_ground_error_emits_one_error_frame(monkeypatch):
    """The ceiling-notice path (Refinement 1): ground returns out['error'] →
    the relay emits exactly one error SSE frame (not a chunk)."""
    items = [
        ("updates", {"ground": {"error": "Council cost ceiling reached ..."}}),
    ]
    resp = await _run_relay(monkeypatch, items)
    errors = [e for e in resp.events() if e.get("type") == "error"]
    chunks = [e for e in resp.events() if e.get("type") == "chunk"]
    assert len(errors) == 1
    assert "ceiling" in errors[0]["message"].lower()
    assert chunks == []
