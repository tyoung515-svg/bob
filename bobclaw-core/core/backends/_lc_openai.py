"""
BoBClaw Core — LangChain ChatOpenAI wrapper for OpenAI-compatible backends.

Used by the opt-in tool-calling loop in ``execute_node``. Normal (non-tool)
turns continue to use the raw aiohttp clients (e.g. ``core/backends/deepseek.py``)
so this wrapper does not change existing behavior.
"""
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from core.config import config


def _deepseek_config() -> dict[str, Any]:
    return {
        "base_url": config.DEEPSEEK_BASE_URL,
        "api_key": config.DEEPSEEK_API_KEY,
        "model": config.DEEPSEEK_MODEL,
    }


def _zai_config() -> dict[str, Any]:
    return {
        "base_url": config.ZAI_BASE_URL,
        "api_key": config.ZAI_API_KEY,
        "model": config.ZAI_MODEL,
    }


def _qwen_research_config() -> dict[str, Any]:
    # MS2-R0: the self-hostable Qwen research floor (local llama.cpp, OpenAI-compat). A research
    # head is a long-horizon TOOL-CALLING agent, so it must be tool-capable via this wrapper. The
    # local server needs no auth, but ChatOpenAI rejects an empty api_key → use a harmless placeholder.
    # This is a lc-openai library quirk ONLY; the bare-client wire path (qwen_research.py `_headers`)
    # forwards an Authorization header solely when a REAL key is configured, never this placeholder.
    return {
        "base_url": config.QWEN_RESEARCH_BASE_URL,
        "api_key": config.QWEN_RESEARCH_API_KEY or "sk-local-noauth",
        "model": config.QWEN_RESEARCH_MODEL,
    }


# Backend string -> constructor kwargs for ChatOpenAI.
_LC_OPENAI_BACKENDS: dict[str, dict[str, Any]] = {
    "deepseek_v4_flash": _deepseek_config,
    "glm_5_2": _zai_config,
    "qwen_research": _qwen_research_config,
}

# Backends that are wired through this wrapper and therefore tool-capable.
TOOL_CAPABLE_BACKENDS: frozenset[str] = frozenset(_LC_OPENAI_BACKENDS.keys())


def build_chat_openai(backend: str) -> ChatOpenAI:
    """Build a ``ChatOpenAI`` instance for an OpenAI-compatible backend.

    Args:
        backend: backend name (e.g. ``"deepseek_v4_flash"``).

    Raises:
        ValueError: if *backend* is not supported by this wrapper.
    """
    try:
        kwargs = _LC_OPENAI_BACKENDS[backend]()
    except KeyError as exc:
        raise ValueError(
            f"Backend {backend!r} is not supported by the ChatOpenAI wrapper; "
            f"supported: {sorted(TOOL_CAPABLE_BACKENDS)}"
        ) from exc

    return ChatOpenAI(
        base_url=kwargs["base_url"],
        api_key=kwargs["api_key"],
        model=kwargs["model"],
        # Tool turns are bounded by the caller; do not let the model loop on its own.
        max_retries=0,
    )
