"""
BoBClaw Core — Unit tests for the per-token streaming transport.

Covers ``_ThinkStripper`` (incremental <think> removal), the minimax streaming
think-strip path, and the kimi_platform/opencode full-string delegation in
``_default_stream_to_backend``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.nodes.execute import (
    _ThinkStripper,
    _default_stream_to_backend,
)


# ─── _ThinkStripper ────────────────────────────────────────────────────────────

def _run_stripper(deltas: list[str]) -> str:
    s = _ThinkStripper()
    out = "".join(s.feed(d) for d in deltas)
    out += s.flush()
    return out


def test_think_block_split_across_deltas_is_stripped():
    # "<think>reasoning</think>answer" arriving token-by-token.
    deltas = ["<th", "ink>rea", "soning", "</thi", "nk>", "ans", "wer"]
    assert _run_stripper(deltas) == "answer"


def test_leading_whitespace_then_think_block_stripped():
    deltas = ["\n  <think>", "hmm", "</think>\n", "final"]
    assert _run_stripper(deltas) == "final"


def test_no_think_block_passes_through_unchanged():
    deltas = ["Hello", ", ", "world", "!"]
    assert _run_stripper(deltas) == "Hello, world!"


def test_plain_text_starting_with_angle_bracket_not_eaten():
    # Looks briefly like it could be <think> but isn't.
    deltas = ["<so", "mething>", " kept"]
    assert _run_stripper(deltas) == "<something> kept"


def test_unclosed_think_falls_back_to_regex_on_flush():
    # No closing tag → full-string regex leaves it untouched (matches the
    # non-streaming _default_send_to_backend behaviour).
    deltas = ["<think>never closes"]
    assert _run_stripper(deltas) == "<think>never closes"


# ─── minimax streaming path ────────────────────────────────────────────────────

class _FakeMiniMax:
    def __init__(self, *a, **k):
        pass

    async def stream_chat(self, messages, model=None, **kwargs):
        for delta in ["<think>", "secret reasoning", "</think>", "visible ", "answer"]:
            yield delta


@pytest.mark.asyncio
async def test_minimax_stream_strips_think_block():
    with patch("core.backends.minimax.MiniMaxClient", _FakeMiniMax):
        out = [
            d
            async for d in _default_stream_to_backend(
                [{"role": "user", "content": "hi"}], "minimax"
            )
        ]
    joined = "".join(out)
    assert "secret reasoning" not in joined
    assert joined == "visible answer"


# ─── kimi_platform / opencode delegate to the full-string path ─────────────────

@pytest.mark.asyncio
async def test_kimi_platform_delegates_to_send_to_backend():
    async def _fake_send(messages, backend, model_override=None):
        assert backend == "kimi_platform"
        return "full platform response"

    with patch("core.nodes.execute._send_to_backend", _fake_send):
        out = [
            d
            async for d in _default_stream_to_backend(
                [{"role": "user", "content": "hi"}], "kimi_platform"
            )
        ]
    assert out == ["full platform response"]


@pytest.mark.asyncio
async def test_opencode_delegates_to_send_to_backend():
    async def _fake_send(messages, backend, model_override=None):
        return "opencode result"

    with patch("core.nodes.execute._send_to_backend", _fake_send):
        out = [
            d
            async for d in _default_stream_to_backend(
                [{"role": "user", "content": "hi"}], "opencode_serve"
            )
        ]
    assert out == ["opencode result"]
