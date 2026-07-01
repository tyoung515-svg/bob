"""BoBClaw Core — Unit + integration tests for hierarchical-managers (2-level tree).

No network: ``_send_to_backend`` is patched at the module-local binding in BOTH
``core.nodes.worker`` (the reused leaf workers) and ``core.nodes.hier`` (the apex
synth + critic audit). ``_append_agent_turn_event`` is patched so the L0 write
never touches a DB.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END
from langgraph.types import Send

from core.nodes import hier
from core.nodes.hier import (
    _chunk_sections,
    _route_after_manager_dispatch,
    manager_dispatch_node,
    manager_join_node,
    mini_manager_node,
)


# ─── _chunk_sections ──────────────────────────────────────────────────────────

def test_chunk_sections_balanced():
    secs = _chunk_sections(list("abcdefghi"), 4)  # 9 over 4 ⇒ 3,2,2,2
    assert [len(s["subtasks"]) for s in secs] == [3, 2, 2, 2]
    assert [s["idx"] for s in secs] == [0, 1, 2, 3]
    # every subtask placed exactly once, in order
    assert [t for s in secs for t in s["subtasks"]] == list("abcdefghi")


def test_chunk_sections_k_capped_to_n():
    secs = _chunk_sections(["x", "y"], 5)
    assert len(secs) == 2 and [len(s["subtasks"]) for s in secs] == [1, 1]


def test_chunk_sections_single():
    secs = _chunk_sections(["only"], 4)
    assert len(secs) == 1 and secs[0]["subtasks"] == ["only"]


# ─── manager_dispatch_node ────────────────────────────────────────────────────

def test_manager_dispatch_chunks_by_section_size(monkeypatch):
    monkeypatch.setattr(hier, "MANAGER_SECTION_SIZE", 4)
    monkeypatch.setattr(hier, "MANAGER_MAX_SECTIONS", 8)
    out = manager_dispatch_node({"subtasks": [f"t{i}" for i in range(9)]})
    assert len(out["sections"]) == 3  # ceil(9/4)


def test_manager_dispatch_respects_override(monkeypatch):
    monkeypatch.setattr(hier, "MANAGER_MAX_SECTIONS", 8)
    out = manager_dispatch_node(
        {"subtasks": [f"t{i}" for i in range(8)], "manager_max_sections": 2}
    )
    assert len(out["sections"]) == 2


def test_manager_dispatch_caps_sections(monkeypatch):
    monkeypatch.setattr(hier, "MANAGER_SECTION_SIZE", 1)  # would give 10 sections
    monkeypatch.setattr(hier, "MANAGER_MAX_SECTIONS", 3)
    out = manager_dispatch_node({"subtasks": [f"t{i}" for i in range(10)]})
    assert len(out["sections"]) == 3  # capped at MANAGER_MAX_SECTIONS


def test_manager_dispatch_no_subtasks_errors():
    out = manager_dispatch_node({"subtasks": []})
    assert out.get("error")
    assert "sections" not in out


# ─── _route_after_manager_dispatch ────────────────────────────────────────────

def test_route_after_manager_dispatch_sends_per_section():
    state = {
        "sections": [{"idx": 0, "subtasks": ["a"]}, {"idx": 1, "subtasks": ["b", "c"]}],
        "team": "hier-fleet",
        "escalation_backend": None,
    }
    sends = _route_after_manager_dispatch(state)
    assert isinstance(sends, list) and len(sends) == 2
    assert all(isinstance(s, Send) and s.node == "mini_manager" for s in sends)
    # roles resolved from hier-fleet
    assert sends[0].arg["worker_backend"] == "deepseek_v4_flash"
    assert sends[0].arg["apex_backend"] == "kimi_cli"  # hier-fleet apex (CX-4 swap)
    assert sends[0].arg["critic_backend"] == "glm_5_2"
    assert sends[1].arg["section_subtasks"] == ["b", "c"]


def test_route_after_manager_dispatch_end_on_error_or_empty():
    assert _route_after_manager_dispatch({"error": "boom"}) == END
    assert _route_after_manager_dispatch({"sections": []}) == END


def test_route_after_manager_dispatch_no_team_falls_back_to_turn_backend():
    state = {"sections": [{"idx": 0, "subtasks": ["a"]}], "backend": "deepseek_v4_flash"}
    sends = _route_after_manager_dispatch(state)
    assert sends[0].arg["worker_backend"] == "deepseek_v4_flash"
    assert sends[0].arg["apex_backend"] == "deepseek_v4_flash"
    assert sends[0].arg["critic_backend"] == ""  # no team ⇒ no critic ⇒ audit skipped


# ─── mini_manager_node ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mini_manager_fans_workers_and_synthesizes():
    async def fake_worker_send(messages, backend, *a):
        return f"out::{messages[-1]['content']}"

    async def fake_apex_send(messages, backend, *a):
        return "SECTION_SYNTH"

    with patch("core.nodes.worker._send_to_backend", side_effect=fake_worker_send), \
         patch("core.nodes.hier._send_to_backend", side_effect=fake_apex_send):
        out = await mini_manager_node({
            "section_idx": 2,
            "section_subtasks": ["task A", "task B", "task C"],
            "worker_backend": "deepseek_v4_flash",
            "apex_backend": "kimi_code",
            "critic_backend": "glm_5_2",
        })
    entry = out["section_results"][0]
    assert entry["idx"] == 2
    assert entry["status"] == "ok"
    assert entry["n_workers"] == 3 and entry["n_ok"] == 3
    assert entry["synthesis"] == "SECTION_SYNTH"
    # workers' raw results are NESTED, not on the top-level worker_results reducer
    assert len(entry["worker_results"]) == 3
    assert entry["worker_results"][0]["content"] == "out::task A"


@pytest.mark.asyncio
async def test_mini_manager_synth_fail_open_to_worker_blob():
    async def fake_worker_send(messages, backend, *a):
        return f"out::{messages[-1]['content']}"

    async def apex_boom(messages, backend, *a):
        raise RuntimeError("apex down")

    with patch("core.nodes.worker._send_to_backend", side_effect=fake_worker_send), \
         patch("core.nodes.hier._send_to_backend", side_effect=apex_boom):
        out = await mini_manager_node({
            "section_idx": 0, "section_subtasks": ["x"],
            "worker_backend": "deepseek_v4_flash", "apex_backend": "kimi_code",
        })
    entry = out["section_results"][0]
    assert entry["status"] == "ok"
    assert "out::x" in entry["synthesis"]  # fell back to the raw worker blob


@pytest.mark.asyncio
async def test_mini_manager_all_workers_fail():
    async def worker_boom(messages, backend, *a):
        raise RuntimeError("boom")

    # apex synth is skipped when there are no successes (no hier send needed)
    with patch("core.nodes.worker._send_to_backend", side_effect=worker_boom):
        out = await mini_manager_node({
            "section_idx": 1, "section_subtasks": ["x", "y"],
            "worker_backend": "deepseek_v4_flash", "apex_backend": "kimi_code",
        })
    entry = out["section_results"][0]
    assert entry["status"] == "failed" and entry["n_ok"] == 0


# ─── manager_join_node ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manager_join_reduces_sorts_and_audits():
    async def fake_audit(messages, backend, *a):
        return "AUDIT_OK"

    state = {
        "team": "hier-fleet",
        "section_results": [
            {"idx": 1, "status": "ok", "synthesis": "S1"},
            {"idx": 0, "status": "ok", "synthesis": "S0"},  # out of order on purpose
        ],
    }
    with patch("core.nodes.hier._send_to_backend", side_effect=fake_audit), \
         patch("core.nodes.hier._append_agent_turn_event", AsyncMock()):
        out = await manager_join_node(state)
    msg = out["messages"][0]["content"]
    assert "## Section 1\nS0" in msg  # sorted by idx
    assert "## Section 2\nS1" in msg
    assert msg.index("## Section 1") < msg.index("## Section 2")
    assert "2 of 2 sections completed" in msg
    assert "**Final audit (glm_5_2):** AUDIT_OK" in msg
    assert out["error"] is None


@pytest.mark.asyncio
async def test_manager_join_all_sections_failed_sets_error():
    async def fake_audit(messages, backend, *a):
        return "A"

    state = {"team": "hier-fleet",
             "section_results": [{"idx": 0, "status": "failed", "synthesis": "x"}]}
    with patch("core.nodes.hier._send_to_backend", side_effect=fake_audit), \
         patch("core.nodes.hier._append_agent_turn_event", AsyncMock()):
        out = await manager_join_node(state)
    assert out["error"] == "All hierarchical sections failed"


@pytest.mark.asyncio
async def test_manager_join_audit_fail_open():
    async def audit_boom(messages, backend, *a):
        raise RuntimeError("glm down")

    state = {"team": "hier-fleet",
             "section_results": [{"idx": 0, "status": "ok", "synthesis": "S0"}]}
    with patch("core.nodes.hier._send_to_backend", side_effect=audit_boom), \
         patch("core.nodes.hier._append_agent_turn_event", AsyncMock()):
        out = await manager_join_node(state)
    body = out["messages"][0]["content"]
    assert "S0" in body and "Final audit" not in body  # audit skipped, answer survives
    assert out["error"] is None


@pytest.mark.asyncio
async def test_manager_join_no_team_skips_audit():
    state = {"section_results": [{"idx": 0, "status": "ok", "synthesis": "S0"}]}
    with patch("core.nodes.hier._append_agent_turn_event", AsyncMock()):
        out = await manager_join_node(state)
    assert "Final audit" not in out["messages"][0]["content"]


@pytest.mark.asyncio
async def test_manager_join_audit_stands_in_when_primary_fails():
    """When the primary critic (glm_5_2 — the HTTP path is balance-dead) hard-fails, the
    final audit falls back to the stand-in (deepseek) so it is not silently lost, tagged
    as a stand-in. Mirrors run_critic's stand-in."""
    async def by_backend(messages, backend, *a):
        if backend == "glm_5_2":
            raise RuntimeError("Z.AI GLM balance/resource exhausted (1113)")
        return "STANDIN_AUDIT_OK"

    state = {"team": "hier-fleet",
             "section_results": [{"idx": 0, "status": "ok", "synthesis": "S0"}]}
    with patch("core.nodes.hier._send_to_backend", side_effect=by_backend), \
         patch("core.nodes.hier._append_agent_turn_event", AsyncMock()):
        out = await manager_join_node(state)
    body = out["messages"][0]["content"]
    assert "deepseek_v4_flash, stand-in" in body
    assert "STANDIN_AUDIT_OK" in body
    assert out["error"] is None


