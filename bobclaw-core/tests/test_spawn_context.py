"""SpawnContext builder — per-position CLI spawn context injection.

Covers the intake acceptance line "Unit tests for SpawnContext rendering":
template resolution + rendering, per-backend context filenames, clean scratch
cwd creation, segregated-home env overrides, project-access derivation, the
descriptor plumbing (posture key + ContextVar scope), and the fail-soft paths
(missing templates, malformed descriptors).
"""
import os
from unittest.mock import patch

import pytest

from core import spawn_context as sc_mod
from core.spawn_context import (
    SpawnContext,
    active_spawn_descriptor,
    build_spawn_context,
    legacy_project_access,
    resolve_spawn_context,
    spawn_descriptor,
)


@pytest.fixture
def scratch_roots(tmp_path):
    """Point every backend scratch root (and the template dir) at tmp_path."""
    roots = {
        "CC_SCRATCH_ROOT": str(tmp_path / "cc"),
        "AGY_SCRATCH_ROOT": str(tmp_path / "agy"),
        "CODEX_SCRATCH_ROOT": str(tmp_path / "codex"),
        "KIMI_CLI_SCRATCH_ROOT": str(tmp_path / "kimi"),
    }
    patchers = [patch.object(sc_mod.config, k, v) for k, v in roots.items()]
    for p in patchers:
        p.start()
    yield roots
    for p in patchers:
        p.stop()


# ─── rendering / template resolution ─────────────────────────────────────────

def test_council_seat_template_renders_role_and_prohibition(scratch_roots):
    sc = build_spawn_context(
        backend="claude_code",
        position="council_seat",
        role="framer",
        task="Should the memory fence stay mandatory?",
        conversation_id="conv-1",
    )
    body = sc.context_files["CLAUDE.md"]
    assert "**framer**" in body
    assert "host system" in body  # the do-not-discuss-the-host constraint
    assert "Should the memory fence stay mandatory?" in body
    # The rendered file is actually ON DISK in the spawn cwd.
    with open(os.path.join(sc.cwd, "CLAUDE.md"), encoding="utf-8") as fh:
        assert fh.read() == body


def test_unknown_position_falls_back_to_default_template(scratch_roots):
    sc = build_spawn_context(backend="codex_code", position="no_such_position")
    assert "Spawn context" in sc.context_files["AGENTS.md"]
    assert "{{" not in sc.context_files["AGENTS.md"]  # every placeholder resolved


def test_explicit_template_wins_over_position(scratch_roots):
    sc = build_spawn_context(
        backend="claude_code", position="council_seat", template="worker"
    )
    assert "Worker spawn" in sc.context_files["CLAUDE.md"]


def test_template_name_traversal_is_flattened(scratch_roots):
    # "../secrets" sanitizes to a non-existent name -> default template, never
    # a path escape.
    sc = build_spawn_context(backend="claude_code", template="../secrets")
    assert "Spawn context" in sc.context_files["CLAUDE.md"]


def test_missing_template_dir_uses_embedded_fallback(scratch_roots, tmp_path):
    with patch.object(sc_mod.config, "SPAWN_CONTEXT_DIR", str(tmp_path / "gone")):
        sc = build_spawn_context(backend="kimi_cli", position="worker")
    assert "spawn plumbing" in sc.context_files["AGENTS.md"]


def test_task_framing_truncated(scratch_roots):
    sc = build_spawn_context(
        backend="claude_code", position="worker", task="x" * 10_000
    )
    body = sc.context_files["CLAUDE.md"]
    assert "task framing truncated" in body
    assert len(body) < 10_000


# ─── per-backend context filenames ────────────────────────────────────────────

@pytest.mark.parametrize(
    "backend,expected",
    [
        ("claude_code", {"CLAUDE.md"}),
        ("agy_code", {"GEMINI.md", "AGENTS.md"}),
        ("codex_code", {"AGENTS.md"}),
        ("kimi_cli", {"AGENTS.md"}),
    ],
)
def test_context_filenames_per_backend(scratch_roots, backend, expected):
    sc = build_spawn_context(backend=backend, position="worker")
    assert set(sc.context_files) == expected
    for name in expected:
        assert os.path.isfile(os.path.join(sc.cwd, name))


# ─── scratch cwd ──────────────────────────────────────────────────────────────

def test_cwd_is_under_backend_scratch_root(scratch_roots):
    sc = build_spawn_context(
        backend="agy_code", position="worker", conversation_id="conv-9"
    )
    assert sc.cwd == os.path.join(scratch_roots["AGY_SCRATCH_ROOT"], "conv-9")
    assert os.path.isdir(sc.cwd)


def test_explicit_scratch_dir_wins(scratch_roots, tmp_path):
    """Backends whose cwd is a capture key (agy uuid-by-cwd) pass their own dir."""
    mine = str(tmp_path / "my-work-dir")
    sc = build_spawn_context(backend="agy_code", position="worker", scratch_dir=mine)
    assert sc.cwd == mine
    assert os.path.isfile(os.path.join(mine, "GEMINI.md"))


