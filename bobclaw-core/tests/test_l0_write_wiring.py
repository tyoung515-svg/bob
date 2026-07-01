from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import config
from core.memory._db import init_schema
from core.memory._hashing import _compute_event_hash
from core.memory.event_log import SQLiteEventLog
from core.memory.exceptions import L0AppendFailed
from core.memory.models import Event
from core.nodes._l0_events import _append_agent_turn_event
from core.nodes.execute import execute_node
from core.nodes.join import join_node


# ── Helpers ────────────────────────────────────────────────────

def _state(**overrides) -> dict:
    base = {
        "messages": [{"role": "user", "content": "hello"}],
        "face_id": "assistant",
        "backend": "local",
        "task": "",
        "approval_response": "approved",
    }
    base.update(overrides)
    return base


def _fanout_state(**overrides) -> dict:
    base = {
        "messages": [{"role": "user", "content": "build the project"}],
        "face_id": "worker-kimi",
        "backend": "kimi_code",
        "task": "",
        "phase": "dispatch",
        "worker_results": [
            {"idx": 0, "status": "ok", "content": "subtask 1 done"},
            {"idx": 1, "status": "ok", "content": "subtask 2 done"},
        ],
    }
    base.update(overrides)
    return base


async def _empty_replay():
    """Async generator that yields nothing (empty event log)."""
    return
    yield  # pragma: no cover


def _mock_memory(event_log_append: AsyncMock | None = None):
    """Return a context manager that patches get_memory to return mock singletons."""
    m = MagicMock()
    m.event_log = MagicMock()
    m.event_log.atomic_append = event_log_append or AsyncMock(return_value=None)
    m.event_log.append = AsyncMock(return_value="evt-id")
    m.event_log.replay = _empty_replay
    singletons = MagicMock()
    singletons.event_log = m.event_log
    return patch("core.memory.bootstrap.get_memory", return_value=singletons)


# ── Fixtures ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_memory_enabled():
    original = config.MEMORY_ENABLED
    config.MEMORY_ENABLED = True
    yield
    config.MEMORY_ENABLED = original


# ═══════════════════════════════════════════════════════════════
# Node-level tests — verify _append_agent_turn_event is called
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_execute_writes_one_event_per_turn():
    """execute_node calls _append_agent_turn_event exactly once (single-worker)."""
    async def _stream_response(messages, backend, model_override=None):
        yield "test response"

    with patch("core.nodes.execute._stream_to_backend", _stream_response):
        with patch("core.nodes.execute._check_escalation_pin", AsyncMock(return_value=None)):
            with patch("core.nodes.execute._append_agent_turn_event", AsyncMock()) as mock_append:
                state = _state()
                result = await execute_node(state)
                mock_append.assert_awaited_once()
                _, kwargs = mock_append.await_args
                assert kwargs["assistant_response"] == "test response"
                assert kwargs.get("error_msg") is None


@pytest.mark.asyncio
async def test_join_writes_one_event_per_turn():
    """join_node calls _append_agent_turn_event exactly once (fan-out final wave)."""
    with patch("core.nodes.join._append_agent_turn_event", AsyncMock()) as mock_append:
        state = _fanout_state()
        result = await join_node(state)
        mock_append.assert_awaited_once()
        _, kwargs = mock_append.await_args
        assert "subtask 1" in kwargs["assistant_response"]
        # All workers succeeded → error_msg should be None
        assert kwargs.get("error_msg") is None


@pytest.mark.asyncio
async def test_execute_writes_event_on_error_fallback():
    """execute_node writes event with error_msg when backend raises."""
    async def _stream_boom(messages, backend, model_override=None):
        raise RuntimeError("backend failure")
        yield  # pragma: no cover — makes this an async generator

    with patch("core.nodes.execute._stream_to_backend", _stream_boom):
        with patch("core.nodes.execute._check_escalation_pin", AsyncMock(return_value=None)):
            with patch("core.nodes.execute._append_agent_turn_event", AsyncMock()) as mock_append:
                state = _state()
                result = await execute_node(state)
                mock_append.assert_awaited_once()
                _, kwargs = mock_append.await_args
                assert "Execution error" in kwargs["assistant_response"]
                assert kwargs["error_msg"] is not None


