"""BoBClaw build pipeline P3.5 — dockerized verify sandbox tests.

The argv construction + mode dispatch are unit-tested without Docker. A real
end-to-end isolation test (the gate runs in a container; an escape-attempt impl
provably cannot read host files / reach the network) is gated on Docker + the image
being available, and SKIPPED otherwise so the suite never depends on Docker.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.graph import END

import core.config as cfg
from core.build import sandbox, skeleton

_C1 = {"name": "addtwo", "signature": "addtwo(a, b)", "doc": "sum",
       "cases": [{"args": [1, 2], "expect": 3}]}


# ── argv construction (the isolation flags) ──────────────────────────────────

def test_docker_argv_is_hardened(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_SANDBOX_IMAGE", "img:test")
    monkeypatch.setattr(cfg, "BUILD_SANDBOX_MEMORY", "256m")
    monkeypatch.setattr(cfg, "BUILD_SANDBOX_PIDS", 64)
    argv = sandbox._docker_argv(tmp_path, ["python", "-m", "pytest"], name="bobclaw-build-x")
    s = " ".join(argv)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--name" in argv and argv[argv.index("--name") + 1] == "bobclaw-build-x"
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv and "no-new-privileges" in argv
    assert "--read-only" in argv
    assert "--tmpfs" in argv
    assert "--memory" in argv and "256m" in argv
    assert "--pids-limit" in argv and "64" in argv
    # ONLY the workspace is mounted (resolved), READ-ONLY, at /work; no other -v.
    assert argv.count("-v") == 1
    assert f"{Path(tmp_path).resolve()}:/work:ro" in argv
    assert "img:test" in argv
    # NO host secrets / env leaked in — only the three explicit PYTHON* vars.
    assert s.count("-e ") == 3
    assert "PYTHONPATH=/work" in argv
    assert not any("API_KEY" in a or "SECRET" in a for a in argv)


# ── mode resolution ──────────────────────────────────────────────────────────

def test_resolve_mode_subprocess_forced(monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "subprocess")
    assert sandbox.resolve_mode() == "subprocess"


def test_resolve_mode_docker_forced_unavailable_raises(monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "docker")
    monkeypatch.setattr(sandbox, "docker_ready", lambda: False)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.resolve_mode()


def test_resolve_mode_auto_falls_back_to_subprocess(monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "auto")
    monkeypatch.setattr(sandbox, "docker_ready", lambda: False)
    assert sandbox.resolve_mode() == "subprocess"      # loud warning, never fails


def test_resolve_mode_auto_uses_docker_when_ready(monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "auto")
    monkeypatch.setattr(sandbox, "docker_ready", lambda: True)
    assert sandbox.resolve_mode() == "docker"


# ── dispatch: subprocess mode delegates to the host runner ───────────────────

def test_subprocess_mode_delegates_to_host(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "subprocess")
    skeleton.write_app(tmp_path, [_C1], {"addtwo": "def addtwo(a, b):\n    return a + b"})
    assert sandbox.build_empty_ok(tmp_path) is True
    gate = sandbox.run_pytest(tmp_path, timeout=120)
    assert gate["passed"] == 1 and gate["failed"] == 0


# ── dispatch: docker mode builds the container command + parses its output ───

def test_docker_mode_runs_container_and_parses(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "docker")
    monkeypatch.setattr(sandbox, "docker_ready", lambda: True)

    class _Proc:
        returncode = 0
        stdout = "1 passed in 0.01s"
        stderr = ""

    calls = {}

    def _fake_run(argv, **kw):
        calls["argv"] = argv
        return _Proc()

    monkeypatch.setattr(sandbox.subprocess, "run", _fake_run)
    gate = sandbox.run_pytest(tmp_path, timeout=120)
    assert gate["passed"] == 1
    assert calls["argv"][:2] == ["docker", "run"]      # really went through docker
    assert "pytest" in calls["argv"]


def test_docker_mode_build_empty_and_cli(tmp_path, monkeypatch):
    # build_empty_ok + run_cli docker dispatch (return-shaping + inner command).
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "docker")
    monkeypatch.setattr(sandbox, "docker_ready", lambda: True)

    class _Proc:
        returncode = 0
        stdout = "minikit demo"
        stderr = ""

    calls: list[list[str]] = []

    def _fake_run(argv, **kw):
        calls.append(argv)
        return _Proc()

    monkeypatch.setattr(sandbox.subprocess, "run", _fake_run)
    assert sandbox.build_empty_ok(tmp_path, timeout=60) is True
    runs, out = sandbox.run_cli(tmp_path, timeout=60)
    assert runs is True and "minikit demo" in out
    assert all(c[:2] == ["docker", "run"] for c in calls)
    assert any("import minikit" in " ".join(c) for c in calls)   # build-empty inner
    assert any("minikit.cli" in " ".join(c) for c in calls)      # cli inner


async def test_verify_node_fatal_on_forced_docker_unavailable(tmp_path, monkeypatch):
    # The central P3.5 fail-loud contract THROUGH verify_node: BUILD_SANDBOX=docker +
    # daemon/image unavailable → a terminal FATAL report, surfaced once, never a silent
    # host fallback that would run LLM code un-isolated.
    from core.nodes import build_verify
    from core.nodes.build_verify import _route_after_verify

    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "docker")
    monkeypatch.setattr(sandbox, "docker_ready", lambda: False)
    skeleton.write_app(tmp_path, [_C1], {"addtwo": "def addtwo(a, b):\n    return a + b"})
    state = {"build_workspace": str(tmp_path), "repair_round": 0, "build_contracts": [_C1]}

    with patch("core.nodes.build_verify._append_agent_turn_event", AsyncMock()):
        out = await build_verify.verify_node(state)
    assert out["verify_report"]["fatal"] is True
    assert out.get("error") and "unavailable" in out["error"].lower()
    assert out["messages"]                                # surfaced once
    state.update(out)
    assert _route_after_verify(state) == END             # terminal, no host fallback


# ── real end-to-end isolation (skipped unless Docker + image are present) ─────

@pytest.mark.skipif(not sandbox.docker_ready(),
                    reason="Docker daemon + build-sandbox image not available")
def test_real_container_isolation(tmp_path, monkeypatch):
    """The gate runs in a real container; an impl that tries to read an absolute host
    path / open a socket is CONFINED (no host fs, --network none) — proven by the gate
    result, while a clean impl passes."""
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "docker")
    # a clean impl passes through the real container gate
    skeleton.write_app(tmp_path, [_C1], {"addtwo": "def addtwo(a, b):\n    return a + b"})
    assert sandbox.build_empty_ok(tmp_path) is True
    gate = sandbox.run_pytest(tmp_path, timeout=180)
    assert gate["passed"] == 1 and gate["failed"] == 0
    runs, _ = sandbox.run_cli(tmp_path)
    assert runs is True
