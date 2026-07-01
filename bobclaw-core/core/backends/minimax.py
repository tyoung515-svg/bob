"""
BoBClaw Core — MiniMax M3 client (OpenAI-compatible API)

Thin async wrapper around MiniMax's /v1/* endpoints. Identical interface to
DeepSeekClient/KimiClient for drop-in interchangeability. MiniMax-M3 is a
reasoning model — its responses embed a <think>...</think> block in
message.content; callers (execute_node) strip it before returning to the user.
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Optional

import aiohttp

from core.config import config


class MiniMaxClient:
    """Async client for MiniMax's OpenAI-compatible REST API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or config.MINIMAX_BASE_URL).rstrip("/")
        self.api_key = api_key or config.MINIMAX_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=120, connect=5)
        self._discover_timeout = aiohttp.ClientTimeout(total=5)

    # ── health & model discovery ───────────────────────────────────────────────

    async def health_check(self) -> bool:
        if not self.api_key:
            return False
        try:
            async with aiohttp.ClientSession(timeout=self._discover_timeout) as s:
                async with s.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        if not self.api_key:
            return []
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
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
        payload = {
            "model": model or config.MINIMAX_MODEL,
            "messages": messages,
            "stream": False,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        payload = {
            "model": model or config.MINIMAX_MODEL,
            "messages": messages,
            "stream": True,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
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
