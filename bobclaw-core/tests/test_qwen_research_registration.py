from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.verify.postcondition import (
    FAMILY_BY_BACKEND,
    decorrelated_critic_backend,
    family_of,
    is_decorrelated,
)
from core.backends._lc_openai import TOOL_CAPABLE_BACKENDS, build_chat_openai


def test_qwen_research_family_taxonomy():
    assert family_of("qwen_research") == "qwen"

    assert is_decorrelated("qwen_research", "deepseek_v4_flash")
    assert is_decorrelated("qwen_research", "minimax")
    assert is_decorrelated("qwen_research", "kimi_cli")
    assert is_decorrelated("qwen_research", "claude_code")

    critic = decorrelated_critic_backend("qwen_research")
    assert family_of(critic) != "qwen"


def test_qwen_research_family_no_regression():
    assert family_of("deepseek_v4_flash") == "deepseek"
    assert family_of("glm_5_2") == "glm"
    assert family_of("claude_code") == "claude"
    assert family_of("minimax") == "minimax"
    assert family_of("holo") == "holo"
    assert family_of("kimi_cli") == "kimi"


def test_qwen_research_tool_capable():
    assert "qwen_research" in TOOL_CAPABLE_BACKENDS
    assert {"deepseek_v4_flash", "glm_5_2"} <= TOOL_CAPABLE_BACKENDS


def test_qwen_research_build_chat_openai():
    chat = build_chat_openai("qwen_research")
    assert chat is not None


@pytest.mark.asyncio
async def test_dispatch_routes_qwen_research():
    from core.nodes.execute import _send_to_backend

    instance = MagicMock()
    instance.chat = AsyncMock(
        return_value={"choices": [{"message": {"content": "ok-qwen"}}]}
    )

    with patch("core.backends.qwen_research.QwenResearchClient") as MockClient:
        MockClient.return_value = instance
        out = await _send_to_backend([{"role": "user", "content": "hi"}], "qwen_research")

    assert out == "ok-qwen"
    instance.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_other_backend_unaffected_by_qwen_branch():
    """No-regression for the additive execute.py dispatch edit: an EXISTING backend (deepseek) still
    routes through its own byte-identical branch with the new qwen_research guard present."""
    from core.nodes.execute import _send_to_backend

    instance = MagicMock()
    instance.chat = AsyncMock(
        return_value={"choices": [{"message": {"content": "ok-deepseek"}}]}
    )

    with patch("core.backends.deepseek.DeepSeekClient") as MockDS:
        MockDS.return_value = instance
        out = await _send_to_backend([{"role": "user", "content": "hi"}], "deepseek_v4_flash")

    assert out == "ok-deepseek"
    instance.chat.assert_awaited_once()
