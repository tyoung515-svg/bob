"""
BoBClaw Core — Unit tests for ClaudeCodeClient (subprocess backend)

ALL subprocess I/O is mocked — ``asyncio.create_subprocess_exec`` is patched
so zero real ``claude`` CLI spawns and zero network happen. Fixtures are built
from the REAL probe output in tasks/2026-06-15-cc-integration/PLAN.md.
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.claude_code import (
    ClaudeCodeClient,
    ClaudeCodeError,
    ClaudeCodeThrottled,
    _argv_byte_length,
    _subscription_env,
)
from core.backends import claude_code as _cc_mod
from core.nodes import execute as execute_module


# ─── Real probe fixtures (from PLAN.md contract crib) ─────────────────────────

_JSON_SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "api_error_status": None,
    "result": "Here is the plan you asked for.",
    "stop_reason": "end_turn",
    "session_id": "11111111-2222-3333-4444-555555555555",
    "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 42, "output_tokens": 17},
    "num_turns": 1,
    "permission_denials": [],
    "terminal_reason": "completed",
}

_JSON_ERROR = {
    "type": "result",
    "subtype": "error",
    "is_error": True,
    "api_error_status": "invalid_request_error",
    "result": "something went wrong",
    "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
}

_JSON_THROTTLED = {
    "type": "result",
    "subtype": "error",
    "is_error": True,
    "api_error_status": "rate_limit_error",
    "result": "You have hit the rate limit; resets in 5 hours.",
    "session_id": "ffffffff-0000-1111-2222-333333333333",
}

# stream-json --verbose NDJSON events (one per line)
_STREAM_SYSTEM_INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "99999999-8888-7777-6666-555555555555",
    "model": "claude-opus-4-8",
    "permissionMode": "plan",
    "tools": ["Read", "Grep"],
}
_STREAM_ASSISTANT = {
    "type": "assistant",
    "message": {
        "content": [{"type": "text", "text": "First message block."}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    },
    "session_id": "99999999-8888-7777-6666-555555555555",
}
_STREAM_ASSISTANT_2 = {
    "type": "assistant",
    "message": {
        "content": [{"type": "text", "text": "Second message block."}],
    },
    "session_id": "99999999-8888-7777-6666-555555555555",
}
_STREAM_RATE_LIMIT_ALLOWED = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "resetsAt": 1234567890,
    },
}
_STREAM_RATE_LIMIT_THROTTLED = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "throttled",
        "rateLimitType": "five_hour",
        "resetsAt": 1234567890,
    },
}
_STREAM_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "First message block.Second message block.",
    "session_id": "99999999-8888-7777-6666-555555555555",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_client(**kwargs) -> ClaudeCodeClient:
    kwargs.setdefault("cli_path", "claude")
    kwargs.setdefault("cwd", "/tmp/proj")
    kwargs.setdefault("timeout", 30)
    return ClaudeCodeClient(**kwargs)


def _fake_proc_communicate(stdout: bytes, stderr: bytes = b"", returncode: int = 0):
    """A fake proc whose .communicate() returns (stdout, stderr)."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


def _fake_proc_stream(lines: list[bytes]):
    """A fake proc whose .stdout.readline() yields *lines* then b'' (EOF).

    ``returncode`` is None to mimic a still-running child mid-stream, so the
    generator's cleanup (kill on early break / raise) fires as it would live.
    """
    proc = MagicMock()
    proc.returncode = None
    queue = list(lines) + [b""]  # terminal EOF

    async def _readline():
        return queue.pop(0) if queue else b""

    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=_readline)
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    # The stream path now feeds the prompt on stdin (WinError 206 fix), so the
    # fake child needs a writable stdin: write/write_eof are sync, drain is async.
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.write_eof = MagicMock()
    proc.stdin.close = MagicMock()
    return proc


def _ndjson(*events) -> list[bytes]:
    return [(json.dumps(e) + "\n").encode("utf-8") for e in events]


