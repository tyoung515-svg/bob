"""CLI backends × SpawnContext wiring (spawn-context intake, 2026-07-18).

Each client calls the builder before ``create_subprocess_exec`` when its
posture carries a ``spawn_context`` descriptor:

* the spawn cwd is the clean scratch dir holding the rendered context file,
* a CLEAN descriptor (project_access False) suppresses every repo grant the
  legacy posture would have emitted (``--add-dir`` repo, the inlined
  ``<project-briefing>`` charter),
* ``project_access: true`` keeps them (the planner_cc_edit opt-in),
* segregated-home env overrides land in the child env.

A posture WITHOUT a descriptor is covered by the existing per-backend suites —
legacy behavior stays byte-identical there.
"""
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.agy_code import AntigravityClient
from core.backends.claude_code import ClaudeCodeClient
from core.backends.codex_code import CodexCodeClient
from core.backends.kimi_cli import KimiCliClient

_CLEAN = {"spawn_context": {"position": "council_seat", "role": "framer"}}


@pytest.fixture
def roots(tmp_path):
    """Scratch roots + repo-with-charter, patched on the shared config object."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("HOST OPERATOR CHARTER — do not leak", encoding="utf-8")
    patches = {
        "CC_SCRATCH_ROOT": str(tmp_path / "cc"),
        "AGY_SCRATCH_ROOT": str(tmp_path / "agy"),
        "CODEX_SCRATCH_ROOT": str(tmp_path / "codex"),
        "KIMI_CLI_SCRATCH_ROOT": str(tmp_path / "kimi"),
    }
    started = [patch(f"core.spawn_context.config.{k}", v) for k, v in patches.items()]
    for p in started:
        p.start()
    yield {"repo": str(repo), **patches}
    for p in started:
        p.stop()


def _proc(stdout: bytes = b"", stderr: bytes = b"", rc: int = 0):
    proc = MagicMock()
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=rc)
    proc.kill = MagicMock()
    return proc


# ─── claude_code ──────────────────────────────────────────────────────────────

_CC_OK = json.dumps(
    {"result": "ok", "session_id": "s-1", "is_error": False}
).encode("utf-8")


@pytest.mark.asyncio
async def test_cc_clean_descriptor_spawns_from_scratch_with_context_file(roots):
    client = ClaudeCodeClient(
        cli_path="claude", cwd=roots["repo"], timeout=30, conversation_id="conv-cc"
    )
    cap: dict = {}

    async def _exec(*argv, **kw):
        cap["argv"] = argv
        cap["cwd"] = kw.get("cwd")
        cap["env"] = kw.get("env")
        return _proc(_CC_OK)

    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec", side_effect=_exec
    ):
        await client.chat(prompt="q", posture=dict(_CLEAN))

    expected_cwd = os.path.join(roots["CC_SCRATCH_ROOT"], "conv-cc")
    assert cap["cwd"] == expected_cwd
    body = open(os.path.join(expected_cwd, "CLAUDE.md"), encoding="utf-8").read()
    assert "**framer**" in body and "host system" in body
    assert "--add-dir" not in cap["argv"]  # no repo grant on a clean spawn


@pytest.mark.asyncio
async def test_cc_clean_descriptor_suppresses_charter_briefing(roots):
    """scratch_write posture + CLEAN descriptor: no <project-briefing> inline,
    no repo --add-dir — the generic flag translation runs instead."""
    client = ClaudeCodeClient(
        cli_path="claude", cwd=roots["repo"], timeout=30, conversation_id="c2"
    )
    posture = {"mode": "scratch_write", "spawn_context": {"position": "worker", "project_access": False}}
    cap: dict = {}

    async def _exec(*argv, **kw):
        cap["argv"] = argv
        proc = _proc(_CC_OK)
        cap["proc"] = proc
        return proc

    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec", side_effect=_exec
    ):
        await client.chat(prompt="the task", posture=posture)

    sent = cap["proc"].communicate.call_args.kwargs.get("input") or b""
    assert b"project-briefing" not in sent
    assert b"OPERATOR CHARTER" not in sent
    assert "--add-dir" not in cap["argv"]


@pytest.mark.asyncio
async def test_cc_project_access_keeps_scratch_write_repo_grant(roots):
    """planner_cc_edit acceptance: project-aware opt-in still sees the repo."""
    client = ClaudeCodeClient(
        cli_path="claude", cwd=roots["repo"], timeout=30, conversation_id="c3"
    )
    posture = {
        "mode": "scratch_write",
        "spawn_context": {"position": "planner", "template": "planner", "project_access": True},
    }
    cap: dict = {}

    async def _exec(*argv, **kw):
        cap["argv"] = argv
        proc = _proc(_CC_OK)
        cap["proc"] = proc
        return proc

    with patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec", side_effect=_exec
    ):
        await client.chat(prompt="plan it", posture=posture)

    argv = list(cap["argv"])
    assert "--add-dir" in argv and roots["repo"] in argv  # repo still readable
    sent = cap["proc"].communicate.call_args.kwargs.get("input") or b""
    assert b"OPERATOR CHARTER" in sent  # briefing kept for the project-aware lane


@pytest.mark.asyncio
async def test_cc_env_gets_config_dir_and_still_strips_metered_key(roots, tmp_path, monkeypatch):
    cc_home = tmp_path / "cc-config"
    cc_home.mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAK")
    client = ClaudeCodeClient(cli_path="claude", cwd=roots["repo"], timeout=30)
    cap: dict = {}

    async def _exec(*argv, **kw):
        cap["env"] = kw.get("env")
        return _proc(_CC_OK)

    with patch("core.spawn_context.config.CC_CONFIG_DIR", str(cc_home)), patch(
        "core.backends.claude_code.asyncio.create_subprocess_exec", side_effect=_exec
    ):
        await client.chat(prompt="q", posture=dict(_CLEAN))

    assert cap["env"]["CLAUDE_CONFIG_DIR"] == str(cc_home)
    assert "ANTHROPIC_API_KEY" not in cap["env"]  # subscription strip still wins


# ─── agy_code ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agy_clean_descriptor_renders_context_and_drops_repo_grant(roots):
    client = AntigravityClient(
        cli_path="agy", cwd=roots["repo"], timeout=30, conversation_id="conv-agy"
    )
    posture = {"read_repo": True, "spawn_context": {"position": "council_seat", "role": "stress", "project_access": False}}
    cap: dict = {}

    async def _exec(*argv, **kw):
        cap["argv"] = argv
        return _proc()

    with patch(
        "core.backends.agy_code.asyncio.create_subprocess_exec", side_effect=_exec
    ), patch.object(AntigravityClient, "_capture_uuid", return_value="u-1"), patch.object(
        AntigravityClient, "_read_reply", return_value="answer"
    ):
        out = await client.chat(prompt="q", posture=posture)

    assert out["text"] == "answer"
    argv = list(cap["argv"])
    assert "--add-dir" not in argv  # read_repo suppressed by the clean descriptor
    prompt_arg = argv[argv.index("-p") + 1]
    assert "OPERATOR CHARTER" not in prompt_arg  # no charter inline
    work_dir = os.path.join(roots["AGY_SCRATCH_ROOT"], "conv-agy")
    body = open(os.path.join(work_dir, "GEMINI.md"), encoding="utf-8").read()
    assert "**stress**" in body
    assert os.path.isfile(os.path.join(work_dir, "AGENTS.md"))


@pytest.mark.asyncio
async def test_agy_project_access_keeps_repo_grant(roots):
    client = AntigravityClient(
        cli_path="agy", cwd=roots["repo"], timeout=30, conversation_id="c-agy2"
    )
    posture = {"read_repo": True, "spawn_context": {"position": "planner", "project_access": True}}
    cap: dict = {}

    async def _exec(*argv, **kw):
        cap["argv"] = argv
        return _proc()

    with patch(
        "core.backends.agy_code.asyncio.create_subprocess_exec", side_effect=_exec
    ), patch.object(AntigravityClient, "_capture_uuid", return_value="u-2"), patch.object(
        AntigravityClient, "_read_reply", return_value="planned"
    ):
        await client.chat(prompt="q", posture=posture)

    assert "--add-dir" in cap["argv"] and roots["repo"] in cap["argv"]


# ─── codex_code ───────────────────────────────────────────────────────────────

_CODEX_EVENTS = "\n".join(
    json.dumps(e)
    for e in (
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
    )
).encode("utf-8")


@pytest.mark.asyncio
async def test_codex_clean_descriptor_pins_read_only_and_writes_agents_md(roots):
    client = CodexCodeClient(
        cli_path="codex", cwd=roots["repo"], timeout=30, conversation_id="conv-cx"
    )
    posture = {"mode": "scratch_write", "spawn_context": {"position": "worker", "project_access": False}}
    cap: dict = {}

    async def _exec(*argv, **kw):
        cap["argv"] = argv
        proc = _proc(_CODEX_EVENTS)
        cap["proc"] = proc
        return proc

    with patch(
        "core.backends.codex_code.asyncio.create_subprocess_exec", side_effect=_exec
    ):
        out = await client.chat(prompt="q", posture=posture)

    assert out["text"] == "done"
    argv = list(cap["argv"])
    assert "--add-dir" not in argv
    assert argv[argv.index("-s") + 1] == "read-only"  # not workspace-write
    work_dir = os.path.join(roots["CODEX_SCRATCH_ROOT"], "conv-cx")
    assert os.path.isfile(os.path.join(work_dir, "AGENTS.md"))
    sent = cap["proc"].communicate.call_args.kwargs.get("input") or b""
    assert b"OPERATOR CHARTER" not in sent


# ─── kimi_cli ─────────────────────────────────────────────────────────────────

_KIMI_OK = (
    json.dumps({"role": "assistant", "content": "kimi says"})
    + "\n"
    + json.dumps({"role": "meta", "type": "session.resume_hint", "session_id": "k-1"})
).encode("utf-8")


@pytest.mark.asyncio
async def test_kimi_descriptor_moves_cwd_off_the_project_dir(roots):
    client = KimiCliClient(
        cli_path="kimi", cwd=roots["repo"], timeout=30, conversation_id="conv-k"
    )
    cap: dict = {}

    async def _exec(*argv, **kw):
        cap["cwd"] = kw.get("cwd")
        return _proc(_KIMI_OK)

    with patch(
        "core.backends.kimi_cli.asyncio.create_subprocess_exec", side_effect=_exec
    ):
        out = await client.chat(prompt="q", posture=dict(_CLEAN))

    assert out["text"] == "kimi says"
    expected = os.path.join(roots["KIMI_CLI_SCRATCH_ROOT"], "conv-k")
    assert cap["cwd"] == expected  # NOT the repo/project dir
    assert os.path.isfile(os.path.join(expected, "AGENTS.md"))


@pytest.mark.asyncio
async def test_kimi_legacy_posture_keeps_project_cwd(roots):
    client = KimiCliClient(cli_path="kimi", cwd=roots["repo"], timeout=30)
    cap: dict = {}

    async def _exec(*argv, **kw):
        cap["cwd"] = kw.get("cwd")
        return _proc(_KIMI_OK)

    with patch(
        "core.backends.kimi_cli.asyncio.create_subprocess_exec", side_effect=_exec
    ):
        await client.chat(prompt="q", posture={})

    assert cap["cwd"] == roots["repo"]
