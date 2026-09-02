"""Spawn-context dispatch threading (intake 2026-07-18, slice 4).

Council seats publish their deliberation identity; the stateless CLI branches
default every fan-out spawn to clean-scratch worker framing; state-aware
planner dispatch merges the face YAML ``spawn_context:`` block; the council
seat table is CLI-first with an env-JSON override.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import _seat_backends_with_env_override
from core.nodes.execute import (
    _cli_spawn_posture,
    _default_send_to_backend,
    _face_spawn_posture,
)
from core.nodes.panel import make_backend_fn, panel_worker_node
from core.spawn_context import active_spawn_descriptor, spawn_descriptor


# ─── council seats publish their posture ──────────────────────────────────────

@pytest.mark.asyncio
async def test_panel_worker_publishes_seat_descriptor():
    seen: dict = {}

    async def _capture(messages, backend, model_override=None):
        seen["descriptor"] = active_spawn_descriptor()
        return "seat says"

    sub = {
        "seat_posture": "framer",
        "backend": "claude_code",
        "fallback_chain": [],
        "task": "PROTOCOLS...\nTOPIC: the question",
        "topic": "the question",
        "seat_idx": 0,
        "panel_round": 0,
        "messages": [],
    }
    with patch("core.nodes.panel._send_to_backend", _capture):
        delta = await panel_worker_node(sub)

    assert delta["panel_results"][0]["text"] == "seat says"
    desc = seen["descriptor"]
    assert desc == {
        "position": "council_seat",
        "role": "framer",
        "template": "council_seat",
        "task": "the question",
    }
    # Scope closed after the node — nothing leaks to the next caller.
    assert active_spawn_descriptor() is None


@pytest.mark.asyncio
async def test_panel_worker_descriptor_covers_fallback_chain():
    calls: list = []

    async def _flaky(messages, backend, model_override=None):
        calls.append((backend, active_spawn_descriptor()["role"]))
        if backend == "claude_code":
            raise RuntimeError("throttled")
        return "fallback answer"

    sub = {
        "seat_posture": "stress",
        "backend": "claude_code",
        "fallback_chain": ["deepseek_v4_flash"],
        "task": "t",
        "topic": "t",
        "seat_idx": 1,
        "panel_round": 0,
        "messages": [],
    }
    with patch("core.nodes.panel._send_to_backend", _flaky):
        delta = await panel_worker_node(sub)

    assert delta["panel_results"][0]["text"] == "fallback answer"
    # Every candidate in the chain saw the SAME seat identity.
    assert calls == [("claude_code", "stress"), ("deepseek_v4_flash", "stress")]


@pytest.mark.asyncio
async def test_make_backend_fn_descriptor_scope():
    seen: dict = {}

    async def _capture(messages, backend, model_override=None):
        seen["descriptor"] = active_spawn_descriptor()
        return "voice"

    with patch("core.nodes.panel._send_to_backend", _capture):
        fn = make_backend_fn(
            "claude_code",
            {"position": "council_seat", "role": "synth", "template": "council_seat"},
        )
        out = await fn("system", "user msg")

    assert out == "voice"
    assert seen["descriptor"]["role"] == "synth"


@pytest.mark.asyncio
async def test_make_backend_fn_without_descriptor_is_legacy():
    seen: dict = {}

    async def _capture(messages, backend, model_override=None):
        seen["descriptor"] = active_spawn_descriptor()
        return "voice"

    with patch("core.nodes.panel._send_to_backend", _capture):
        await make_backend_fn("minimax")("s", "u")
    assert seen["descriptor"] is None


# ─── stateless CLI branches default to clean worker framing ───────────────────

def test_cli_spawn_posture_defaults_to_worker():
    posture = _cli_spawn_posture()
    assert posture == {"spawn_context": {"position": "worker"}}
    posture = _cli_spawn_posture("glm-5.2")
    assert posture["model"] == "glm-5.2"
    assert posture["spawn_context"] == {"position": "worker"}


def test_cli_spawn_posture_prefers_published_descriptor():
    with spawn_descriptor({"position": "council_seat", "role": "wildcard"}):
        posture = _cli_spawn_posture()
    assert posture["spawn_context"]["role"] == "wildcard"


@pytest.mark.asyncio
async def test_stateless_claude_code_branch_injects_descriptor():
    fake = MagicMock()
    fake.chat = AsyncMock(return_value={"text": "ok", "session_id": "s"})
    with patch("core.backends.claude_code.ClaudeCodeClient", return_value=fake):
        out = await _default_send_to_backend(
            [{"role": "user", "content": "hi"}], "claude_code"
        )
    assert out == "ok"
    posture = fake.chat.call_args.kwargs["posture"]
    assert posture["spawn_context"] == {"position": "worker"}


@pytest.mark.asyncio
async def test_stateless_kimi_branch_injects_descriptor_and_model():
    fake = MagicMock()
    fake.chat = AsyncMock(return_value={"text": "k", "session_id": None})
    with patch("core.backends.kimi_cli.KimiCliClient", return_value=fake):
        await _default_send_to_backend(
            [{"role": "user", "content": "hi"}], "kimi_cli", "kimi-k2.7"
        )
    posture = fake.chat.call_args.kwargs["posture"]
    assert posture["model"] == "kimi-k2.7"
    assert posture["spawn_context"]["position"] == "worker"


# ─── face YAML spawn_context: block (state-aware planner tier) ────────────────

def test_face_spawn_posture_merges_planner_cc_edit_block():
    """planner-cc-edit ships the project-aware opt-in in its YAML."""
    posture = {"mode": "scratch_write", "permission_mode": "acceptEdits"}
    merged = _face_spawn_posture(posture, {"face_id": "planner-cc-edit"})
    assert merged["spawn_context"]["project_access"] is True
    assert merged["spawn_context"]["template"] == "planner"
    # Original posture object is not mutated.
    assert "spawn_context" not in posture


def test_face_spawn_posture_existing_descriptor_wins():
    posture = {"spawn_context": {"position": "custom"}}
    merged = _face_spawn_posture(posture, {"face_id": "planner-cc-edit"})
    assert merged["spawn_context"] == {"position": "custom"}


def test_face_spawn_posture_unknown_face_is_noop():
    posture = {"permission_mode": "plan"}
    assert _face_spawn_posture(posture, {"face_id": "nope"}) == posture
    assert _face_spawn_posture(posture, {}) == posture


# ─── seat table: CLI-first defaults + env override ────────────────────────────

def test_seat_env_override_merges_per_seat():
    defaults = {
        "framer": {"backend": "claude_code", "fallback_chain": ["deepseek_v4_flash"]},
        "stress": {"backend": "agy_code", "fallback_chain": ["gemini_flash"]},
    }
    merged = _seat_backends_with_env_override(
        defaults, '{"framer": {"backend": "claude_api"}}'
    )
    assert merged["framer"]["backend"] == "claude_api"
    # Unnamed fields + unnamed seats keep the defaults.
    assert merged["framer"]["fallback_chain"] == ["deepseek_v4_flash"]
    assert merged["stress"]["backend"] == "agy_code"
    # Defaults dict itself is untouched.
    assert defaults["framer"]["backend"] == "claude_code"


def test_seat_env_override_malformed_json_keeps_defaults():
    defaults = {"framer": {"backend": "claude_code", "fallback_chain": []}}
    assert _seat_backends_with_env_override(defaults, "{not json") == defaults
    assert _seat_backends_with_env_override(defaults, '["a list"]') == defaults
    assert _seat_backends_with_env_override(defaults, "") == defaults
