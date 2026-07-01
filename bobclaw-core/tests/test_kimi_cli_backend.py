"""BoBClaw Core — Unit tests for KimiCliClient (kimi -p subprocess backend).

All subprocess I/O mocked — zero real ``kimi`` spawns, zero network. Fixtures
mirror the locked contract probed against kimi 0.17.1 (2026-06-29).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.kimi_cli import (
    KimiCliClient,
    KimiCliError,
    KimiCliThrottled,
    _looks_throttled,
    _parse_kimi_stream,
)


def _fake_proc(stdout: bytes = b"", stderr: bytes = b"", rc: int = 0):
    proc = MagicMock()
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=rc)
    proc.kill = MagicMock()
    return proc


def _stream(reply="hi", session_id="session_x") -> bytes:
    lines = [json.dumps({"role": "assistant", "content": reply})]
    if session_id:
        lines.append(json.dumps({
            "role": "meta", "type": "session.resume_hint",
            "session_id": session_id, "command": f"kimi -r {session_id}",
        }))
    return ("\n".join(lines)).encode("utf-8")


def _client(**kw):
    kw.setdefault("cli_path", "kimi")
    kw.setdefault("cwd", "/repo")
    kw.setdefault("timeout", 30)
    return KimiCliClient(**kw)


# ─── parser ───────────────────────────────────────────────────────────────────

def test_parse_stream():
    reply, sid = _parse_kimi_stream(_stream("hello", "session_9").decode())
    assert reply == "hello" and sid == "session_9"


def test_parse_stream_last_assistant_wins():
    blob = "\n".join([
        json.dumps({"role": "assistant", "content": "first"}),
        json.dumps({"role": "assistant", "content": "final"}),
    ])
    reply, sid = _parse_kimi_stream(blob)
    assert reply == "final" and sid is None


def test_parse_stream_skips_garbage():
    blob = "noise line\n" + _stream("ok", "session_1").decode()
    reply, sid = _parse_kimi_stream(blob)
    assert reply == "ok" and sid == "session_1"


# ─── throttle ─────────────────────────────────────────────────────────────────

def test_looks_throttled():
    assert _looks_throttled("HTTP 429 too many requests")
    assert _looks_throttled("rate limit hit")
    assert not _looks_throttled("ordinary failure")


# ─── argv ─────────────────────────────────────────────────────────────────────

def test_build_argv_basic():
    c = _client()
    argv = c._build_argv("do it", posture={}, resume_session=None)
    assert argv == ["kimi", "-p", "do it", "--output-format", "stream-json"]


def test_build_argv_model_and_resume():
    c = _client()
    argv = c._build_argv("x", posture={"model": "kimi-k2.7"}, resume_session="session_5")
    assert argv[argv.index("-m") + 1] == "kimi-k2.7"
    assert argv[argv.index("-r") + 1] == "session_5"


def test_build_argv_never_adds_yolo():
    # -p excludes -y/--yolo ("Cannot combine --prompt with --yolo")
    c = _client()
    argv = c._build_argv("x", posture={}, resume_session=None)
    assert "-y" not in argv and "--yolo" not in argv


# ─── chat ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_success():
    c = _client()
    proc = _fake_proc(stdout=_stream("the answer", "session_42"))
    with patch("core.backends.kimi_cli.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        out = await c.chat(prompt="go")
    assert out["text"] == "the answer"
    assert out["session_id"] == "session_42"
    assert c.last_session_id == "session_42"
    assert out["is_error"] is False


@pytest.mark.asyncio
async def test_chat_prompt_in_argv_stdin_closed():
    c = _client()
    proc = _fake_proc(stdout=_stream("ok"))
    captured = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["stdin"] = kwargs.get("stdin")
        return proc

    with patch("core.backends.kimi_cli.asyncio.create_subprocess_exec",
               side_effect=_fake_exec):
        await c.chat(prompt="PROMPT_K")
    assert "PROMPT_K" in captured["argv"]            # kimi reads the prompt from -p argv
    assert captured["stdin"] == asyncio.subprocess.DEVNULL  # stdin closed (one-shot)


@pytest.mark.asyncio
async def test_chat_throttled_raises():
    c = _client()
    proc = _fake_proc(stderr=b"HTTP 429 rate limited", rc=1)
    with patch("core.backends.kimi_cli.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        with pytest.raises(KimiCliThrottled):
            await c.chat(prompt="x")


@pytest.mark.asyncio
async def test_chat_error_raises():
    c = _client()
    proc = _fake_proc(stderr=b"some failure", rc=1)
    with patch("core.backends.kimi_cli.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        with pytest.raises(KimiCliError) as ei:
            await c.chat(prompt="x")
    assert not isinstance(ei.value, KimiCliThrottled)


@pytest.mark.asyncio
async def test_chat_no_reply_raises():
    c = _client()
    proc = _fake_proc(stdout=b'{"role":"meta","type":"x"}', rc=0)  # no assistant block
    with patch("core.backends.kimi_cli.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        with pytest.raises(KimiCliError):
            await c.chat(prompt="x")


@pytest.mark.asyncio
async def test_chat_missing_binary_raises():
    c = _client(cli_path="/nope/kimi")
    with patch("core.backends.kimi_cli.asyncio.create_subprocess_exec",
               AsyncMock(side_effect=FileNotFoundError("no kimi"))):
        with pytest.raises(KimiCliError):
            await c.chat(prompt="x")


# ─── health ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_true():
    c = _client()
    proc = _fake_proc(stdout=b"0.17.1\n", rc=0)
    with patch("core.backends.kimi_cli.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        assert await c.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_when_binary_missing():
    c = _client(cli_path="/nope/kimi")
    with patch("core.backends.kimi_cli.asyncio.create_subprocess_exec",
               AsyncMock(side_effect=FileNotFoundError("missing"))):
        assert await c.health_check() is False


# ─── face + team wiring ───────────────────────────────────────────────────────

def test_worker_kimi_cli_face():
    from core.faces.registry import FaceRegistry
    f = FaceRegistry().get_face("worker-kimi-cli")
    assert f.role == "worker"
    assert f.preferred_backend == "kimi_cli"
    assert f.escalation_backend == "kimi_code"  # HTTP membership = the fallback


def test_hier_fleet_apex_is_kimi_cli():
    import core.teams as teams
    assert teams.role_backend("hier-fleet", "apex") == "kimi_cli"
