"""
BoBClaw Core — Anthropic Claude Messages API client

Async wrapper around Anthropic's /v1/messages and /v1/models endpoints.
NOT OpenAI-compatible — translates message format and headers.
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Optional

import aiohttp

from core.config import config


class ClaudeClient:
    """Async client for Anthropic's Messages API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or config.ANTHROPIC_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.ANTHROPIC_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=120, connect=5)
        self._discover_timeout = aiohttp.ClientTimeout(total=5)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    # ── health & model discovery ───────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Return True if Anthropic API is reachable.

        Returns False immediately when no API key is configured.
        """
        if not self.api_key:
            return False
        try:
            async with aiohttp.ClientSession(timeout=self._discover_timeout) as s:
                async with s.get(
                    f"{self.base_url}/v1/models",
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        """Return a list of model ids available on the Anthropic account."""
        if not self.api_key:
            return []
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.get(
                    f"{self.base_url}/v1/models",
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
        messages: list[dict[str, str]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Non-streaming chat completion — returns the raw API response dict."""
        payload: dict[str, Any] = {
            "model": model or config.ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "messages": messages,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/v1/messages",
                json=payload,
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Streaming chat — yields SSE delta text strings."""
        payload: dict[str, Any] = {
            "model": model or config.ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "messages": messages,
            "stream": True,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/v1/messages",
                json=payload,
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk:
                        continue
                    try:
                        obj = json.loads(chunk)
                        event_type = obj.get("type", "")
                        if event_type == "content_block_delta":
                            text = obj.get("delta", {}).get("text", "")
                            if text:
                                yield text
                        elif event_type == "message_stop":
                            break
                    except Exception:
                        continue
