"""
BoBClaw Core — Google Gemini REST API client

Direct aiohttp wrapper around the Gemini API (not OpenAI-compatible).
Uses x-goog-api-key auth and Content format for messages.
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Optional

import aiohttp

from core.config import config


class GeminiClient:
    """Async client for Google Gemini REST API (direct aiohttp)."""

    API_BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or config.GOOGLE_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=120, connect=5)

    # ── health & model discovery ───────────────────────────────────────────────

    async def health_check(self) -> bool:
        if not self.api_key:
            return False
        try:
            url = f"{self.API_BASE}/models"
            headers = {"x-goog-api-key": self.api_key}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                async with s.get(url, headers=headers) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        if not self.api_key:
            return []
        try:
            url = f"{self.API_BASE}/models"
            headers = {"x-goog-api-key": self.api_key}
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.get(url, headers=headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    # ── chat completion ────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        model_name = model
        system_instruction = None
        contents: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role == "system":
                system_instruction = {"parts": [{"text": m["content"]}]}
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": m["content"]}]})
            else:
                contents.append({"role": role, "parts": [{"text": m["content"]}]})

        payload: dict[str, Any] = {
            "contents": contents,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        if system_instruction:
            payload["system_instruction"] = system_instruction

        url = f"{self.API_BASE}/models/{model_name}:generateContent"
        headers = {"x-goog-api-key": self.api_key, "content-type": "application/json"}
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        model_name = model
        contents: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role == "assistant":
                contents.append({"role": "model", "parts": [{"text": m["content"]}]})
            else:
                contents.append({"role": role, "parts": [{"text": m["content"]}]})

        payload: dict[str, Any] = {
            "contents": contents,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        url = f"{self.API_BASE}/models/{model_name}:streamGenerateContent"
        headers = {"x-goog-api-key": self.api_key, "content-type": "application/json"}
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith("["):
                        continue
                    try:
                        obj = json.loads(line)
                        parts = obj.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                        for part in parts:
                            text = part.get("text", "")
                            if text:
                                yield text
                    except json.JSONDecodeError:
                        continue
