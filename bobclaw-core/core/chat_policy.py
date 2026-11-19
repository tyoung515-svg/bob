"""Topology choices only; admission, ownership, tools and budgets remain separate."""
from __future__ import annotations


def explicit_work_turn(state: dict) -> bool:
    return bool(
        state.get("agent_service_single_turn")
        or state.get("research_request") or state.get("research_tier")
        or state.get("hierarchical") or state.get("council_spec") is not None
        or state.get("build_request") or state.get("build_contracts") is not None
        or state.get("dispatch_subtask") is not None
        or state.get("fanout_width") is not None or state.get("team")
        or state.get("phase") in {"dispatch", "execute", "build", "worker"}
    )


def single_pass_chat(state: dict, *, dispatch: bool = False) -> bool:
    from core.config import CHAT_SINGLE_PASS_FACES
    from core.lane_config import configured_mode

    if not configured_mode():
        return (state.get("face_id") in CHAT_SINGLE_PASS_FACES and
                (not dispatch or state.get("fanout_width") is None))
    if explicit_work_turn(state):
        return False
    return bool(state.get("chat_single_pass") or
                state.get("face_id") in CHAT_SINGLE_PASS_FACES)
