"""
BoBClaw Core — MS9-W1 live council theater wiring (emit_events opt-in).

Proves the ADDITIVE, default-OFF path that closes finding #1 (the U7 council_event
frames never reached the app): a request that opts into ``emit_events`` results in
``council_spec["emit_events"] == True`` AND the SSE relay forwarding the council
theater frames; a request that does NOT opt in is byte-identical to today.

Seam-1 (core) coverage:
  * ``_build_council_spec`` / ``_build_council_spec_from_profile`` stamp the gate ONLY
    when opted in — the spec is byte-identical (no ``emit_events`` key) when absent.
  * ``route_node`` (council-max face AND profile-driven council) propagates the flag.
  * ``/api/chat`` threads ``emit_events`` from the request into the initial AgentState
    and into ``_stream_graph_turn``'s ``emit_council_events`` (strict JSON ``true``).
  * the SSE relay forwards ``council_event`` / ``council_seat`` / ``council_synth``
    custom frames ONLY when opted in — with opt-in absent, a council turn's custom
    council frames are dropped exactly as before (byte-identical), while the token
    chunk path is untouched in both cases.
"""
from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from api import server as server_mod
from api.server import GRAPH_KEY, _stream_graph_turn, build_app
from core.config import COUNCIL_DEFAULT_SEATS
from core.nodes.route import (
    _build_council_spec,
    _build_council_spec_from_profile,
    route_node,
)


# ─── seam 1a: spec builders (byte-identical when off) ─────────────────────────

def test_build_council_spec_no_emit_events_is_byte_identical():
    spec = _build_council_spec({"face_id": "council-max", "task": "x"})
    assert "emit_events" not in spec  # absent ⇒ OFF ⇒ byte-identical
    assert spec == {
        "mode": "fusion",
        "seats": list(COUNCIL_DEFAULT_SEATS),
        "synth_backend": spec["synth_backend"],
    }


def test_build_council_spec_opts_in_when_flag_set():
    spec = _build_council_spec({"face_id": "council-max", "task": "x", "emit_events": True})
    assert spec["emit_events"] is True


def test_build_council_spec_falsy_flag_stays_off():
    for falsy in (False, None, 0, ""):
        spec = _build_council_spec({"task": "x", "emit_events": falsy})
        assert "emit_events" not in spec


def test_build_council_spec_from_profile_default_off_byte_identical():
    prof = {"shape": "fusion", "seats": [{"posture": "framer"}], "synth_backend": "minimax"}
    spec = _build_council_spec_from_profile(prof)
    assert "emit_events" not in spec


def test_build_council_spec_from_profile_opts_in():
    prof = {"shape": "fusion", "seats": [{"posture": "framer"}], "synth_backend": "minimax"}
    spec = _build_council_spec_from_profile(prof, emit_events=True)
    assert spec["emit_events"] is True


# ─── seam 1b: route_node propagation ──────────────────────────────────────────

async def test_route_node_council_max_threads_emit_events():
    on = await route_node({"face_id": "council-max", "task": "x", "emit_events": True})
    assert on["council_spec"]["emit_events"] is True
    off = await route_node({"face_id": "council-max", "task": "x"})
    assert "emit_events" not in off["council_spec"]  # byte-identical spec


async def test_route_node_profile_council_threads_emit_events(tmp_path):
    from core import teams

    teams.set_custom_teams_dir(tmp_path)
    try:
        teams.create_profile("council-fast", {
            "seats": [{"posture": "framer"}, {"posture": "stress", "backend": "gemini_flash"}],
            "shape": "fusion",
            "synth_backend": "minimax",
        })
        on = await route_node({"face_id": "assistant", "task": "x",
                               "profile_name": "council-fast", "emit_events": True})
        assert on["council_spec"]["emit_events"] is True
        off = await route_node({"face_id": "assistant", "task": "x",
                                "profile_name": "council-fast"})
        assert "emit_events" not in off["council_spec"]
    finally:
        teams.set_custom_teams_dir(None)


# ─── seam 1c: /api/chat threads emit_events into state + relay ────────────────

class _DummyGraph:
    """Non-None graph so the handler passes its graph-availability guard; never run
    (``_stream_graph_turn`` is monkeypatched to capture what the handler forwards)."""


