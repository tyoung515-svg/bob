"""BoBClaw build pipeline P3 — sandbox hardening tests (network-free).

Covers the deterministic security boundary: the constrained subprocess env (secrets
stripped from generated-code execution), hard path containment, the static impl
safety gate (pure/stdlib-only/no-I/O), and the Gate-Router build scope threading.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import core.config as cfg
from core.build import skeleton
from core.build.contracts import is_safe_impl
from core.nodes import build_plan
from core.permissions import is_path_within

_C1 = {"name": "addtwo", "signature": "addtwo(a, b)", "doc": "sum",
       "cases": [{"args": [1, 2], "expect": 3}]}


def _mock_backend(monkeypatch, reply: str):
    async def _fake(messages, backend):
        return reply
    monkeypatch.setattr(build_plan, "_send_to_backend", _fake)


# ── constrained subprocess env: secrets stripped ─────────────────────────────

def test_build_env_strips_secrets_keeps_essentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    monkeypatch.setenv("BOBCLAW_SECRET", "topsecret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")
    env = skeleton.build_env(tmp_path)
    # NO inherited credentials reach the generated-code subprocess.
    for secret in ("DEEPSEEK_API_KEY", "BOBCLAW_SECRET", "ANTHROPIC_API_KEY",
                   "AWS_SECRET_ACCESS_KEY"):
        assert secret not in env
    # but the package + the essentials Python/pytest need are present.
    assert env["PYTHONPATH"] == str(tmp_path)
    assert env["PYTHONIOENCODING"] == "utf-8"
    if "PATH" in os.environ:
        assert "PATH" in env


def test_build_env_preserves_essentials(tmp_path, monkeypatch):
    # Allowlisted system vars Python/pytest need on Windows must survive the strip,
    # else the subprocess would break (a future edit dropping one would be caught here).
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE")
    env = skeleton.build_env(tmp_path)
    assert env.get("SYSTEMROOT") == r"C:\Windows"
    assert env.get("COMSPEC")
    assert env.get("PATHEXT")


# ── hard path containment ────────────────────────────────────────────────────

def test_is_path_within(tmp_path):
    inside = tmp_path / "a" / "b"
    inside.mkdir(parents=True)
    assert is_path_within(str(inside), str(tmp_path))          # nested
    assert is_path_within(str(tmp_path), str(tmp_path))        # equal
    assert not is_path_within(str(tmp_path.parent), str(tmp_path))   # outside (parent)
    assert not is_path_within(str(tmp_path / ".."), str(tmp_path))   # resolves outside


# ── static impl safety gate ──────────────────────────────────────────────────

def test_is_safe_impl_accepts_pure_stdlib():
    assert is_safe_impl("def f(x):\n    return math.sqrt(x)")[0]
    assert is_safe_impl("def f(s):\n    return s.upper()")[0]
    assert is_safe_impl("def f():\n    import json\n    return json.dumps({})")[0]
    assert is_safe_impl("from collections import Counter\ndef f(x):\n    return Counter(x)")[0]
    # common benign dunders are NOT blocked (only the escape-gadget ones are)
    assert is_safe_impl("def f(x):\n    return x.__class__.__name__")[0]


def test_is_safe_impl_rejects_indirect_escapes():
    # The vectors the security review demonstrated end-to-end — all must be rejected.
    for src in [
        "def f():\n    return ().__class__.__bases__[0].__subclasses__()",   # subclass walk
        "def f():\n    return __builtins__['open']('x')",                    # __builtins__ name
        "def f():\n    return getattr(__builtins__, 'open')('x')",           # getattr gadget
        "def f():\n    g = open\n    return g('x')",                         # alias
        "def f():\n    return f.__globals__",                               # __globals__ attr
    ]:
        ok, reason = is_safe_impl(src)
        assert not ok and reason, src


def test_is_safe_impl_rejects_param_shadowing_banned_name():
    # Accepted safe-direction false positive: a param named like a banned builtin that is
    # CALLED is rejected (keeps stub, surfaced) — an over-rejection, never an escape.
    assert not is_safe_impl("def f(open):\n    return open('x')")[0]


def test_is_safe_impl_rejects_disallowed_imports():
    assert not is_safe_impl("def f():\n    import os\n    return os.getpid()")[0]
    assert not is_safe_impl("import socket\ndef f():\n    return 1")[0]
    assert not is_safe_impl("def f():\n    from subprocess import run\n    return run")[0]
    assert not is_safe_impl("from . import x\ndef f():\n    return 1")[0]


def test_is_safe_impl_rejects_dangerous_calls():
    for bad in ["open('/etc/passwd')", "eval('1')", "exec('x=1')",
                "__import__('os')", "input()", "compile('1','<s>','eval')"]:
        ok, reason = is_safe_impl(f"def f():\n    return {bad}")
        assert not ok and reason, bad


def test_is_safe_impl_rejects_unparseable():
    assert not is_safe_impl("def f(:\n  pass")[0]


# ── _build_worker enforces the gate (unsafe → keep stub, surfaced) ───────────

async def test_build_worker_rejects_unsafe_impl():
    from core.nodes.worker import worker_node

    async def _send(messages, backend):
        return "def addtwo(a, b):\n    import os\n    return os.getpid()"

    with patch("core.nodes.worker._send_to_backend", _send):
        out = await worker_node({"build_contract": _C1, "subtask_idx": 0,
                                 "backend": "deepseek_v4_flash"})
    entry = out["build_impls"][0]
    assert entry["source"] is None                            # not written
    assert entry["status"].startswith("unsafe")


async def test_build_worker_accepts_safe_impl():
    from core.nodes.worker import worker_node

    async def _send(messages, backend):
        return "def addtwo(a, b):\n    return a + b"

    with patch("core.nodes.worker._send_to_backend", _send):
        out = await worker_node({"build_contract": _C1, "subtask_idx": 0,
                                 "backend": "deepseek_v4_flash"})
    assert out["build_impls"][0]["status"] == "ok"
    assert "return a + b" in out["build_impls"][0]["source"]


# ── Gate-Router build scope ──────────────────────────────────────────────────

async def test_plan_contracts_sets_build_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    _mock_backend(monkeypatch, json.dumps({"units": [_C1]}))
    out = await build_plan.plan_contracts_node({
        "task": "x", "backend": "claude_api", "conversation_id": "c", "build_units": 1})
    scope = out["scope"]
    assert scope["may_touch"] == [out["build_workspace"]]
    assert scope["may_not_touch"]                             # repo root off-limits
    # the workspace really is inside the sandbox root
    assert is_path_within(out["build_workspace"], str(tmp_path))


async def test_plan_contracts_fails_loud_on_workspace_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(build_plan, "is_path_within", lambda *a, **k: False)
    _mock_backend(monkeypatch, json.dumps({"units": [_C1]}))
    out = await build_plan.plan_contracts_node({
        "task": "x", "backend": "claude_api", "build_units": 1})
    assert out.get("error") and "escape" in out["error"].lower()
    assert "build_workspace" not in out                       # nothing written


def test_build_sends_carry_scope():
    from core.nodes.dispatch import _route_after_dispatch
    scope = {"may_touch": ["/ws"], "may_not_touch": ["/repo"], "auto_actions": []}
    sends = _route_after_dispatch({"build_contracts": [_C1], "team": "demo-fleet",
                                   "build_workspace": "/ws", "scope": scope})
    assert sends[0].arg["scope"] == scope


# ── the repair path enforces the SAME gate as the worker (no bypass-by-omission) ──

async def test_repair_path_rejects_unsafe_fix(tmp_path, monkeypatch):
    from core.nodes import build_verify
    skeleton.write_app(tmp_path, [_C1], {})              # failing stub skeleton
    state = {
        "build_contracts": [_C1], "build_workspace": str(tmp_path),
        "verify_report": {"failing": ["addtwo"]}, "repair_round": 0,
        "team": "demo-fleet", "build_impls": [],
    }

    async def _unsafe(messages, backend):
        return json.dumps({"addtwo": "def addtwo(a, b):\n    import os\n    return os.getpid()"})

    monkeypatch.setattr(build_verify, "_send_to_backend", _unsafe)
    out = await build_verify.repair_node(state)
    assert out["build_impls"] == []                      # unsafe fix dropped
    assert out["repair_round"] == 1                      # round still advances (loop bounds)
    fns = (tmp_path / "minikit" / "functions.py").read_text(encoding="utf-8")
    assert "import os" not in fns                         # never written into the app
    assert "raise NotImplementedError" in fns            # stub kept → surfaced as failing
