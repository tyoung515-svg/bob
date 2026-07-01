"""
BoBClaw Core — Kimi membership HTTP client (OpenAI-compatible API)

Thin async wrapper around the Moonshot membership endpoint
(api.moonshot.ai/v1) for the `kimi_code` backend.
Identical interface to KimiPlatformClient for drop-in interchangeability.
Uses the real API model ID (default: kimi-k2.7-code), NOT the CLI/IDE slug
"kimi-for-coding". Strips `temperature` from outbound payloads because the
membership endpoint rejects it.
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Optional

import aiohttp

from core.config import config


class KimiClient:
    """Async client for Kimi's OpenAI-compatible REST API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or config.KIMI_BASE_URL).rstrip("/")
        self.api_key = api_key or config.KIMI_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=120, connect=5)
        self._discover_timeout = aiohttp.ClientTimeout(total=5)

    # ── health & model discovery ───────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Return True if Kimi is reachable and serving models.

        Returns False immediately when no API key is configured.
        """
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
        """Return a list of model ids available on the Kimi account."""
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

    @staticmethod
    def _prepare_payload(
        *,
        model: Optional[str],
        messages: list[dict[str, str]],
        stream: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build the outbound JSON payload, stripping unsupported fields."""
        # The membership endpoint rejects temperature even when set to 0.
        kwargs.pop("temperature", None)
        return {
            "model": model or config.KIMI_MODEL,
            "messages": messages,
            "stream": stream,
            **{k: v for k, v in kwargs.items() if v is not None},
        }

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Non-streaming chat completion — returns the raw API response dict."""
        payload = self._prepare_payload(
            model=model, messages=messages, stream=False, **kwargs
        )
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
        """Streaming chat — yields SSE delta content strings."""
        payload = self._prepare_payload(
            model=model, messages=messages, stream=True, **kwargs
        )
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
