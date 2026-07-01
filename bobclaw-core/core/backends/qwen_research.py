"""
BoBClaw Core — Qwen Research client (OpenAI-compatible API for self-hosted llama.cpp)

Thin async wrapper around the local Qwen 35B-A3B Q4 expert-split research floor
served via llama.cpp's llama-server (OpenAI-compatible, no auth).

Cites DESIGN-MS-D2 §3 MS2-R0 + OD#1.  This is the self-hostable research floor
(Qwen 35B-A3B Q4, local llama.cpp, OpenAI-compat, no auth).  The ONE documented
deviation from the cloud-backed backends (e.g., DeepSeek/Kimi) is that
`health_check` is **reachability-based, not key-gated** — a local server has no
API key, so we never short-circuit on an empty key.
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Optional

import aiohttp

from core.config import config


class QwenResearchClient:
    """Async client for a local Qwen research server via OpenAI-compatible API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or config.QWEN_RESEARCH_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.QWEN_RESEARCH_API_KEY
        self._timeout = aiohttp.ClientTimeout(
            total=config.QWEN_RESEARCH_TIMEOUT_S, connect=5
        )
        self._discover_timeout = aiohttp.ClientTimeout(total=5)

    # ── private helpers ─────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        """Return headers.  Only send Bearer token when an API key is set (local
        server has none by default and must not receive a bogus Authorization header)."""
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    # ── health & model discovery ───────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Reachability-based health check.  Unlike cloud backends this does NOT
        short-circuit on an empty API key — local servers have no key but are
        reachable via HTTP."""
        try:
            async with aiohttp.ClientSession(timeout=self._discover_timeout) as s:
                async with s.get(
                    f"{self.base_url}/models",
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        """Fetch available model IDs from the local server."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.get(
                    f"{self.base_url}/models",
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    return [m["id"] for m in data.get("data", [])]
        except Exception:
            return []

    # ── chat completion ────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request (non-streaming).  All extra kwargs
        (tools, tool_choice, temperature, max_tokens, etc.) are passed through
        after removing None values (but temperature=0 is kept because 0 is not None)."""
        payload = {
            "model": model or config.QWEN_RESEARCH_MODEL,
            "messages": messages,
            "stream": False,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Send a streaming chat completion request and yield content deltas
        from SSE-parsed events.  Follows the same delta-parsing logic as the
        DeepSeek client."""
        payload = {
            "model": model or config.QWEN_RESEARCH_MODEL,
            "messages": messages,
            "stream": True,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                        delta = obj["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except Exception:
                        continue
