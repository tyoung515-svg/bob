"""
BoBClaw Core — decompose_node backend-selection tests.

Proves decompose honours an explicit state/request backend (routing through
execute._send_to_backend) instead of forcing the local router, while keeping
the local path as the default/fallback.  All backend/router calls are mocked —
no live or paid backend calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import core.nodes.decompose as decompose
from core.nodes.decompose import _default_call_llm, decompose_node


COMPLEX_TASK = "Implement a full REST API with authentication, storage, and tests"
NUMBERED = "1. Set up database\n2. Create API routes\n3. Write tests"


def _fake_local_router(monkeypatch, chunks="1. local-a\n2. local-b"):
    """Patch LocalModelRouter so the local path yields a deterministic list."""
    class _FakeBackend:
        name = "lmstudio"
        url = "http://localhost:1234"
        models = ["some-model"]

    class _FakeRouter:
        async def discover(self):
            return [_FakeBackend()]

        def get_best_backend(self, backends=None):
            return _FakeBackend()

        async def chat(self, messages, backend=None, **kw):
            for line in chunks.splitlines(keepends=True):
                yield line

    monkeypatch.setattr(
        "core.backends.local_router.LocalModelRouter", _FakeRouter
    )


# ─── explicit backend is honoured ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_call_llm_honors_requested_backend(monkeypatch):
    """A non-local backend routes through execute._send_to_backend, not local."""
    sent = AsyncMock(return_value=NUMBERED)
    monkeypatch.setattr("core.nodes.execute._send_to_backend", sent)
    # If the local router were used, this would explode — proving it's not.
    monkeypatch.setattr(
        "core.backends.local_router.LocalModelRouter",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("local router used")),
    )

    out = await _default_call_llm(COMPLEX_TASK, backend="deepseek_v4_flash")

    assert out == ["Set up database", "Create API routes", "Write tests"]
    sent.assert_awaited_once()
    called_messages, called_backend = sent.await_args.args
    assert called_backend == "deepseek_v4_flash"
    assert "Break the following task" in called_messages[0]["content"]


@pytest.mark.asyncio
async def test_decompose_node_uses_requested_backend(monkeypatch):
    """End-to-end through decompose_node: requested backend reaches the call."""
    sent = AsyncMock(return_value=NUMBERED)
    monkeypatch.setattr("core.nodes.execute._send_to_backend", sent)

    state = {"task": COMPLEX_TASK, "backend": "deepseek_v4_flash", "face_id": "assistant"}
    result = await decompose_node(state)

    assert result.get("subtasks") == ["Set up database", "Create API routes", "Write tests"]
    assert "3 subtask" in result["messages"][0]["content"]
    assert sent.await_args.args[1] == "deepseek_v4_flash"


# ─── default / local fallback preserved ───────────────────────────────────────

@pytest.mark.asyncio
async def test_default_call_llm_local_backend_uses_router(monkeypatch):
    """backend='local' keeps the original LocalModelRouter discovery path."""
    _fake_local_router(monkeypatch, chunks="1. local-a\n2. local-b")
    sent = AsyncMock()
    monkeypatch.setattr("core.nodes.execute._send_to_backend", sent)

    out = await _default_call_llm(COMPLEX_TASK, backend="local")

    assert out == ["local-a", "local-b"]
    sent.assert_not_awaited()  # cloud transport must NOT be used for local


@pytest.mark.asyncio
async def test_default_call_llm_empty_backend_uses_router(monkeypatch):
    """Empty backend (nothing requested) also falls back to local — default."""
    _fake_local_router(monkeypatch, chunks="1. only-one")
    sent = AsyncMock()
    monkeypatch.setattr("core.nodes.execute._send_to_backend", sent)

    out = await _default_call_llm(COMPLEX_TASK, backend="")

    assert out == ["only-one"]
    sent.assert_not_awaited()


# ─── error handling ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_call_llm_falls_back_to_single_task_on_error(monkeypatch):
    """A backend failure degrades to [task] rather than breaking the turn."""
    boom = AsyncMock(side_effect=RuntimeError("backend down"))
    monkeypatch.setattr("core.nodes.execute._send_to_backend", boom)

    out = await _default_call_llm(COMPLEX_TASK, backend="deepseek_v4_flash")

    assert out == [COMPLEX_TASK]
