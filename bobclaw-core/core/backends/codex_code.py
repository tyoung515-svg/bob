"""Codex CLI backend with legacy and packaged worker execution modes.

Legacy retains CLI output files, spawn briefing and its stripped child environment.
In worker mode the installed profile selects the executable and clean environment;
bounded NDJSON supplies replies and health checks cross the same boundary.
"""
from __future__ import annotations

from core.worker import mode as execution_mode
from core.worker import client as worker_client

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

from core.config import config
from core.spawn_context import SpawnContext, resolve_spawn_context

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


def _is_gpt_model(model: Any) -> bool:
    """True for a native OpenAI/ChatGPT model id (``gpt-5.6-sol`` / ``gpt-5.6-terra`` …).

    These run under the ChatGPT-login ``openai`` provider (the ``gpt`` profile), NOT
    the LiteLLM proxy. GLM / DeepSeek / Qwen / MiniMax ids return False (proxy path,
    now retired in favour of the ``pi_code`` backend)."""
    return str(model or "").lower().startswith("gpt")


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

    _STRIP_FROM_CHILD = (
        "BOBCLAW_SECRET",
        "BOBCLAW_PASSWORD",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    )

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
        if execution_mode() == "legacy":
            os.makedirs(work, exist_ok=True)
        else:
            if os.name == 'nt':
                os.makedirs(work, mode=0o770, exist_ok=True)  # shape-only Windows tests
            else:
                from core.worker.auth_file import directory
                from pathlib import Path
                with directory(Path(work), create=True):
                    pass
        return work

    @staticmethod
    def _is_scratch_write(posture: dict) -> bool:
        return str(posture.get("mode") or "").lower() == "scratch_write" or bool(
            posture.get("read_repo")
        )

    # ── prompt briefing ─────────────────────────────────────────────────────────

    def _brief_prompt(
        self, prompt: str, posture: dict, spawn_ctx: Optional[SpawnContext] = None
    ) -> str:
        """Inline the repo ``CLAUDE.md`` for a briefed/repo-read posture (planner
        tier), and by default steer to a no-tool answer so an unattended spawn
        can't stall. Workers run unbriefed (small prompts). Prompt rides stdin, so
        the ~30 KB briefing is safe (no argv overflow). A CLEAN spawn context
        (descriptor present, ``project_access`` False) suppresses the charter
        inline — that briefing is the leak the spawn-context seam closes."""
        if execution_mode() == "legacy":
            parts: list[str] = []
            clean_spawn = spawn_ctx is not None and not spawn_ctx.project_access
            if not clean_spawn and (posture.get("brief") or self._is_scratch_write(posture)):
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
        else:
            return prompt

    # ── argv construction ──────────────────────────────────────────────────────

    def _build_argv(
        self, *, posture: dict, outfile: str, work_dir: str,
        resume_thread: Optional[str],
        spawn_ctx: Optional[SpawnContext] = None,
    ) -> list[str]:
        # Common flags accepted by BOTH `exec` and `exec resume`.
        if execution_mode() == "legacy":
            common = ["--json", "-o", outfile, "--skip-git-repo-check"]
        else:
            common = ["--json", "--skip-git-repo-check"]
        if resume_thread:
            model = posture.get('model')
            return [self.cli_path, "exec", "resume", str(resume_thread), *common,
                    *(['-m', str(model)] if model else [])]

        argv = [self.cli_path, "exec", *common]

        # BoBClaw codex is GPT-ONLY by construction (Travis 2026-07-03): GLM/DeepSeek/
        # MiniMax agentic work moved to the ``pi_code`` backend. So:
        #   • an explicit profile (gpt | gpt-mini) → ``-p <profile>`` (native openai);
        #   • a GPT model id → ``-p gpt -m <model>`` (native; e.g. gpt-5.6-terra worker);
        #   • a non-GPT model id → legacy ``-m <model> -c model_provider=litellm`` (the
        #     retired proxy path — kept so an explicit legacy call still resolves, but no
        #     face uses it now);
        #   • NEITHER set → default to the native ``gpt`` profile, NEVER the shared
        #     ~/.codex litellm base default (that default is why a bare spawn used to hit
        #     the dead proxy).
        profile = posture.get("profile")
        model = posture.get("model")
        if profile:
            argv += ["-p", str(profile)]
            if model:
                argv += ["-m", str(model)]
        elif model:
            if _is_gpt_model(model):
                argv += ["-p", "gpt", "-m", str(model)]
            else:
                argv += ["-m", str(model), "-c", "model_provider=litellm"]
        else:
            argv += ["-p", "gpt"]

        # Sandbox + working root. Scratch-write reads the repo but writes only the
        # scratch cwd, network OFF (codex defaults network ON under workspace-write).
        # A CLEAN spawn context (project_access False) withholds the repo grant
        # and pins the read-only sandbox (spawn-context seam).
        if self._is_scratch_write(posture) and (
            spawn_ctx is None or spawn_ctx.project_access
        ):
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
        posture = dict(self.posture if posture is None else posture)
        from core.lane_config import configured_mode, get_lane
        if configured_mode():
            lane = get_lane()
            selected = lane.backends.get('codex_code')
            if selected and selected.model and not posture.get('model'):
                posture['model'] = selected.model
            if selected and 'openai_codex' in lane.backends and not posture.get('profile'):
                # Account-visible aliases need not start with "gpt". Keep every
                # subscription choice on the lane's native OpenAI profile.
                posture['profile'] = lane.values.get('CODEX_PROFILE') or 'gpt'
        work_dir = self._work_dir()
        if execution_mode() == "legacy":
            spawn_ctx = self._spawn_context(posture)
        else:
            spawn_ctx = None
        outfile = os.path.join(work_dir, f"codex_out_{os.urandom(6).hex()}.txt")
        argv = self._build_argv(
            posture=posture, outfile=outfile, work_dir=work_dir,
            resume_thread=resume_session_id,
            spawn_ctx=spawn_ctx,
        )
        briefed = self._brief_prompt(prompt, posture, spawn_ctx)
        if execution_mode() == "legacy":
            try:
                stdout, stderr, rc = await self._spawn(
                    argv, cwd=work_dir, stdin_data=briefed,
                    env_overrides=spawn_ctx.env_overrides if spawn_ctx is not None else None,
                )
                events = _parse_events(stdout.decode("utf-8", errors="replace"))
                reply = self._read_outfile(outfile) or events.get("reply") or ""
            finally:
                try:
                    os.remove(outfile)
                except OSError:
                    pass
        else:
            stdout, stderr, rc = await self._spawn(
                argv, cwd=work_dir, stdin_data=briefed,
                env_overrides=spawn_ctx.env_overrides if spawn_ctx is not None else None,
            )
            events = _parse_events(stdout.decode("utf-8", errors="replace"))
            reply = events.get("reply") or ""
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
        """True if ``codex --version`` succeeds.

        codex_code is now GPT-ONLY (native ChatGPT-login ``openai`` provider), so it
        no longer depends on the local LiteLLM proxy — the old ``:4000`` liveness gate
        was retired 2026-07-03 (it was marking codex unhealthy whenever the proxy was
        down, forcing false failover to opencode). A turn-time 429 still raises
        ``CodexThrottled`` → the face's escalation backend.
        """
        if execution_mode() == "legacy":
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
            if proc.returncode != 0 or not stdout.decode("utf-8", errors="replace").strip():
                return False
            return True
        else:
            try:
                receipt = await worker_client.execute_async("codex", args=["--version"], timeout=10)
                return receipt["returncode"] == 0 and bool(receipt["stdout"].strip())
            except (OSError, RuntimeError):
                return False

    async def _spawn(
        self, argv: list[str], cwd: str, stdin_data: Optional[str] = None,
        env_overrides: Optional[dict] = None,
    ) -> tuple[bytes, bytes, Optional[int]]:
        """Spawn ``codex exec``, feed the prompt on stdin, return (stdout, stderr, rc).

        The prompt rides stdin (``communicate(input=...)``), never argv — so a
        large briefing can't overflow the command line. Spawn-context
        ``env_overrides`` layer on top of the F9-stripped env (idempotent for
        CODEX_HOME — the builder derives it from the same config). Raises
        ``CodexError`` on a missing binary or timeout (the child is killed first).
        """
        if execution_mode() == "legacy":
            input_bytes = stdin_data.encode("utf-8") if stdin_data is not None else None
            env = self._subprocess_env()
            if env_overrides:
                env.update(env_overrides)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
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
        else:
            try:
                receipt = await worker_client.execute_async("codex", cwd=cwd, args=argv[1:],
                    input=stdin_data or "", timeout=self.timeout)
            except (OSError, RuntimeError) as exc:
                raise CodexError(str(exc)) from exc
            if receipt.get("killed"):
                raise CodexError("codex job terminated: " + str(receipt.get("reason")))
            return receipt["stdout"], receipt["stderr"], receipt["returncode"]

    @staticmethod
    def _read_outfile(outfile: str) -> str:
        if execution_mode() != "legacy":
            raise worker_client.WorkerUnavailable("unavailable: legacy execution helper")
        try:
            with open(outfile, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _subprocess_env(self) -> dict:
        """Env for the codex child: the parent env MINUS host secrets it has no need for
        (F9), pointed at a segregated CODEX_HOME when configured. Always returns a dict so
        the child never silently inherits the full environment."""
        if execution_mode() != "legacy":
            raise worker_client.WorkerUnavailable("unavailable: legacy execution helper")
        env = {k: v for k, v in os.environ.items() if k not in self._STRIP_FROM_CHILD}
        home = (config.CODEX_HOME or "").strip()
        if home and os.path.isdir(home):
            env["CODEX_HOME"] = home
        return env

    def _spawn_context(self, posture: dict) -> Optional[SpawnContext]:
        """Build the SpawnContext when the posture carries a descriptor.

        The scratch dir is the client's OWN ``_work_dir()`` (the ``-C`` working
        root and where the ``-o`` reply file lands); the builder renders
        AGENTS.md into it and hands back the env overrides.
        """
        if execution_mode() != "legacy":
            raise worker_client.WorkerUnavailable("unavailable: legacy execution helper")
        descriptor = posture.get("spawn_context")
        if not isinstance(descriptor, dict) or not descriptor:
            return None
        return resolve_spawn_context(
            "codex_code",
            posture,
            scratch_dir=self._work_dir(),
            conversation_id=self.conversation_id,
        )
