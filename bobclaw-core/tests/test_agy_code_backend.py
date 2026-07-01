"""
BoBClaw Core — Unit tests for AntigravityClient (agy subprocess backend)

ALL subprocess I/O is mocked — ``asyncio.create_subprocess_exec`` is patched so
zero real ``agy`` CLI spawns and zero network happen. The agy contract (verified
live 2026-06-28) differs sharply from a normal CLI:

* stdin MUST be closed (DEVNULL) or ``agy -p`` hangs.
* the reply is NOT on stdout — it is read from the conversation transcript.
* agy mints its own conversation UUID, recovered from last_conversations.json.
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.agy_code import (
    AntigravityClient,
    AgyError,
    AgyThrottled,
    _argv_byte_length,
    _looks_throttled,
    _reply_from_transcript,
    _sanitize_conv_id,
)
from core.backends import agy_code as _agy_mod


# ─── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep _work_dir() off the real /repos/scratch/agy and skip retry sleeps."""
    monkeypatch.setattr(
        "core.backends.agy_code.config.AGY_SCRATCH_ROOT", str(tmp_path / "scratch")
    )
    monkeypatch.setattr("core.backends.agy_code._CAPTURE_RETRY_DELAY", 0)


def _make_client(**kwargs) -> AntigravityClient:
    kwargs.setdefault("cli_path", "agy")
    kwargs.setdefault("cwd", "/repo")
    kwargs.setdefault("timeout", 30)
    return AntigravityClient(**kwargs)


def _fake_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


def _patch_exec(proc=None, side_effect=None, capture=None):
    """Patch create_subprocess_exec; optionally capture call kwargs into `capture`."""
    if capture is not None:
        def _f(*a, **k):
            capture.update(k)
            capture.setdefault("argv", a)
            return proc
        return patch("core.backends.agy_code.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_f))
    if side_effect is not None:
        return patch("core.backends.agy_code.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=side_effect))
    return patch("core.backends.agy_code.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc))


# ─── Pure helpers ─────────────────────────────────────────────────────────────


def test_reply_from_transcript_takes_last_model_step():
    body = "\n".join(
        json.dumps(s)
        for s in [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "hi"},
            {"step_index": 1, "source": "SYSTEM", "type": "CONVERSATION_HISTORY"},
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "first"},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "FINAL"},
            {"step_index": 4, "source": "SYSTEM", "type": "CHECKPOINT", "content": "ignore me"},
        ]
    )
    assert _reply_from_transcript(body) == "FINAL"


def test_reply_from_transcript_empty_when_no_model():
    body = json.dumps({"source": "SYSTEM", "type": "CHECKPOINT", "content": "x"})
    assert _reply_from_transcript(body) == ""


def test_reply_from_transcript_skips_bad_lines():
    body = "not json\n" + json.dumps({"source": "MODEL", "content": "OK"})
    assert _reply_from_transcript(body) == "OK"


def test_looks_throttled_markers():
    assert _looks_throttled("HTTP 429 Too Many Requests")
    assert _looks_throttled("RESOURCE_EXHAUSTED: quota exceeded")
    assert not _looks_throttled("everything is fine")


def test_sanitize_conv_id():
    assert _sanitize_conv_id("a/b\\c..d") == "a_b_c_d"
    assert _sanitize_conv_id("  clean  ") == "clean"


# ─── Construction ─────────────────────────────────────────────────────────────


def test_unique_conversation_id_per_client_when_unset():
    a = AntigravityClient(cli_path="agy", cwd="/r")
    b = AntigravityClient(cli_path="agy", cwd="/r")
    assert a.conversation_id and b.conversation_id and a.conversation_id != b.conversation_id


def test_explicit_conversation_id_sanitized():
    c = _make_client(conversation_id="a/b")
    assert c.conversation_id == "a_b"


# ─── argv construction ────────────────────────────────────────────────────────


def test_build_argv_minimal_no_resume():
    c = _make_client(conversation_id="conv-1")
    argv = c._build_argv("do it", posture={}, resume_uuid=None)
    assert argv[:3] == ["agy", "-p", "do it"]
    assert "--conversation" not in argv  # fresh turn: agy mints the uuid
    for flag in ("--output-format", "--verbose", "--resume", "--permission-mode"):
        assert flag not in argv


