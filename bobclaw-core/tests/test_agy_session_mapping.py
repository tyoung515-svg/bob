"""execute_node wiring for the agy_code backend: uuid capture/resume sidecar +
throttle→gemini_pro fall-through + stateless dispatch."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.agy_sessions import _lookup_agy_session
from core.backends.agy_code import AgyThrottled
from core.config import config
from core.nodes import execute as execute_module


class _FakeAgyClient:
    def __init__(self, *, text: str = "", uuid: str = "", throttle: bool = False) -> None:
        self.last_session_id = uuid
        if throttle:
            self.chat = AsyncMock(side_effect=AgyThrottled("quota exceeded"))
        else:
            self.chat = AsyncMock(return_value={"text": text, "session_id": uuid})


@pytest.fixture
def agy_session_db(tmp_path: Path):
    original = config.MEMORY_SQLITE_PATH
    config.MEMORY_SQLITE_PATH = str(tmp_path / "agy_sessions.db")
    try:
        yield Path(config.MEMORY_SQLITE_PATH)
    finally:
        config.MEMORY_SQLITE_PATH = original


def _state(conversation_id: str, task: str = "plan it") -> dict:
    return {
        "task": task,
        "conversation_id": conversation_id,
        "backend": "agy_code",
        "messages": [],
        "agy_posture": {"model": "gemini-3.1-pro"},
        "escalation_backend": "gemini_pro",
    }


async def _run_agy_turn(client: _FakeAgyClient, state: dict) -> dict:
    with patch("core.backends.agy_code.AntigravityClient", return_value=client), patch.object(
        execute_module, "_append_agent_turn_event", AsyncMock()
    ):
        return await execute_module.execute_node(state)


@pytest.mark.asyncio
async def test_first_turn_records_uuid_without_resume(agy_session_db):
    client = _FakeAgyClient(text="first plan", uuid="uuid-1")
    result = await _run_agy_turn(client, _state("conv-1"))
    _, kwargs = client.chat.await_args
    assert kwargs["resume_session_id"] is None
    assert result["agy_resume_session_id"] == "uuid-1"
    assert result["messages"][0]["content"] == "first plan"
    assert await _lookup_agy_session("conv-1") == "uuid-1"


@pytest.mark.asyncio
async def test_second_turn_resumes_stored_uuid(agy_session_db):
    first = _FakeAgyClient(text="a", uuid="uuid-1")
    second = _FakeAgyClient(text="b", uuid="uuid-2")
    await _run_agy_turn(first, _state("conv-1"))
    result = await _run_agy_turn(second, _state("conv-1", task="continue"))
    _, kwargs = second.chat.await_args
    assert kwargs["resume_session_id"] == "uuid-1"
    assert result["agy_resume_session_id"] == "uuid-2"
    assert await _lookup_agy_session("conv-1") == "uuid-2"


@pytest.mark.asyncio
async def test_distinct_conversations_keep_distinct_uuids(agy_session_db):
    await _run_agy_turn(_FakeAgyClient(text="a", uuid="uuid-a"), _state("conv-a"))
    await _run_agy_turn(_FakeAgyClient(text="b", uuid="uuid-b"), _state("conv-b"))
    assert await _lookup_agy_session("conv-a") == "uuid-a"
    assert await _lookup_agy_session("conv-b") == "uuid-b"


@pytest.mark.asyncio
async def test_throttle_falls_through_to_gemini_pro(agy_session_db):
    """AgyThrottled retargets to escalation_backend (gemini_pro) + the generic path."""
    client = _FakeAgyClient(throttle=True)
    captured = {}

    async def _fake_stream(messages, effective_backend, model_override, writer):
        captured["backend"] = effective_backend
        return "metered fallback"

    with patch("core.backends.agy_code.AntigravityClient", return_value=client), patch.object(
        execute_module, "_append_agent_turn_event", AsyncMock()
    ), patch.object(execute_module, "_check_escalation_pin", AsyncMock(return_value=None)), patch.object(
        execute_module, "_stream_and_collect", _fake_stream
    ):
        result = await execute_module.execute_node(_state("conv-x"))
    assert captured["backend"] == "gemini_pro"
    assert result["messages"][0]["content"] == "metered fallback"


@pytest.mark.asyncio
async def test_stateless_send_routes_agy_code():
    client = _FakeAgyClient(text="stateless reply", uuid="u")
    with patch("core.backends.agy_code.AntigravityClient", return_value=client):
        out = await execute_module._default_send_to_backend(
            [{"role": "user", "content": "x"}], "agy_code", None
        )
    assert out == "stateless reply"


# ── posture resolution: the PRODUCTION path reads it from the face registry ──
# (route does not thread agy_posture, so state omits it and execute falls back).


def _state_no_posture(face_id: str) -> dict:
    return {
        "task": "x",
        "conversation_id": "conv-p",
        "backend": "agy_code",
        "messages": [],
        "face_id": face_id,
        "escalation_backend": "gemini_pro",
    }


@pytest.mark.asyncio
async def test_posture_resolved_from_face_when_state_omits_it(agy_session_db):
    client = _FakeAgyClient(text="p", uuid="u")
    await _run_agy_turn(client, _state_no_posture("planner-gemini"))
    _, kwargs = client.chat.await_args
    assert kwargs["posture"].get("model") == "gemini-3.1-pro"
    assert kwargs["posture"].get("brief") is True


@pytest.mark.asyncio
async def test_posture_falls_back_to_empty_on_unknown_face(agy_session_db):
    client = _FakeAgyClient(text="p", uuid="u")
    await _run_agy_turn(client, _state_no_posture("does-not-exist"))
    _, kwargs = client.chat.await_args
    assert kwargs["posture"] == {}


@pytest.mark.asyncio
async def test_non_throttle_error_degrades_gracefully(agy_session_db):
    from core.backends.agy_code import AgyError

    client = _FakeAgyClient()
    client.chat = AsyncMock(side_effect=AgyError("no uuid recorded"))
    result = await _run_agy_turn(client, _state("conv-e"))
    assert result["error"] == "no uuid recorded"
    assert "Execution error" in result["messages"][0]["content"]
    # resume sidecar is NOT written on a failed turn
    assert await _lookup_agy_session("conv-e") is None


@pytest.mark.asyncio
async def test_fanout_worker_threads_agy_model():
    """worker-agy fan-out must apply its configured model (not agy's default)."""
    from core.nodes import worker as worker_module

    captured = {}

    async def _fake_send(messages, backend, model_override=None):
        captured["backend"] = backend
        captured["model"] = model_override
        return "worker output"

    sub_state = {
        "task": "do a chunk",
        "backend": "agy_code",
        "subtask_idx": 0,
        "agy_posture": {"model": "gemini-3.5-flash"},
    }
    with patch.object(worker_module, "_send_to_backend", _fake_send):
        out = await worker_module.worker_node(sub_state)
    assert captured["model"] == "gemini-3.5-flash"
    assert out["worker_results"][0]["status"] == "ok"
