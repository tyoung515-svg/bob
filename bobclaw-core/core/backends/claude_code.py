"""
BoBClaw Core — Claude Code subprocess backend

Unlike the other backends in this package, ``claude_code`` is NOT an HTTP/
OpenAI-compat client. It drives the genuine ``claude`` CLI under the user's
single-seat subscription login as a headless one-shot subprocess
(``claude -p ... --output-format json``). There is no aiohttp, no API key,
and no ``/v1`` endpoint here — transport is ``asyncio.create_subprocess_exec``.

Compliance posture (see SPEC-cc-bobclaw-integration.md): this lane uses ONLY
the real CLI under the subscription OAuth login. It never extracts OAuth
tokens for a third-party client and never touches the metered Anthropic API
key (that is the separate ``claude_api`` backend).

Shape mirrors the other backends:
* a clean client class (``ClaudeCodeClient``)
* ``health_check()`` short-circuits with no network (runs ``claude --version``)
* typed errors (``ClaudeCodeError`` / ``ClaudeCodeThrottled``)

Streaming is **message-level, not token-delta** (probe-confirmed):
``--output-format stream-json --verbose`` emits one whole text block per
``assistant`` event. We surface those blocks as deltas — we do NOT fake
token streaming.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any, AsyncIterator, Optional

from core.config import config

logger = logging.getLogger(__name__)


class ClaudeCodeError(RuntimeError):
    """A Claude Code spawn failed, errored, or returned an error result."""


class ClaudeCodeThrottled(ClaudeCodeError):
    """The CLI hit a subscription rate-limit (5-hour window spent).

    ``execute_node`` branches on this to fall back to the face's
    ``escalation_backend`` (= ``claude_api`` after Decision 1).
    """


# Substrings that mark a throttle when the json-mode object surfaces it via
# ``api_error_status`` / ``result`` text rather than the stream-mode
# ``rate_limit_event``. Split by KIND because they are NOT the same condition:
#   * rate_limit — a genuine subscription/account rate limit (5-hour window).
#     Persistent for the window ⇒ ESCALATE to the face's fallback backend.
#   * overload   — a TRANSIENT server-side 5xx (529 "Overloaded" / 503). NOT an
#     account limit (you can run several CLIs fine) ⇒ retry ONCE in place before
#     escalating, so a momentary blip doesn't abandon a scratch-write planning turn.
_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "throttl")
_OVERLOAD_MARKERS = ("overloaded", "529", "503", "overloaded_error")


def _classify_throttle(*parts: Any) -> Optional[str]:
    """Return ``"rate_limit"`` | ``"overload"`` | ``None`` for the error parts.

    rate_limit wins ties (the safer escalation). ``None`` ⇒ not a throttle.
    """
    blob = " ".join(str(p) for p in parts if p).lower()
    if any(marker in blob for marker in _RATE_LIMIT_MARKERS):
        return "rate_limit"
    if any(marker in blob for marker in _OVERLOAD_MARKERS):
        return "overload"
    return None


def _looks_throttled(*parts: Any) -> bool:
    """Back-compat predicate (the streaming path still uses it): any throttle kind."""
    return _classify_throttle(*parts) is not None


# Total non-stream chat attempts: 1 normal + 1 transient-overload retry.
_MAX_OVERLOAD_ATTEMPTS = 2

# Defensive argv-length guard (P2): even with the prompt on stdin, a huge
# --add-dir / extra_args set could still overflow the OS command-line limit
# (Windows ~32 KB → WinError 206 / E2BIG). Fail LOUD before spawning rather than
# letting it surface as a cryptic spawn error.
_ARGV_BYTE_LIMIT = 32_000 if os.name == "nt" else 2_000_000


def _argv_byte_length(argv: list[str]) -> int:
    """Approx command-line byte length (each arg + a separator)."""
    return sum(len(str(a).encode("utf-8")) + 1 for a in argv)


# Env vars that redirect the `claude` CLI OFF its subscription OAuth login — onto the
# METERED Anthropic API, a different base URL, or a Bedrock/Vertex endpoint. `core/config.py`
# calls `load_dotenv` on `.secrets/bobclaw.env` (which holds a real ANTHROPIC_API_KEY for
# the SEPARATE `claude_api` backend) at import, so it lands in `os.environ`. A bare
# `create_subprocess_exec` inherits the parent environment — the spawned CLI would then
# silently bill metered API credit (the "credit balance too low" surprise) or hit the wrong
# endpoint rather than the user's flat subscription. We strip ALL of these from EVERY
# claude_code spawn so the CLI always falls back to its own OAuth login (the documented
# compliance posture: "never touches the metered Anthropic API key").
#
# F5: the original tuple covered only the two API-key vars. ANTHROPIC_BASE_URL +
# CLAUDE_CODE_USE_BEDROCK/_USE_VERTEX (+ their base URLs) ALSO redirect billing/endpoint, so
# a future operator who adds any of them to `.secrets` would silently re-bleed. Strip the
# whole family and name the constant for what it does.
_STRIP_FOR_SUBSCRIPTION = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
)


def _subscription_env() -> dict[str, str]:
    """`os.environ` minus the vars that would redirect the CLI off its subscription OAuth
    login (metered key, alternate base URL, Bedrock/Vertex). Subscription-OAuth only.

    Returns a COPY — never mutate ``os.environ`` (other backends, e.g.
    ``claude_api``, legitimately read ANTHROPIC_API_KEY).
    """
    env = dict(os.environ)
    for var in _STRIP_FOR_SUBSCRIPTION:
        env.pop(var, None)
    return env


class ClaudeCodeClient:
    """Subprocess client for the genuine ``claude`` CLI (headless ``-p`` mode).

    Parameters
    ----------
    cli_path:
        Path to the ``claude`` binary. Defaults to ``config.CC_CLI_PATH`` or,
        when that is unset, resolves ``claude`` on ``PATH``.
    cwd:
        Project directory the spawn briefs from (where ``CLAUDE.md`` lives).
        Defaults to ``config.CC_PROJECT_DIR``.
    posture:
        A dict of CLI-flag pieces (permission mode, allowed tools, …) supplied
        by the caller. C1 just threads it through to argv; C2 fills it per face.
    timeout:
        Per-spawn wall-clock timeout in seconds. Defaults to
        ``config.CC_TIMEOUT_SECONDS``.
    conversation_id:
        BoBClaw conversation id (C2.1/C3). Keys the per-conversation scratch dir
        ``CC_SCRATCH_ROOT/<conversation_id>`` used as the spawn cwd in the
        scratch-write posture. Threaded in from ``state`` by ``execute_node``.
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        cwd: Optional[str] = None,
        posture: Optional[dict] = None,
        timeout: Optional[int] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        # Resolve the binary lazily-ish: prefer explicit/config, else PATH.
        self.cli_path = cli_path or config.CC_CLI_PATH or shutil.which("claude") or "claude"
        self.cwd = cwd or config.CC_PROJECT_DIR
        self.posture = posture or {}
        self.timeout = timeout if timeout is not None else config.CC_TIMEOUT_SECONDS
        self.conversation_id = conversation_id
        # Last session id seen in a response — C3 reads this to persist the
        # session_id -> conversation_id sidecar mapping.
        self.last_session_id: Optional[str] = None

    # ── scratch-write posture (C2.1) ────────────────────────────────────────────

    @staticmethod
    def _is_scratch_write(posture: dict) -> bool:
        """True when the posture asks for the read-repo + write-scratch posture.

        Two equivalent spellings (face authors can use either):
        * ``mode: scratch_write`` (explicit), or
        * ``scratch_dir`` set together with ``permission_mode: acceptEdits``.

        Plain ``permission_mode: plan`` (no scratch) is the stricter option and
        is NOT scratch-write.
        """
        if str(posture.get("mode") or "").lower() == "scratch_write":
            return True
        return bool(posture.get("scratch_dir")) and (
            str(posture.get("permission_mode") or "").lower() == "acceptedits"
        )

    def _scratch_dir(self) -> str:
        """Per-conversation scratch dir ``CC_SCRATCH_ROOT/<conversation_id>``.

        Created on demand. Falls back to a stable ``_default`` bucket when no
        conversation id was threaded in (keeps tests + ad-hoc spawns working).
        It is OUTSIDE ``CC_PROJECT_DIR`` so the ``Write(<repo>/**)`` deny does
        not eat scratch writes (manager probe 2026-06-15).
        """
        conv = (self.conversation_id or "_default").strip() or "_default"
        # Guard against path traversal / separators in the conversation id.
        conv = conv.replace("/", "_").replace("\\", "_").replace("..", "_")
        scratch = os.path.join(config.CC_SCRATCH_ROOT, conv)
        os.makedirs(scratch, exist_ok=True)
        return scratch

    def _effective_cwd(self, posture: dict) -> str:
        """cwd for the spawn: the scratch dir for scratch-write, else ``self.cwd``."""
        if self._is_scratch_write(posture):
            return self._scratch_dir()
        return self.cwd

    def _brief_prompt(self, prompt: str, posture: dict) -> str:
        """Prepend the repo ``CLAUDE.md`` briefing for scratch-write spawns.

        FALLBACK for C2.1 #4: a normal spawn auto-loads ``CLAUDE.md`` from its
        cwd (the repo). In scratch-write mode the cwd is the scratch dir, so that
        auto-load is lost; ``--add-dir <repo>`` only grants READ access, it does
        not guarantee the briefing is auto-injected. We deterministically inline
        the repo ``CLAUDE.md`` into the prompt so the session is briefed
        regardless. (Manager confirms live whether the inline copy is redundant
        with auto-load; harmless if so.) Best-effort: silently skipped if the
        file is missing/unreadable.
        """
        if not self._is_scratch_write(posture):
            return prompt
        claude_md = os.path.join(self.cwd, "CLAUDE.md")
        try:
            with open(claude_md, "r", encoding="utf-8", errors="replace") as fh:
                briefing = fh.read().strip()
        except OSError:
            return prompt
        if not briefing:
            return prompt
        return (
            "<project-briefing source=\"CLAUDE.md\">\n"
            f"{briefing}\n"
            "</project-briefing>\n\n"
            f"{prompt}"
        )

    # ── argv construction ──────────────────────────────────────────────────────

    def _posture_flags(self, posture: dict) -> list[str]:
        """Translate a posture dict into CLI flags.

        Recognised keys (all optional — C2 owns the policy):
        * ``permission_mode``  -> ``--permission-mode <mode>``  (e.g. "plan")
        * ``allowed_tools``    -> ``--allowedTools "<csv-or-str>"``
        * ``disallowed_tools`` -> ``--disallowedTools "<csv-or-str>"``
        * ``add_dirs``         -> ``--add-dir <dir>`` (repeated; list or str)
        * ``model``            -> ``--model <model>``
        * ``extra_args``       -> appended verbatim (list[str]) — escape hatch

        Unknown keys are ignored (forward-compatible: C2 can add flags here
        without breaking C1's transport).

        Scratch-write posture (C2.1) is special: it emits the manager-verified
        flag set instead of the generic permission/tool keys —
        ``--permission-mode acceptEdits``, ``--add-dir <repo>`` (read the repo),
        and ``--disallowedTools Write(<repo>/**) Edit(<repo>/**) Bash`` (repo
        write-denied + no shell). The spawn cwd is the scratch dir (see
        ``_effective_cwd``), which is OUTSIDE the repo so scratch writes survive
        the deny.
        """
        if self._is_scratch_write(posture):
            return self._scratch_write_flags(posture)

        flags: list[str] = []
        mode = posture.get("permission_mode")
        if mode:
            flags += ["--permission-mode", str(mode)]

        allowed = posture.get("allowed_tools")
        if allowed:
            flags += ["--allowedTools", _as_tool_str(allowed)]

        disallowed = posture.get("disallowed_tools")
        if disallowed:
            flags += ["--disallowedTools", _as_tool_str(disallowed)]

        add_dirs = posture.get("add_dirs")
        if add_dirs:
            for d in (add_dirs if isinstance(add_dirs, (list, tuple)) else [add_dirs]):
                flags += ["--add-dir", str(d)]

        model = posture.get("model")
        if model:
            flags += ["--model", str(model)]

        extra = posture.get("extra_args")
        if extra:
            flags += [str(a) for a in extra]

        return flags

    def _scratch_write_flags(self, posture: dict) -> list[str]:
        """The manager-verified scratch-write flag set (probe 2026-06-15).

        Writes to the scratch cwd are allowed; the repo is read-only (via
        ``--add-dir``) and write-denied; ``Bash`` is denied so the CLI cannot
        shell-write around the Write deny. ``model`` / ``extra_args`` from the
        posture are still honoured.
        """
        repo = self.cwd  # the repo dir we still want to READ (not the cwd here)
        deny = f"{repo}/**"
        flags = [
            "--permission-mode", "acceptEdits",
            "--add-dir", str(repo),
            "--disallowedTools",
            f"Write({deny})",
            f"Edit({deny})",
            "Bash",
        ]
        # Allow extra read dirs (e.g. a sibling repo) if the face asked for them.
        add_dirs = posture.get("add_dirs")
        if add_dirs:
            for d in (add_dirs if isinstance(add_dirs, (list, tuple)) else [add_dirs]):
                flags += ["--add-dir", str(d)]
        model = posture.get("model")
        if model:
            flags += ["--model", str(model)]
        extra = posture.get("extra_args")
        if extra:
            flags += [str(a) for a in extra]
        return flags

    def _build_argv(
        self,
        *,
        output_format: str,
        resume_session_id: Optional[str],
        posture: dict,
        stream: bool = False,
    ) -> list[str]:
        # The prompt is fed on STDIN, NOT as an argv element. It inlines the
        # ~30 KB CLAUDE.md briefing (scratch-write posture) via _brief_prompt,
        # which overflows Windows' ~32 KB command-line limit (WinError 206) when
        # passed as argv. ``claude -p`` with no positional prompt reads it from
        # stdin (live-verified 2026-06-29). NOTE: claude is the OPPOSITE of agy
        # here — agy needs stdin CLOSED (DEVNULL) or it hangs; claude READS stdin.
        argv = [self.cli_path, "-p", "--output-format", output_format]
        if stream:
            # --verbose is REQUIRED for stream-json (probe-confirmed).
            argv.append("--verbose")
        if resume_session_id:
            # Always --resume <id>, never --continue (multi-process race).
            argv += ["--resume", str(resume_session_id)]
        argv += self._posture_flags(posture)
        return argv

    # ── non-stream chat ────────────────────────────────────────────────────────

    async def chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Run a one-shot ``claude -p --output-format json`` and return a
        normalized dict.

        Returns
        -------
        ``{"text", "session_id", "is_error", "api_error_status", "raw"}``

        Raises
        ------
        ClaudeCodeThrottled
            On a subscription rate-limit.
        ClaudeCodeError
            On any other CLI/parse failure or ``is_error`` result.
        """
        posture = self.posture if posture is None else posture
        briefed = self._brief_prompt(prompt, posture)
        argv = self._build_argv(
            output_format="json",
            resume_session_id=resume_session_id,
            posture=posture,
        )

        cwd = self._effective_cwd(posture)

        # One retry reserved for a TRANSIENT overload (529/503) — a momentary
        # server blip should not abandon a (scratch-write planning) turn and
        # silently escalate. A genuine rate_limit escalates on the first hit.
        for attempt in range(_MAX_OVERLOAD_ATTEMPTS):
            stdout, stderr = await self._spawn(argv, cwd=cwd, stdin_data=briefed)

            text = stdout.decode("utf-8", errors="replace").strip()
            if not text:
                err = stderr.decode("utf-8", errors="replace").strip()
                raise ClaudeCodeError(
                    f"claude CLI produced no output: {err or '<empty stderr>'}"
                )

            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ClaudeCodeError(
                    f"could not parse claude --output-format json output: {exc}"
                ) from exc

            result = obj.get("result")
            session_id = obj.get("session_id")
            is_error = bool(obj.get("is_error"))
            api_error_status = obj.get("api_error_status")

            if session_id:
                self.last_session_id = session_id

            if is_error or api_error_status:
                kind = _classify_throttle(api_error_status, result)
                if kind == "overload" and attempt + 1 < _MAX_OVERLOAD_ATTEMPTS:
                    logger.warning(
                        "claude_code transient overload (%s); retrying once in place",
                        api_error_status or result,
                    )
                    continue
                if kind:  # rate_limit, OR overload that survived the retry
                    raise ClaudeCodeThrottled(
                        f"claude CLI throttled [{kind}]: {api_error_status or result}"
                    )
                raise ClaudeCodeError(
                    f"claude CLI error (api_error_status={api_error_status!r}): "
                    f"{result or '<no result text>'}"
                )

            return {
                "text": result or "",
                "session_id": session_id,
                "is_error": is_error,
                "api_error_status": api_error_status,
                "raw": obj,
            }

    # ── streaming chat (message-level) ─────────────────────────────────────────

    async def stream_chat(
        self,
        *,
        prompt: str,
        resume_session_id: Optional[str] = None,
        posture: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        """Run ``claude -p --output-format stream-json --verbose`` and yield
        each ``assistant`` text block as it arrives (message-level chunks).

        Captures ``session_id`` onto ``self.last_session_id``. Raises
        ``ClaudeCodeThrottled`` on a throttled ``rate_limit_event`` and
        ``ClaudeCodeError`` on an error ``result`` / CLI failure.
        """
        posture = self.posture if posture is None else posture
        briefed = self._brief_prompt(prompt, posture)
        argv = self._build_argv(
            output_format="stream-json",
            resume_session_id=resume_session_id,
            posture=posture,
            stream=True,
        )

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._effective_cwd(posture),
            env=_subscription_env(),  # never bill the metered API key (subscription only)
        )

        # Feed the prompt on stdin, then half-close (send EOF) so the CLI
        # proceeds. The briefing is < the OS pipe buffer (64 KB on Windows), so a
        # single write+EOF before reading stdout cannot deadlock — claude reads
        # stdin to EOF before producing output. (WinError 206 fix: prompt off argv.)
        await self._write_stdin(proc, briefed)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout
        try:
            assert proc.stdout is not None
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise ClaudeCodeError(
                        f"claude CLI stream timed out after {self.timeout}s"
                    )
                try:
                    raw_line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    raise ClaudeCodeError(
                        f"claude CLI stream timed out after {self.timeout}s"
                    )
                if not raw_line:
                    break  # EOF — stream closed without an explicit result event

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                stop, blocks = self._handle_stream_event(line)
                for block in blocks:
                    yield block
                if stop:
                    return
        finally:
            # Never leak a running child if the consumer breaks early or we raise.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass

    def _handle_stream_event(self, line: str) -> tuple[bool, list[str]]:
        """Parse one NDJSON line. Return ``(stop, text_blocks)``.

        ``stop`` is True once the terminal ``result`` event is seen. Raises
        ``ClaudeCodeThrottled`` / ``ClaudeCodeError`` per the contract.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # The CLI occasionally fences non-JSON lines; skip them.
            return False, []

        etype = event.get("type")
        blocks: list[str] = []

        if etype in ("system", "init") and event.get("session_id"):
            self.last_session_id = event["session_id"]

        elif etype == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    blocks.append(block["text"])
            if event.get("session_id"):
                self.last_session_id = event["session_id"]

        elif etype == "rate_limit_event":
            info = event.get("rate_limit_info") or {}
            if info.get("status") and info["status"] != "allowed":
                raise ClaudeCodeThrottled(
                    f"claude CLI rate-limited: status={info.get('status')!r} "
                    f"type={info.get('rateLimitType')!r}"
                )

        elif etype == "result":
            if event.get("session_id"):
                self.last_session_id = event["session_id"]
            if event.get("is_error"):
                api_error_status = event.get("api_error_status")
                result = event.get("result")
                if _looks_throttled(api_error_status, result):
                    raise ClaudeCodeThrottled(
                        f"claude CLI rate-limited: {api_error_status or result}"
                    )
                raise ClaudeCodeError(
                    f"claude CLI error (api_error_status="
                    f"{api_error_status!r}): {result or '<no result text>'}"
                )
            return True, blocks

        return False, blocks

    # ── health check (no network) ──────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Return True if the CLI is invokable (``claude --version`` succeeds).

        Does NOT make a turn or touch auth (that would cost a request). Returns
        False if the binary is missing or the version probe fails.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=_subscription_env(),  # consistent posture (version probe is auth-free)
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

    @staticmethod
    async def _write_stdin(proc: Any, data: str) -> None:
        """Write the prompt to the streaming child's stdin and half-close (EOF).

        Best-effort + defensive: a broken pipe (child exited / errored before
        consuming stdin) must not mask the real error surfaced via stdout/stderr.
        Used by the streaming path; the non-stream path writes stdin via
        ``communicate(input=...)`` in ``_spawn``.
        """
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(data.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
            return
        try:
            proc.stdin.write_eof()
        except (NotImplementedError, OSError, RuntimeError):
            try:
                proc.stdin.close()
            except Exception:  # pragma: no cover - defensive
                pass

    async def _spawn(
        self,
        argv: list[str],
        cwd: Optional[str] = None,
        stdin_data: Optional[str] = None,
    ) -> tuple[bytes, bytes]:
        """Spawn the CLI, feed the prompt on stdin, return (stdout, stderr) bytes.

        ``stdin_data`` (the briefed prompt) is written to the child's stdin via
        ``communicate(input=...)`` — it is NOT an argv element, so the ~30 KB
        CLAUDE.md briefing can't overflow Windows' command-line limit
        (WinError 206). ``cwd`` overrides ``self.cwd`` (used for the scratch-write
        posture, where the spawn must run OUTSIDE the repo). Raises
        ``ClaudeCodeError`` if the binary is missing or the call times out (the
        child is killed first).
        """
        input_bytes = stdin_data.encode("utf-8") if stdin_data is not None else None
        # Defensive: fail loud BEFORE spawning if the argv (flags only — the prompt
        # rides stdin) would overflow the OS command-line limit.
        if _argv_byte_length(argv) > _ARGV_BYTE_LIMIT:
            raise ClaudeCodeError(
                f"claude argv is {_argv_byte_length(argv)} bytes, over the "
                f"~{_ARGV_BYTE_LIMIT}-byte command-line limit (would WinError 206 / "
                f"E2BIG); an arg (--add-dir / extra_args) is oversized"
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or self.cwd,
                env=_subscription_env(),  # never bill the metered API key (subscription only)
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ClaudeCodeError(
                f"claude CLI not found at {self.cli_path!r}: {exc}"
            ) from exc
        except OSError as exc:
            # A spawn OSError that is NOT a missing binary — most notably Windows
            # WinError 206 (command line too long / E2BIG): an argv overflowed the
            # OS limit. The prompt rides stdin now, but a huge --add-dir/extra_args
            # set could still overflow. Distinguish it so it stops masquerading as
            # "claude CLI not found" (the misdiagnosis that hid the original 206).
            raise ClaudeCodeError(
                f"claude CLI spawn failed ({type(exc).__name__}: {exc}); "
                f"if this is WinError 206 / 'command line too long', an argv "
                f"(e.g. --add-dir / extra_args) is oversized"
            ) from exc

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
            raise ClaudeCodeError(
                f"claude CLI timed out after {self.timeout}s"
            )
        return stdout, stderr


def _as_tool_str(value: Any) -> str:
    """Render a tool list/string into the comma-separated form the CLI takes."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)