# ─── Construction ─────────────────────────────────────────────────────────────

def test_resolves_explicit_cli_path():
    client = _make_client(cli_path="/usr/local/bin/claude")
    assert client.cli_path == "/usr/local/bin/claude"


def test_default_cli_path_resolves_on_path_or_bare_claude():
    with patch("core.backends.claude_code.shutil.which", return_value=None):
        client = ClaudeCodeClient(cli_path=None, cwd="/x")
    assert client.cli_path == "claude"


def test_posture_flags_translation():
    client = _make_client()
    flags = client._posture_flags(
        {
            "permission_mode": "plan",
            "allowed_tools": ["Read", "Grep", "Glob"],
            "model": "claude-opus-4-8",
        }
    )
    assert "--permission-mode" in flags
    assert flags[flags.index("--permission-mode") + 1] == "plan"
    assert "--allowedTools" in flags
    assert flags[flags.index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert "--model" in flags


def test_build_argv_includes_resume_and_format():
    client = _make_client(cli_path="claude")
    argv = client._build_argv(
        output_format="json",
        resume_session_id="sess-7",
        posture={"permission_mode": "plan"},
    )
    # Prompt is NOT in argv (rides stdin — WinError 206 fix).
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert "--resume" in argv and "sess-7" in argv
    assert "--permission-mode" in argv
    # No --verbose for non-stream json.
    assert "--verbose" not in argv


def test_build_argv_translates_cc_posture_to_cli_flags():
    client = _make_client(cli_path="claude")
    argv = client._build_argv(
        output_format="json",
        resume_session_id=None,
        posture={
            "permission_mode": "plan",
            "allowed_tools": ["Read", "Grep", "Glob", "WebSearch"],
        },
    )
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob,WebSearch"


def test_build_argv_stream_adds_verbose():
    client = _make_client()
    argv = client._build_argv(
        output_format="stream-json", resume_session_id=None, posture={}, stream=True
    )
    assert "--verbose" in argv
    assert "--resume" not in argv


# ─── chat() ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_parses_result_and_session_id():
    client = _make_client()
    proc = _fake_proc_communicate(json.dumps(_JSON_SUCCESS).encode("utf-8"))
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        out = await client.chat(prompt="plan it", resume_session_id=None, posture={})
    assert out["text"] == "Here is the plan you asked for."
    assert out["session_id"] == _JSON_SUCCESS["session_id"]
    assert out["is_error"] is False
    assert out["raw"] == _JSON_SUCCESS
    # last_session_id stashed for C3.
    assert client.last_session_id == _JSON_SUCCESS["session_id"]


@pytest.mark.asyncio
async def test_chat_passes_resume_and_posture_into_argv():
    client = _make_client()
    proc = _fake_proc_communicate(json.dumps(_JSON_SUCCESS).encode("utf-8"))
    captured = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return proc

    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        await client.chat(
            prompt="hi",
            resume_session_id="resume-me",
            posture={"permission_mode": "plan"},
        )
    assert "--resume" in captured["argv"]
    assert "resume-me" in captured["argv"]
    assert "--permission-mode" in captured["argv"]
    assert captured["cwd"] == "/tmp/proj"


@pytest.mark.asyncio
async def test_chat_sends_prompt_on_stdin_not_argv():
    """WinError 206 fix: the (briefing-inflated) prompt rides stdin, never argv.

    Passing it as an argv element overflows Windows' ~32 KB cmdline limit when
    the scratch-write posture inlines the ~30 KB CLAUDE.md briefing.
    """
    client = _make_client()
    proc = _fake_proc_communicate(json.dumps(_JSON_SUCCESS).encode("utf-8"))
    captured = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["stdin"] = kwargs.get("stdin")
        return proc

    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        await client.chat(prompt="PROMPT_BODY_X", resume_session_id=None, posture={})
    # The prompt is NOT an argv element ...
    assert "PROMPT_BODY_X" not in captured["argv"]
    assert "-p" in captured["argv"]
    # ... a stdin pipe was opened ...
    assert captured["stdin"] == __import__("asyncio").subprocess.PIPE
    # ... and the prompt was fed via communicate(input=...).
    _, comm_kwargs = proc.communicate.call_args
    assert comm_kwargs["input"] == b"PROMPT_BODY_X"


# ─── Subscription-only env (never bill the metered API key) ────────────────────

def test_subscription_env_strips_metered_auth_vars(monkeypatch):
    """`_subscription_env` removes the metered-API auth vars, keeps everything else.

    `config.py` loads `.secrets/bobclaw.env` (real ANTHROPIC_API_KEY for the
    SEPARATE claude_api backend) into `os.environ`. If a claude_code spawn
    inherited it, the CLI would bill metered API credit instead of the flat
    subscription — the "credit balance too low" surprise. Guard against that.
    """
    # F5: the WHOLE family that redirects off the subscription OAuth login — key, alt base
    # URL, and Bedrock/Vertex — must be stripped, not just the two API-key vars.
    redirectors = {
        "ANTHROPIC_API_KEY": "sk-ant-should-not-leak",
        "ANTHROPIC_AUTH_TOKEN": "should-not-leak-either",
        "ANTHROPIC_BASE_URL": "https://evil.example/v1",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock.example",
        "ANTHROPIC_VERTEX_BASE_URL": "https://vertex.example",
    }
    for k, v in redirectors.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", os.environ.get("PATH", "x"))  # a benign var to keep

    env = _subscription_env()

    for k in redirectors:
        assert k not in env, f"{k} must be stripped from the subscription env"
    assert "PATH" in env  # the rest of the environment is preserved
    # The real os.environ is NOT mutated (claude_api legitimately reads the key).
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-should-not-leak"


@pytest.mark.asyncio
async def test_chat_spawn_strips_anthropic_api_key_from_child_env(monkeypatch):
    """The actual chat() spawn must pass an env with the metered key removed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAK")
    client = _make_client()
    proc = _fake_proc_communicate(json.dumps(_JSON_SUCCESS).encode("utf-8"))
    captured = {}

    async def _fake_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return proc

    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        await client.chat(prompt="hi", resume_session_id=None, posture={})
    assert captured["env"] is not None, "spawn must pass an explicit env, not inherit"
    assert "ANTHROPIC_API_KEY" not in captured["env"]


@pytest.mark.asyncio
async def test_stream_chat_spawn_strips_anthropic_api_key_from_child_env(monkeypatch):
    """The streaming spawn path must also strip the metered key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAK")
    client = _make_client()
    proc = _fake_proc_stream(_ndjson(_STREAM_SYSTEM_INIT, _STREAM_ASSISTANT, _STREAM_RESULT))
    captured = {}

    async def _fake_exec(*argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return proc

    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        async for _ in client.stream_chat(prompt="hi", resume_session_id=None, posture={}):
            pass
    assert captured["env"] is not None
    assert "ANTHROPIC_API_KEY" not in captured["env"]


@pytest.mark.asyncio
async def test_stream_chat_sends_prompt_on_stdin_not_argv():
    """Streaming path also feeds the prompt on stdin + half-closes (write_eof)."""
    client = _make_client()
    proc = _fake_proc_stream(_ndjson(_STREAM_ASSISTANT, _STREAM_RESULT))
    captured = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["stdin"] = kwargs.get("stdin")
        return proc

    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        chunks = [
            c
            async for c in client.stream_chat(
                prompt="STREAM_PROMPT", resume_session_id=None, posture={}
            )
        ]
    assert chunks == ["First message block."]
    assert "STREAM_PROMPT" not in captured["argv"]
    assert captured["stdin"] == __import__("asyncio").subprocess.PIPE
    proc.stdin.write.assert_called_once_with(b"STREAM_PROMPT")
    proc.stdin.write_eof.assert_called_once()


@pytest.mark.asyncio
async def test_chat_raises_on_is_error():
    client = _make_client()
    proc = _fake_proc_communicate(json.dumps(_JSON_ERROR).encode("utf-8"))
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        with pytest.raises(ClaudeCodeError) as ei:
            await client.chat(prompt="x", resume_session_id=None, posture={})
    # Not a throttle — generic error.
    assert not isinstance(ei.value, ClaudeCodeThrottled)


@pytest.mark.asyncio
async def test_chat_raises_throttled_on_rate_limit_status():
    client = _make_client()
    proc = _fake_proc_communicate(json.dumps(_JSON_THROTTLED).encode("utf-8"))
    # A genuine rate_limit escalates on the FIRST hit (no retry).
    spawn = AsyncMock(return_value=proc)
    with patch("core.backends.claude_code.asyncio.create_subprocess_exec", spawn):
        with pytest.raises(ClaudeCodeThrottled):
            await client.chat(prompt="x", resume_session_id=None, posture={})
    assert spawn.call_count == 1


_JSON_OVERLOADED = {
    "type": "result", "subtype": "error", "is_error": True,
    "api_error_status": "overloaded_error", "result": "Overloaded (529)",
    "session_id": "ffffffff-0000-1111-2222-333333333333",
}


@pytest.mark.asyncio
async def test_chat_retries_once_on_transient_overload_then_succeeds():
    """A transient 529 'overloaded' is retried ONCE in place; the second (good)
    spawn's reply is returned — the turn is NOT abandoned to escalation."""
    client = _make_client()
    overloaded = _fake_proc_communicate(json.dumps(_JSON_OVERLOADED).encode("utf-8"))
    ok = _fake_proc_communicate(json.dumps(_JSON_SUCCESS).encode("utf-8"))
    spawn = AsyncMock(side_effect=[overloaded, ok])
    with patch("core.backends.claude_code.asyncio.create_subprocess_exec", spawn):
        out = await client.chat(prompt="x", resume_session_id=None, posture={})
    assert spawn.call_count == 2
    assert out["text"] == _JSON_SUCCESS["result"]


@pytest.mark.asyncio
async def test_chat_raises_throttled_when_overload_persists():
    """If the overload survives the single retry, it escalates as ClaudeCodeThrottled."""
    client = _make_client()
    overloaded = _fake_proc_communicate(json.dumps(_JSON_OVERLOADED).encode("utf-8"))
    spawn = AsyncMock(side_effect=[overloaded, overloaded])
    with patch("core.backends.claude_code.asyncio.create_subprocess_exec", spawn):
        with pytest.raises(ClaudeCodeThrottled):
            await client.chat(prompt="x", resume_session_id=None, posture={})
    assert spawn.call_count == 2


@pytest.mark.asyncio
async def test_chat_raises_on_empty_stdout():
    client = _make_client()
    proc = _fake_proc_communicate(b"", stderr=b"boom")
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        with pytest.raises(ClaudeCodeError):
            await client.chat(prompt="x", resume_session_id=None, posture={})


@pytest.mark.asyncio
async def test_chat_raises_on_unparseable_json():
    client = _make_client()
    proc = _fake_proc_communicate(b"not json at all")
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        with pytest.raises(ClaudeCodeError):
            await client.chat(prompt="x", resume_session_id=None, posture={})


@pytest.mark.asyncio
async def test_chat_raises_clean_error_when_binary_missing():
    client = _make_client(cli_path="/nope/claude")
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("no such file")),
    ):
        with pytest.raises(ClaudeCodeError):
            await client.chat(prompt="x", resume_session_id=None, posture={})


