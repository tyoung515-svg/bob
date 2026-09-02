"""Flight substrate L0.2 — emit layer (frame shape + fail-safe forks + node wiring).

Proves:
  * ``build_frame`` is FLAT and reserved keys (type/flight_id/ts) win over payload.
  * ``emit_event`` forks to BOTH Redis pub/sub (``bobclaw:monitor``) and the in-turn
    stream writer, and is FAIL-SAFE (a raising Redis / no stream context never raises).
  * ``emit_event_sync`` schedules the Redis fork on a running loop + does the stream fork.
  * ``worker_node`` emits ``worker_state`` TWICE per worker (running → terminal) with the
    right flight + status — the mid-run "watch the fleet" signal.

Sockets are disabled (pytest addopts); the fakes never touch the network.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from core.telemetry import emit as emit_mod
from core.telemetry.emit import (
    KIND_WORKER_STATE,
    MONITOR_CHANNEL,
    build_frame,
    emit_event,
    emit_event_sync,
)


class _FakeRedis:
    def __init__(self):
        self.published = []

    async def publish(self, channel, data):
        self.published.append((channel, data))


class _RaisingRedis:
    async def publish(self, channel, data):
        raise RuntimeError("redis down")


def _fake_send(text):
    async def _s(messages, backend, *a, **k):
        return text
    return _s


# ── build_frame ───────────────────────────────────────────────────────────────
def test_build_frame_flat_and_reserved_win():
    f = build_frame("worker_state", "ms-5", {"idx": 3, "type": "NOPE", "ts": "NOPE"}, ts="T")
    assert f["type"] == "worker_state"     # reserved wins over payload
    assert f["ts"] == "T"
    assert f["flight_id"] == "ms-5"
    assert f["idx"] == 3


def test_build_frame_none_payload():
    assert build_frame("cost", None, None, ts="T") == {
        "type": "cost", "flight_id": None, "ts": "T",
    }


# ── emit_event (async) ────────────────────────────────────────────────────────
async def test_emit_event_forks_redis_and_stream(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(emit_mod, "_get_redis", lambda: fake)
    written = []
    monkeypatch.setattr("core.nodes.execute._get_stream_writer", lambda: written.append)

    frame = await emit_event(KIND_WORKER_STATE, "ms-5", {"idx": 1, "status": "running"}, ts="T")

    assert frame["idx"] == 1
    assert len(fake.published) == 1
    ch, data = fake.published[0]
    assert ch == MONITOR_CHANNEL
    assert json.loads(data)["status"] == "running"
    assert written and written[0]["type"] == "worker_state"
    assert written[0]["flight_id"] == "ms-5"


async def test_emit_event_failsafe_on_redis_error(monkeypatch):
    monkeypatch.setattr(emit_mod, "_get_redis", lambda: _RaisingRedis())
    monkeypatch.setattr("core.nodes.execute._get_stream_writer", lambda: None)
    # Must NOT raise — telemetry never breaks the turn.
    frame = await emit_event(KIND_WORKER_STATE, "ms-5", {"idx": 1}, ts="T")
    assert frame["flight_id"] == "ms-5"


async def test_emit_event_failsafe_on_stream_error(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(emit_mod, "_get_redis", lambda: fake)

    def _boom():
        raise RuntimeError("writer broke")

    monkeypatch.setattr("core.nodes.execute._get_stream_writer", _boom)
    frame = await emit_event(KIND_WORKER_STATE, "ms-5", {"idx": 1}, ts="T")
    # stream fork swallowed the error; redis fork still happened
    assert frame["flight_id"] == "ms-5"
    assert len(fake.published) == 1


# ── emit_event_sync ───────────────────────────────────────────────────────────
async def test_emit_event_sync_schedules_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(emit_mod, "_get_redis", lambda: fake)
    monkeypatch.setattr("core.nodes.execute._get_stream_writer", lambda: None)

    emit_event_sync("fleet_start", "ms-5", {"n_workers": 3}, ts="T")
    # scheduled as a task on the running loop — let it run
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(fake.published) == 1
    assert json.loads(fake.published[0][1])["n_workers"] == 3


def test_emit_event_sync_no_loop_skips_redis(monkeypatch):
    # No running loop (plain sync call, like dispatch_node in a LangGraph executor
    # thread): the redis fork is skipped, the stream fork still runs, and it never raises.
    fake = _FakeRedis()
    monkeypatch.setattr(emit_mod, "_get_redis", lambda: fake)
    written = []
    monkeypatch.setattr("core.nodes.execute._get_stream_writer", lambda: written.append)

    frame = emit_event_sync("fleet_start", "ms-5", {"n_workers": 3}, ts="T")

    assert frame["n_workers"] == 3
    assert written and written[0]["type"] == "fleet_start"
    assert fake.published == []   # no loop ⇒ no redis fork


# ── worker_node wiring (running → terminal) ───────────────────────────────────
async def test_worker_node_emits_running_then_terminal(monkeypatch):
    calls = []

    async def _capture(kind, flight_id, payload=None, **kw):
        calls.append((kind, flight_id, dict(payload or {})))
        return {}

    monkeypatch.setattr("core.nodes.worker.emit_event", _capture)
    with patch("core.nodes.worker._send_to_backend", _fake_send("done")):
        res = await worker_node_import()(
            {"task": "hi", "backend": "deepseek_v4_flash", "subtask_idx": 2,
             "flight_id": "ms-5", "face_id": "assistant", "messages": []}
        )

    assert res["worker_results"][0]["status"] == "ok"
    kinds = [c[0] for c in calls]
    assert kinds == [KIND_WORKER_STATE, KIND_WORKER_STATE]
    assert calls[0][1] == "ms-5" and calls[0][2]["status"] == "running"
    assert calls[0][2]["idx"] == 2 and calls[0][2]["role"] == "assistant"
    assert calls[1][2]["status"] == "ok"


async def test_worker_node_ambient_flight_when_unset(monkeypatch):
    calls = []

    async def _capture(kind, flight_id, payload=None, **kw):
        calls.append(flight_id)
        return {}

    monkeypatch.setattr("core.nodes.worker.emit_event", _capture)
    with patch("core.nodes.worker._send_to_backend", _fake_send("done")):
        await worker_node_import()(
            {"task": "hi", "backend": "deepseek_v4_flash", "subtask_idx": 0, "messages": []}
        )
    # No flight + no conversation_id in the Send sub-state ⇒ ambient.
    assert calls == ["ambient", "ambient"]


# ── tokens on terminal worker_state frames (MS6-G0(b)) ────────────────────────
# The monitor sums a per-worker token tick (post-hoc measure_spend; there is no
# per-backend USD table). Contract: EVERY terminal worker frame (chat/research AND
# build fan-outs) + every council seat carries an int `tokens`; `running` frames don't.
async def test_chat_worker_terminal_frame_carries_tokens(monkeypatch):
    """The chat/research worker: running frame is token-less; terminal carries int tokens."""
    calls = []

    async def _capture(kind, flight_id, payload=None, **kw):
        calls.append((kind, flight_id, dict(payload or {})))
        return {}

    monkeypatch.setattr("core.nodes.worker.emit_event", _capture)
    with patch("core.nodes.worker._send_to_backend", _fake_send("a real response body")):
        await worker_node_import()(
            {"task": "some task", "backend": "deepseek_v4_flash", "subtask_idx": 0,
             "flight_id": "ms-6", "messages": []}
        )

    running, terminal = calls[0][2], calls[1][2]
    # running frame: no output yet ⇒ no token field.
    assert "tokens" not in running or running["tokens"] is None
    # terminal frame: an int token estimate of the real request+response I/O.
    assert isinstance(terminal["tokens"], int) and terminal["tokens"] > 0


async def test_build_worker_terminal_frame_carries_tokens(monkeypatch):
    """The build fan-out terminal frame now carries `tokens` too (the G0(b) fix)."""
    calls = []

    async def _capture(kind, flight_id, payload=None, **kw):
        calls.append((kind, flight_id, dict(payload or {})))
        return {}

    contract = {"name": "add", "signature": "add(a, b)", "doc": "sum a and b",
                "cases": [{"args": [1, 2], "expect": 3}]}
    monkeypatch.setattr("core.nodes.worker.emit_event", _capture)
    with patch("core.nodes.worker._send_to_backend",
               _fake_send("def add(a, b):\n    return a + b")):
        res = await worker_node_import()(
            {"build_contract": contract, "backend": "deepseek_v4_flash",
             "subtask_idx": 1, "flight_id": "ms-6"}
        )

    assert res["build_impls"][0]["status"] == "ok"
    kinds = [c[0] for c in calls]
    assert kinds == [KIND_WORKER_STATE, KIND_WORKER_STATE]  # running → terminal
    running, terminal = calls[0][2], calls[1][2]
    assert "tokens" not in running or running["tokens"] is None
    assert isinstance(terminal["tokens"], int) and terminal["tokens"] > 0
    assert terminal["status"] == "ok" and terminal["name"] == "add"


async def test_council_seat_frame_carries_tokens(monkeypatch):
    """A council seat frame carries a per-seat token estimate (panel.py, already true)."""
    from core.telemetry.emit import KIND_COUNCIL_SEAT

    calls = []

    async def _capture(kind, flight_id, payload=None, **kw):
        calls.append((kind, dict(payload or {})))
        return {}

    monkeypatch.setattr("core.nodes.panel.emit_event", _capture)
    with patch("core.nodes.panel._send_to_backend", _fake_send("a framer voice")):
        from core.nodes.panel import panel_worker_node
        await panel_worker_node(
            {"seat_posture": "framer", "backend": "deepseek_v4_flash",
             "fallback_chain": [], "task": "shared prompt", "seat_idx": 0,
             "flight_id": "ms-6", "messages": []}
        )

    seat_frames = [p for k, p in calls if k == KIND_COUNCIL_SEAT]
    assert seat_frames, "expected a council_seat frame"
    assert isinstance(seat_frames[0]["tokens"], int) and seat_frames[0]["tokens"] > 0


def worker_node_import():
    # Imported lazily so monkeypatch of core.nodes.worker.emit_event is in place first.
    from core.nodes.worker import worker_node
    return worker_node
