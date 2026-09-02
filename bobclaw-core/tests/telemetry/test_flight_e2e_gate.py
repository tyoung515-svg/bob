"""Flight substrate L0 GATE — live fan-out E2E (deterministic, in-process).

The KICKOFF §6 gate: assert monitor events actually arrive DURING a real fan-out run
(mid-run), **not just at join**. This drives a real fan-out (dispatch → worker×N → join)
through a real LangGraph streaming context and asserts on the ``custom`` stream — the same
channel the emit layer's in-turn fork writes to. Backends are mocked (no network); the emit
Redis fork fails open (sockets disabled). N=20 (deepseek single-wave cap); the LIVE 100-agent
run over real Redis + /ws/monitor is the Travis-gated confirmation
(``scripts/flight_monitor_e2e.py``).

Proves:
  * every worker emits a ``worker_state`` (``running``) frame — the mid-run fleet feed;
  * those frames ALL arrive BEFORE the ``fleet_join`` frame (mid-run, not just at join);
  * every frame carries the turn's ``flight_id`` (grouping key);
  * ``fleet_start`` n_workers / ``fleet_join`` ok reconcile to N.
"""
from __future__ import annotations

from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from core.graph import AgentState
from core.nodes.dispatch import _route_after_dispatch, dispatch_node
from core.nodes.join import join_node
from core.nodes.worker import worker_node

N = 20
FLIGHT = "e2e-flight"


def _fake_send(text):
    async def _s(messages, backend, *a, **k):
        return text
    return _s


def _fanout_graph():
    """The real fan-out subgraph (real emit-wired nodes) in a streaming-capable graph."""
    g = StateGraph(AgentState)
    g.add_node("dispatch", dispatch_node)
    g.add_node("worker", worker_node)
    g.add_node("join", join_node)
    g.add_edge(START, "dispatch")
    # dispatch fans out via list[Send] to "worker" (Send targets bypass this map, like the
    # real graph); the string returns terminate the (non-fan-out) path.
    g.add_conditional_edges(
        "dispatch", _route_after_dispatch, {"execute": END, "approval": END, END: END},
    )
    g.add_edge("worker", "join")
    g.add_edge("join", END)
    return g.compile(checkpointer=MemorySaver())


async def _run_fanout():
    graph = _fanout_graph()
    state = {
        "task": "gate",
        "face_id": "assistant",
        "backend": "deepseek_v4_flash",
        "subtasks": [f"t{i}" for i in range(N)],
        "fanout_width": N,
        "flight_id": FLIGHT,
        "messages": [],
        "worker_results": [],
    }
    config = {"configurable": {"thread_id": "gate-1"}}
    seq: list[tuple[str, object]] = []
    with patch("core.nodes.worker._send_to_backend", _fake_send("ok")):
        async for mode, chunk in graph.astream(
            state, config, stream_mode=["custom", "updates"]
        ):
            seq.append((mode, chunk))
    return seq


async def test_events_arrive_mid_run_not_just_at_join():
    seq = await _run_fanout()
    custom = [c for (m, c) in seq if m == "custom" and isinstance(c, dict)]

    worker_running = [
        i for i, c in enumerate(custom)
        if c.get("type") == "worker_state" and c.get("status") == "running"
    ]
    fleet_join_idxs = [i for i, c in enumerate(custom) if c.get("type") == "fleet_join"]

    # (1) every worker lit up mid-run
    assert len(worker_running) == N, f"expected {N} running frames, got {len(worker_running)}"
    # (2) a fleet_join happened
    assert fleet_join_idxs, "no fleet_join frame emitted"
    # (3) THE GATE: all worker_state running frames arrived BEFORE the join — i.e. the
    #     monitor saw the fleet mid-run, not only at completion.
    assert max(worker_running) < min(fleet_join_idxs), (
        "worker_state frames did not all precede fleet_join — mid-run emit regressed"
    )


async def test_all_frames_carry_the_flight_id():
    seq = await _run_fanout()
    fleet_frames = [
        c for (m, c) in seq
        if m == "custom" and isinstance(c, dict)
        and c.get("type") in ("fleet_start", "worker_state", "fleet_join")
    ]
    assert fleet_frames
    assert all(c.get("flight_id") == FLIGHT for c in fleet_frames)


async def test_counts_reconcile():
    seq = await _run_fanout()
    custom = [c for (m, c) in seq if m == "custom" and isinstance(c, dict)]
    fleet_join = next(c for c in custom if c.get("type") == "fleet_join")
    assert fleet_join["ok"] == N
    assert fleet_join["total"] == N
    assert fleet_join["failed"] == 0
    # worker terminal states: N ok
    worker_ok = [c for c in custom if c.get("type") == "worker_state" and c.get("status") == "ok"]
    assert len(worker_ok) == N


async def test_fleet_start_emitted_with_count():
    seq = await _run_fanout()
    custom = [c for (m, c) in seq if m == "custom" and isinstance(c, dict)]
    fleet_starts = [c for c in custom if c.get("type") == "fleet_start"]
    # dispatch's fleet_start rides the sync stream-writer fork; assert it lands with N.
    assert fleet_starts, "no fleet_start frame (dispatch sync stream fork regressed)"
    assert fleet_starts[0]["n_workers"] == N
    assert fleet_starts[0]["flight_id"] == FLIGHT
