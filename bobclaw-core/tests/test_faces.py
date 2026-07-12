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
    "planner-gpt",
    "worker-codex",
    "worker-kimi-cli",
}

@pytest.fixture(scope="module")
def registry() -> FaceRegistry:
    return FaceRegistry(profiles_dir=PROFILES_DIR)


# ─── Loading ──────────────────────────────────────────────────────────────────

def test_all_profiles_loaded(registry: FaceRegistry):
    assert len(registry) == 24


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
    assert len(summaries) == 24
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


# ─── U2 (D10): display metadata — optional, display-only, id-fallback ──────────

_DISPLAY_FIELDS = ("display_name", "blurb", "simple_slot")


def test_display_fields_default_none_when_absent():
    """A face that declares none of the display fields gets all three as None."""
    face = Face(id="bare", name="Bare", system_prompt="Hi")
    assert face.display_name is None
    assert face.blurb is None
    assert face.simple_slot is None


def test_display_name_falls_back_to_id_when_absent(registry: FaceRegistry):
    """Absent display_name ⇒ the documented convention resolves to the id."""
    face = Face(id="bare", name="Bare", system_prompt="Hi")
    assert (face.display_name or face.id) == "bare"
    # and via a real registry face constructed without the field
    stripped = registry.get_face("assistant").model_copy(
        update={"display_name": None}
    )
    assert (stripped.display_name or stripped.id) == "assistant"


def test_all_profiles_have_display_name_and_blurb(registry: FaceRegistry):
    """Every face in profiles/*.yaml is populated with display_name + blurb (D10)."""
    for face in registry.all_faces():
        assert face.display_name and face.display_name.strip(), (
            f"face '{face.id}' missing display_name"
        )
        assert face.blurb and face.blurb.strip(), f"face '{face.id}' missing blurb"


def test_simple_slots_unique_and_canonical(registry: FaceRegistry):
    """Each plain-language Simple-mode slot maps to exactly ONE face (unambiguous
    picker), and the three canonical §6 slots are all present."""
    slots = {f.id: f.simple_slot for f in registry.all_faces() if f.simple_slot}
    values = list(slots.values())
    assert len(values) == len(set(values)), f"duplicate simple_slot: {slots}"
    assert set(values) == {"quick", "think_hard", "team_of_experts"}, slots
    # faces without a Simple-mode slot leave it None (Pro-only / internal faces)
    unslotted = [f.id for f in registry.all_faces() if f.simple_slot is None]
    assert len(unslotted) == len(registry) - 3


def test_face_summary_carries_display_metadata(registry: FaceRegistry):
    """list_faces() summaries (the /api/faces surface) carry the display fields."""
    by_id = {s.id: s for s in registry.list_faces()}
    asst = by_id["assistant"]
    assert asst.display_name == "Everyday Assistant"
    assert asst.simple_slot == "quick"
    assert asst.blurb
    # a face with no Simple slot keeps it None on the summary too
    assert by_id["reviewer"].simple_slot is None
    assert by_id["reviewer"].display_name == "Reviewer"


def test_display_metadata_is_orthogonal_to_prompt():
    """Two faces identical except display metadata are byte-identical everywhere
    else, and their system_prompt (the ONLY face→prompt contribution) is equal."""
    base = dict(id="x", name="X", system_prompt="Do exactly one thing.")
    plain = Face(**base)
    decorated = Face(
        **base,
        display_name="Friendly X",
        blurb="Does exactly one thing.",
        simple_slot="quick",
    )
    plain_d = plain.model_dump()
    dec_d = decorated.model_dump()
    for k in _DISPLAY_FIELDS:
        plain_d.pop(k)
        dec_d.pop(k)
    assert plain_d == dec_d  # every non-display field is identical
    assert plain.system_prompt == decorated.system_prompt


def test_prompt_assembly_byte_identical_with_and_without_display(tmp_path):
    """END-TO-END at the registry assembly seam: the SAME face loaded WITH vs
    WITHOUT display metadata produces a byte-for-byte identical assembled prompt.
    Proves the fields are display-only and never enter prompt assembly (SPEC U2)."""
    import yaml as _yaml

    base = {
        "id": "meta-face",
        "name": "Meta",
        "system_prompt": "You are Meta.\nDo one thing, then stop.\n",
    }
    d_with = tmp_path / "with"
    d_without = tmp_path / "without"
    d_with.mkdir()
    d_without.mkdir()
    (d_with / "f.yaml").write_text(
        _yaml.safe_dump(
            {
                **base,
                "display_name": "Friendly Meta",
                "blurb": "A friendly meta face.",
                "simple_slot": "quick",
            }
        ),
        encoding="utf-8",
    )
    (d_without / "f.yaml").write_text(_yaml.safe_dump(base), encoding="utf-8")

    reg_with = FaceRegistry(profiles_dir=d_with)
    reg_without = FaceRegistry(profiles_dir=d_without)

    # the assembled system prompt is byte-for-byte identical …
    assert reg_with.get_system_prompt("meta-face") == reg_without.get_system_prompt(
        "meta-face"
    )
    # … even though one carries display metadata and the other does not.
    assert reg_with.get_face("meta-face").display_name == "Friendly Meta"
    assert reg_without.get_face("meta-face").display_name is None


# ─── R0: GPT-5.6 roster (Sol / Terra / Luna) ──────────────────────────────────