def test_build_argv_resume_passes_conversation():
    c = _make_client(conversation_id="conv-1")
    argv = c._build_argv("x", posture={}, resume_uuid="uuid-9")
    assert argv[argv.index("--conversation") + 1] == "uuid-9"


def test_build_argv_model_and_repo_read():
    c = _make_client(cwd="/repo", conversation_id="conv-1")
    argv = c._build_argv("x", posture={"model": "gemini-3.1-pro", "mode": "scratch_write"}, resume_uuid=None)
    assert argv[argv.index("--model") + 1] == "gemini-3.1-pro"
    assert argv[argv.index("--add-dir") + 1] == "/repo"


def test_work_dir_is_per_conversation(tmp_path):
    with patch("core.backends.agy_code.config.AGY_SCRATCH_ROOT", str(tmp_path)):
        c = _make_client(conversation_id="conv-9")
        wd = c._work_dir()
    assert "conv-9" in wd and os.path.isdir(wd)


# ─── uuid capture + reply read (real temp files) ──────────────────────────────


def _seed_home(tmp_path, work_dir, uuid, reply):
    """Write a fake segregated home with last_conversations.json + a transcript."""
    cli = tmp_path / ".gemini" / "antigravity-cli"
    (cli / "cache").mkdir(parents=True, exist_ok=True)
    (cli / "cache" / "last_conversations.json").write_text(
        json.dumps({work_dir: uuid}), encoding="utf-8"
    )
    logs = cli / "brain" / uuid / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "transcript.jsonl").write_text(
        json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "content": reply}),
        encoding="utf-8",
    )


def test_capture_uuid_and_read_reply_roundtrip(tmp_path):
    work_dir = str(tmp_path / "work" / "conv-1")
    _seed_home(tmp_path, work_dir, "uuid-abc", "the answer")
    c = _make_client(conversation_id="conv-1")
    with patch("core.backends.agy_code.config.AGY_HOME", str(tmp_path)):
        assert c._capture_uuid(work_dir) == "uuid-abc"
        assert c._read_reply("uuid-abc") == "the answer"


def test_capture_uuid_normalizes_path(tmp_path):
    # agy stores a backslash/normalized key; lookup must still match a forward-slash cwd.
    stored = str(tmp_path / "work" / "conv-1").replace("/", "\\")
    _seed_home(tmp_path, stored, "uuid-xyz", "ok")
    c = _make_client(conversation_id="conv-1")
    query = str(tmp_path / "work" / "conv-1").replace("\\", "/")
    with patch("core.backends.agy_code.config.AGY_HOME", str(tmp_path)):
        assert c._capture_uuid(query) == "uuid-xyz"


def test_capture_uuid_none_when_missing(tmp_path):
    c = _make_client()
    with patch("core.backends.agy_code.config.AGY_HOME", str(tmp_path)):
        assert c._capture_uuid(str(tmp_path / "nope")) is None


# ─── chat() control flow ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_success_reads_reply_from_transcript():
    c = _make_client(conversation_id="conv-1")
    with _patch_exec(_fake_proc(b"", b"", 0)), patch.object(
        AntigravityClient, "_capture_uuid", return_value="uuid-1"
    ), patch.object(AntigravityClient, "_read_reply", return_value="# Plan\n1. go"):
        r = await c.chat(prompt="plan it")
    assert r["text"] == "# Plan\n1. go"
    assert r["session_id"] == "uuid-1"
    assert c.last_session_id == "uuid-1"


@pytest.mark.asyncio
async def test_chat_resume_skips_capture_uses_given_uuid():
    c = _make_client(conversation_id="conv-1")
    cap = MagicMock()
    with _patch_exec(_fake_proc(b"", b"", 0)), patch.object(
        AntigravityClient, "_capture_uuid", cap
    ), patch.object(AntigravityClient, "_read_reply", return_value="resumed"):
        r = await c.chat(prompt="more", resume_session_id="uuid-prev")
    assert r["session_id"] == "uuid-prev"
    cap.assert_not_called()


@pytest.mark.asyncio
async def test_chat_raises_throttled_on_nonzero_marker():
    c = _make_client()
    with _patch_exec(_fake_proc(b"", b"Error: 429 Resource exhausted: Quota exceeded", 1)):
        with pytest.raises(AgyThrottled):
            await c.chat(prompt="x")


