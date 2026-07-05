"""
BoBClaw Core — Codex CLI subprocess backend (``codex_code``)

Drives the genuine ``codex`` CLI (``codex exec``) as a headless one-shot
subprocess. Two provider paths:

* **GPT, native** — a ``gpt`` profile under a ChatGPT-subscription login (OAuth,
  no API key) runs GPT (e.g. gpt-5.5) directly, with NO proxy (see the
  ``planner-gpt`` face).
* **Non-OpenAI (glm / deepseek / qwen)** — per-provider profiles that route through
  a LOCAL LiteLLM proxy (``LITELLM_BASE_URL``, default ``http://127.0.0.1:4000``)
  translating Codex's Responses API to each provider's Chat Completions (the proxy
  also strips tool defs the provider rejects ⇒ answer-only).

NOT an HTTP/OpenAI-compat client here — transport is
``asyncio.create_subprocess_exec`` (the ``claude_code`` / ``agy_code`` shape).

Codex 0.142+ config: profiles live in per-file ``~/.codex/<profile>.config.toml``
(an inline ``[profiles.x]`` block is rejected as legacy) and custom providers
support only ``wire_api = "responses"``. Kimi is deliberately NOT exposed here — it
has its own ``kimi_cli`` backend (the operator: "Kimi stays in its own CLI").

Contract — EMPIRICALLY DERIVED against codex-cli 0.142.3 (2026-06-29; the locked
facts the code depends on, like the agy contract):

* **Prompt on STDIN.** ``codex exec`` with no positional prompt reads it from
  stdin ("Reading prompt from stdin..."). Unlike agy (stdin MUST be closed),
  codex READS stdin — we feed it via ``communicate(input=...)``.
* **Reply via ``-o <file>``.** ``--output-last-message`` writes the agent's final
  message to a file (deterministic; empty on failure). We also parse ``--json``
  as a fallback / for the thread id + errors.
* **Events on ``--json``** (NDJSON to stdout): ``thread.started{thread_id}`` (the
  RESUME key), ``item.completed{item:{type:"agent_message",text}}``,
  ``turn.completed{usage}``; on failure ``{type:"error",message}`` +
  ``turn.failed{error:{message}}`` (message embeds the provider error JSON with a
  ``code`` like 400/429).
* **Provider via ``-p <profile>``** — ``gpt`` (native ChatGPT login, no proxy) or a
  non-OpenAI profile (``glm`` | ``deepseek`` | ``qwen``) layered on the litellm base
  config; or ``-m <model> -c model_provider=litellm``.
* **Errors / throttle** = non-zero exit + the ``error`` / ``turn.failed`` message;
  a 429 / rate marker raises ``CodexThrottled`` (→ escalation), else ``CodexError``.
* **LiteLLM proxy** at ``LITELLM_BASE_URL`` is required for the non-OpenAI profiles
  (a native ``gpt`` profile does not need it). ``health_check`` is the codex-CLI
  liveness ONLY — it does not probe the proxy, so a native ``gpt`` face is never
  wrongly gated on :4000 under a health-walk; a litellm-routed profile that hits a
  down proxy escalates at runtime instead.

Streaming is message-level (one block) — codex exec buffers the whole reply.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

from core.config import config

logger = logging.getLogger(__name__)


class CodexError(RuntimeError):
    """A codex exec spawn failed, errored, or returned a non-zero exit code."""


class CodexThrottled(CodexError):
    """The codex/litellm path hit a provider rate-limit (429).

    ``execute_node`` branches on this to fall back to the face's
    ``escalation_backend`` (e.g. ``opencode_serve``).
    """


# Throttle markers — ONLY consulted on a non-zero exit / a turn.failed message,
# so a reply that merely discusses "rate limits" cannot false-positive.
_THROTTLE_MARKERS = ("429", "rate limit", "rate_limit", "ratelimit", "too many requests",
                     "quota", "resource exhausted", "throttl", "overloaded", "503", "529")


def _looks_throttled(*parts: Any) -> bool:
    blob = " ".join(str(p) for p in parts if p).lower()
    return any(marker in blob for marker in _THROTTLE_MARKERS)


def _sanitize_conv_id(conv: str) -> str:
    conv = (conv or "").strip()
    return conv.replace("/", "_").replace("\\", "_").replace("..", "_")


def _parse_events(stdout_text: str) -> dict:
    """Parse the ``--json`` NDJSON stream → {thread_id, reply, error, failed}.

    ``reply`` is the last ``agent_message`` text (a fallback for the ``-o`` file).
    ``error`` is the first error / turn.failed message. ``failed`` is True if a
    terminal failure event was seen.
    """
    out: dict = {"thread_id": None, "reply": "", "error": "", "failed": False}
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "thread.started" and ev.get("thread_id"):
            out["thread_id"] = ev["thread_id"]
        elif etype == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                out["reply"] = item["text"]  # keep the last
            elif item.get("type") == "error" and item.get("message") and not out["error"]:
                out["error"] = item["message"]
        elif etype == "error" and ev.get("message"):
            out["error"] = out["error"] or ev["message"]
            out["failed"] = True
        elif etype == "turn.failed":
            err = (ev.get("error") or {}).get("message") or "turn failed"
            out["error"] = out["error"] or err
            out["failed"] = True
    return out


class CodexCodeClient:
    """Subprocess client for the genuine ``codex`` CLI (``codex exec``).

    Parameters mirror ``ClaudeCodeClient`` / ``AntigravityClient``:
    ``cli_path`` (defaults to ``config.CODEX_CLI_PATH`` or ``codex`` on PATH),
    ``cwd`` (the REPO to brief from / grant read), ``posture`` (face policy:
    ``profile`` glm|deepseek|qwen, ``model``, ``mode``/``read_repo`` for
    scratch-write, ``brief``, ``add_dirs``, ``extra_args``), ``timeout``,
    ``conversation_id`` (keys the per-conversation scratch cwd).
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        cwd: Optional[str] = None,
        posture: Optional[dict] = None,
        timeout: Optional[int] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        self.cli_path = cli_path or config.CODEX_CLI_PATH or "codex"
        self.repo = cwd or config.CODEX_PROJECT_DIR
        self.posture = posture or {}
        self.timeout = timeout if timeout is not None else config.CODEX_TIMEOUT_SECONDS
        self.conversation_id = (
            _sanitize_conv_id(conversation_id) if conversation_id else ""
        ) or _sanitize_conv_id(os.urandom(8).hex())
        # codex's thread_id from the last turn — execute.py persists it for resume.
        self.last_session_id: Optional[str] = None

    # ── working dir (per-conversation scratch cwd) ──────────────────────────────

    def _work_dir(self) -> str:
        work = os.path.join(config.CODEX_SCRATCH_ROOT, self.conversation_id or "_default")
        os.makedirs(work, exist_ok=True)
        return work

    @staticmethod
    def _is_scratch_write(posture: dict) -> bool:
        return str(posture.get("mode") or "").lower() == "scratch_write" or bool(
            posture.get("read_repo")
        )

    # F9: host secrets the codex CLI never needs — the gateway<->core vouch key and the
    # metered Anthropic auth (codex talks to the LOCAL LiteLLM proxy, not Anthropic). The
    # previous code inherited the FULL os.environ, leaking these into the codex subprocess.
    _STRIP_FROM_CHILD = (
        "BOBCLAW_SECRET",
        "BOBCLAW_PASSWORD",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    )

    def _subprocess_env(self) -> dict:
        """Env for the codex child: the parent env MINUS host secrets it has no need for
        (F9), pointed at a segregated CODEX_HOME when configured. Always returns a dict so
        the child never silently inherits the full environment."""
        env = {k: v for k, v in os.environ.items() if k not in self._STRIP_FROM_CHILD}
        home = (config.CODEX_HOME or "").strip()
        if home and os.path.isdir(home):
            env["CODEX_HOME"] = home
        return env

    # ── prompt briefing ─────────────────────────────────────────────────────────

    def _brief_prompt(self, prompt: str, posture: dict) -> str:
        """Inline the repo ``CLAUDE.md`` for a briefed/repo-read posture (planner
        tier), and by default steer to a no-tool answer so an unattended spawn
        can't stall. Workers run unbriefed (small prompts). Prompt rides stdin, so
        the ~30 KB briefing is safe (no argv overflow)."""
        parts: list[str] = []
        if posture.get("brief") or self._is_scratch_write(posture):
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
        parts.append(prompt)
        return "\n\n".join(parts)

    # ── argv construction ──────────────────────────────────────────────────────

    def _build_argv(
        self, *, posture: dict, outfile: str, work_dir: str,
        resume_thread: Optional[str],
    ) -> list[str]:
        # Common flags accepted by BOTH `exec` and `exec resume`.
        common = ["--json", "-o", outfile, "--skip-git-repo-check"]

        # `codex exec resume <id>` takes a LIMITED flag set (no -p/-m/-s/-C/--color/
        # --add-dir — empirically verified): the resumed session retains its original
        # provider / sandbox / working root. The new prompt rides stdin.
        if resume_thread:
            return [self.cli_path, "exec", "resume", str(resume_thread), *common]

        argv = [self.cli_path, "exec", *common]

        profile = posture.get("profile")
        model = posture.get("model")
        if profile:
            argv += ["-p", str(profile)]
            # A profile already selects the provider (e.g. `gpt` = native OpenAI /
            # ChatGPT login, NOT the LiteLLM proxy). Honour an explicit model pick
            # WITHIN that profile's provider — do NOT force model_provider=litellm
            # here (that would break gpt-native and re-route it through the proxy).
            # This is what lets a `gpt`-profile face run a *chosen* gpt model
            # (e.g. gpt-5.5) instead of only the profile's default.
            if model:
                argv += ["-m", str(model)]
        elif model:
            # Bare model, no profile = the LiteLLM-routed worker path
            # (glm / deepseek / qwen); force the litellm provider.
            argv += ["-m", str(model), "-c", "model_provider=litellm"]

        # Sandbox + working root. Scratch-write reads the repo but writes only the
        # scratch cwd, network OFF (codex defaults network ON under workspace-write).
        if self._is_scratch_write(posture):
            argv += [
                "-s", "workspace-write", "-C", work_dir,
                "--add-dir", str(self.repo),
                "-c", "sandbox_workspace_write.network_access=false",
            ]
        else:
            argv += ["-s", "read-only", "-C", work_dir]

        add_dirs = posture.get("add_dirs")
        if add_dirs:
            for d in add_dirs if isinstance(add_dirs, (list, tuple)) else [add_dirs]:
                argv += ["--add-dir", str(d)]
        extra = posture.get("extra_args")
        if extra:
            argv += [str(a) for a in extra]
        return argv

    @staticmethod
    def _read_outfile(outfile: str) -> str:
        try:
            with open(outfile, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    # ── non-stream chat ────────────────────────────────────────────────────────

    async def chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Run a one-shot ``codex exec`` and return a normalized dict.

        Returns ``{"text", "session_id", "is_error", "raw"}`` — ``text`` from the
        ``-o`` file (or the last ``agent_message``); ``session_id`` is the codex
        ``thread_id`` (for resume).

        Raises ``CodexThrottled`` on a 429/rate failure, ``CodexError`` on any
        other failure (non-zero exit, no reply, missing binary, timeout).
        """
        posture = self.posture if posture is None else posture
        work_dir = self._work_dir()
        outfile = os.path.join(work_dir, f"codex_out_{os.urandom(6).hex()}.txt")
        argv = self._build_argv(
            posture=posture, outfile=outfile, work_dir=work_dir,
            resume_thread=resume_session_id,
        )
        briefed = self._brief_prompt(prompt, posture)

        try:
            stdout, stderr, rc = await self._spawn(argv, cwd=work_dir, stdin_data=briefed)
            events = _parse_events(stdout.decode("utf-8", errors="replace"))
            reply = self._read_outfile(outfile) or events.get("reply") or ""
        finally:
            try:
                os.remove(outfile)
            except OSError:
                pass

        thread_id = events.get("thread_id") or resume_session_id
        if thread_id:
            self.last_session_id = thread_id

        err = events.get("error") or stderr.decode("utf-8", errors="replace").strip()
        if rc != 0 or events.get("failed"):
            if _looks_throttled(err):
                raise CodexThrottled(f"codex exec throttled (exit {rc}): {err}")
            raise CodexError(f"codex exec error (exit {rc}): {err or '<empty>'}")
        if not reply:
            raise CodexError(
                f"codex exec succeeded but produced no reply: {err or '<empty stderr>'}"
            )

        return {
            "text": reply,
            "session_id": thread_id,
            "is_error": False,
            "raw": {"thread_id": thread_id, "returncode": rc, "stderr": err},
        }

    # ── streaming chat (message-level) ─────────────────────────────────────────

    async def stream_chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        """Run ``codex exec`` and yield the whole reply as one message-level block."""
        result = await self.chat(
            prompt=prompt, resume_session_id=resume_session_id, posture=posture
        )
        text = result["text"]
        if text:
            yield text

    # ── health check ─────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """True iff the codex CLI is present and runnable (``codex --version``).

        The backend's liveness is the CLI, NOT the optional LiteLLM proxy: the native
        ``gpt`` profile (ChatGPT login) needs no proxy, so gating the whole backend on the
        proxy wrongly marked native faces unhealthy under a team health-walk (a false
        negative that stranded planner-gpt whenever :4000 was down). A litellm-routed
        profile (glm/deepseek/qwen) that hits a down proxy fails and escalates at RUNTIME
        via the existing 429/transient chain — the proxy is a per-profile runtime
        dependency, not a backend-liveness signal.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path, "--version",
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
        return proc.returncode == 0 and bool(stdout.decode("utf-8", errors="replace").strip())

    # ── internals ──────────────────────────────────────────────────────────────

    async def _spawn(
        self, argv: list[str], cwd: str, stdin_data: Optional[str] = None,
    ) -> tuple[bytes, bytes, Optional[int]]:
        """Spawn ``codex exec``, feed the prompt on stdin, return (stdout, stderr, rc).

        The prompt rides stdin (``communicate(input=...)``), never argv — so a
        large briefing can't overflow the command line. Raises ``CodexError`` on a
        missing binary or timeout (the child is killed first).
        """
        input_bytes = stdin_data.encode("utf-8") if stdin_data is not None else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._subprocess_env(),
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise CodexError(f"codex CLI not found at {self.cli_path!r}: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_bytes), timeout=self.timeout
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
            raise CodexError(f"codex exec timed out after {self.timeout}s")
        return stdout, stderr, proc.returncode
