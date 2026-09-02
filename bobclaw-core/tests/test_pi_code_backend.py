"""BoBClaw Core — Unit tests for PiCodeClient (pi headless subprocess backend).

All subprocess I/O is mocked (``asyncio.create_subprocess_exec`` patched) — zero
real ``pi`` spawns, zero network. Fixtures mirror the locked contract probed
against pi 0.80.3 (2026-07-03): ``--mode json`` NDJSON with a ``session`` id and the
reply carried as the last ``message_end`` assistant message's ``content[]`` text
parts (``thinking`` parts dropped).
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.pi_code import (
    PiCodeClient,
    PiError,
    PiThrottled,
    _extract_text,
    _looks_throttled,
    _parse_pi_events,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _fake_proc(stdout: bytes = b"", stderr: bytes = b"", rc: int = 0):
    proc = MagicMock()
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=rc)
    proc.kill = MagicMock()
    return proc


def _pi_events(session_id="s1", reply="hi", thinking="reasoning",
               error=None, will_retry=False) -> bytes:
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if reply:
        content.append({"type": "text", "text": reply})
    lines = [json.dumps({"type": "session", "id": session_id, "version": 1})]
    lines.append(json.dumps(
        {"type": "message_end", "message": {"role": "assistant", "content": content}}
    ))
    if error:
        lines.append(json.dumps({"type": "error", "message": error}))
    lines.append(json.dumps(
        {"type": "agent_end", "messages": [{"role": "assistant", "content": content}],
         "willRetry": will_retry}
    ))
    return ("\n".join(lines)).encode("utf-8")


def _client(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr("core.backends.pi_code.config.PI_SCRATCH_ROOT", str(tmp_path))
    kw.setdefault("cwd", "/repo")
    kw.setdefault("timeout", 30)
    c = PiCodeClient(**kw)
    # Pin the invocation so argv tests don't depend on a resolvable pi/node on PATH.
    monkeypatch.setattr(c, "_resolve_argv0", lambda: ["pi"])
    return c


# ─── _parse_pi_events / _extract_text ─────────────────────────────────────────

def test_parse_pi_events_success():
    ev = _parse_pi_events(_pi_events(session_id="abc", reply="hi there").decode())
    assert ev["session_id"] == "abc"
    assert ev["reply"] == "hi there"
    assert ev["failed"] is False and not ev["error"]


def test_parse_pi_events_skips_thinking():
    ev = _parse_pi_events(_pi_events(reply="ANSWER", thinking="secret chain").decode())
    assert ev["reply"] == "ANSWER"
    assert "secret" not in ev["reply"]


def test_parse_pi_events_error_event():
    ev = _parse_pi_events(_pi_events(reply="", error="boom 429", will_retry=True).decode())
    assert ev["failed"] is True and "429" in ev["error"]


def test_parse_pi_events_falls_back_to_agent_end():
    # No message_end reply, but agent_end carries the final assistant message.
    lines = [
        json.dumps({"type": "session", "id": "s9"}),
        json.dumps({"type": "agent_end",
                    "messages": [{"role": "assistant",
                                  "content": [{"type": "text", "text": "final"}]}],
                    "willRetry": False}),
    ]
    ev = _parse_pi_events("\n".join(lines))
    assert ev["reply"] == "final" and ev["session_id"] == "s9"


def test_extract_text_handles_string_and_parts():
    assert _extract_text({"content": "plain"}) == "plain"
    assert _extract_text({"content": [{"type": "text", "text": "a"},
                                      {"type": "thinking", "thinking": "z"},
                                      {"type": "text", "text": "b"}]}) == "a\nb"


def test_extract_text_strips_inline_think_block():
    # MiniMax inlines its reasoning as <think>…</think> in the text content.
    assert _extract_text(
        {"content": [{"type": "text", "text": "<think>reasoning here</think>MiniMax-M3 READY"}]}
    ) == "MiniMax-M3 READY"
    assert _extract_text({"content": "<think>x\ny</think>  answer"}) == "answer"


def test_looks_throttled():
    assert _looks_throttled("error code 429")
    assert _looks_throttled("rate limit exceeded")
    assert not _looks_throttled("invalid model 400")


# ─── _build_argv ──────────────────────────────────────────────────────────────

def test_build_argv_defaults(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv("do it", posture={"model": "deepseek/deepseek-v4-flash"},
                         resume_session=None)
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
    assert "--no-tools" in argv          # tools OFF by default
    assert "--no-session" in argv        # ephemeral by default
    assert "--no-context-files" in argv
    assert argv[argv.index("--model") + 1] == "deepseek/deepseek-v4-flash"
    assert argv[-2:] == ["-p", "do it"]  # prompt LAST


def test_build_argv_tools_posture_enables_loop(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv("x", posture={"model": "zai/glm-5.2", "tools": True},
                         resume_session=None)
    assert "--no-tools" not in argv


def test_build_argv_resume_uses_session(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv("x", posture={}, resume_session="sess-7")
    assert "--session" in argv and argv[argv.index("--session") + 1] == "sess-7"
    assert "--no-session" not in argv


# ─── chat ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_success(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    proc = _fake_proc(stdout=_pi_events(session_id="sess-1", reply="PI_OK"), rc=0)
    with patch("core.backends.pi_code.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        out = await c.chat(prompt="hi", posture={"model": "deepseek/deepseek-v4-flash"})
    assert out["text"] == "PI_OK"
    assert out["session_id"] == "sess-1"
    assert c.last_session_id == "sess-1"


@pytest.mark.asyncio
async def test_chat_throttled(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    proc = _fake_proc(stdout=b"", stderr=b"HTTP 429 too many requests", rc=1)
    with patch("core.backends.pi_code.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        with pytest.raises(PiThrottled):
            await c.chat(prompt="hi", posture={"model": "zai/glm-5.2"})


@pytest.mark.asyncio
async def test_chat_no_reply_raises(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    proc = _fake_proc(stdout=_pi_events(reply="", thinking=None), rc=0)
    with patch("core.backends.pi_code.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        with pytest.raises(PiError):
            await c.chat(prompt="hi", posture={"model": "deepseek/deepseek-v4-flash"})


# ─── health_check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_true(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    proc = _fake_proc(stdout=b"0.80.3\n", rc=0)
    with patch("core.backends.pi_code.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        assert await c.health_check() is True


@pytest.mark.asyncio
async def test_health_check_false_when_missing(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    with patch("core.backends.pi_code.asyncio.create_subprocess_exec",
               AsyncMock(side_effect=FileNotFoundError("no pi"))):
        assert await c.health_check() is False


# ─── _subprocess_env ──────────────────────────────────────────────────────────

def test_subprocess_env_injects_provider_keys_strips_secrets(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("core.backends.pi_code.config.ZAI_API_KEY", "zk")
    monkeypatch.setattr("core.backends.pi_code.config.DEEPSEEK_API_KEY", "dk")
    monkeypatch.setattr("core.backends.pi_code.config.MINIMAX_API_KEY", "mk")
    monkeypatch.setenv("BOBCLAW_SECRET", "should-not-leak")
    env = c._subprocess_env()
    assert env["ZAI_API_KEY"] == "zk"
    assert env["DEEPSEEK_API_KEY"] == "dk"
    assert env["MINIMAX_API_KEY"] == "mk"
    assert "BOBCLAW_SECRET" not in env