# ─── P2: argv-length guard + spawn-error disambiguation ───────────────────────

def test_argv_byte_length_counts_args():
    assert _argv_byte_length(["claude", "-p"]) == len("claude") + 1 + len("-p") + 1


@pytest.mark.asyncio
async def test_spawn_guard_rejects_oversized_argv(monkeypatch):
    """The defensive guard fails LOUD before spawning when argv would overflow."""
    monkeypatch.setattr(_cc_mod, "_ARGV_BYTE_LIMIT", 16)  # tiny so any real argv trips it
    client = _make_client()
    spawn = AsyncMock()
    with patch("core.backends.claude_code.asyncio.create_subprocess_exec", spawn):
        with pytest.raises(ClaudeCodeError) as ei:
            await client._spawn(["claude", "-p", "--output-format", "json"],
                                stdin_data="prompt")
    assert "command-line limit" in str(ei.value)
    spawn.assert_not_called()  # guard fired BEFORE the spawn


@pytest.mark.asyncio
async def test_spawn_distinguishes_oversized_from_missing_binary():
    """WinError 206-style OSError ⇒ 'spawn failed' (NOT the misleading 'not found')."""
    client = _make_client()
    # a generic OSError stands in for WinError 206 / E2BIG
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=OSError("[WinError 206] The filename or extension is too long")),
    ):
        with pytest.raises(ClaudeCodeError) as ei:
            await client._spawn(["claude", "-p"], stdin_data="x")
    msg = str(ei.value)
    assert "spawn failed" in msg and "not found" not in msg
    # FileNotFoundError still maps to "not found"
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("missing")),
    ):
        with pytest.raises(ClaudeCodeError) as ei2:
            await client._spawn(["claude", "-p"], stdin_data="x")
    assert "not found" in str(ei2.value)


