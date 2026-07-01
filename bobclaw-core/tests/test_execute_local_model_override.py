"""
BoBClaw Core — execute_node / _default_send_to_backend local-model override tests.

Pins the threading contract for ``state.model_override`` through to the local
router's ``chat()`` call. All HTTP and router calls are mocked; no network.

Sprint 04 hardening (local backend: honor requested model / prefer resident).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.nodes.execute import _default_send_to_backend


LOCAL = "lmstudio"


def _fake_local_backend():
    class _BE:
        name = LOCAL
        url = "http://localhost:1234"
        models = ["qwen3.6", "gemma-4-27b", "phi-4"]
        resident_models = ["qwen3.6"]

    return _BE()


def _patched_router(monkeypatch, *, chat_chunks=("hi",), chat_model_capture=None,
                    discover_models=None):
    """Patch the module-level ``_router`` in core.nodes.execute so the local
    branch uses a fake. Records the model name passed to chat() in
    *chat_model_capture* (a list)."""
    if discover_models is None:
        discover_models = [_fake_local_backend()]

    class _FakeRouter:
        async def discover(self):
            return discover_models

        def get_best_backend(self, backends=None):
            return discover_models[0] if discover_models else None

        async def chat(self, messages, model=None, backend=None, **kw):
            if chat_model_capture is not None:
                chat_model_capture.append(model)
            for chunk in chat_chunks:
                yield chunk

    monkeypatch.setattr("core.nodes.execute._router", _FakeRouter())


# ─── _default_send_to_backend: threading ──────────────────────────────────────

@pytest.mark.asyncio
async def test_default_send_to_backend_local_threads_model_override(monkeypatch):
    """The local branch must pass model_override straight through to the
    router's chat() call — no picking, no substitution."""
    captured: list = []
    _patched_router(monkeypatch, chat_chunks=("ok",), chat_model_capture=captured)

    out = await _default_send_to_backend(
        messages=[{"role": "user", "content": "hi"}],
        backend=LOCAL,
        model_override="qwen3.6",
    )

    assert out == "ok"
    assert captured == ["qwen3.6"]


@pytest.mark.asyncio
async def test_default_send_to_backend_local_no_override_uses_residency(monkeypatch):
    """No override → router's chat() is called with model=None and the
    router applies its residency-aware picker."""
    captured: list = []
    _patched_router(monkeypatch, chat_chunks=("ok",), chat_model_capture=captured)

    out = await _default_send_to_backend(
        messages=[{"role": "user", "content": "hi"}],
        backend=LOCAL,
    )

    assert out == "ok"
    assert captured == [None]


@pytest.mark.asyncio
async def test_default_send_to_backend_local_override_unavailable_returns_clean_error(monkeypatch):
    """If the override names a model the backend doesn't have, the user gets
    the clean '[No local backend available: ...]' string — NOT a 400 from
    the backend, NOT a silent substitution."""
    class _FakeRouter:
        async def discover(self):
            return [_fake_local_backend()]

        def get_best_backend(self, backends=None):
            return _fake_local_backend()

        async def chat(self, messages, model=None, backend=None, **kw):
            # Simulate the router's clean-error contract: raise before HTTP.
            raise RuntimeError(
                f"Requested model {model!r} is not available on backend "
                f"{backend.name!r} (known: {backend.models})"
            )
            yield  # pragma: no cover — async generator

    monkeypatch.setattr("core.nodes.execute._router", _FakeRouter())

    out = await _default_send_to_backend(
        messages=[{"role": "user", "content": "hi"}],
        backend=LOCAL,
        model_override="nonexistent-model",
    )

    assert out.startswith("[No local backend available:")
    assert "nonexistent-model" in out
    assert "not available on backend" in out


@pytest.mark.asyncio
async def test_default_send_to_backend_local_no_backend_returns_clean_error(monkeypatch):
    """When discovery returns nothing, the user-facing error string is
    returned (not raised) so the WS path doesn't surface a stack trace."""
    _patched_router(monkeypatch, discover_models=[])

    out = await _default_send_to_backend(
        messages=[{"role": "user", "content": "hi"}],
        backend=LOCAL,
        model_override="qwen3.6",
    )

    assert out.startswith("[No local backend available")


