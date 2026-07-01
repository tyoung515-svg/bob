"""
BoBClaw Core — OpenCode serve client

Async wrapper around a single opencode serve HTTP endpoint.
Each client instance is bound to one (host, port).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from core.config import config

logger = logging.getLogger(__name__)


class OpenCodeServeClient:
    """Async client for a single OpenCode serve instance."""

    def __init__(self, host: str, port: int) -> None:
        self.base_url = f"http://{host}:{port}"
        self._timeout = aiohttp.ClientTimeout(
            total=config.OPENCODE_DEFAULT_TIMEOUT_S, connect=5
        )

    # ── health ─────────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Return True if the OpenCode serve instance is reachable."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.get(f"{self.base_url}/") as resp:
                    return resp.status == 200
        except Exception:
            return False

    # ── session lifecycle ──────────────────────────────────────────────────────

    async def create_session(self, workspace_dir: str) -> str:
        """Create a new session and return its id."""
        payload = {"workspace_dir": workspace_dir}
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/session", json=payload
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                return data["session_id"]

    async def delete_session(self, session_id: str) -> None:
        """Best-effort session cleanup. Log but don't raise on failure."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.delete(
                    f"{self.base_url}/session/{session_id}"
                ) as resp:
                    pass
        except Exception:
            logger.debug(
                "Best-effort opencode session cleanup failed for session_id=%r",
                session_id,
                exc_info=True,
            )

    # ── prompt ─────────────────────────────────────────────────────────────────

    async def prompt(self, session_id: str, text: str) -> str:
        """Send a message to an existing session and return the response text."""
        payload = {"text": text}
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/session/{session_id}/message",
                json=payload,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                return data["text"]

    # ── chat adapter ───────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, str]],
        workspace_dir: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Ephemeral session adapter: create, send conversation, read response, cleanup.

        Returns the assistant's response text.
        """
        session_id = ""
        try:
            session_id = await self.create_session(workspace_dir or ".")
            # Flatten conversation into a single prompt (OpenCode is prompt-based)
            prompt_text = "\n\n".join(
                f"{m['role'].upper()}: {m['content']}" for m in messages
            )
            response = await self.prompt(session_id, prompt_text)
            return response
        finally:
            if session_id:
                await self.delete_session(session_id)