@pytest.mark.asyncio
async def test_chat_times_out_and_kills_proc():
    import asyncio as _asyncio

    client = _make_client(timeout=1)
    proc = _fake_proc_communicate(b"{}")
    proc.communicate = AsyncMock(side_effect=_asyncio.TimeoutError())
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        with pytest.raises(ClaudeCodeError):
            await client.chat(prompt="x", resume_session_id=None, posture={})
    proc.kill.assert_called()


# ─── stream_chat() ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_chat_yields_assistant_blocks_and_stops_on_result():
    client = _make_client()
    lines = _ndjson(
        _STREAM_SYSTEM_INIT,
        _STREAM_ASSISTANT,
        _STREAM_RATE_LIMIT_ALLOWED,
        _STREAM_ASSISTANT_2,
        _STREAM_RESULT,
    )
    proc = _fake_proc_stream(lines)
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        chunks = []
        async for c in client.stream_chat(prompt="go", resume_session_id=None, posture={}):
            chunks.append(c)
    assert chunks == ["First message block.", "Second message block."]
    assert client.last_session_id == _STREAM_SYSTEM_INIT["session_id"]


@pytest.mark.asyncio
async def test_stream_chat_raises_throttled_on_rate_limit_event():
    client = _make_client()
    lines = _ndjson(
        _STREAM_SYSTEM_INIT,
        _STREAM_ASSISTANT,
        _STREAM_RATE_LIMIT_THROTTLED,
        _STREAM_RESULT,
    )
    proc = _fake_proc_stream(lines)
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        with pytest.raises(ClaudeCodeThrottled):
            async for _ in client.stream_chat(
                prompt="go", resume_session_id=None, posture={}
            ):
                pass
    # Child must be killed when we bail mid-stream.
    proc.kill.assert_called()