async def _chat_capture(monkeypatch, body: dict) -> dict:
    captured: dict = {}

    async def _fake_stream(**kwargs):
        captured.update(kwargs)
        from aiohttp import web
        return web.Response(text="ok")

    monkeypatch.setattr(server_mod, "_stream_graph_turn", _fake_stream)
    app = build_app(graph=_DummyGraph())
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat", json=body)
    return captured


async def test_chat_forwards_emit_events_true(monkeypatch):
    cap = await _chat_capture(
        monkeypatch,
        {"conversation_id": "c1", "content": "hi", "face_id": "council-max", "emit_events": True},
    )
    assert cap["graph_input"]["emit_events"] is True
    assert cap["emit_council_events"] is True


async def test_chat_emit_events_absent_is_off(monkeypatch):
    cap = await _chat_capture(
        monkeypatch, {"conversation_id": "c1", "content": "hi", "face_id": "council-max"}
    )
    assert cap["graph_input"]["emit_events"] is False
    assert cap["emit_council_events"] is False


async def test_chat_emit_events_non_true_is_off(monkeypatch):
    # Strict JSON `true` only — a truthy string must NOT flip it (mirrors `hierarchical`).
    cap = await _chat_capture(
        monkeypatch,
        {"conversation_id": "c1", "content": "hi", "emit_events": "true"},
    )
    assert cap["graph_input"]["emit_events"] is False
    assert cap["emit_council_events"] is False


# ─── seam 1d: SSE relay forwards council frames only when opted in ────────────

class _FakeResponse:
    def __init__(self):
        self.chunks: list[bytes] = []

    async def prepare(self, request):
        pass

    async def write(self, data: bytes):
        self.chunks.append(data)

    async def write_eof(self):
        pass

    def events(self) -> list[dict]:
        out: list[dict] = []
        for block in b"".join(self.chunks).decode("utf-8").split("\n\n"):
            block = block.strip()
            if block.startswith("data:"):
                try:
                    out.append(json.loads(block[5:].strip()))
                except json.JSONDecodeError:
                    pass
        return out


class _FakeGraph:
    def __init__(self, items):
        self._items = items

    async def astream(self, graph_input, config, stream_mode=None):
        for item in self._items:
            yield item


async def _run_relay(monkeypatch, items, *, emit_council_events: bool) -> _FakeResponse:
    resp = _FakeResponse()
    monkeypatch.setattr(server_mod.web, "StreamResponse", lambda *a, **k: resp)
    await _stream_graph_turn(
        request=object(),
        graph=_FakeGraph(items),
        graph_input={},
        thread_id="t1",
        approvals={},
        face_id="council-max",
        user_content="q",
        model_override=None,
        backend_override=None,
        emit_council_events=emit_council_events,
    )
    return resp


_COUNCIL_ITEMS = [
    ("custom", {"type": "token", "content": "answer"}),
    ("custom", {"type": "council_event", "phase": "panel_start", "round": 0,
                "seats": ["framer", "stress"], "flight_id": "f", "ts": "t"}),
    ("custom", {"type": "council_seat", "idx": 0, "posture": "framer",
                "backend": "claude_api", "round": 0, "status": "ok", "tokens": 12}),
    ("custom", {"type": "council_synth", "backend": "minimax", "status": "ok"}),
]


async def test_relay_forwards_council_frames_when_opted_in(monkeypatch):
    resp = await _run_relay(monkeypatch, _COUNCIL_ITEMS, emit_council_events=True)
    types = [e.get("type") for e in resp.events()]
    assert "council_event" in types
    assert "council_seat" in types
    assert "council_synth" in types
    # The token chunk is still forwarded verbatim (unchanged), plus message_complete.
    assert "chunk" in types
    # Council frames pass through VERBATIM (flat top-level fields preserved).
    ce = next(e for e in resp.events() if e.get("type") == "council_event")
    assert ce["phase"] == "panel_start" and ce["seats"] == ["framer", "stress"]


async def test_relay_drops_council_frames_when_off_byte_identical(monkeypatch):
    """Opt-in absent ⇒ a council turn's council_* custom frames are dropped exactly as
    before; only the token chunk + message_complete reach the client (byte-identical)."""
    resp = await _run_relay(monkeypatch, _COUNCIL_ITEMS, emit_council_events=False)
    types = [e.get("type") for e in resp.events()]
    assert "council_event" not in types
    assert "council_seat" not in types
    assert "council_synth" not in types
    # The pre-existing token path is untouched.
    chunks = [e for e in resp.events() if e.get("type") == "chunk"]
    assert len(chunks) == 1 and chunks[0]["content"] == "answer"