@pytest.mark.asyncio
async def test_no_double_write_for_fanout_turn():
    """A turn that goes through fan-out (join_node) does NOT also write at
    execute_node — the graph topology makes this mutually exclusive."""
    with patch("core.nodes.join._append_agent_turn_event", AsyncMock()) as mock_join:
        with patch("core.nodes.execute._append_agent_turn_event", AsyncMock()) as mock_exec:
            state = _fanout_state()
            result = await join_node(state)
            mock_join.assert_awaited_once()
            mock_exec.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# Helper-level tests — _append_agent_turn_event behavior
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_memory_disabled_no_writes():
    """MEMORY_ENABLED=false → event_log.append is NEVER called."""
    config.MEMORY_ENABLED = False
    mock_append = AsyncMock()
    with _mock_memory(event_log_append=mock_append):
        await _append_agent_turn_event(_state(), assistant_response="hello")
        mock_append.assert_not_called()


@pytest.mark.asyncio
async def test_event_body_shape():
    """Event body has all required fields; model_capability_class is a slot name."""
    mock_append = AsyncMock()
    with _mock_memory(event_log_append=mock_append):
        state = _state(
            messages=[{"role": "user", "content": "what is bob"}],
            face_id="assistant",
            cost_usd=0.05,
            duration_ms=1500,
            model_capability_class="chat_general",
        )
        await _append_agent_turn_event(
            state, assistant_response="bob is a builder",
        )
        mock_append.assert_called_once()
        body: dict = mock_append.call_args[0][0]
        assert body["user_message"] == "what is bob"
        assert body["assistant_response"] == "bob is a builder"
        assert body["face_id"] == "assistant"
        assert isinstance(body["turn_id"], str) and len(body["turn_id"]) > 0
        assert body["cost_usd"] == 0.05
        assert body["duration_ms"] == 1500
        assert body["model_capability_class"] == "chat_general"
        assert body["error"] is None
        # model_capability_class must NOT be a model name
        assert body["model_capability_class"] not in (
            "claude", "gpt", "kimi", "deepseek", "gemini", "llama", "qwen",
        )
        assert "agent_turn" not in body["model_capability_class"]  # not default


@pytest.mark.asyncio
async def test_event_body_shape_defaults_to_none():
    """Optional fields default to None when absent from state."""
    mock_append = AsyncMock()
    with _mock_memory(event_log_append=mock_append):
        state = _state(messages=[{"role": "user", "content": "hi"}])
        await _append_agent_turn_event(state, assistant_response="hey")
        body: dict = mock_append.call_args[0][0]
        assert body["cost_usd"] is None
        assert body["duration_ms"] is None
        assert body["model_capability_class"] is None
        assert body["error"] is None


@pytest.mark.asyncio
async def test_append_failure_propagates():
    """event_log.append raises → _append_agent_turn_event propagates (not swallowed)."""
    failing_append = AsyncMock(side_effect=L0AppendFailed("evt-1", "simulated failure"))
    with _mock_memory(event_log_append=failing_append):
        with pytest.raises(L0AppendFailed) as exc:
            await _append_agent_turn_event(
                _state(messages=[{"role": "user", "content": "hi"}]),
                assistant_response="hello",
            )
        assert "simulated failure" in str(exc.value) or "evt-1" in str(exc.value)


@pytest.mark.asyncio
async def test_event_hash_chain_intact_across_turns(tmp_path):
    """Three sequential turns produce a valid hash chain."""
    db_path = tmp_path / "hash_chain.db"
    await init_schema(db_path)
    event_log = SQLiteEventLog(db_path)

    prev_hash: str | None = None
    event_ids: list[str] = []

    for i in range(3):
        body = {
            "user_message": f"msg {i}",
            "assistant_response": f"resp {i}",
            "face_id": "assistant",
            "turn_id": f"turn-{i}",
            "cost_usd": None,
            "duration_ms": None,
            "model_capability_class": None,
            "error": None,
        }
        h = _compute_event_hash(body, prev_hash)
        event = Event(
            event_id=f"evt-{i}",
            kind="agent_turn",
            body=body,
            ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            hash=h,
            prev_hash=prev_hash,
        )
        await event_log.append(event)
        event_ids.append(event.event_id)
        prev_hash = h

    # Replay and verify the chain
    events = [e async for e in event_log.replay()]
    assert len(events) == 3
    assert events[0].prev_hash is None
    assert events[1].prev_hash == events[0].hash
    assert events[2].prev_hash == events[1].hash

    # Verify hashes are correct
    for i, ev in enumerate(events):
        expected_body = {
            "user_message": f"msg {i}",
            "assistant_response": f"resp {i}",
            "face_id": "assistant",
            "turn_id": f"turn-{i}",
            "cost_usd": None,
            "duration_ms": None,
            "model_capability_class": None,
            "error": None,
        }
        expected_hash = _compute_event_hash(expected_body, ev.prev_hash)
        assert ev.hash == expected_hash, f"hash mismatch at event {i}"
