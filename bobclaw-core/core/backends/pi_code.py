"""
BoBClaw Core — Pi CLI subprocess backend (``pi_code``)

Drives the genuine ``pi`` coding agent (earendil-works/pi, ``@earendil-works/
pi-coding-agent``) as a headless one-shot subprocess. Pi is the AGENTIC tool-loop
harness for the OPEN providers **GLM / DeepSeek / MiniMax** — it speaks each one's
OpenAI-compatible Chat Completions API DIRECTLY (NO LiteLLM proxy; that is codex's
legacy path, retired in favour of this). Provider/model config lives in
``~/.pi/agent/models.json`` (providers ``zai`` / ``deepseek`` / ``minimax``); the
per-provider API keys are injected into the child env by ``_subprocess_env`` from
``core.config`` — they are NOT read from the user's shell.

Transport is ``asyncio.create_subprocess_exec`` (the ``codex_code`` / ``kimi_cli``
shape), NOT an HTTP client. This backend is the agentic sibling of the direct-HTTP
``glm_5_2`` / ``deepseek_v4_flash`` / ``minimax`` backends: those are plain
one-shot chat; ``pi_code`` runs the read/edit/bash tool loop.

Contract — EMPIRICALLY DERIVED against pi 0.80.3 (2026-07-03; the locked facts the
code depends on, like the codex / kimi / agy contracts):

* **No ``pi.exe``.** npm ships ``pi.cmd`` / ``pi.ps1`` + the JS entry ``dist/cli.js``.
  ``create_subprocess_exec("pi", ...)`` fails on Windows (no exe; .cmd needs a shell).
  We resolve ``node`` + ``dist/cli.js`` and spawn node directly (``_resolve_argv0``).
* **Prompt on ARGV** via ``-p "<prompt>"`` (one-shot). stdin is UNUSED and MUST be
  closed (``DEVNULL``) or pi hangs in init forever (verified: an open console stdin
  blocked pi for >200 s at ~0 CPU). Argv value ⇒ keep prompts bounded (Windows
  ~32 KB cmdline limit; no 30 KB CLAUDE.md briefing here — same rule as ``kimi_cli``).
* **JSON events on ``--mode json``** (NDJSON to stdout): ``session{id}`` (the RESUME
  key + the run id), ``agent_start`` / ``turn_start``, ``message_start`` /
  ``message_update`` (streaming deltas) / ``message_end{message}``, ``turn_end``,
  ``agent_end{messages,willRetry}``. The reply is the LAST ``message_end`` whose
  ``message.role=='assistant'`` — its ``content[]`` ``type=='text'`` parts joined
  (``type=='thinking'`` parts are dropped). ``message.usage`` carries token/cost.
* **``--offline`` DISABLES the inference call** (not just startup net ops) — do NOT
  pass it; the child runs online for the provider call.
* **Model via ``--model <provider>/<id>``** (e.g. ``deepseek/deepseek-v4-flash``,
  ``zai/glm-5.2``, ``minimax/MiniMax-M3``). Threaded from the face/worker as
  ``model_override``; absent ⇒ pi's own default provider (google) — so callers
  SHOULD always pass a model.
* **Tools OFF by default** (``--no-tools``): the stateless worker/seat path wants a
  clean text answer, fast + side-effect-free. A posture ``{"tools": true}`` enables
  the agentic loop (read/edit/bash) in the scratch cwd. ``--no-context-files`` keeps
  AGENTS.md/CLAUDE.md out; ``--no-session`` keeps runs ephemeral (v1).
* **Errors / throttle** = non-zero exit and/or an ``error`` event / ``willRetry`` with
  no assistant reply; a 429 / rate marker raises ``PiThrottled`` (→ escalation), else
  ``PiError``.

Streaming is message-level (one block) — pi buffers the whole reply here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any, AsyncIterator, Optional

from core.config import config

logger = logging.getLogger(__name__)

# Some providers (notably MiniMax) inline their chain-of-thought as a <think>…</think>
# block in the TEXT content rather than a separate reasoning part. pi surfaces that as
# plain text, so strip it here to keep worker output clean (the direct-HTTP `minimax`
# backend strips the same). DOTALL so it spans newlines; also drop a stray unclosed tag.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_STRAY = re.compile(r"</?think>", re.IGNORECASE)


def _strip_think(text: str) -> str:
    return _THINK_STRAY.sub("", _THINK_BLOCK.sub("", text)).strip()


class PiError(RuntimeError):
    """A pi spawn failed, errored, or returned a non-zero exit code."""


class PiThrottled(PiError):
    """The pi/provider path hit a rate-limit (429).

    ``execute_node`` branches on this to fall back to the face's
    ``escalation_backend`` (e.g. the direct-HTTP twin of the same provider).
    """


# Throttle markers — consulted on a non-zero exit / an error event / a no-reply
# turn, so a reply that merely discusses "rate limits" cannot false-positive.
_THROTTLE_MARKERS = ("429", "rate limit", "rate_limit", "ratelimit", "too many requests",
                     "quota", "resource exhausted", "throttl", "overloaded", "503", "529")


def _looks_throttled(*parts: Any) -> bool:
    blob = " ".join(str(p) for p in parts if p).lower()
    return any(marker in blob for marker in _THROTTLE_MARKERS)


def _sanitize_conv_id(conv: str) -> str:
    conv = (conv or "").strip()
    return conv.replace("/", "_").replace("\\", "_").replace("..", "_")


def _extract_text(message: dict) -> str:
    """Join the ``type=='text'`` parts of a pi assistant message's content[]."""
    content = message.get("content")
    if isinstance(content, str):
        return _strip_think(content)
    if not isinstance(content, list):
        return ""
    parts = [
        p.get("text", "")
        for p in content
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    return _strip_think("\n".join(t for t in parts if t))


def _parse_pi_events(stdout_text: str) -> dict:
    """Parse the ``--mode json`` NDJSON stream → {session_id, reply, error, failed}.

    ``reply`` = the last ``message_end`` assistant message's text parts (falling back
    to the final ``agent_end`` assistant message). ``session_id`` = the ``session``
    event id. ``error``/``failed`` from an ``error`` event or an empty ``willRetry``
    terminal.
    """
    out: dict = {"session_id": None, "reply": "", "error": "", "failed": False}
    agent_end_reply = ""
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
        etype = ev.get("type")
        if etype == "session" and ev.get("id"):
            out["session_id"] = str(ev["id"])
        elif etype == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                text = _extract_text(msg)
                if text:
                    out["reply"] = text  # keep the last assistant block
        elif etype == "agent_end":
            msgs = ev.get("messages") or []
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "assistant":
                    agent_end_reply = _extract_text({"content": m.get("content")})
                    break
            if ev.get("willRetry"):
                out["failed"] = True
        elif etype == "error":
            out["error"] = out["error"] or (ev.get("message") or ev.get("error") or "pi error")
            out["failed"] = True
    if not out["reply"] and agent_end_reply:
        out["reply"] = agent_end_reply
    return out


class PiCodeClient:
    """Subprocess client for the genuine ``pi`` coding agent (headless ``pi -p``).

    Parameters mirror the other CLI backends. ``posture``: ``model`` (a
    ``provider/id`` string), ``tools`` (bool — enable the agentic loop), ``cwd``
    override, ``extra_args``. ``conversation_id`` keys the per-conversation scratch
    cwd; pi mints its own ``session id`` which we capture from the stream.
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        cwd: Optional[str] = None,
        posture: Optional[dict] = None,
        timeout: Optional[int] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        # cli_path, when set, is the path to pi's dist/cli.js (spawned via node).
        self.cli_path = cli_path or config.PI_CLI_PATH or ""
        self.repo = cwd or config.PI_PROJECT_DIR
        self.posture = posture or {}
        self.timeout = timeout if timeout is not None else config.PI_TIMEOUT_SECONDS
        self.conversation_id = (
            _sanitize_conv_id(conversation_id) if conversation_id else ""
        ) or _sanitize_conv_id(os.urandom(8).hex())
        self.last_session_id: Optional[str] = None

    # ── invocation resolution (no pi.exe: spawn node + cli.js) ──────────────────

    @staticmethod
    def _resolve_cli_js(explicit: str) -> Optional[str]:
        """Locate pi's dist/cli.js. Order: explicit config path → next to the ``pi``
        shim on PATH → None (caller falls back to a bare ``pi`` spawn attempt)."""
        if explicit and explicit.lower().endswith(".js") and os.path.isfile(explicit):
            return explicit
        shim = shutil.which("pi") or shutil.which("pi.cmd")
        if shim:
            base = os.path.dirname(shim)
            cand = os.path.join(
                base, "node_modules", "@earendil-works", "pi-coding-agent", "dist", "cli.js"
            )
            if os.path.isfile(cand):
                return cand
        return None

    def _resolve_argv0(self) -> list[str]:
        """Return the argv prefix to launch pi: ``[node, cli.js]`` when resolvable,
        else a bare ``["pi"]`` (best-effort; only works if a real ``pi`` exe exists)."""
        cli_js = self._resolve_cli_js(self.cli_path)
        if cli_js:
            node = shutil.which("node") or "node"
            return [node, cli_js]
        return [self.cli_path or "pi"]

    # ── env (inject provider keys; strip host secrets pi never needs) ───────────

    _STRIP_FROM_CHILD = ("BOBCLAW_SECRET", "BOBCLAW_PASSWORD", "ANTHROPIC_AUTH_TOKEN")

    def _subprocess_env(self) -> dict:
        """Parent env MINUS host secrets pi has no need for, PLUS the three provider
        keys pi's models.json references (``$ZAI_API_KEY`` / ``$DEEPSEEK_API_KEY`` /
        ``$MINIMAX_API_KEY``). Always a fresh dict so the child never silently inherits
        a surprising environment."""
        env = {k: v for k, v in os.environ.items() if k not in self._STRIP_FROM_CHILD}
        if config.ZAI_API_KEY:
            env["ZAI_API_KEY"] = config.ZAI_API_KEY
        if config.DEEPSEEK_API_KEY:
            env["DEEPSEEK_API_KEY"] = config.DEEPSEEK_API_KEY
        if config.MINIMAX_API_KEY:
            env["MINIMAX_API_KEY"] = config.MINIMAX_API_KEY
        return env

    # ── working dir (per-conversation scratch cwd) ──────────────────────────────

    def _work_dir(self) -> str:
        if self.posture.get("cwd"):
            return str(self.posture["cwd"])
        work = os.path.join(config.PI_SCRATCH_ROOT, self.conversation_id or "_default")
        os.makedirs(work, exist_ok=True)
        return work

    # ── argv construction ──────────────────────────────────────────────────────

    def _build_argv(self, prompt: str, posture: dict, resume_session: Optional[str]) -> list[str]:
        argv = [*self._resolve_argv0(), "--mode", "json", "--no-context-files"]
        # Tools OFF unless a posture opts into the agentic loop.
        if not posture.get("tools"):
            argv.append("--no-tools")
        # Ephemeral unless a caller wants a persisted/resumable session.
        if resume_session:
            argv += ["--session", str(resume_session)]
        else:
            argv.append("--no-session")
        model = posture.get("model")
        if model:
            argv += ["--model", str(model)]
        extra = posture.get("extra_args")
        if extra:
            argv += [str(a) for a in extra]
        # -p + the prompt LAST (argv value; keep bounded).
        argv += ["-p", prompt]
        return argv

    # ── non-stream chat ────────────────────────────────────────────────────────

    async def chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Run a one-shot headless ``pi`` and return a normalized dict.

        Returns ``{"text", "session_id", "is_error", "raw"}``. Raises
        ``PiThrottled`` on a throttled failure, ``PiError`` on any other failure
        (non-zero exit, no reply, missing binary, timeout).
        """
        posture = self.posture if posture is None else posture
        work_dir = self._work_dir()
        argv = self._build_argv(prompt, posture, resume_session_id)
        stdout, stderr, rc = await self._spawn(argv, cwd=work_dir)
        events = _parse_pi_events(stdout.decode("utf-8", errors="replace"))
        reply = events.get("reply") or ""
        session_id = events.get("session_id") or resume_session_id
        if session_id:
            self.last_session_id = session_id

        err = events.get("error") or stderr.decode("utf-8", errors="replace").strip()
        if rc != 0 or (events.get("failed") and not reply):
            if _looks_throttled(err, reply):
                raise PiThrottled(f"pi throttled (exit {rc}): {err or '<no reply>'}")
            raise PiError(f"pi error (exit {rc}): {err or '<empty>'}")
        if not reply:
            raise PiError(
                f"pi succeeded but produced no reply: {err or '<empty stderr>'}"
            )
        return {
            "text": reply,
            "session_id": session_id,
            "is_error": False,
            "raw": {"session_id": session_id, "returncode": rc, "stderr": err},
        }

    # ── streaming chat (message-level) ─────────────────────────────────────────

    async def stream_chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        """Run ``pi`` and yield the whole reply as one message-level block."""
        result = await self.chat(
            prompt=prompt, resume_session_id=resume_session_id, posture=posture
        )
        text = result["text"]
        if text:
            yield text

    # ── health check ─────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """True if ``pi --version`` succeeds (no turn, no provider spend)."""
        argv = [*self._resolve_argv0(), "--version"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
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

    async def _spawn(self, argv: list[str], cwd: str) -> tuple[bytes, bytes, Optional[int]]:
        """Spawn pi with stdin CLOSED (DEVNULL — an open stdin hangs pi), enforce the
        timeout, return (stdout, stderr, rc). Raises ``PiError`` on a missing binary
        or timeout (the child is killed first)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._subprocess_env(),
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise PiError(f"pi CLI not found ({argv[0]!r}): {exc}") from exc

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
            raise PiError(f"pi timed out after {self.timeout}s")
        return stdout, stderr, proc.returncode