@pytest.mark.asyncio
async def test_chat_raises_error_on_nonzero_nonthrottle():
    c = _make_client()
    with _patch_exec(_fake_proc(b"", b"context deadline exceeded", 1)):
        with pytest.raises(AgyError) as ei:
            await c.chat(prompt="x")
    assert not isinstance(ei.value, AgyThrottled)


@pytest.mark.asyncio
async def test_chat_raises_when_no_uuid_captured():
    c = _make_client()
    with _patch_exec(_fake_proc(b"", b"", 0)), patch.object(
        AntigravityClient, "_capture_uuid", return_value=None
    ):
        with pytest.raises(AgyError):
            await c.chat(prompt="x")


@pytest.mark.asyncio
async def test_chat_raises_when_no_reply_in_transcript():
    c = _make_client()
    with _patch_exec(_fake_proc(b"", b"", 0)), patch.object(
        AntigravityClient, "_capture_uuid", return_value="uuid-1"
    ), patch.object(AntigravityClient, "_read_reply", return_value=""):
        with pytest.raises(AgyError):
            await c.chat(prompt="x")


@pytest.mark.asyncio
async def test_chat_closes_stdin_devnull():
    """The #1 gotcha: agy -p must be spawned with stdin closed."""
    c = _make_client(conversation_id="conv-1")
    cap: dict = {}
    with _patch_exec(_fake_proc(b"", b"", 0), capture=cap), patch.object(
        AntigravityClient, "_capture_uuid", return_value="u"
    ), patch.object(AntigravityClient, "_read_reply", return_value="ok"):
        await c.chat(prompt="x")
    import asyncio as _a
    assert cap.get("stdin") == _a.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_chat_times_out_and_kills():
    c = _make_client(timeout=1)
    proc = _fake_proc()
    import asyncio as _a
    proc.communicate = AsyncMock(side_effect=_a.TimeoutError())
    with _patch_exec(proc):
        with pytest.raises(AgyError):
            await c.chat(prompt="x")
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_chat_missing_binary_raises_error():
    c = _make_client()
    with _patch_exec(side_effect=FileNotFoundError("no agy")):
        with pytest.raises(AgyError):
            await c.chat(prompt="x")


@pytest.mark.asyncio
async def test_spawn_injects_userprofile_when_home_seeded(tmp_path):
    c = _make_client(conversation_id="conv-1")
    cap: dict = {}
    with patch("core.backends.agy_code.config.AGY_HOME", str(tmp_path)), _patch_exec(
        _fake_proc(b"", b"", 0), capture=cap
    ), patch.object(AntigravityClient, "_capture_uuid", return_value="u"), patch.object(
        AntigravityClient, "_read_reply", return_value="ok"
    ):
        await c.chat(prompt="x")
    assert cap.get("env") is not None and cap["env"].get("USERPROFILE") == str(tmp_path)


# ─── stream_chat ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_chat_yields_single_chunk():
    c = _make_client(conversation_id="conv-1")
    with _patch_exec(_fake_proc(b"", b"", 0)), patch.object(
        AntigravityClient, "_capture_uuid", return_value="u"
    ), patch.object(AntigravityClient, "_read_reply", return_value="hello"):
        chunks = [x async for x in c.stream_chat(prompt="x")]
    assert chunks == ["hello"]


# ─── health_check ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_true_on_version():
    c = _make_client()
    with _patch_exec(_fake_proc(b"1.0.13\n", b"", 0)):
        assert await c.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_on_nonzero():
    c = _make_client()
    with _patch_exec(_fake_proc(b"", b"boom", 1)):
        assert await c.health_check() is False


@pytest.mark.asyncio
async def test_health_check_false_on_missing_binary():
    c = _make_client()
    with _patch_exec(side_effect=FileNotFoundError("no agy")):
        assert await c.health_check() is False


# ─── _brief_prompt (no-tool steer + brief-without-repo-read) ──────────────────


def test_brief_prompt_appends_no_tool_steer_by_default():
    out = _make_client()._brief_prompt("do the thing", posture={})
    assert "do the thing" in out
    assert "do not call any tools" in out.lower()


def test_brief_prompt_omits_steer_when_tools_allowed():
    out = _make_client()._brief_prompt("x", posture={"allow_tools": True})
    assert "do not call any tools" not in out.lower()


def test_brief_prompt_inlines_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Brief\nSECRET_MARKER_42", encoding="utf-8")
    out = _make_client(cwd=str(tmp_path))._brief_prompt("task", posture={"brief": True})
    assert "SECRET_MARKER_42" in out
    assert "<project-briefing" in out


