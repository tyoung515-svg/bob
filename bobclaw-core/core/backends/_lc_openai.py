"""
BoBClaw Core — LangChain ChatOpenAI wrapper for OpenAI-compatible backends.

Used by the opt-in tool-calling loop in ``execute_node``. Normal (non-tool)
turns continue to use the raw aiohttp clients (e.g. ``core/backends/deepseek.py``)
so this wrapper does not change existing behavior.
"""
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
from langchain_core.language_models.chat_models import BaseChatModel

from core.config import config


def _deepseek_config() -> dict[str, Any]:
    from core.settings_client import provider_key
    return {
        "base_url": config.DEEPSEEK_BASE_URL,
        "api_key": provider_key('deepseek', config.DEEPSEEK_API_KEY),
        "model": config.DEEPSEEK_MODEL,
    }


def _zai_config() -> dict[str, Any]:
    from core.settings_client import provider_key
    return {
        "base_url": config.ZAI_BASE_URL,
        "api_key": provider_key('glm', config.ZAI_API_KEY),
        "model": config.ZAI_MODEL,
    }


def _qwen_cloud_config() -> dict[str, Any]:
    from core.settings_client import provider_key
    return {"base_url": config.QWEN_BASE_URL,
            "api_key": provider_key('qwen', config.DASHSCOPE_API_KEY),
            "model": config.QWEN_MODEL}


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


def _openrouter_glm_flash_config() -> dict[str, Any]:
    # GLM 5.3 Flash through OpenRouter. Same OpenAI-compatible shape as the Z.AI
    # path; the slug is vendor-qualified on the aggregator.
    return {
        "base_url": config.OPENROUTER_BASE_URL,
        "api_key": config.OPENROUTER_API_KEY,
        "model": config.OPENROUTER_GLM_FLASH_MODEL,
    }


# Backend string -> constructor kwargs for ChatOpenAI.
_LC_OPENAI_BACKENDS: dict[str, dict[str, Any]] = {
    "deepseek_v4_flash": _deepseek_config,
    "glm_5_3": _zai_config,
    "glm_5_3_flash_or": _openrouter_glm_flash_config,
    "qwen_research": _qwen_research_config,
    "qwen_cloud": _qwen_cloud_config,
}

# Backends that are wired through this wrapper and therefore tool-capable.
from core.model_choices import TOOL_CAPABLE_BACKENDS


def claude_turns_enabled():
    """Confined Claude turns: worker mode, and a lane enrolled through Bob's own sign-in.

    A legacy roster keeps its old Claude path (and stays refused in worker mode)."""
    from core.worker import mode as execution_mode
    from core.lane_config import configured_mode
    return execution_mode() == 'worker' and configured_mode()


def tool_backend(backend):
    """Provider wrappers plus the separately validated worker-only CLI adapter."""
    if backend == 'claude_code':
        return claude_turns_enabled()
    return backend in TOOL_CAPABLE_BACKENDS


def build_chat_openai(backend: str, model_override: str | None = None) -> BaseChatModel:
    """Build a ``ChatOpenAI`` instance for an OpenAI-compatible backend.

    Args:
        backend: backend name (e.g. ``"deepseek_v4_flash"``).

    Raises:
        ValueError: if *backend* is not supported by this wrapper.
    """
    if backend == "openai_codex":
        from core.backends.openai_codex import ChatOpenAICodex
        return ChatOpenAICodex(model_override=model_override)
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
        model=model_override or kwargs["model"],
        # Tool turns are bounded by the caller; do not let the model loop on its own.
        max_retries=0,
        timeout=180,
    )