def test_conversation_id_is_sanitized(scratch_roots):
    sc = build_spawn_context(
        backend="claude_code", position="worker", conversation_id="../../evil"
    )
    assert ".." not in os.path.relpath(sc.cwd, scratch_roots["CC_SCRATCH_ROOT"])


# ─── env overrides (segregated homes) ─────────────────────────────────────────

def test_agy_env_overrides_set_home_and_userprofile_when_seeded(scratch_roots, tmp_path):
    home = tmp_path / "agy-home"
    home.mkdir()
    with patch.object(sc_mod.config, "AGY_HOME", str(home)):
        sc = build_spawn_context(backend="agy_code", position="worker")
    assert sc.env_overrides == {"USERPROFILE": str(home), "HOME": str(home)}


def test_agy_env_overrides_empty_when_unseeded(scratch_roots, tmp_path):
    with patch.object(sc_mod.config, "AGY_HOME", str(tmp_path / "missing")):
        sc = build_spawn_context(backend="agy_code", position="worker")
    assert sc.env_overrides == {}


def test_claude_env_overrides_use_cc_config_dir(scratch_roots, tmp_path):
    home = tmp_path / "cc-config"
    home.mkdir()
    with patch.object(sc_mod.config, "CC_CONFIG_DIR", str(home)):
        sc = build_spawn_context(backend="claude_code", position="worker")
    assert sc.env_overrides == {"CLAUDE_CONFIG_DIR": str(home)}


def test_codex_env_overrides_use_codex_home(scratch_roots, tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    with patch.object(sc_mod.config, "CODEX_HOME", str(home)):
        sc = build_spawn_context(backend="codex_code", position="worker")
    assert sc.env_overrides == {"CODEX_HOME": str(home)}


def test_kimi_has_no_home_override(scratch_roots):
    sc = build_spawn_context(backend="kimi_cli", position="worker")
    assert sc.env_overrides == {}


# ─── project access derivation ────────────────────────────────────────────────

def test_project_access_defaults_false(scratch_roots):
    assert build_spawn_context(backend="claude_code").project_access is False


def test_project_access_explicit_flag(scratch_roots):
    sc = build_spawn_context(backend="claude_code", project_access=True)
    assert sc.project_access is True


@pytest.mark.parametrize(
    "posture",
    [{"mode": "scratch_write"}, {"read_repo": True}, {"brief": True}],
)
def test_project_access_derived_from_legacy_posture(scratch_roots, posture):
    assert legacy_project_access(posture) is True
    sc = build_spawn_context(backend="claude_code", face_posture=posture)
    assert sc.project_access is True


def test_descriptor_project_access_false_overrides_legacy(scratch_roots):
    """An explicit project_access: false in the descriptor beats legacy keys."""
    posture = {"brief": True, "spawn_context": {"project_access": False}}
    sc = resolve_spawn_context("claude_code", posture)
    assert sc is not None and sc.project_access is False


# ─── resolve_spawn_context (the backend entry point) ─────────────────────────

def test_resolve_returns_none_without_descriptor(scratch_roots):
    assert resolve_spawn_context("claude_code", {}) is None
    assert resolve_spawn_context("claude_code", None) is None
    assert resolve_spawn_context("claude_code", {"model": "x"}) is None


def test_resolve_treats_malformed_descriptor_as_absent(scratch_roots):
    assert resolve_spawn_context("claude_code", {"spawn_context": "council"}) is None
    assert resolve_spawn_context("claude_code", {"spawn_context": {}}) is None


def test_resolve_builds_from_descriptor(scratch_roots):
    posture = {
        "spawn_context": {
            "position": "council_seat",
            "role": "stress",
            "task": "the question",
        }
    }
    sc = resolve_spawn_context("agy_code", posture, conversation_id="c-2")
    assert isinstance(sc, SpawnContext)
    assert sc.role == "stress"
    assert "**stress**" in sc.context_files["GEMINI.md"]
    assert sc.project_access is False


# ─── ContextVar scope ─────────────────────────────────────────────────────────

def test_spawn_descriptor_scope_sets_and_resets():
    assert active_spawn_descriptor() is None
    with spawn_descriptor({"position": "council_seat", "role": "framer"}):
        got = active_spawn_descriptor()
        assert got == {"position": "council_seat", "role": "framer"}
        # Mutating the returned copy does not poison the active value.
        got["role"] = "hacked"
        assert active_spawn_descriptor()["role"] == "framer"
    assert active_spawn_descriptor() is None


@pytest.mark.asyncio
async def test_spawn_descriptor_is_task_local():
    import asyncio

    async def seat(role: str) -> str:
        with spawn_descriptor({"role": role}):
            await asyncio.sleep(0)  # interleave with the other seat
            return active_spawn_descriptor()["role"]

    roles = await asyncio.gather(seat("framer"), seat("stress"))
    assert roles == ["framer", "stress"]
