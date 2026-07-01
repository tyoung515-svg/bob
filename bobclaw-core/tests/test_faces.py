"""
BoBClaw Core — Unit tests for FaceRegistry

No I/O mocking needed: tests load from the real profiles/ directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.faces.registry import Face, FaceRegistry, FaceSummary

# Build registry once for all tests
PROFILES_DIR = (
    Path(__file__).parent.parent / "core" / "faces" / "profiles"
)
EXPECTED_FACE_IDS = {
    "builder-bob",
    "researcher",
    "reviewer",
    "council-lite",
    "council-max",
    "assistant",
    "assistant-actions",
    "assistant-tools",
    "assistant-tools-mcp",
    "planner-claude",
    "planner-cc-edit",
    "planner-cc-edit-codex",
    "planner-minimax",
    "planner-kimi",
    "worker-kimi",
    "worker-kimi-bulk",
    "worker-deepseek",
    "worker-opencode",
    "planner-gemini",
    "worker-agy",
    "planner-codex",
    "worker-codex",
    "worker-kimi-cli",
}

@pytest.fixture(scope="module")
def registry() -> FaceRegistry:
    return FaceRegistry(profiles_dir=PROFILES_DIR)


# ─── Loading ──────────────────────────────────────────────────────────────────

def test_all_profiles_loaded(registry: FaceRegistry):
    assert len(registry) == 23


def test_all_expected_ids_present(registry: FaceRegistry):
    present = {s.id for s in registry.list_faces()}
    assert EXPECTED_FACE_IDS == present


# ─── get_face() ───────────────────────────────────────────────────────────────

def test_get_face_builder_bob(registry: FaceRegistry):
    face = registry.get_face("builder-bob")
    assert isinstance(face, Face)
    assert face.name == "Builder Bob"
    assert face.avatar == "👷"
    assert face.preferred_backend == "local"
    assert face.escalation_backend == "minimax"
    assert face.ui_theme == "orange"


def test_get_face_planner_kimi(registry: FaceRegistry):
    face = registry.get_face("planner-kimi")
    assert face.name == "Planner (Kimi)"
    assert face.preferred_backend == "kimi_code"
    assert face.escalation_backend == "claude_api"
    assert face.ui_theme == "cyan"


def test_get_face_planner_claude(registry: FaceRegistry):
    face = registry.get_face("planner-claude")
    assert face.name == "Planner (Claude)"
    assert face.preferred_backend == "claude_code"
    assert face.escalation_backend == "claude_api"
    # C2.1: scratch-write posture (read repo + web, write ideation to scratch).
    assert face.cc_posture["mode"] == "scratch_write"
    assert face.cc_posture["permission_mode"] == "acceptEdits"
    assert face.cc_posture["scratch_dir"] == "scratch"
    assert face.ui_theme == "indigo"


def test_get_face_planner_cc_edit(registry: FaceRegistry):
    face = registry.get_face("planner-cc-edit")
    assert face.name == "Planner (Claude · edits)"
    assert face.preferred_backend == "claude_code"
    assert face.escalation_backend == "claude_api"
    # C2.1: scratch-write posture; diff is written to proposed_<n>.diff in scratch.
    assert face.cc_posture["mode"] == "scratch_write"
    assert face.cc_posture["permission_mode"] == "acceptEdits"
    assert "proposed_" in face.system_prompt
    assert ".diff" in face.system_prompt
    assert "applied by BoBClaw, not by" in face.system_prompt
    assert "you only propose" in face.system_prompt
    assert face.ui_theme == "indigo"


def test_get_face_worker_kimi(registry: FaceRegistry):
    face = registry.get_face("worker-kimi")
    assert face.name == "Kimi Worker"
    assert face.preferred_backend == "kimi_code"
    assert face.escalation_backend == "kimi_platform"
    assert face.ui_theme == "amber"


def test_get_face_worker_kimi_bulk(registry: FaceRegistry):
    face = registry.get_face("worker-kimi-bulk")
    assert face.name == "Kimi Worker (bulk)"
    assert face.preferred_backend == "kimi_platform"
    assert face.escalation_backend == "kimi_code"
    assert face.ui_theme == "brown"


def test_get_face_worker_opencode(registry: FaceRegistry):
    face = registry.get_face("worker-opencode")
    assert face.name == "OpenCode Worker"
    assert face.preferred_backend == "opencode_serve"
    assert face.escalation_backend == "kimi_platform"
    assert face.ui_theme == "teal"


def test_get_face_researcher(registry: FaceRegistry):
    face = registry.get_face("researcher")
    assert face.name == "Researcher"
    assert face.avatar == "🔬"
    assert face.escalation_backend == "gemini_deep_research"
    assert face.ui_theme == "blue"


def test_get_face_reviewer(registry: FaceRegistry):
    face = registry.get_face("reviewer")
    assert face.name == "Reviewer"
    assert face.ui_theme == "purple"
    assert face.escalation_backend == "claude_api"


def test_get_face_council_lite(registry: FaceRegistry):
    face = registry.get_face("council-lite")
    assert face.name == "The Council (Lite)"
    assert face.ui_theme == "gold"
    assert face.avatar == "⚖️"
    assert face.preferred_backend == "local"


def test_get_face_council_max(registry: FaceRegistry):
    face = registry.get_face("council-max")
    assert face.name == "The Council (Max)"
    assert face.ui_theme == "gold"
    assert face.avatar == "⚖️"
    assert face.preferred_backend == "minimax"
    assert face.escalation_backend == "claude_api"
    # Seats hold no tools (design "off the long-agentic path").
    assert face.allowed_tools == []
    # The old mock id is gone.
    import pytest as _pytest
    with _pytest.raises(KeyError):
        registry.get_face("council")


def test_get_face_assistant(registry: FaceRegistry):
    face = registry.get_face("assistant")
    assert face.name == "General Assistant"
    assert face.ui_theme == "green"


def test_get_face_assistant_tools(registry: FaceRegistry):
    face = registry.get_face("assistant-tools")
    assert face.name == "Assistant (Tools)"
    assert face.preferred_backend == "deepseek_v4_flash"
    assert face.allowed_tools == ["get_server_time", "list_backends", "create_team"]


def test_get_face_invalid_raises_key_error(registry: FaceRegistry):
    with pytest.raises(KeyError, match="Unknown face id"):
        registry.get_face("does-not-exist")


def test_get_face_empty_string_raises_key_error(registry: FaceRegistry):
    with pytest.raises(KeyError):
        registry.get_face("")


# ─── list_faces() ─────────────────────────────────────────────────────────────

def test_list_faces_returns_summaries(registry: FaceRegistry):
    summaries = registry.list_faces()
    assert len(summaries) == 23
    for s in summaries:
        assert isinstance(s, FaceSummary)
        assert s.id
        assert s.name
        assert s.avatar


def test_list_faces_all_have_preferred_backend(registry: FaceRegistry):
    for s in registry.list_faces():
        assert s.preferred_backend, f"Face '{s.id}' missing preferred_backend"


# ─── get_system_prompt() ──────────────────────────────────────────────────────

@pytest.mark.parametrize("face_id", [
    "builder-bob",
    "researcher",
    "reviewer",
    "council-lite",
    "council-max",
    "assistant",
    "assistant-tools",
    "planner-claude",
    "planner-cc-edit",
    "planner-cc-edit-codex",
    "planner-minimax",
    "planner-kimi",
    "worker-kimi",
    "worker-kimi-bulk",
    "worker-deepseek",
    "worker-opencode",
    "planner-gemini",
    "worker-agy",
])
def test_system_prompt_non_empty(registry: FaceRegistry, face_id: str):
    prompt = registry.get_system_prompt(face_id)
    assert isinstance(prompt, str)
    assert len(prompt.strip()) > 0, f"system_prompt for '{face_id}' is empty"


def test_system_prompt_invalid_id_raises(registry: FaceRegistry):
    with pytest.raises(KeyError):
        registry.get_system_prompt("ghost")


# ─── get_allowed_tools() ──────────────────────────────────────────────────────

def test_builder_bob_allowed_tools_empty(registry: FaceRegistry):
    tools = registry.get_allowed_tools("builder-bob")
    assert tools == []


def test_reviewer_allowed_tools_empty(registry: FaceRegistry):
    tools = registry.get_allowed_tools("reviewer")
    assert tools == []


def test_council_lite_allowed_tools_empty(registry: FaceRegistry):
    tools = registry.get_allowed_tools("council-lite")
    assert tools == []


def test_council_max_allowed_tools_empty(registry: FaceRegistry):
    tools = registry.get_allowed_tools("council-max")
    assert tools == []


def test_get_allowed_tools_returns_copy(registry: FaceRegistry):
    """Mutating the returned list must not affect the registry."""
    tools = registry.get_allowed_tools("assistant-tools")
    tools.clear()
    assert registry.get_allowed_tools("assistant-tools") == [
        "get_server_time", "list_backends", "create_team",
    ]


def test_get_allowed_tools_invalid_id_raises(registry: FaceRegistry):
    with pytest.raises(KeyError):
        registry.get_allowed_tools("nope")


def test_no_profile_uses_fantasy_tool_labels(registry: FaceRegistry):
    """All loaded faces must use real tool IDs, not abstract fantasy labels."""
    for face in registry.list_faces():
        tools = registry.get_allowed_tools(face.id)
        for label in {"code", "files", "shell", "search", "docs", "email", "browser"}:
            assert label not in tools, f"face '{face.id}' uses fantasy tool label '{label}'"


def test_face_model_rejects_fantasy_tool_labels():
    from core.faces.registry import FANTASY_TOOL_LABELS
    for label in FANTASY_TOOL_LABELS:
        with pytest.raises(ValueError, match="fantasy labels"):
            Face(
                id="bad",
                name="Bad Face",
                system_prompt="Hello",
                allowed_tools=[label],
            )


# ─── contains / membership ────────────────────────────────────────────────────

def test_contains_known_face(registry: FaceRegistry):
    assert "assistant" in registry


def test_not_contains_unknown_face(registry: FaceRegistry):
    assert "phantom" not in registry


# ─── Pydantic validation edge cases ───────────────────────────────────────────

def test_face_model_rejects_empty_system_prompt():
    with pytest.raises(Exception):
        Face(
            id="bad",
            name="Bad Face",
            system_prompt="   ",   # whitespace-only
        )


def test_face_model_rejects_empty_id():
    with pytest.raises(Exception):
        Face(
            id="  ",
            name="Bad Face",
            system_prompt="Hello",
        )


def test_face_model_strips_system_prompt_whitespace():
    face = Face(
        id="test",
        name="Test",
        system_prompt="  Hello world  ",
    )
    assert face.system_prompt == "Hello world"