def test_build_argv_brief_does_not_grant_repo_read():
    """Security property: brief inlines CLAUDE.md but must NOT add --add-dir."""
    argv = _make_client(conversation_id="c")._build_argv(
        "x", posture={"brief": True}, resume_uuid=None
    )
    assert "--add-dir" not in argv


# ─── _capture_uuid disambiguation among many entries ──────────────────────────


def test_capture_uuid_picks_matching_key_among_many(tmp_path):
    cli = tmp_path / ".gemini" / "antigravity-cli" / "cache"
    cli.mkdir(parents=True)
    wd = str(tmp_path / "work" / "conv-2")
    mapping = {
        str(tmp_path / "work" / "conv-1"): "uuid-1",
        wd: "uuid-2",
        str(tmp_path / "work" / "conv-3"): "uuid-3",
    }
    (cli / "last_conversations.json").write_text(json.dumps(mapping), encoding="utf-8")
    with patch("core.backends.agy_code.config.AGY_HOME", str(tmp_path)):
        assert _make_client(conversation_id="conv-2")._capture_uuid(wd) == "uuid-2"


def test_capture_uuid_none_when_populated_but_key_absent(tmp_path):
    cli = tmp_path / ".gemini" / "antigravity-cli" / "cache"
    cli.mkdir(parents=True)
    (cli / "last_conversations.json").write_text(
        json.dumps({str(tmp_path / "other"): "u"}), encoding="utf-8"
    )
    with patch("core.backends.agy_code.config.AGY_HOME", str(tmp_path)):
        assert _make_client()._capture_uuid(str(tmp_path / "nope")) is None


# ─── stream_chat error propagation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_chat_propagates_throttle():
    c = _make_client()
    with patch.object(AntigravityClient, "chat", AsyncMock(side_effect=AgyThrottled("q"))):
        with pytest.raises(AgyThrottled):
            async for _ in c.stream_chat(prompt="x"):
                pass


@pytest.mark.asyncio
async def test_stream_chat_propagates_error():
    c = _make_client()
    with patch.object(AntigravityClient, "chat", AsyncMock(side_effect=AgyError("boom"))):
        with pytest.raises(AgyError):
            async for _ in c.stream_chat(prompt="x"):
                pass


# ─── throttle gating: zero-exit + marker must NOT false-throttle ──────────────


@pytest.mark.asyncio
async def test_chat_zero_exit_with_throttle_marker_does_not_raise():
    c = _make_client(conversation_id="conv-1")
    with _patch_exec(_fake_proc(b"", b"...429 rate limit...", 0)), patch.object(
        AntigravityClient, "_capture_uuid", return_value="u"
    ), patch.object(AntigravityClient, "_read_reply", return_value="real reply"):
        r = await c.chat(prompt="explain rate limits")
    assert r["text"] == "real reply"


# ─── P2 parity sweep: argv-length guard + spawn-error disambiguation ───────────

def test_argv_byte_length_counts_args():
    assert _argv_byte_length(["agy", "-p"]) == len("agy") + 1 + len("-p") + 1


@pytest.mark.asyncio
async def test_spawn_guard_rejects_oversized_argv(monkeypatch):
    """agy carries the prompt in argv (-p), so the guard matters most here."""
    monkeypatch.setattr(_agy_mod, "_ARGV_BYTE_LIMIT", 8)  # tiny so any real argv trips it
    c = _make_client()
    spawn = AsyncMock()
    with patch("core.backends.agy_code.asyncio.create_subprocess_exec", spawn):
        with pytest.raises(AgyError) as ei:
            await c._spawn(["agy", "-p", "a long prompt that overflows"], cwd="/w")
    assert "command-line limit" in str(ei.value)
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_distinguishes_oversized_from_missing_binary():
    c = _make_client()
    with patch(
        "core.backends.agy_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=OSError("[WinError 206] command line too long")),
    ):
        with pytest.raises(AgyError) as ei:
            await c._spawn(["agy", "-p", "x"], cwd="/w")
    assert "spawn failed" in str(ei.value) and "not found" not in str(ei.value)
    with patch(
        "core.backends.agy_code.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("missing")),
    ):
        with pytest.raises(AgyError) as ei2:
            await c._spawn(["agy", "-p", "x"], cwd="/w")
    assert "not found" in str(ei2.value)
