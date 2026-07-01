"""BoBClaw build pipeline P0 — plan_contracts_node tests.

The backend (apex skeleton call) is mocked; the deterministic skeleton + build-empty
gate run for real against a tmp sandbox (BUILD_WORKSPACE_ROOT monkeypatched to
tmp_path). Asserts the happy path, the fail-loud abort on no contracts, and the
sandbox path-containment invariant.
"""
from __future__ import annotations

import json
from pathlib import Path

import core.config as cfg
from core.nodes import build_plan


def _skeleton_json(*units):
    return json.dumps({"units": list(units)})


_C1 = {"name": "addtwo", "signature": "addtwo(a, b)", "doc": "sum",
       "cases": [{"args": [1, 2], "expect": 3}]}
_C2 = {"name": "neg", "signature": "neg(x)", "doc": "negate",
       "cases": [{"args": [5], "expect": -5}]}


def _mock_backend(monkeypatch, reply: str):
    async def _fake(messages, backend):
        return reply
    monkeypatch.setattr(build_plan, "_send_to_backend", _fake)


async def test_plan_contracts_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    _mock_backend(monkeypatch, _skeleton_json(_C1, _C2))

    out = await build_plan.plan_contracts_node({
        "task": "build a tiny pure-function toolkit",
        "backend": "claude_api",
        "conversation_id": "conv-1",
        "build_units": 2,
    })

    assert "error" not in out
    assert [c["name"] for c in out["build_contracts"]] == ["addtwo", "neg"]
    assert out["repair_round"] == 0
    report = out["verify_report"]
    assert report["phase"] == "skeleton"
    assert report["builds_empty"] is True
    assert report["tests_collected"] == 2
    assert report["units_valid"] == 2
    ws = Path(out["build_workspace"])
    assert ws.exists()
    assert (ws / "minikit" / "functions.py").exists()


async def test_plan_contracts_aborts_on_no_valid_contracts(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    _mock_backend(monkeypatch, "Sorry, here is some prose and no JSON at all.")

    out = await build_plan.plan_contracts_node({
        "task": "x", "backend": "claude_api",
    })

    assert out.get("error")
    assert "build_contracts" not in out
    # Fail-loud BEFORE touching the filesystem — no sandbox is created.
    assert list(tmp_path.iterdir()) == []


async def test_plan_contracts_workspace_is_contained_under_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    _mock_backend(monkeypatch, _skeleton_json(_C1))

    out = await build_plan.plan_contracts_node({
        "task": "x", "backend": "claude_api",
        "conversation_id": "conv-2", "build_units": 1,
    })
    ws = Path(out["build_workspace"]).resolve()
    assert tmp_path.resolve() in ws.parents


async def test_plan_contracts_sanitizes_hostile_conversation_id(tmp_path, monkeypatch):
    """A ``..``-laden conversation id can never escape the sandbox root."""
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    _mock_backend(monkeypatch, _skeleton_json(_C1))

    out = await build_plan.plan_contracts_node({
        "task": "x", "backend": "claude_api",
        "conversation_id": "../../../etc/evil", "build_units": 1,
    })
    ws = Path(out["build_workspace"]).resolve()
    assert tmp_path.resolve() in ws.parents       # still contained
    assert ".." not in out["build_workspace"]


async def test_plan_contracts_fails_loud_on_unbuildable_skeleton(tmp_path, monkeypatch):
    """A parser-admitted but import-breaking contract must drive the build-empty
    fail-loud branch: error set, builds_empty False, the build path ABORTED (no
    repair_round), with contracts + workspace still returned for diagnosis. (Without
    this test a regression to silent success would pass the whole suite.)"""
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    # signature compiles (syntax-only) but NameErrors at import (undefined default).
    # (Has a case so it survives coerce; the undefined NAME is not a banned builtin, so
    # the safety screen passes — it fails only at IMPORT, exercising the build-empty gate.)
    bad = {"name": "bad", "signature": "bad(x=__nope_undefined__)", "doc": "",
           "cases": [{"args": [1], "expect": 1}]}
    _mock_backend(monkeypatch, json.dumps({"units": [bad]}))

    out = await build_plan.plan_contracts_node({
        "task": "x", "backend": "claude_api",
        "conversation_id": "conv-bad", "build_units": 1,
    })

    assert out.get("error") and "failed to build" in out["error"].lower()
    assert out["verify_report"]["builds_empty"] is False
    assert "repair_round" not in out                       # build path aborted
    assert [c["name"] for c in out["build_contracts"]] == ["bad"]   # surfaced for diagnosis
    assert out["build_workspace"]


async def test_plan_contracts_defaults_units_when_unset(tmp_path, monkeypatch):
    """No build_units → falls back to BUILD_DEFAULT_UNITS (parse caps; mock has 1)."""
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg, "BUILD_DEFAULT_UNITS", 10)
    _mock_backend(monkeypatch, _skeleton_json(_C1))

    out = await build_plan.plan_contracts_node({"task": "x", "backend": "claude_api"})
    assert [c["name"] for c in out["build_contracts"]] == ["addtwo"]
    assert out["verify_report"]["builds_empty"] is True
