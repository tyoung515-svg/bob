"""
BoBClaw Core — Ollama client (OpenAI-compatible API)

Thin async wrapper around Ollama's /v1/* endpoints.
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Optional

import aiohttp

from core.config import config


class OllamaClient:
    """Async client for the Ollama OpenAI-compatible REST API."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or config.OLLAMA_URL).rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=120, connect=5)
        self._discover_timeout = aiohttp.ClientTimeout(total=2)

    # ── health & model discovery ───────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Return True if Ollama is reachable and serving models."""
        try:
            async with aiohttp.ClientSession(timeout=self._discover_timeout) as s:
                async with s.get(f"{self.base_url}/v1/models") as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        """Return a list of model ids available in Ollama."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.get(f"{self.base_url}/v1/models") as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    return [m["id"] for m in data.get("data", [])]
        except Exception:
            return []

    # ── chat completion ────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Non-streaming chat completion — returns the raw API response dict."""
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/v1/chat/completions", json=payload
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """Streaming chat — yields SSE delta content strings.

        Raises RuntimeError if the backend returns an error body or yields
        zero content chunks (e.g. model is advertised but not loaded).
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/v1/chat/completions", json=payload
            ) as resp:
                resp.raise_for_status()
                yielded_any = False
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        try:
                            err_body = json.loads(line)
                            if "error" in err_body:
                                raise RuntimeError(
                                    f"Model {model!r} error: {err_body['error']}"
                                )
                        except json.JSONDecodeError:
                            pass
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                        delta = obj["choices"][0]["delta"].get("content", "")
                        if delta:
                            yielded_any = True
                            yield delta
                    except Exception:
                        continue
                if not yielded_any:
                    raise RuntimeError(
                        f"Model {model!r} returned empty output"
                    )
