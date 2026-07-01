"""
BoBClaw Core — Antigravity (agy) subprocess backend

The ``agy_code`` backend is to Gemini what ``claude_code`` is to Claude: a
connected, subscription-driven tier driven through the genuine ``agy`` CLI
(Google Antigravity / Gemini) as a headless one-shot subprocess. There is no
aiohttp, no API key, and no ``/v1`` endpoint — transport is
``asyncio.create_subprocess_exec``. (The metered Gemini REST path — the
``google-antigravity`` SDK and the ``gemini_pro`` backend — both require
``GEMINI_API_KEY`` and are SEPARATE from this subscription tier.)

Contract — EMPIRICALLY DERIVED against agy v1.0.13 (2026-06-28). The agy
agent's self-reported contract was wrong on several points; these are the
verified facts the code depends on:

* **stdin MUST be closed** (``stdin=DEVNULL``). Otherwise ``agy -p`` blocks
  forever in init, before the turn even starts (``--print-timeout`` never
  fires). This is the #1 gotcha.
* **The reply is NOT on stdout.** When stdout is piped (always, for a
  subprocess), agy writes nothing useful to stdout. The model's answer lands in
  the transcript: ``<home>/.gemini/antigravity-cli/brain/<uuid>/.system_generated/
  logs/transcript.jsonl`` — the last ``source=="MODEL"`` step's ``content``.
* **agy owns the conversation id.** ``--conversation <id>`` RESUMES an existing
  id; it does NOT create one with a chosen id. A fresh turn mints a UUID. We
  recover it from ``<home>/.gemini/antigravity-cli/cache/last_conversations.json``,
  which maps ``{cwd: uuid}`` — so we run each conversation from its own cwd
  (the per-conversation scratch dir) and read the uuid back by cwd key. This is
  concurrency-safe (distinct conversation ⇒ distinct cwd ⇒ distinct key).
* **Errors / throttle = exit code + stderr** (no structured stdout). A non-zero
  exit whose stderr matches a throttle marker raises ``AgyThrottled``.
* **Segregated home.** Each spawn runs with ``USERPROFILE=AGY_HOME`` (when set
  AND seeded) so agy reads a BoBClaw-owned ``~/.gemini`` — keeping any strict
  posture OFF the user's own interactive agy. Auth carries via the seeded home.

Streaming is message-level (one block) — agy buffers the whole reply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

from core.config import config

logger = logging.getLogger(__name__)


class AgyError(RuntimeError):
    """An agy spawn failed, errored, or returned a non-zero exit code."""


class AgyThrottled(AgyError):
    """The agy CLI hit a subscription quota / rate-limit.

    ``execute_node`` branches on this to fall back to the face's
    ``escalation_backend`` (= ``gemini_pro``, the metered REST twin).
    """


# Throttle markers scraped from stderr — ONLY consulted on a non-zero exit code,
# so a reply that merely discusses "rate limits" cannot false-positive.
_THROTTLE_MARKERS = (
    "429",
    "quota exceeded",
    "resource exhausted",
    "rate limit",
    "ratelimit",
    "throttl",
)

# transcript step source for the model's answer.
_MODEL_SOURCE = "MODEL"

# agy may not have flushed last_conversations.json / the transcript the instant
# the process exits — poll briefly before giving up (handles the exit-0 capture
# race, esp. under a shared AGY_HOME with concurrent fan-out spawns).
_CAPTURE_RETRIES = 6
_CAPTURE_RETRY_DELAY = 0.2

# Defensive argv-length guard (P2 parity sweep). agy is MORE exposed than claude:
# it passes the prompt as an argv VALUE (`agy -p <prompt>`), and a brief:true planner
# inlines the ~30 KB CLAUDE.md into that prompt — so it can overflow the OS
# command-line limit (Windows ~32 KB → WinError 206 / E2BIG). Fail LOUD before spawn.
_ARGV_BYTE_LIMIT = 32_000 if os.name == "nt" else 2_000_000


def _argv_byte_length(argv: list[str]) -> int:
    """Approx command-line byte length (each arg + a separator)."""
    return sum(len(str(a).encode("utf-8")) + 1 for a in argv)


def _looks_throttled(*parts: Any) -> bool:
    blob = " ".join(str(p) for p in parts if p).lower()
    return any(marker in blob for marker in _THROTTLE_MARKERS)


def _sanitize_conv_id(conv: str) -> str:
    """Strip path separators / traversal from a conversation id (used as a dir name)."""
    conv = (conv or "").strip()
    return conv.replace("/", "_").replace("\\", "_").replace("..", "_")


def _reply_from_transcript(text: str) -> str:
    """Extract the model's answer from a transcript.jsonl body.

    Returns the ``content`` of the LAST ``source=="MODEL"`` step with non-empty
    string content. SYSTEM steps (e.g. CHECKPOINT summaries) are ignored.
    """
    reply = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            step = json.loads(line)
        except json.JSONDecodeError:
            continue
        if step.get("source") != _MODEL_SOURCE:
            continue
        content = step.get("content")
        if isinstance(content, str) and content.strip():
            reply = content.strip()  # keep the last one
    return reply


class AntigravityClient:
    """Subprocess client for the genuine ``agy`` CLI (headless ``-p`` mode).

    Parameters
    ----------
    cli_path:
        Path to the ``agy`` binary. Defaults to ``config.AGY_CLI_PATH`` (an
        ABSOLUTE path — agy is not on PATH).
    cwd:
        The REPO directory to brief from (``CLAUDE.md``) and grant read access
        to. Defaults to ``config.AGY_PROJECT_DIR``. NOTE: this is NOT the spawn
        cwd — the spawn runs from the per-conversation scratch dir (the capture
        key); the repo is mounted read via ``--add-dir`` when the posture asks.
    posture:
        Face policy: ``model``, ``mode`` (``scratch_write`` ⇒ read the repo),
        ``add_dirs``, ``allow_tools`` (when False/absent the prompt is steered to
        a no-tool answer so an unattended turn can't block on a permission prompt).
    timeout:
        Per-spawn wall-clock timeout (s). Defaults to ``config.AGY_TIMEOUT_SECONDS``.
    conversation_id:
        BoBClaw conversation id — keys the per-conversation scratch dir / cwd, so
        agy's generated UUID can be recovered by cwd. Defaults to a fresh uuid for
        a stateless / fan-out client (distinct cwd ⇒ no capture race).
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        cwd: Optional[str] = None,
        posture: Optional[dict] = None,
        timeout: Optional[int] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        self.cli_path = cli_path or config.AGY_CLI_PATH or "agy"
        self.repo = cwd or config.AGY_PROJECT_DIR
        self.posture = posture or {}
        self.timeout = timeout if timeout is not None else config.AGY_TIMEOUT_SECONDS
        self.conversation_id = (
            _sanitize_conv_id(conversation_id) if conversation_id else ""
        ) or _sanitize_conv_id(os.urandom(8).hex())
        # The agy-generated UUID captured after the last turn — read by execute.py
        # to persist the session_id -> conversation_id sidecar mapping (resume).
        self.last_session_id: Optional[str] = None

    # ── home / state-file paths (segregated home) ───────────────────────────────

    def _home_dir(self) -> str:
        """The home agy reads ``~/.gemini`` from: ``AGY_HOME`` if seeded, else real."""
        home = (config.AGY_HOME or "").strip()
        if home and os.path.isdir(home):
            return home
        return os.path.expanduser("~")

    def _cli_dir(self) -> str:
        return os.path.join(self._home_dir(), ".gemini", "antigravity-cli")

    def _subprocess_env(self) -> Optional[dict]:
        """Env override pointing agy at the segregated home (or None to inherit)."""
        home = (config.AGY_HOME or "").strip()
        if home and os.path.isdir(home):
            return {**os.environ, "USERPROFILE": home}
        return None

    # ── working dir (the capture key) ───────────────────────────────────────────

    def _work_dir(self) -> str:
        """Per-conversation spawn cwd ``AGY_SCRATCH_ROOT/<conversation_id>``.

        This is the agy working dir AND the key under which agy records the
        conversation uuid in ``cache/last_conversations.json``. Created on demand.
        """
        work = os.path.join(config.AGY_SCRATCH_ROOT, self.conversation_id or "_default")
        os.makedirs(work, exist_ok=True)
        return work

    @staticmethod
    def _is_repo_read(posture: dict) -> bool:
        return str(posture.get("mode") or "").lower() == "scratch_write" or bool(
            posture.get("read_repo")
        )

    # ── uuid capture + reply read ───────────────────────────────────────────────

    def _capture_uuid(self, work_dir: str) -> Optional[str]:
        """Recover agy's conversation uuid for ``work_dir`` from last_conversations.json.

        The file maps ``{cwd: uuid}``; we match on a normcase/normpath basis since
        agy may canonicalize the path. Best-effort: returns None if the file is
        missing or unparseable (a torn read mid-write).
        """
        cache = os.path.join(self._cli_dir(), "cache", "last_conversations.json")
        try:
            with open(cache, "r", encoding="utf-8", errors="replace") as fh:
                mapping = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(mapping, dict):
            return None
        target = os.path.normcase(os.path.normpath(work_dir))
        for key, uuid in mapping.items():
            if os.path.normcase(os.path.normpath(str(key))) == target:
                return str(uuid)
        return None

    def _read_reply(self, uuid: str) -> str:
        """Read the model's answer from ``brain/<uuid>/.../transcript.jsonl``."""
        transcript = os.path.join(
            self._cli_dir(),
            "brain",
            uuid,
            ".system_generated",
            "logs",
            "transcript.jsonl",
        )
        try:
            with open(transcript, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            return ""
        return _reply_from_transcript(body)

    # ── prompt briefing ─────────────────────────────────────────────────────────

    def _brief_prompt(self, prompt: str, posture: dict) -> str:
        """Inline the repo ``CLAUDE.md`` (repo-read posture) and, by default, steer
        the turn to a no-tool answer so an unattended spawn cannot block on a
        permission prompt (the strict tool-deny settings schema is an A2 follow-up).
        """
        parts: list[str] = []
        # ``brief`` inlines CLAUDE.md WITHOUT granting tool/--add-dir access, so a
        # planner can be project-aware while staying a safe no-tool turn.
        if posture.get("brief") or self._is_repo_read(posture):
            claude_md = os.path.join(self.repo, "CLAUDE.md")
            try:
                with open(claude_md, "r", encoding="utf-8", errors="replace") as fh:
                    briefing = fh.read().strip()
            except OSError:
                briefing = ""
            if briefing:
                parts.append(
                    f'<project-briefing source="CLAUDE.md">\n{briefing}\n</project-briefing>'
                )
        if not posture.get("allow_tools"):
            parts.append(
                "Answer directly from the information given. Do NOT call any "
                "tools, run any commands, or read/write any files."
            )
        parts.append(prompt)
        return "\n\n".join(parts)

    # ── argv construction ──────────────────────────────────────────────────────

    def _build_argv(
        self, prompt: str, *, posture: dict, resume_uuid: Optional[str]
    ) -> list[str]:
        argv = [self.cli_path, "-p", prompt]
        if resume_uuid:
            # Resume an existing agy conversation by its uuid.
            argv += ["--conversation", str(resume_uuid)]
        model = posture.get("model")
        if model:
            argv += ["--model", str(model)]
        if self._is_repo_read(posture):
            argv += ["--add-dir", str(self.repo)]
        add_dirs = posture.get("add_dirs")
        if add_dirs:
            for d in add_dirs if isinstance(add_dirs, (list, tuple)) else [add_dirs]:
                argv += ["--add-dir", str(d)]
        extra = posture.get("extra_args")
        if extra:
            argv += [str(a) for a in extra]
        return argv

    # ── non-stream chat ────────────────────────────────────────────────────────

    async def chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Run a one-shot ``agy -p`` and return a normalized dict.

        Returns ``{"text", "session_id", "is_error", "raw"}`` — ``text`` is read
        from the transcript; ``session_id`` is agy's conversation uuid (for resume).

        Raises ``AgyThrottled`` on a throttled non-zero exit, ``AgyError`` on any
        other failure (non-zero exit, missing uuid/reply, missing binary, timeout).
        """
        posture = self.posture if posture is None else posture
        work_dir = self._work_dir()
        argv = self._build_argv(
            self._brief_prompt(prompt, posture),
            posture=posture,
            resume_uuid=resume_session_id,
        )

        stdout, stderr, returncode = await self._spawn(argv, cwd=work_dir)
        err = stderr.decode("utf-8", errors="replace").strip()

        if returncode != 0:
            if _looks_throttled(err):
                raise AgyThrottled(f"agy CLI rate-limited (exit {returncode}): {err}")
            raise AgyError(
                f"agy CLI error (exit {returncode}): {err or '<empty stderr>'}"
            )

        # Bounded poll: the uuid (last_conversations.json) and the reply
        # (transcript.jsonl) may not be flushed the instant the process exits.
        uuid = resume_session_id
        reply = ""
        for attempt in range(_CAPTURE_RETRIES):
            if not uuid:
                uuid = self._capture_uuid(work_dir)
            if uuid:
                reply = self._read_reply(uuid)
                if reply:
                    break
            if attempt < _CAPTURE_RETRIES - 1:
                await asyncio.sleep(_CAPTURE_RETRY_DELAY)

        if not uuid:
            raise AgyError(
                "agy CLI succeeded but no conversation uuid was recorded "
                f"for cwd {work_dir!r} (last_conversations.json)"
            )
        if not reply:
            raise AgyError(
                f"agy CLI succeeded but no model reply found in transcript for "
                f"uuid {uuid!r}: {err or '<empty stderr>'}"
            )

        self.last_session_id = uuid
        return {
            "text": reply,
            "session_id": uuid,
            "is_error": False,
            "raw": {"uuid": uuid, "stderr": err, "returncode": returncode},
        }

    # ── streaming chat (message-level) ─────────────────────────────────────────

    async def stream_chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        """Run ``agy -p`` and yield the whole reply as one message-level block."""
        result = await self.chat(
            prompt=prompt, resume_session_id=resume_session_id, posture=posture
        )
        text = result["text"]
        if text:
            yield text

    # ── health check (no network) ──────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Return True if ``agy --version`` succeeds (no turn, no quota burn)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path,
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.repo,
                env=self._subprocess_env(),
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

    # ── internals ──────────────────────────────────────────────────────────────

    async def _spawn(
        self, argv: list[str], cwd: str
    ) -> tuple[bytes, bytes, Optional[int]]:
        """Spawn the CLI with **stdin closed** (else it hangs), enforce the timeout,
        return (stdout, stderr, returncode). Injects the segregated home env.
        """
        # Defensive: fail loud BEFORE spawning if the argv (which INCLUDES the -p
        # prompt for agy) would overflow the OS command-line limit.
        if _argv_byte_length(argv) > _ARGV_BYTE_LIMIT:
            raise AgyError(
                f"agy argv is {_argv_byte_length(argv)} bytes, over the "
                f"~{_ARGV_BYTE_LIMIT}-byte command-line limit (would WinError 206 / "
                f"E2BIG); agy reads the prompt from argv (-p), and brief:true inlines "
                f"CLAUDE.md — shrink the prompt / drop brief, or an --add-dir is oversized"
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,  # CRITICAL: agy -p hangs otherwise
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._subprocess_env(),
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise AgyError(f"agy CLI not found at {self.cli_path!r}: {exc}") from exc
        except OSError as exc:
            # A spawn OSError that is NOT a missing binary (e.g. WinError 206 / E2BIG
            # if the guard's limit is mis-tuned). Distinguish from "not found".
            raise AgyError(
                f"agy CLI spawn failed ({type(exc).__name__}: {exc}); if this is "
                f"WinError 206 / 'command line too long', the -p prompt or an "
                f"--add-dir arg is oversized"
            ) from exc

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
            raise AgyError(f"agy CLI timed out after {self.timeout}s")
        return stdout, stderr, proc.returncode
