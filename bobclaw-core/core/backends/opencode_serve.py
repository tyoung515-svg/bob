"""
BoBClaw Core — OpenCode serve client

Async wrapper around a single ``opencode serve`` HTTP endpoint. Each client instance
is bound to one (host, port).

Speaks the **opencode 1.17 server API** (verified against opencode 1.17.13,
2026-07-03):

* ``POST /session`` (empty body) → ``{"id": ...}``  (the session id is under ``id``;
  the workspace is the serve instance's own cwd, set at launch — NOT a body field).
* ``POST /session/{id}/message`` wants
  ``{"model": {"providerID", "modelID"}, "parts": [{"type": "text", "text"}]}`` and
  returns ``{"info": {...}, "parts": [{"type": "text", "text"}...]}`` — the assistant
  answer is the concatenation of the ``text`` parts.

**The model is PINNED per request** from ``config.OPENCODE_SEAT_MODEL``
(``providerID/modelID``, default the GitHub Copilot GPT seat). ``opencode serve``
does NOT choose a model itself: without this pin it answers with the instance's
config-default model, which here is a LOCAL llama.cpp model — the "opencode silently
serves local" failure. Sending the model on every message guarantees a Copilot GPT
seat is genuine cloud GPT. (An empty ``OPENCODE_SEAT_MODEL`` sends no model = legacy
instance-default behaviour; set empty only deliberately.)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from core.config import config

logger = logging.getLogger(__name__)


def _seat_model() -> Optional[tuple[str, str]]:
    """Parse ``config.OPENCODE_SEAT_MODEL`` (``providerID/modelID``) → (provider, model),
    or None when unset (⇒ send no model, legacy instance-default)."""
    raw = (config.OPENCODE_SEAT_MODEL or "").strip()
    if not raw:
        return None
    provider, _, model = raw.partition("/")
    if not provider or not model:
        logger.warning(
            "OPENCODE_SEAT_MODEL=%r is not 'providerID/modelID'; sending no model "
            "pin (opencode will use its instance-default model)", raw,
        )
        return None
    return provider, model


class OpenCodeServeClient:
    """Async client for a single OpenCode serve instance (opencode 1.17 API)."""

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

    async def create_session(self, workspace_dir: Optional[str] = None) -> str:
        """Create a new session and return its id (1.17: id under ``id``).

        ``workspace_dir`` is accepted for signature compatibility but NOT sent — the
        1.17 server binds the workspace to the serve instance's cwd (set at launch),
        not per session.
        """
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(f"{self.base_url}/session", json={}) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                return data["id"]

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
        """Send a message to an existing session and return the response text.

        Pins the seat model (config.OPENCODE_SEAT_MODEL) so the reply comes from the
        intended cloud model, never a silent instance-default local model.
        """
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        seat = _seat_model()
        if seat:
            body["model"] = {"providerID": seat[0], "modelID": seat[1]}
        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(
                f"{self.base_url}/session/{session_id}/message",
                json=body,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                parts = data.get("parts") or []
                # assistant answer = concat of text parts (skip reasoning/step parts).
                return "\n".join(
                    p.get("text", "") for p in parts if p.get("type") == "text"
                ).strip()

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
            session_id = await self.create_session(workspace_dir)
            # Flatten conversation into a single prompt (OpenCode is prompt-based)
            prompt_text = "\n\n".join(
                f"{m['role'].upper()}: {m['content']}" for m in messages
            )
            response = await self.prompt(session_id, prompt_text)
            return response
        finally:
            if session_id:
                await self.delete_session(session_id)
