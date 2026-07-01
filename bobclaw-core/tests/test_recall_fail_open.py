"""
BoBClaw Core — recall fail-open policy (operational resilience).

The recall *node* deliberately propagates errors (see test_recall_node.py).
The operational wrapper (`graph._recall_node_wrapper`) applies the resilience
policy: an unavailable embedder / Qdrant degrades to empty recall so the turn
keeps moving, while correctness/security errors (e.g. ACLViolation) still
propagate.  All deps mocked — no live backend / embedder.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

import core.graph as graph
from core.memory.exceptions import (
    ACLViolation,
    EmbedderUnavailable,
    RetrievalProviderError,
)


def _fake_memory():
    """Stand-in for MemorySingletons with the observable error field."""
    return types.SimpleNamespace(
        retriever=object(),
        fact_store=object(),
        last_recall_error="SENTINEL",  # proves success path clears it
    )


def _patch_memory_enabled(monkeypatch, mem, enabled=True):
    monkeypatch.setattr("core.config.config.MEMORY_ENABLED", enabled, raising=False)
    monkeypatch.setattr("core.memory.bootstrap.get_memory", lambda: mem)


STATE = {"messages": [{"role": "user", "content": "what does BoBClaw use?"}],
         "task": "what does BoBClaw use?"}


@pytest.mark.asyncio
async def test_wrapper_fails_open_on_embedder_unavailable(monkeypatch):
    mem = _fake_memory()
    _patch_memory_enabled(monkeypatch, mem)
    exc = EmbedderUnavailable("http://localhost:8081", "connection refused")
    monkeypatch.setattr(graph, "recall_node", AsyncMock(side_effect=exc))

    result = await graph._recall_node_wrapper(dict(STATE))

    assert result == {"recalled_facts": []}
    assert mem.last_recall_error is exc  # observable, not swallowed silently


@pytest.mark.asyncio
async def test_wrapper_fails_open_on_retrieval_provider_error(monkeypatch):
    mem = _fake_memory()
    _patch_memory_enabled(monkeypatch, mem)
    exc = RetrievalProviderError("qdrant_local", "connection refused")
    monkeypatch.setattr(graph, "recall_node", AsyncMock(side_effect=exc))

    result = await graph._recall_node_wrapper(dict(STATE))

    assert result == {"recalled_facts": []}
    assert mem.last_recall_error is exc


@pytest.mark.asyncio
async def test_wrapper_propagates_acl_violation(monkeypatch):
    """ACLViolation is a correctness/security signal — must NOT fail open."""
    mem = _fake_memory()
    _patch_memory_enabled(monkeypatch, mem)
    monkeypatch.setattr(
        graph, "recall_node",
        AsyncMock(side_effect=ACLViolation("bobclaw_default", "provider not allowed")),
    )

    with pytest.raises(ACLViolation):
        await graph._recall_node_wrapper(dict(STATE))


@pytest.mark.asyncio
async def test_wrapper_passthrough_and_clears_error_on_success(monkeypatch):
    mem = _fake_memory()
    _patch_memory_enabled(monkeypatch, mem)
    facts = ["fact-a", "fact-b"]
    monkeypatch.setattr(
        graph, "recall_node", AsyncMock(return_value={"recalled_facts": facts}),
    )

    result = await graph._recall_node_wrapper(dict(STATE))

    assert result == {"recalled_facts": facts}
    assert mem.last_recall_error is None  # cleared on healthy recall


@pytest.mark.asyncio
async def test_wrapper_disabled_short_circuits(monkeypatch):
    """MEMORY_ENABLED=False returns empty without touching memory."""
    monkeypatch.setattr("core.config.config.MEMORY_ENABLED", False, raising=False)
    sentinel = AsyncMock(side_effect=AssertionError("recall_node must not run"))
    monkeypatch.setattr(graph, "recall_node", sentinel)

    result = await graph._recall_node_wrapper(dict(STATE))

    assert result == {"recalled_facts": []}
    sentinel.assert_not_called()