@pytest.mark.asyncio
async def test_stream_chat_raises_error_on_error_result():
    client = _make_client()
    err_result = dict(_STREAM_RESULT, is_error=True, api_error_status="invalid_request_error",
                      result="bad")
    lines = _ndjson(_STREAM_SYSTEM_INIT, err_result)
    proc = _fake_proc_stream(lines)
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        with pytest.raises(ClaudeCodeError):
            async for _ in client.stream_chat(
                prompt="go", resume_session_id=None, posture={}
            ):
                pass


@pytest.mark.asyncio
async def test_stream_chat_skips_non_json_lines():
    client = _make_client()
    lines = [b"warning: some fence line\n"] + _ndjson(_STREAM_ASSISTANT, _STREAM_RESULT)
    proc = _fake_proc_stream(lines)
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        chunks = [
            c
            async for c in client.stream_chat(
                prompt="go", resume_session_id=None, posture={}
            )
        ]
    assert chunks == ["First message block."]


# ─── health_check() ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_true_on_version_string():
    client = _make_client()
    proc = _fake_proc_communicate(b"2.1.177 (Claude Code)\n", returncode=0)
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        assert await client.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_when_binary_missing():
    client = _make_client(cli_path="/nope/claude")
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("missing")),
    ):
        assert await client.health_check() is False


