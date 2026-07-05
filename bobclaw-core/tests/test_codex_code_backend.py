"""BoBClaw Core — Unit tests for CodexCodeClient (codex exec subprocess backend).

All subprocess I/O is mocked (``asyncio.create_subprocess_exec`` patched) — zero
real ``codex`` spawns, zero network. Fixtures mirror the locked contract probed
against codex-cli 0.142.3 (2026-06-29).
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.codex_code import (
    CodexCodeClient,
    CodexError,
    CodexThrottled,
    _looks_throttled,
    _parse_events,
)
from core.nodes import execute as execute_module


# ─── helpers ──────────────────────────────────────────────────────────────────

def _fake_proc(stdout: bytes = b"", stderr: bytes = b"", rc: int = 0):
    proc = MagicMock()
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=rc)
    proc.kill = MagicMock()
    return proc


def _events(thread_id="t1", reply="hello", error=None) -> bytes:
    lines = [json.dumps({"type": "thread.started", "thread_id": thread_id})]
    if reply:
        lines.append(json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": reply}}
        ))
    if error:
        lines.append(json.dumps({"type": "turn.failed", "error": {"message": error}}))
    else:
        lines.append(json.dumps({"type": "turn.completed", "usage": {}}))
    return ("\n".join(lines)).encode("utf-8")


def _client(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr("core.backends.codex_code.config.CODEX_SCRATCH_ROOT", str(tmp_path))
    kw.setdefault("cli_path", "codex")
    kw.setdefault("cwd", "/repo")
    kw.setdefault("timeout", 30)
    return CodexCodeClient(**kw)


# ─── _parse_events ────────────────────────────────────────────────────────────

def test_parse_events_success():
    ev = _parse_events(_events(thread_id="abc", reply="hi there").decode())
    assert ev["thread_id"] == "abc"
    assert ev["reply"] == "hi there"
    assert ev["failed"] is False and not ev["error"]


def test_parse_events_turn_failed():
    ev = _parse_events(_events(reply="", error="rate limit 429").decode())
    assert ev["failed"] is True
    assert "429" in ev["error"]


def test_parse_events_error_event():
    blob = json.dumps({"type": "error", "message": "boom"})
    ev = _parse_events(blob)
    assert ev["failed"] is True and ev["error"] == "boom"


def test_parse_events_skips_non_json_lines():
    blob = "warning: noise\n" + _events(reply="ok").decode()
    ev = _parse_events(blob)
    assert ev["reply"] == "ok" and ev["thread_id"] == "t1"


# ─── _looks_throttled ─────────────────────────────────────────────────────────

def test_looks_throttled():
    assert _looks_throttled("error code 429")
    assert _looks_throttled("rate limit exceeded")
    assert _looks_throttled("provider 503 overloaded")
    assert not _looks_throttled("invalid model 400")
    assert not _looks_throttled("all good")


# ─── _build_argv ──────────────────────────────────────────────────────────────

def test_build_argv_profile(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={"profile": "glm"}, outfile="/o.txt",
                         work_dir="/w", resume_thread=None)
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv and "-o" in argv and "/o.txt" in argv
    assert argv[argv.index("-p") + 1] == "glm"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv


def test_build_argv_model_routes_litellm(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={"model": "glm-5.2"}, outfile="/o.txt",
                         work_dir="/w", resume_thread=None)
    assert argv[argv.index("-m") + 1] == "glm-5.2"
    assert "model_provider=litellm" in argv


def test_build_argv_profile_plus_model_stays_native(tmp_path, monkeypatch):
    """A profile + an explicit model = pick that model within the profile's
    provider (gpt-native), WITHOUT forcing the litellm proxy — the fix that lets
    a `gpt`-profile face run a chosen gpt model instead of only the default."""
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={"profile": "gpt", "model": "gpt-5.5"},
                         outfile="/o.txt", work_dir="/w", resume_thread=None)
    assert argv[argv.index("-p") + 1] == "gpt"
    assert argv[argv.index("-m") + 1] == "gpt-5.5"
    assert "model_provider=litellm" not in argv


def test_build_argv_scratch_write_locks_network(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, cwd="/repo")
    argv = c._build_argv(posture={"mode": "scratch_write", "profile": "deepseek"},
                         outfile="/o.txt", work_dir="/w", resume_thread=None)
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert "--add-dir" in argv and "/repo" in argv
    assert "sandbox_workspace_write.network_access=false" in argv


def test_build_argv_resume_subcommand(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={"profile": "glm"}, outfile="/o.txt",
                         work_dir="/w", resume_thread="tid9")
    assert argv[:4] == ["codex", "exec", "resume", "tid9"]
    # resume takes the LIMITED flag set only (no -p/-s/-C/--color/--add-dir)
    assert "--json" in argv and "-o" in argv and "--skip-git-repo-check" in argv
    for rejected in ("-p", "-s", "-C", "--color", "--add-dir"):
        assert rejected not in argv


# ─── chat ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_success(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, conversation_id="cv")
    proc = _fake_proc(stdout=_events(thread_id="th7", reply="ignored-stdout"))
    with patch("core.backends.codex_code.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)), \
         patch.object(CodexCodeClient, "_read_outfile", staticmethod(lambda p: "FROM_FILE")):
        out = await c.chat(prompt="go", posture={"profile": "glm"})
    assert out["text"] == "FROM_FILE"        # -o file wins over stdout reply
    assert out["session_id"] == "th7"
    assert out["is_error"] is False
    assert c.last_session_id == "th7"


@pytest.mark.asyncio
async def test_chat_prompt_on_stdin_not_argv(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    proc = _fake_proc(stdout=_events(reply="ok"))
    captured = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["stdin"] = kwargs.get("stdin")
        return proc

    with patch("core.backends.codex_code.asyncio.create_subprocess_exec",
               side_effect=_fake_exec), \
         patch.object(CodexCodeClient, "_read_outfile", staticmethod(lambda p: "ok")):
        await c.chat(prompt="PROMPT_X", posture={})
    assert "PROMPT_X" not in captured["argv"]            # prompt rides stdin
    assert captured["stdin"] == __import__("asyncio").subprocess.PIPE
    _, kw = proc.communicate.call_args
    assert kw["input"] == b"PROMPT_X"


@pytest.mark.asyncio
async def test_chat_throttled_raises(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    proc = _fake_proc(stdout=_events(reply="", error="429 Too Many Requests"), rc=1)
    with patch("core.backends.codex_code.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)), \
         patch.object(CodexCodeClient, "_read_outfile", staticmethod(lambda p: "")):
        with pytest.raises(CodexThrottled):
            await c.chat(prompt="x", posture={})


@pytest.mark.asyncio
async def test_chat_nonthrottle_error_raises_codexerror(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    proc = _fake_proc(stdout=_events(reply="", error="400 invalid model"), rc=1)
    with patch("core.backends.codex_code.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)), \
         patch.object(CodexCodeClient, "_read_outfile", staticmethod(lambda p: "")):
        with pytest.raises(CodexError) as ei:
            await c.chat(prompt="x", posture={})
    assert not isinstance(ei.value, CodexThrottled)


@pytest.mark.asyncio
async def test_chat_no_reply_raises(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    proc = _fake_proc(stdout=_events(reply=""), rc=0)  # success exit, but no message
    with patch("core.backends.codex_code.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)), \
         patch.object(CodexCodeClient, "_read_outfile", staticmethod(lambda p: "")):
        with pytest.raises(CodexError):
            await c.chat(prompt="x", posture={})


@pytest.mark.asyncio
async def test_chat_missing_binary_raises(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, cli_path="/nope/codex")
    with patch("core.backends.codex_code.asyncio.create_subprocess_exec",
               AsyncMock(side_effect=FileNotFoundError("no codex"))):
        with pytest.raises(CodexError):
            await c.chat(prompt="x", posture={})


# ─── health_check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_true_is_cli_only(tmp_path, monkeypatch):
    """CLI reachable ⇒ healthy. NOTE: no ``_litellm_reachable`` patch — health_check is the
    codex-CLI liveness ONLY (the LiteLLM proxy is a per-profile runtime dependency now, not a
    backend-liveness signal). This doubles as the regression guard for the native-gpt fix: a
    re-added proxy gate would call the real probe here (:4000) and flake, failing this test."""
    c = _client(tmp_path, monkeypatch)
    proc = _fake_proc(stdout=b"codex-cli 0.142.3\n", rc=0)
    with patch("core.backends.codex_code.asyncio.create_subprocess_exec",
               AsyncMock(return_value=proc)):
        assert await c.health_check() is True  # proxy state is irrelevant to backend health


@pytest.mark.asyncio
async def test_health_check_false_when_binary_missing(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, cli_path="/nope/codex")
    with patch("core.backends.codex_code.asyncio.create_subprocess_exec",
               AsyncMock(side_effect=FileNotFoundError("missing"))):
        assert await c.health_check() is False


# ─── CX-2: faces + fan-out posture threading ──────────────────────────────────

def test_worker_codex_face_is_above_opencode():
    from core.faces.registry import FaceRegistry
    f = FaceRegistry().get_face("worker-codex")
    assert f.role == "worker"
    assert f.preferred_backend == "codex_code"
    assert f.escalation_backend == "opencode_serve"  # opencode = the fallback
    assert f.codex_posture.get("model")              # threads a provider model


def test_planner_codex_face():
    from core.faces.registry import FaceRegistry
    f = FaceRegistry().get_face("planner-codex")
    assert f.preferred_backend == "codex_code"
    assert f.codex_posture.get("profile") == "glm"


def test_dispatch_threads_codex_posture_into_worker_send():
    from core.nodes.dispatch import _route_after_dispatch
    sends = _route_after_dispatch({
        "fanout_subtasks": [{"idx": 0, "text": "a"}, {"idx": 1, "text": "b"}],
        "face_id": "worker-codex",
        "backend": "codex_code",
    })
    assert all(s.arg.get("codex_posture", {}).get("model") == "glm-5.2" for s in sends)


@pytest.mark.asyncio
async def test_worker_node_uses_codex_posture_model():
    from core.nodes import worker as worker_mod

    captured = {}

    async def fake_send(messages, backend, model=None):
        captured["backend"], captured["model"] = backend, model
        return "ok"

    with patch.object(worker_mod, "_send_to_backend", side_effect=fake_send):
        out = await worker_mod.worker_node({
            "task": "do x", "backend": "codex_code", "subtask_idx": 0,
            "messages": [], "codex_posture": {"model": "glm-5.2"},
        })
    assert captured["backend"] == "codex_code" and captured["model"] == "glm-5.2"
    assert out["worker_results"][0]["status"] == "ok"


# ─── CX-3: codex_sessions resume + state-aware execute block ───────────────────

@pytest.mark.asyncio
async def test_codex_sessions_roundtrip(tmp_path, monkeypatch):
    from core import codex_sessions as cs
    monkeypatch.setattr("core.codex_sessions.config.MEMORY_SQLITE_PATH",
                        str(tmp_path / "sessions.db"))
    assert await cs._lookup_codex_session("conv1") is None
    await cs._record_codex_session("conv1", "thread-abc")
    assert await cs._lookup_codex_session("conv1") == "thread-abc"
    await cs._record_codex_session("conv1", "thread-xyz")   # upsert
    assert await cs._lookup_codex_session("conv1") == "thread-xyz"
    await cs._record_codex_session("", "ignored")           # blank id ⇒ no-op
    assert await cs._lookup_codex_session("") is None


@pytest.mark.asyncio
async def test_execute_node_codex_code_success(mock_redis):
    """State-aware codex_code block returns the turn + records the resume thread."""
    fake = MagicMock()
    fake.chat = AsyncMock(return_value={"text": "codex plan", "session_id": "th1"})
    fake.last_session_id = "th1"
    with patch("core.backends.codex_code.CodexCodeClient", return_value=fake), \
         patch("core.codex_sessions._lookup_codex_session", AsyncMock(return_value=None)), \
         patch("core.codex_sessions._record_codex_session", AsyncMock()) as rec:
        result = await execute_module.execute_node({
            "task": "plan x", "backend": "codex_code", "messages": [],
            "codex_posture": {"profile": "glm", "brief": True},
            "escalation_backend": "opencode_serve", "conversation_id": "conv-cx",
        })
    assert result["messages"][0]["content"] == "codex plan"
    assert result["error"] is None
    assert result["codex_resume_session_id"] == "th1"
    # the planner-codex posture (profile + brief) BINDS — threaded into chat()
    _, kwargs = fake.chat.call_args
    assert kwargs["posture"] == {"profile": "glm", "brief": True}
    rec.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_node_codex_code_model_override_binds(mock_redis):
    """A UI-picked model (state.model_override) threads into the codex posture so
    a gpt-mode (profile=gpt) turn runs the CHOSEN gpt model, not only the default."""
    fake = MagicMock()
    fake.chat = AsyncMock(return_value={"text": "gpt plan", "session_id": "th2"})
    fake.last_session_id = "th2"
    with patch("core.backends.codex_code.CodexCodeClient", return_value=fake), \
         patch("core.codex_sessions._lookup_codex_session", AsyncMock(return_value=None)), \
         patch("core.codex_sessions._record_codex_session", AsyncMock()):
        await execute_module.execute_node({
            "task": "plan y", "backend": "codex_code", "messages": [],
            "codex_posture": {"profile": "gpt", "brief": True},
            "model_override": "gpt-5.5",
            "escalation_backend": "claude_api", "conversation_id": "conv-gpt",
        })
    _, kwargs = fake.chat.call_args
    # profile preserved, picked model threaded in alongside it
    assert kwargs["posture"] == {"profile": "gpt", "brief": True, "model": "gpt-5.5"}


@pytest.mark.asyncio
async def test_execute_node_codex_code_throttle_escalates(mock_redis):
    """CodexThrottled → fall through to escalation_backend (opencode_serve)."""
    fake = MagicMock()
    fake.chat = AsyncMock(side_effect=CodexThrottled("429"))

    async def _mock_stream(messages, backend, model_override=None):
        assert backend == "opencode_serve"   # codex above opencode, opencode fallback
        yield "fallback response"

    with patch("core.backends.codex_code.CodexCodeClient", return_value=fake), \
         patch.object(execute_module, "_stream_to_backend", _mock_stream):
        result = await execute_module.execute_node({
            "task": "plan", "backend": "codex_code", "messages": [],
            "codex_posture": {}, "escalation_backend": "opencode_serve",
        })
    assert result["messages"][0]["content"] == "fallback response"
    assert result["error"] is None


# ─── F9: host secrets never leak into the codex child env ─────────────────────

def test_subprocess_env_strips_host_secrets(tmp_path, monkeypatch):
    """F9: the codex child env must NOT carry host secrets — BOBCLAW_SECRET (the
    gateway<->core vouch key) or the metered ANTHROPIC key (codex talks to the local
    LiteLLM proxy). Benign vars (PATH) are preserved; os.environ is not mutated."""
    monkeypatch.setenv("BOBCLAW_SECRET", "hmac-vouch-key-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "x"))

    env = _client(tmp_path, monkeypatch)._subprocess_env()

    assert "BOBCLAW_SECRET" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "PATH" in env
    assert os.environ.get("BOBCLAW_SECRET") == "hmac-vouch-key-should-not-leak"
