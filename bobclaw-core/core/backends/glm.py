"""
BoBClaw Core — GLM-5.2 client (OpenAI-compatible API)

Thin async wrapper around Z.AI's /api/paas/v4/* endpoints.
Identical interface to DeepSeekClient for drop-in interchangeability.

Resilience (NB-W2 P0): Z.AI returns HTTP **429** for two very different conditions —
a genuine transient rate burst (retry helps) AND an **account-balance exhaustion**
(error code ``1113`` / "Insufficient balance or no resource package"), where retry is
futile and the only cures are a recharge or a stand-in backend. We therefore classify
the 429 body: a balance error raises :class:`GLMUnavailableError` IMMEDIATELY (no retry,
clear message); a transient 429/5xx is retried with bounded exponential backoff. The
critic layer (``core/nodes/critic.py``) stands in a healthy backend when GLM raises.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Optional

import aiohttp

from core.config import config


class GLMUnavailableError(RuntimeError):
    """GLM is unusable for this call (balance exhausted, or transient 429/5xx that
    exhausted the retry budget). Distinct from a generic HTTP error so callers — the
    critic stand-in, the health-walk — can route around GLM deterministically."""


# Z.AI balance / quota markers (HTTP 429 body). Retrying these is pointless.
_BALANCE_MARKERS = ("1113", "insufficient balance", "no resource package")


def _is_balance_error(body: str) -> bool:
    low = (body or "").lower()
    return any(m in low for m in _BALANCE_MARKERS)


# Bounded retry for genuinely transient failures (rate burst / 5xx).
_MAX_RETRIES = 2          # total attempts = 1 + _MAX_RETRIES
_BACKOFF_BASE = 0.5       # seconds: 0.5, 1.0 (jitter-free; small, bounded)


class GLMClient:
    """Async client for GLM-5.2's OpenAI-compatible REST API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or config.ZAI_BASE_URL).rstrip("/")
        self.api_key = api_key or config.ZAI_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=120, connect=5)
        self._discover_timeout = aiohttp.ClientTimeout(total=5)

    # ── health & model discovery ───────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Return True when the backend is reachable and usable.

        We first probe ``{base_url}/models`` (OpenAI-compatible discovery). Z.AI's
        ``paas/v4`` surface has been observed to 404 on ``/models``; in that case
        we fall back to a simple key-presence check so a valid key still reads
        healthy.

        NOTE: ``/models`` reachability does NOT detect a balance-exhausted account
        (that 429s only on ``/chat/completions``); the critic stand-in owns that
        negative so a healthy-looking-but-broke key still degrades gracefully.
        """
        if not self.api_key:
            return False
        try:
            async with aiohttp.ClientSession(timeout=self._discover_timeout) as s:
                async with s.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as resp:
                    if resp.status == 200:
                        return True
                    # Z.AI paas/v4 does not always expose /models; treat a 404
                    # as "API base reachable, key is present" rather than down.
                    if resp.status == 404:
                        return True
                    return False
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
            "model": model or config.ZAI_MODEL,
            "messages": messages,
            "stream": False,
            **{k: v for k, v in kwargs.items() if v is not None},
        }
        last_err: Optional[str] = None
        for attempt in range(_MAX_RETRIES + 1):
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                ) as resp:
                    status = getattr(resp, "status", None)
                    # Real responses carry an int status; test mocks leave a MagicMock
                    # here — the isinstance guard keeps mocked success paths unchanged.
                    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
                        body = await resp.text()
                        if status == 429 and _is_balance_error(body):
                            raise GLMUnavailableError(
                                "Z.AI GLM balance/resource exhausted (HTTP 429, code 1113 - "
                                "recharge required; retry is futile): " + body[:200]
                            )
                        last_err = f"HTTP {status}: {body[:200]}"
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    else:
                        resp.raise_for_status()
                        return await resp.json(content_type=None)
            # transient failure — back off and retry unless budget exhausted
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(retry_after if retry_after else _BACKOFF_BASE * (2 ** attempt))
        raise GLMUnavailableError(f"Z.AI GLM transient failure, retries exhausted: {last_err}")

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        payload = {
            "model": model or config.ZAI_MODEL,
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
                status = getattr(resp, "status", None)
                # Surface a balance-exhausted 429 as a clear, distinct error before
                # streaming begins (mocked tests leave a non-int status → unchanged).
                if isinstance(status, int) and status == 429:
                    body = await resp.text()
                    if _is_balance_error(body):
                        raise GLMUnavailableError(
                            "Z.AI GLM balance/resource exhausted (HTTP 429, code 1113 — "
                            "recharge required): " + body[:200]
                        )
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


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a ``Retry-After`` header (delta-seconds form). Returns None when absent or
    not a plain number (HTTP-date form is ignored — we fall back to fixed backoff)."""
    if not value:
        return None
    try:
        secs = float(value.strip())
        return secs if secs >= 0 else None
    except (TypeError, ValueError):
        return None
