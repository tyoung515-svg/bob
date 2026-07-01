"""
BoBClaw Core — Kimi CLI subprocess backend (``kimi_cli``)

Drives the genuine ``kimi`` CLI (``kimi -p``) as a headless one-shot subprocess
under the Kimi membership login. DISTINCT from ``kimi_code`` (the HTTP membership
API client) and from codex's ``-p kimi`` — the operator: "Kimi stays in its own CLI,
it's built for it." ``kimi_code`` (HTTP) is the natural escalation when the CLI
is unavailable.

Contract — EMPIRICALLY DERIVED against kimi 0.17.1 (2026-06-29):

* ``kimi -p "<prompt>" --output-format stream-json`` — run ONE prompt
  non-interactively. The prompt is an ARGV VALUE (not stdin), so keep prompts
  bounded (no 30 KB CLAUDE.md briefing → no Windows argv overflow). stdin is
  unused → closed (``DEVNULL``).
* stdout = NDJSON: ``{"role":"assistant","content":...}`` (the reply) +
  ``{"role":"meta","type":"session.resume_hint","session_id":...}`` (the RESUME
  key). The reasoning trace + the resume hint also go to stderr in text mode —
  we use stream-json so the reply is machine-readable.
* resume: ``kimi -r <session_id>``. model: ``-m <alias>``.
* ``-p`` EXCLUDES ``-y``/``--yolo`` ("Cannot combine --prompt with --yolo" — ``-p``
  is already non-interactive).
* errors / throttle = non-zero exit + stderr markers (a 429/rate marker raises
  ``KimiCliThrottled`` → escalation).

Streaming is message-level (one block) — kimi buffers the whole reply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any, AsyncIterator, Optional

from core.config import config

logger = logging.getLogger(__name__)


class KimiCliError(RuntimeError):
    """A kimi CLI spawn failed, errored, or returned a non-zero exit code."""


class KimiCliThrottled(KimiCliError):
    """The kimi CLI hit a membership rate-limit (Allegretto 5-hour window / 429).

    ``execute_node`` branches on this to fall back to the face's
    ``escalation_backend`` (e.g. ``kimi_code``, the HTTP membership twin).
    """


_THROTTLE_MARKERS = ("429", "rate limit", "rate_limit", "ratelimit",
                     "too many requests", "quota", "throttl")


def _looks_throttled(*parts: Any) -> bool:
    blob = " ".join(str(p) for p in parts if p).lower()
    return any(marker in blob for marker in _THROTTLE_MARKERS)


def _parse_kimi_stream(stdout_text: str) -> tuple[str, Optional[str]]:
    """Parse the ``--output-format stream-json`` NDJSON → (reply, session_id).

    ``reply`` is the last ``{"role":"assistant"}`` content; ``session_id`` is the
    ``session.resume_hint`` meta event's id.
    """
    reply = ""
    session_id: Optional[str] = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        role = ev.get("role")
        if role == "assistant" and isinstance(ev.get("content"), str):
            reply = ev["content"]  # keep the last assistant block
        elif (
            role == "meta"
            and ev.get("type") == "session.resume_hint"
            and ev.get("session_id")
        ):
            session_id = str(ev["session_id"])
    return reply, session_id


class KimiCliClient:
    """Subprocess client for the genuine ``kimi`` CLI (``kimi -p``).

    Parameters mirror the other CLI backends. ``posture``: ``model`` (a kimi model
    alias), ``extra_args``. ``conversation_id`` is carried for the resume sidecar
    (CX-3) — kimi mints its own ``session_id`` which we capture from the reply.
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        cwd: Optional[str] = None,
        posture: Optional[dict] = None,
        timeout: Optional[int] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        self.cli_path = cli_path or config.KIMI_CLI_PATH or shutil.which("kimi") or "kimi"
        self.cwd = cwd or config.KIMI_CLI_PROJECT_DIR
        self.posture = posture or {}
        self.timeout = timeout if timeout is not None else config.KIMI_CLI_TIMEOUT_SECONDS
        self.conversation_id = conversation_id
        self.last_session_id: Optional[str] = None

    def _build_argv(
        self, prompt: str, posture: dict, resume_session: Optional[str]
    ) -> list[str]:
        # NOTE: the prompt is an argv VALUE (kimi reads it from -p). Keep callers'
        # prompts bounded — no inlined briefing (that would overflow the cmdline).
        argv = [self.cli_path, "-p", prompt, "--output-format", "stream-json"]
        model = posture.get("model")
        if model:
            argv += ["-m", str(model)]
        if resume_session:
            argv += ["-r", str(resume_session)]
        extra = posture.get("extra_args")
        if extra:
            argv += [str(a) for a in extra]
        return argv

    async def chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Run a one-shot ``kimi -p`` and return a normalized dict.

        Returns ``{"text", "session_id", "is_error", "raw"}``. Raises
        ``KimiCliThrottled`` on a throttled non-zero exit, ``KimiCliError`` on any
        other failure (non-zero exit, no reply, missing binary, timeout).
        """
        posture = self.posture if posture is None else posture
        argv = self._build_argv(prompt, posture, resume_session_id)
        stdout, stderr, rc = await self._spawn(argv)
        err = stderr.decode("utf-8", errors="replace").strip()

        if rc != 0:
            if _looks_throttled(err):
                raise KimiCliThrottled(f"kimi CLI rate-limited (exit {rc}): {err}")
            raise KimiCliError(f"kimi CLI error (exit {rc}): {err or '<empty stderr>'}")

        reply, session_id = _parse_kimi_stream(stdout.decode("utf-8", errors="replace"))
        if session_id:
            self.last_session_id = session_id
        if not reply:
            raise KimiCliError(
                f"kimi CLI succeeded but produced no assistant reply: "
                f"{err or '<empty stderr>'}"
            )
        return {
            "text": reply,
            "session_id": session_id,
            "is_error": False,
            "raw": {"session_id": session_id, "returncode": rc, "stderr": err},
        }

    async def stream_chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        """Run ``kimi -p`` and yield the whole reply as one message-level block."""
        result = await self.chat(
            prompt=prompt, resume_session_id=resume_session_id, posture=posture
        )
        text = result["text"]
        if text:
            yield text

    async def health_check(self) -> bool:
        """True if ``kimi --version`` succeeds (no turn, no quota burn)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path, "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
        except (FileNotFoundError, NotADirectoryError, OSError):
            return False
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return False
        if proc.returncode != 0:
            return False
        return bool(stdout.decode("utf-8", errors="replace").strip())

    async def _spawn(self, argv: list[str]) -> tuple[bytes, bytes, Optional[int]]:
        """Spawn ``kimi -p`` with stdin CLOSED (one-shot; the prompt is an argv
        value), enforce the timeout, return (stdout, stderr, rc). Raises
        ``KimiCliError`` on a missing binary or timeout (the child is killed first).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise KimiCliError(f"kimi CLI not found at {self.cli_path!r}: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
            raise KimiCliError(f"kimi CLI timed out after {self.timeout}s")
        return stdout, stderr, proc.returncode
