"""JOAT v0 P1 — integration: a selected team measurably changes what ``route_node``
resolves, while the default team is byte-for-byte today's answer.

Uses faces whose resolved backends are NON-local on both paths (worker-deepseek,
planner-minimax) so the assertions never depend on local-backend discovery.
"""
from __future__ import annotations

import pytest

from core import teams
from core.nodes.route import route_node


@pytest.fixture(autouse=True)
def _reset_team_state():
    teams.set_active_team(None)
    yield
    teams.set_active_team(None)


def _state(face_id: str, **kw) -> dict:
    base = {
        "task": "summarize this file",  # benign: no plan/dispatch face-swap
        "face_id": face_id,
        "model_override": None,
        "backend_override": None,
        "team": None,
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_default_team_route_is_unchanged():
    """No team → route_node resolves the face's own preferred/escalation."""
    out = await route_node(_state("worker-deepseek"))
    assert out["backend"] == "deepseek_v4_flash"      # worker-deepseek.preferred
    assert out["escalation_backend"] == "kimi_code"   # worker-deepseek.escalation


@pytest.mark.asyncio
async def test_process_team_changes_worker_resolution():
    """Process-level team (cloud-heavy) remaps the worker role end-to-end."""
    teams.set_active_team("cloud-heavy")
    out = await route_node(_state("worker-deepseek"))
    assert out["backend"] == "glm_5_2"                 # cloud-heavy worker backend
    assert out["escalation_backend"] == "deepseek_v4_flash"  # chain head, not kimi_code


@pytest.mark.asyncio
async def test_per_conversation_pin_changes_apex_resolution():
    """A per-conversation team pin (no process team set) remaps the apex role."""
    out_default = await route_node(_state("planner-minimax"))
    assert out_default["backend"] == "minimax"         # planner-minimax.preferred

    out_pinned = await route_node(_state("planner-minimax", team="cloud-heavy"))
    assert out_pinned["backend"] == "claude_code"      # cloud-heavy apex backend


@pytest.mark.asyncio
async def test_pin_overrides_process_team_in_route():
    teams.set_active_team("local-first")               # process apex = minimax
    out = await route_node(_state("planner-minimax", team="cloud-heavy"))
    assert out["backend"] == "claude_code"             # pin wins over process team


# ── pin_authoritative: honor an explicit face, skip the intent heuristic ─────────
# 'propose' trips _PLAN_INTENT; without code-shape the heuristic swaps to
# planner-minimax (and can NEVER yield planner-cc-edit). The headless contract pins it.
_TRIPS_HEURISTIC = "Do NOT propose changes; reply ACK."


@pytest.mark.asyncio
async def test_pin_authoritative_honors_explicit_face_over_heuristic():
    out = await route_node(_state("planner-cc-edit", task=_TRIPS_HEURISTIC,
                                  pin_authoritative=True))
    assert out["face_id"] == "planner-cc-edit"          # pin honored, no swap
    assert out["backend"] == "claude_code"              # planner-cc-edit's backend
    assert out["cc_posture"].get("mode") == "scratch_write"


@pytest.mark.asyncio
async def test_without_pin_the_heuristic_still_swaps():
    """Interactive (unpinned) turns are byte-for-byte unchanged: the same task swaps."""
    out = await route_node(_state("planner-cc-edit", task=_TRIPS_HEURISTIC))
    assert out["face_id"] == "planner-minimax"          # heuristic still fires
    assert out["backend"] == "minimax"


@pytest.mark.asyncio
async def test_profile_can_opt_into_pin_authoritative(tmp_path):
    """A profile with pin_authoritative=true activates the bypass (interactive override)."""
    teams.set_custom_teams_dir(tmp_path)
    try:
        teams.create_profile("pinned-edit", {
            "roles": {"apex": {"backend": "claude_code"}},
            "pin_authoritative": True,
        })
        out = await route_node(_state("planner-cc-edit", task=_TRIPS_HEURISTIC,
                                      profile_name="pinned-edit"))
        assert out["face_id"] == "planner-cc-edit"      # profile flag honored the pin
        assert out["backend"] == "claude_code"
    finally:
        teams.set_custom_teams_dir(None)


# ── NB-W2 A2: production hierarchical trigger ────────────────────────────────
# A profile / ingress flag sets state["hierarchical"]=True so _route_after_recall
# diverts recall → manager_dispatch (the 2-level agent tree). Absent ⇒ no key in
# the route delta (byte-identical, no regression).

@pytest.mark.asyncio
async def test_route_omits_hierarchical_when_absent():
    """No trigger → route_node delta has NO `hierarchical` key (byte-identical)."""
    out = await route_node(_state("planner-minimax"))
    assert "hierarchical" not in out


@pytest.mark.asyncio
async def test_ingress_hierarchical_flag_threads_through_route():
    """The /api/chat ingress sets state["hierarchical"]; route_node preserves it in
    its delta so it survives into _route_after_recall."""
    out = await route_node(_state("planner-minimax", hierarchical=True))
    assert out.get("hierarchical") is True
    assert out["backend"] == "minimax"          # face resolution otherwise unchanged


@pytest.mark.asyncio
async def test_profile_can_opt_into_hierarchical(tmp_path):
    """A profile with hierarchical=true flips the trigger via route_node (no raw state field)."""
    teams.set_custom_teams_dir(tmp_path)
    try:
        teams.create_profile("hier-profile", {
            "roles": {"apex": {"backend": "minimax"}},
            "hierarchical": True,
        })
        out = await route_node(_state("planner-minimax", profile_name="hier-profile"))
        assert out.get("hierarchical") is True
        # No `shape` → still the normal face-resolution path (not the council subgraph).
        assert "council_spec" not in out
    finally:
        teams.set_custom_teams_dir(None)


def test_load_profile_round_trips_hierarchical(tmp_path):
    """teams.load_profile surfaces a persisted `hierarchical` flag (key-copy lists)."""
    teams.set_custom_teams_dir(tmp_path)
    try:
        teams.create_profile("hier-rt", {
            "roles": {"apex": {"backend": "minimax"}},
            "hierarchical": True,
        })
        prof = teams.load_profile("hier-rt")
        assert prof is not None
        assert prof.get("hierarchical") is True
    finally:
        teams.set_custom_teams_dir(None)


def test_validate_profile_rejects_non_bool_hierarchical():
    from core.teams import validate_profile
    errs = validate_profile({"roles": {"apex": {"backend": "minimax"}},
                             "hierarchical": "yes-please"})
    assert any("hierarchical" in e for e in errs)


def test_route_after_recall_hierarchical_goes_to_manager_dispatch():
    """_route_after_recall: the hierarchical flag diverts to manager_dispatch; absent
    falls through to dispatch (byte-identical)."""
    from core.graph import _route_after_recall
    assert _route_after_recall({"hierarchical": True}) == "manager_dispatch"
    assert _route_after_recall({}) == "dispatch"
    # build_request takes precedence over hierarchical (its own arm, checked first).
    assert _route_after_recall({"build_request": True, "hierarchical": True}) == "plan_contracts"