# ─── 2-arg call sites (decompose seam) still work ────────────────────────────

@pytest.mark.asyncio
async def test_default_send_to_backend_local_two_arg_call_still_works(monkeypatch):
    """Existing 2-arg callers (decompose._default_call_llm) must not need
    edits: passing model_override implicitly as None is preserved."""
    captured: list = []
    _patched_router(monkeypatch, chat_chunks=("legacy",), chat_model_capture=captured)

    out = await _default_send_to_backend(
        messages=[{"role": "user", "content": "hi"}],
        backend=LOCAL,
    )

    assert out == "legacy"
    assert captured == [None]


# ─── execute_node reads state.model_override ──────────────────────────────────

@pytest.mark.asyncio
async def test_execute_node_threads_state_model_override(monkeypatch):
    """End-to-end: execute_node reads state.model_override and passes it to
    _send_to_backend, which then forwards it to the router."""
    captured: list = []
    _patched_router(monkeypatch, chat_chunks=("ok",), chat_model_capture=captured)

    monkeypatch.setattr(
        "core.nodes.execute._check_escalation_pin",
        AsyncMock(return_value=None),
    )

    from core.nodes.execute import execute_node

    state = {
        "task": "hi",
        "backend": LOCAL,
        "model_override": "qwen3.6",
        "messages": [],
        "recalled_facts": [],
        "approval_response": None,
    }
    result = await execute_node(state)

    # The router got our explicit override, not None.
    assert captured == ["qwen3.6"]
    assert result["messages"][-1]["content"] == "ok"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_execute_node_no_state_override_passes_none(monkeypatch):
    """When state has no model_override, execute_node must pass None (so
    the router's residency picker runs) — not an empty string or sentinel."""
    captured: list = []
    _patched_router(monkeypatch, chat_chunks=("ok",), chat_model_capture=captured)

    monkeypatch.setattr(
        "core.nodes.execute._check_escalation_pin",
        AsyncMock(return_value=None),
    )

    from core.nodes.execute import execute_node

    state = {
        "task": "hi",
        "backend": LOCAL,
        "model_override": None,
        "messages": [],
        "recalled_facts": [],
        "approval_response": None,
    }
    result = await execute_node(state)

    assert captured == [None]
    assert result["error"] is None


@pytest.mark.asyncio
async def test_execute_node_429_fallback_preserves_model_override(monkeypatch):
    """The 429 escalation fallback (kimi_code → kimi_platform) must also
    forward state.model_override, not drop it on the floor."""
    captured: list = []
    _patched_router(monkeypatch, chat_chunks=("ok",), chat_model_capture=captured)

    # First call: kimi_code returns 429. Second call (escalation): local path
    # via the fake router.
    call_count = {"n": 0}
    seen_overrides: list = []

    async def _stream_side_effect(messages, backend, model_override=None):
        call_count["n"] += 1
        seen_overrides.append(model_override)
        if call_count["n"] == 1:
            from aiohttp import ClientResponseError
            raise ClientResponseError(
                request_info=None, history=(), status=429,
                message="rate limited",
            )
        yield "fallback-ok"

    monkeypatch.setattr(
        "core.nodes.execute._check_escalation_pin",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "core.nodes.execute._stream_to_backend",
        _stream_side_effect,
    )

    from core.nodes.execute import execute_node

    state = {
        "task": "hi",
        "backend": "kimi_code",
        "model_override": "qwen3.6",
        "messages": [],
        "recalled_facts": [],
        "approval_response": None,
        "escalation_backend": LOCAL,
    }
    result = await execute_node(state)

    # We asserted via the call counter that the second call was issued and
    # returned the fallback string. State.model_override must have been
    # threaded through both attempts.
    assert call_count["n"] == 2
    assert seen_overrides == ["qwen3.6", "qwen3.6"]
    assert result["messages"][-1]["content"] == "fallback-ok"
    assert result["error"] is None
