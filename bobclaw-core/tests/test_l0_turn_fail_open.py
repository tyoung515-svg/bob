"""Regression tests for L0 persistence failures at the graph-node boundary.

All persistence and backend seams are mocked; no SQLite database, model server,
or external backend is contacted.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.config import config
from core.memory.exceptions import L0AppendFailed
from core.nodes import execute as execute_module
from core.nodes._l0_events import _append_agent_turn_event


@pytest.mark.asyncio
async def test_execute_returns_generated_reply_when_l0_append_fails(monkeypatch):
    append_error = L0AppendFailed("evt-test", "database is locked")
    atomic_append = AsyncMock(side_effect=append_error)
    memory = SimpleNamespace(
        event_log=SimpleNamespace(atomic_append=atomic_append),
        last_l0_append_error=None,
    )
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_L1_EXTRACTION_ENABLED", False)
    monkeypatch.setattr("core.memory.bootstrap.get_memory", lambda: memory)

    async def fake_stream(messages, backend, model_override=None):
        yield "generated reply"

    monkeypatch.setattr(execute_module, "_stream_to_backend", fake_stream)
    monkeypatch.setattr(
        execute_module,
        "_check_escalation_pin",
        AsyncMock(return_value=None),
    )

    result = await execute_module.execute_node(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "task": "",
            "face_id": "assistant",
            "backend": "local",
            "approval_response": "approved",
        }
    )

    assert result["messages"] == [
        {"role": "assistant", "content": "generated reply"}
    ]
    assert result["error"] is None
    assert memory.last_l0_append_error is append_error
    atomic_append.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_memory_singletons_does_not_abort_turn_event(monkeypatch):
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)

    def unavailable_memory():
        raise RuntimeError("memory bootstrap unavailable")

    monkeypatch.setattr(
        "core.memory.bootstrap.get_memory",
        unavailable_memory,
    )

    await _append_agent_turn_event(
        {"messages": [{"role": "user", "content": "hello"}]},
        assistant_response="reply still succeeds",
    )
