"""
BoBClaw Core — Kimi Platform daily cost tracker & cap enforcement

Single-tenant in-memory counter. Replace with Postgres-backed storage
for multi-tenant or multi-process deployments.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from core.config import config

# date-keyed cumulative USD spend: "YYYY-MM-DD" → float
_DAILY_USD: dict[str, float] = {}

# K2.6 PAYG rates (per-million tokens)
_INPUT_USD_PER_M: float = 0.95
_CACHED_USD_PER_M: float = 0.16
_OUTPUT_USD_PER_M: float = 4.00


def _today() -> str:
    return date.today().isoformat()


def _prune() -> None:
    """Drop stale date keys (anything not today)."""
    today = _today()
    stale = [k for k in _DAILY_USD if k != today]
    for k in stale:
        del _DAILY_USD[k]


def track_cost(
    input_tokens: int = 0,
    cached_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    """Accumulate USD spend for the current day and return the new total.

    Args:
        input_tokens: Non-cached input tokens.
        cached_tokens: Cache-hit input tokens.
        output_tokens: Output tokens.
    """
    _prune()
    today = _today()
    spend = (
        (input_tokens / 1_000_000) * _INPUT_USD_PER_M
        + (cached_tokens / 1_000_000) * _CACHED_USD_PER_M
        + (output_tokens / 1_000_000) * _OUTPUT_USD_PER_M
    )
    _DAILY_USD[today] = _DAILY_USD.get(today, 0.0) + spend
    return _DAILY_USD[today]


def check_cap() -> tuple[bool, float, str]:
    """Return (ok, total_usd, mode) where mode is "ok", "warn", or "block".

    ok=True means proceed (either under warn threshold or in warn zone).
    ok=False means hard-block (at/above daily limit).
    """
    _prune()
    today = _today()
    total = _DAILY_USD.get(today, 0.0)
    limit = config.KIMI_PLATFORM_DAILY_USD_LIMIT
    warn = config.KIMI_PLATFORM_DAILY_USD_WARN

    if total >= limit:
        return False, total, "block"
    if total >= warn:
        return True, total, "warn"
    return True, total, "ok"


def remaining_budget(backend: str) -> float:
    """Return remaining cap budget for a metered backend, in USD.

    For unmetered backends (local, opencode_serve, claude_api, kimi_code),
    returns float('inf') — these are not cost-tracked today.
    """
    if backend == "kimi_platform":
        _prune()
        today = _today()
        total = _DAILY_USD.get(today, 0.0)
        return max(0.0, config.KIMI_PLATFORM_DAILY_USD_LIMIT - total)
    return float("inf")


def parse_usage(raw_response: dict) -> dict[str, int]:
    """Extract token counts from a Moonshot/Kimi-shaped response dict.

    Returns:
        {"input_tokens": int, "cached_tokens": int, "output_tokens": int}
    """
    usage = raw_response.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    cached_tokens = usage.get("cached_tokens", 0)
    return {
        "input_tokens": prompt_tokens - cached_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": usage.get("completion_tokens", 0),
    }