@pytest.mark.asyncio
async def test_health_check_false_on_nonzero_exit():
    client = _make_client()
    proc = _fake_proc_communicate(b"", returncode=1)
    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    ):
        assert await client.health_check() is False


# ─── scratch-write posture (C2.1) ─────────────────────────────────────────────

_REPO = "/repo/bobclaw"


def _scratch_client(tmp_root, conversation_id=None):
    """Client with cwd=repo and a tmp scratch root (no real dir under the repo)."""
    return ClaudeCodeClient(
        cli_path="claude",
        cwd=_REPO,
        timeout=30,
        conversation_id=conversation_id,
    )


def test_is_scratch_write_detection():
    c = _make_client()
    # explicit mode
    assert c._is_scratch_write({"mode": "scratch_write"}) is True
    # scratch_dir + acceptEdits
    assert c._is_scratch_write(
        {"scratch_dir": "scratch", "permission_mode": "acceptEdits"}
    ) is True
    # plain plan mode → NOT scratch-write
    assert c._is_scratch_write(
        {"permission_mode": "plan", "allowed_tools": ["Read"]}
    ) is False
    # scratch_dir alone (no acceptEdits) → NOT scratch-write
    assert c._is_scratch_write({"scratch_dir": "scratch"}) is False


def test_scratch_write_argv_has_verified_flag_set(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.backends.claude_code.config.CC_SCRATCH_ROOT", str(tmp_path)
    )
    monkeypatch.setattr(
        "core.backends.claude_code.config.CC_PROJECT_DIR", _REPO
    )
    client = _scratch_client(tmp_path, conversation_id="conv-abc")
    argv = client._build_argv(
        output_format="json",
        resume_session_id=None,
        posture={"mode": "scratch_write", "permission_mode": "acceptEdits"},
    )
    # --permission-mode acceptEdits
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    # --add-dir <repo>
    assert "--add-dir" in argv
    assert _REPO in argv
    # the three disallowedTools entries, in order, immediately after the flag
    i = argv.index("--disallowedTools")
    assert argv[i + 1] == f"Write({_REPO}/**)"
    assert argv[i + 2] == f"Edit({_REPO}/**)"
    assert argv[i + 3] == "Bash"
    # plan-only keys are NOT emitted for scratch-write
    assert "plan" not in argv


@pytest.mark.asyncio
async def test_scratch_write_uses_scratch_dir_as_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.backends.claude_code.config.CC_SCRATCH_ROOT", str(tmp_path)
    )
    monkeypatch.setattr(
        "core.backends.claude_code.config.CC_PROJECT_DIR", _REPO
    )
    client = _scratch_client(tmp_path, conversation_id="conv-xyz")
    proc = _fake_proc_communicate(json.dumps(_JSON_SUCCESS).encode("utf-8"))
    captured = {}

    async def _fake_exec(*argv, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return proc

    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ):
        await client.chat(
            prompt="ideate",
            resume_session_id=None,
            posture={"mode": "scratch_write", "permission_mode": "acceptEdits"},
        )
    # cwd is the per-conversation scratch dir, NOT the repo.
    expected = str(tmp_path / "conv-xyz")
    assert captured["cwd"] == expected
    assert captured["cwd"] != _REPO
    import os as _os
    assert _os.path.isdir(expected)  # created on demand