# ─── routing (graph) ──────────────────────────────────────────────────────────

def test_route_after_recall_hierarchical_arm():
    from core.graph import _route_after_recall
    assert _route_after_recall({"hierarchical": True}) == "manager_dispatch"
    # non-hierarchical unchanged (byte-for-byte today)
    assert _route_after_recall({}) == "dispatch"
    # build_request still wins (existing precedence)
    assert _route_after_recall({"build_request": True, "hierarchical": True}) == "plan_contracts"


# ─── full-graph integration (the 2-level tree end-to-end, no network) ─────────

@pytest.mark.asyncio
async def test_graph_hierarchical_end_to_end(monkeypatch):
    from core.config import config as _cfg
    monkeypatch.setattr(_cfg, "MEMORY_ENABLED", False)  # recall returns empty, no net

    async def fake_worker_send(messages, backend, *a):
        return f"worked::{messages[-1]['content']}"

    async def fake_hier_send(messages, backend, *a):
        return f"glm_says_ok::{backend}"

    from core.graph import build_graph
    g = build_graph(MemorySaver())

    with patch("core.nodes.worker._send_to_backend", side_effect=fake_worker_send), \
         patch("core.nodes.hier._send_to_backend", side_effect=fake_hier_send), \
         patch("core.nodes.hier._append_agent_turn_event", AsyncMock()):
        result = await g.ainvoke(
            {
                "task": "do it",  # simple ⇒ decompose passes through, keeps subtasks
                "subtasks": ["s0", "s1", "s2", "s3", "s4"],
                "hierarchical": True,
                "team": "hier-fleet",
                "face_id": "assistant",
                "messages": [],
            },
            config={"configurable": {"thread_id": "hm-e2e"}},
        )

    assistant = [m for m in result["messages"] if m.get("role") == "assistant"]
    assert assistant, "manager_join must emit exactly one assembled assistant message"
    final = assistant[-1]["content"]
    # 5 subtasks / section_size 4 ⇒ ceil(5/4) = 2 mini-managers
    assert "## Section 1" in final and "## Section 2" in final
    assert "sections completed" in final
    assert result.get("error") is None
    assert len(result.get("section_results", [])) == 2
