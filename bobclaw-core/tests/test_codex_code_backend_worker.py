"""BoBClaw Core — Unit tests for CodexCodeClient (codex exec subprocess backend).

All worker socket I/O is mocked (``execute_async`` patched) — zero
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
    assert "--json" in argv and "-o" not in argv
    assert argv[argv.index("-p") + 1] == "glm"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "--skip-git-repo-check" in argv


def test_build_argv_model_routes_litellm(tmp_path, monkeypatch):
    # Legacy: a NON-GPT model id still emits the (retired) litellm route.
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={"model": "glm-5.2"}, outfile="/o.txt",
                         work_dir="/w", resume_thread=None)
    assert argv[argv.index("-m") + 1] == "glm-5.2"
    assert "model_provider=litellm" in argv


def test_build_argv_gpt_model_routes_native(tmp_path, monkeypatch):
    # GPT-only cut: a GPT model id routes NATIVELY (`-p gpt -m <model>`), never litellm.
    # This older-generation gpt-5.4-mini fixture intentionally covers the generic `startswith("gpt")`
    # prefix; current GPT-5.6 fleet seats are covered by test_build_argv_gpt56_{sol,terra,luna}_native.
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={"model": "gpt-5.4-mini"}, outfile="/o.txt",
                         work_dir="/w", resume_thread=None)
    assert argv[argv.index("-p") + 1] == "gpt"
    assert argv[argv.index("-m") + 1] == "gpt-5.4-mini"
    assert "model_provider=litellm" not in argv


def test_build_argv_empty_posture_defaults_gpt(tmp_path, monkeypatch):
    # GPT-only by construction: an unset posture defaults to the native `gpt` profile,
    # NEVER the shared ~/.codex litellm base default.
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={}, outfile="/o.txt", work_dir="/w", resume_thread=None)
    assert argv[argv.index("-p") + 1] == "gpt"
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
    assert "--json" in argv and "-o" not in argv and "--skip-git-repo-check" in argv
    for rejected in ("-p", "-s", "-C", "--color", "--add-dir"):
        assert rejected not in argv


# ─── chat ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_success(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    worker = AsyncMock(return_value={"stdout": _events(reply="FROM_SOCKET"), "stderr": b"", "returncode": 0})
    with patch("core.backends.codex_code.worker_client.execute_async", worker):
        result = await c.chat(prompt="hello")
    assert result["text"] == "FROM_SOCKET" and result["session_id"] == "t1"
    assert worker.await_args.args == ("codex",)
    assert worker.await_args.kwargs["input"] == "hello"
    assert "hello" not in worker.await_args.kwargs["args"]
    assert "env" not in worker.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,error,rc,exception", [
    ("", "rate limit 429", 1, CodexThrottled),
    ("", "invalid model", 1, CodexError),
    ("", None, 0, CodexError),
])
async def test_chat_worker_errors(tmp_path, monkeypatch, reply, error, rc, exception):
    c = _client(tmp_path, monkeypatch)
    worker = AsyncMock(return_value={"stdout": _events(reply=reply,error=error), "stderr": b"", "returncode": rc})
    with patch("core.backends.codex_code.worker_client.execute_async", worker):
        with pytest.raises(exception):
            await c.chat(prompt="hello")


@pytest.mark.asyncio
async def test_worker_unavailable_never_falls_back(tmp_path, monkeypatch):
    from core.worker.client import WorkerUnavailable
    c = _client(tmp_path, monkeypatch)
    with patch("core.backends.codex_code.worker_client.execute_async", AsyncMock(side_effect=WorkerUnavailable("unavailable"))):
        assert not await c.health_check()
        with pytest.raises(CodexError, match="unavailable"):
            await c.chat(prompt="hello")


@pytest.mark.asyncio
async def test_worker_timeout_receipt(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    receipt = {"stdout": b"", "stderr": b"", "returncode": -9, "killed": True, "reason": "timeout"}
    with patch("core.backends.codex_code.worker_client.execute_async", AsyncMock(return_value=receipt)):
        with pytest.raises(CodexError, match="timeout"):
            await c.chat(prompt="hello")


@pytest.mark.asyncio
async def test_health_checks_worker_profile(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    worker = AsyncMock(return_value={"stdout": b"codex-cli test", "stderr": b"", "returncode": 0})
    with patch("core.backends.codex_code.worker_client.execute_async", worker):
        assert await c.health_check()
    assert worker.await_args.kwargs["args"] == ["--version"]


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
    # GPT-only cut: planner-codex is now the GPT-5.6 generation via the native `gpt` profile.
    assert f.codex_posture.get("profile") == "gpt"


def test_dispatch_threads_codex_posture_into_worker_send():
    from core.nodes.dispatch import _route_after_dispatch
    sends = _route_after_dispatch({
        "fanout_subtasks": [{"idx": 0, "text": "a"}, {"idx": 1, "text": "b"}],
        "face_id": "worker-codex",
        "backend": "codex_code",
    })
    # worker-codex is the native GPT mid-tier worker (gpt-5.6-terra), threaded as a model.
    assert all(s.arg.get("codex_posture", {}).get("model") == "gpt-5.6-terra" for s in sends)


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
async def test_execute_node_codex_code_throttle_refuses_excluded_escalation(mock_redis):
    """An old face's OpenCode fallback is refused in this isolated release."""
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
    assert 'unavailable' in result['error']
    assert 'fallback response' not in str(result['messages'])


# ─── F9: host secrets never leak into the codex child env ─────────────────────

def test_build_argv_gpt56_sol_native(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={"model": "gpt-5.6-sol"}, outfile="/o.txt",
                         work_dir="/w", resume_thread=None)
    assert argv[argv.index("-p") + 1] == "gpt"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-sol"
    assert "model_provider=litellm" not in argv


def test_build_argv_gpt56_terra_native(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={"model": "gpt-5.6-terra"}, outfile="/o.txt",
                         work_dir="/w", resume_thread=None)
    assert argv[argv.index("-p") + 1] == "gpt"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-terra"
    assert "model_provider=litellm" not in argv


def test_build_argv_gpt56_luna_native(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    argv = c._build_argv(posture={"model": "gpt-5.6-luna"}, outfile="/o.txt",
                         work_dir="/w", resume_thread=None)
    assert argv[argv.index("-p") + 1] == "gpt"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"
    assert "model_provider=litellm" not in argv


def test_dispatch_threads_named_terra_face_posture():
    from core.nodes.dispatch import _route_after_dispatch

    sends = _route_after_dispatch({
        "fanout_subtasks": [{"idx": 0, "text": "a"}, {"idx": 1, "text": "b"}],
        "face_id": "worker-gpt56-terra",
        "backend": "codex_code",
    })

    assert all(s.arg.get("codex_posture", {}).get("model") == "gpt-5.6-terra" for s in sends)


@pytest.fixture(autouse=True)
def packaged_worker_mode(monkeypatch):
    monkeypatch.setenv("BOB_EXECUTION_BOUNDARY", "worker")
