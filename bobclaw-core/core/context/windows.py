"""Per-backend context-window and prompt-transport limits.

Why this exists: chat history used to be cut to the last 20 messages / 8192 characters (about 2K
tokens) for every model, whatever its real window. The history budget is now derived from each
backend's window, with explicit lower caps only where a limit is real:

  * CLI backends that take the whole prompt as ONE argv entry (Linux caps a single argument at
    128 KiB, so a bigger prompt dies with E2BIG instead of degrading gracefully);
  * local models whose window is small or not verified.

Windows are in tokens. Sources (2026-10-23): the models.dev catalog snapshot in
``~/.hermes/models_dev_cache.json`` (deepseek-v4-flash 1,000,000; glm-5.3 1,000,000; gpt-6.1-sol
1,050,000; k3 1,048,576; kimi-k2.6 262,144; MiniMax-M3 1,048,576; Claude 5.5 family 1,000,000;
Gemini 3.x 1,048,576). Strata (qwen_research / local_strata) is configurable to a 128K window
(n_ctx 131072); its output cap is 2048 tokens. Prefill is about 3,000 tokens/s, so a very full
prompt costs tens of seconds per turn.

Every number can be overridden per backend from the environment, e.g.
``BOB_CTX_WINDOW_QWEN_RESEARCH=65536`` or ``BOB_CTX_MAX_PROMPT_TOKENS_LOCAL=16000``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Optional

# Conservative: real English text is closer to 4 chars/token, code and non-Latin scripts are lower.
# Over-estimating tokens only makes the budget slightly smaller, never overflows a window.
CHARS_PER_TOKEN: float = 3.0

# One argv entry is limited to 131072 bytes on Linux; keep room for UTF-8 expansion and CLI flags.
_ARGV_PROMPT_CHARS: int = 100_000

# Share of the context window the prior-conversation block may use. The rest is left for the system
# prompt, tool schemas, recalled memory, reasoning and the answer itself.
DEFAULT_HISTORY_FRACTION: float = 0.5

# ``BOB_CTX_PROFILE=long``: the high-context option (about 700-800K tokens on 1M-window models). Whether it
# beats the default 0.5 is an open question to settle with the compaction eval; the default stays 0.5 until then.
LONG_PROFILE_HISTORY_FRACTION: float = 0.75


@dataclass(frozen=True)
class BackendLimits:
    window_tokens: int
    # Hard cap for the WHOLE prompt (tokens) where a backend is only trusted up to a known size.
    max_prompt_tokens: Optional[int] = None
    # Hard cap for the whole prompt (characters) where the transport itself has a limit.
    max_prompt_chars: Optional[int] = None
    # Per-backend share of the window for history (None -> DEFAULT_HISTORY_FRACTION).
    history_fraction: Optional[float] = None
    note: str = ""


DEFAULT_LIMITS = BackendLimits(128_000, note="unknown backend: conservative window")

# Strata at 128K: window 131072, minus the 2048-token output cap and a margin for the system prompt.
_STRATA = BackendLimits(
    131_072, max_prompt_tokens=124_000, history_fraction=0.75,
    note="Strata configured to 128K; ~3K tok/s prefill",
)

_LIMITS: dict[str, BackendLimits] = {
    "deepseek_v4_flash": BackendLimits(1_000_000),
    "deepseek_v4": BackendLimits(1_000_000),
    "glm_5_3": BackendLimits(1_000_000),
    "glm_5_3_flash_or": BackendLimits(1_048_576, note="OpenRouter z-ai/glm-5.3-flash"),
    "codex_code": BackendLimits(1_050_000, note="prompt via stdin"),
    "openai_codex": BackendLimits(128_000, note="conservative until account model window is known"),
    "claude_code": BackendLimits(1_000_000, note="prompt via ssh stdin"),
    "claude_api": BackendLimits(200_000, note="fresh-lane preset claude-sonnet-5-5; lane may override"),
    "kimi_code": BackendLimits(1_048_576),
    "kimi_platform": BackendLimits(262_144),
    "kimi_cli": BackendLimits(1_048_576, max_prompt_chars=_ARGV_PROMPT_CHARS, note="prompt is one argv entry"),
    "minimax": BackendLimits(1_048_576),
    "gemini_flash": BackendLimits(1_048_576),
    "gemini_pro": BackendLimits(1_048_576),
    "gemini_deep_research": BackendLimits(1_048_576),
    "agy_code": BackendLimits(1_048_576, max_prompt_chars=_ARGV_PROMPT_CHARS, note="prompt is one argv entry"),
    "pi_code": BackendLimits(1_000_000, max_prompt_chars=_ARGV_PROMPT_CHARS, note="prompt is one argv entry"),
    "opencode_serve": BackendLimits(200_000, note="window not verified"),
    "qwen_research": _STRATA,
    "qwen_cloud": BackendLimits(32_768, note="conservative until cloud model window is confirmed"),
    "local_strata": _STRATA,
    "local": BackendLimits(32_768, max_prompt_tokens=8_000, note="unverified local model: conservative"),
}


# Backends that run on this machine's own hardware. Conversation text sent to them never leaves the box.
LOCAL_BACKENDS: frozenset[str] = frozenset({"qwen_research", "local_strata", "local", "local_router", "ollama", "lmstudio"})


def is_cloud_backend(backend: Optional[str]) -> bool:
    """True only for a KNOWN backend that is not local. Unset or unknown backends are not cloud: routing could land
    them anywhere, so anything that ships conversation text to a third party must not assume they are."""
    name = (backend or "").strip().lower()
    return bool(name) and name in _LIMITS and name not in LOCAL_BACKENDS


def _env_int(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def limits_for(backend: Optional[str]) -> BackendLimits:
    """Limits for *backend* (unknown / None -> conservative defaults), with env overrides applied."""
    name = (backend or "").strip().lower()
    base = _LIMITS.get(name, DEFAULT_LIMITS)
    key = (name or "default").upper().replace("-", "_")
    window = _env_int(f"BOB_CTX_WINDOW_{key}")
    max_tokens = _env_int(f"BOB_CTX_MAX_PROMPT_TOKENS_{key}")
    max_chars = _env_int(f"BOB_CTX_MAX_PROMPT_CHARS_{key}")
    if window or max_tokens or max_chars:
        base = replace(
            base,
            window_tokens=window or base.window_tokens,
            max_prompt_tokens=max_tokens or base.max_prompt_tokens,
            max_prompt_chars=max_chars or base.max_prompt_chars,
        )
    return base


def fit_enabled() -> bool:
    """Kill switch: ``BOB_CONTEXT_FIT_ENABLED=false`` restores the legacy flat flatten with no fitting."""
    return os.getenv("BOB_CONTEXT_FIT_ENABLED", "true").strip().lower() != "false"


def _clamp_fraction(value: float) -> float:
    return min(max(value, 0.05), 0.9)


def history_fraction(limits: Optional[BackendLimits] = None) -> float:
    """Share of the window for history.

    Precedence: ``BOB_CTX_HISTORY_FRACTION`` (all backends) > ``BOB_CTX_PROFILE=long`` > the backend's own
    fraction > the default (0.5). Profile ``long`` (0.75, about 750K tokens on the 1M-window models) is the
    deliberate high-context option; transport caps (argv CLIs, hard prompt caps) still apply on top.
    """
    raw = os.getenv("BOB_CTX_HISTORY_FRACTION", "").strip()
    if raw:
        try:
            return _clamp_fraction(float(raw))
        except ValueError:
            pass
    if os.getenv("BOB_CTX_PROFILE", "").strip().lower() == "long":
        return LONG_PROFILE_HISTORY_FRACTION
    if limits is not None and limits.history_fraction:
        return _clamp_fraction(limits.history_fraction)
    return DEFAULT_HISTORY_FRACTION


def prompt_char_cap(limits: BackendLimits) -> Optional[int]:
    """Hard cap on the whole prompt in characters, or None when only the window applies."""
    caps = []
    if limits.max_prompt_chars:
        caps.append(limits.max_prompt_chars)
    if limits.max_prompt_tokens:
        caps.append(int(limits.max_prompt_tokens * CHARS_PER_TOKEN))
    return min(caps) if caps else None


def history_budget_tokens(limits: BackendLimits) -> int:
    """Token budget for the prior-conversation block of one prompt."""
    budget = int(limits.window_tokens * history_fraction(limits))
    cap = prompt_char_cap(limits)
    if cap is not None:
        # Leave 20% of a hard cap for the system prompt, recalled memory and the new user turn.
        budget = min(budget, int(cap * 0.8 / CHARS_PER_TOKEN))
    return max(budget, 1_000)