def test_per_conversation_scratch_dirs_are_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.backends.claude_code.config.CC_SCRATCH_ROOT", str(tmp_path)
    )
    c1 = _scratch_client(tmp_path, conversation_id="conv-1")
    c2 = _scratch_client(tmp_path, conversation_id="conv-2")
    d1 = c1._scratch_dir()
    d2 = c2._scratch_dir()
    assert d1 != d2
    assert d1.endswith("conv-1")
    assert d2.endswith("conv-2")


def test_scratch_dir_sanitizes_conversation_id(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.backends.claude_code.config.CC_SCRATCH_ROOT", str(tmp_path)
    )
    client = _scratch_client(tmp_path, conversation_id="../../etc/evil")
    d = client._scratch_dir()
    # No traversal escapes the scratch root.
    import os as _os
    assert _os.path.commonpath([str(tmp_path), d]) == str(tmp_path)


def test_plain_plan_mode_no_scratch_cwd_and_plan_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.backends.claude_code.config.CC_SCRATCH_ROOT", str(tmp_path)
    )
    client = _scratch_client(tmp_path, conversation_id="conv-plan")
    posture = {"permission_mode": "plan", "allowed_tools": ["Read", "Grep"]}
    argv = client._build_argv(
        output_format="json", resume_session_id=None, posture=posture
    )
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    # No scratch-write flags.
    assert "--add-dir" not in argv
    assert "Bash" not in argv
    # cwd stays the repo (no scratch).
    assert client._effective_cwd(posture) == _REPO


def test_scratch_write_injects_claude_md_briefing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("PROJECT BRIEF: build the thing", encoding="utf-8")
    monkeypatch.setattr(
        "core.backends.claude_code.config.CC_SCRATCH_ROOT", str(tmp_path / "scratch")
    )
    client = ClaudeCodeClient(
        cli_path="claude", cwd=str(repo), timeout=30, conversation_id="c1"
    )
    posture = {"mode": "scratch_write", "permission_mode": "acceptEdits"}
    briefed = client._brief_prompt("do the task", posture)
    assert "PROJECT BRIEF: build the thing" in briefed
    assert "do the task" in briefed
    # Plain plan mode is NOT briefed (auto-load from cwd=repo still works there).
    assert client._brief_prompt("do the task", {"permission_mode": "plan"}) == "do the task"


# ─── execute.py routing ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_to_backend_routes_claude_code(monkeypatch):
    """_default_send_to_backend routes claude_code to ClaudeCodeClient.chat."""
    fake = MagicMock()
    fake.chat = AsyncMock(return_value={"text": "routed-text", "session_id": "s1"})

    with patch("core.backends.claude_code.ClaudeCodeClient", return_value=fake):
        out = await execute_module._default_send_to_backend(
            [{"role": "user", "content": "plan x"}], "claude_code"
        )
    assert out == "routed-text"
    fake.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_node_claude_code_success(mock_redis):
    """State-aware claude_code block returns the assistant turn."""
    fake = MagicMock()
    fake.chat = AsyncMock(return_value={"text": "cc plan", "session_id": "s9"})
    fake.last_session_id = "s9"

    with patch("core.backends.claude_code.ClaudeCodeClient", return_value=fake):
        result = await execute_module.execute_node(
            {
                "task": "design the roadmap",
                "backend": "claude_code",
                "messages": [],
                "cc_posture": {"permission_mode": "plan"},
                "escalation_backend": "claude_api",
            }
        )
    assert result["messages"][0]["content"] == "cc plan"
    assert result["error"] is None
    # posture threaded through from state.
    _, kwargs = fake.chat.call_args
    assert kwargs["posture"] == {"permission_mode": "plan"}


@pytest.mark.asyncio
async def test_execute_node_claude_code_throttle_falls_through(mock_redis):
    """ClaudeCodeThrottled → fall through to escalation_backend (claude_api)."""
    fake = MagicMock()
    fake.chat = AsyncMock(side_effect=ClaudeCodeThrottled("throttled"))

    async def _mock_stream(messages, backend, model_override=None):
        # The fall-through routes through _stream_to_backend with the
        # escalation backend.
        assert backend == "claude_api"
        yield "fallback response"

    with patch("core.backends.claude_code.ClaudeCodeClient", return_value=fake):
        with patch.object(execute_module, "_stream_to_backend", _mock_stream):
            result = await execute_module.execute_node(
                {
                    "task": "design the roadmap",
                    "backend": "claude_code",
                    "messages": [],
                    "cc_posture": {},
                    "escalation_backend": "claude_api",
                }
            )
    assert result["messages"][0]["content"] == "fallback response"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_execute_node_threads_conversation_id_into_client(mock_redis):
    """execute_node passes state['conversation_id'] to ClaudeCodeClient."""
    fake = MagicMock()
    fake.chat = AsyncMock(return_value={"text": "cc plan", "session_id": "s9"})
    fake.last_session_id = "s9"
    captured = {}

    def _factory(*args, **kwargs):
        captured["conversation_id"] = kwargs.get("conversation_id")
        return fake

    # Isolate the C3 session store: this test only asserts conversation_id
    # threading, but execute_node also calls the real _lookup/_record (SQLite +
    # default sidecar). Stub both so nothing writes outside the test.
    with patch("core.backends.claude_code.ClaudeCodeClient", side_effect=_factory), \
         patch("core.cc_sessions._lookup_cc_session", AsyncMock(return_value=None)), \
         patch("core.cc_sessions._record_cc_session", AsyncMock()):
        await execute_module.execute_node(
            {
                "task": "design the roadmap",
                "backend": "claude_code",
                "messages": [],
                "cc_posture": {"mode": "scratch_write", "permission_mode": "acceptEdits"},
                "escalation_backend": "claude_api",
                "conversation_id": "conv-42",
            }
        )
    assert captured["conversation_id"] == "conv-42"


@pytest.mark.asyncio
async def test_execute_node_streams_cc_reply_via_writer(mock_redis):
    """Regression (2026-06-16 live E2E): a claude_code turn must emit its reply
    on the 'custom' stream channel. The server's 'updates' relay SKIPS execute
    node's assistant message (assumes token-streaming happened), so without this
    emit the whole CC reply is silently dropped to the client."""
    fake = MagicMock()
    fake.chat = AsyncMock(return_value={"text": "a feature flag is a toggle", "session_id": "s1"})
    fake.last_session_id = "s1"
    emitted: list = []

    with patch("core.backends.claude_code.ClaudeCodeClient", return_value=fake), \
         patch("core.cc_sessions._lookup_cc_session", AsyncMock(return_value=None)), \
         patch("core.cc_sessions._record_cc_session", AsyncMock()), \
         patch("core.nodes.execute._get_stream_writer", return_value=emitted.append):
        out = await execute_module.execute_node(
            {
                "task": "what is a feature flag?",
                "backend": "claude_code",
                "messages": [],
                "cc_posture": {"mode": "scratch_write", "permission_mode": "acceptEdits"},
                "escalation_backend": "claude_api",
                "conversation_id": "conv-stream",
            }
        )
    # Reply present in the node output AND emitted as a 'custom' token chunk.
    assert out["messages"][0]["content"] == "a feature flag is a toggle"
    token_events = [e for e in emitted if isinstance(e, dict) and e.get("type") == "token"]
    assert token_events, "claude_code reply was not emitted on the custom channel"
    assert token_events[0]["content"] == "a feature flag is a toggle"
    assert token_events[0]["backend"] == "claude_code"
