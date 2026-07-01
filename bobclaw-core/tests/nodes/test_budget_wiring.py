"""MS-4 — budget BIND-01/02 live-wiring tests (PURE; the no-regression heart).

Proves:
  * BIND-01: a per-branch token reservation is injected onto each Send at fan-out.
  * NON-BUDGETED BYTE-IDENTICAL (the load-bearing invariant): the SAME fan-out with NO
    budget emits Send args with no `branch_budget` key, exactly equal to the baseline —
    for BOTH the chat and the build branches.
  * BIND-02: a worker meters its own spend IN-BRANCH; a tiny reservation trips the
    breaker + the OVERSPEND escalation; a generous one does not; no key without a budget.
  * reconcile-on-merge + the §2.7 contested-by-cost SURFACE at join (overspend AND the
    run ceiling) — surface only, never an approval gate; byte-identical body without budget.
  * budget is NOT its own routing arm (recall→dispatch byte-identical) and the graph
    still compiles with the two new AgentState fields.

No network (pytest runs --disable-socket); worker_node's backend send is monkeypatched.
"""
from __future__ import annotations

from unittest.mock import patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Send

from core.graph import AgentState, _route_after_recall, build_graph
from core.nodes.dispatch import _route_after_dispatch, dispatch_node
from core.nodes.join import _build_join, join_node
from core.nodes.worker import worker_node


# ── shared fan-out states ─────────────────────────────────────────────────────
def _chat_state(budget=None, **ov):
    st = {
        "task": "do",
        "face_id": "assistant",
        "backend": "deepseek_v4_flash",
        "subtasks": ["a", "b", "c"],
        "fanout_width": 3,
        "escalation_backend": None,
        "messages": [],
    }
    st.update(ov)
    if budget is not None:
        st["budget"] = budget
    st.update(dispatch_node(st))
    return st


_C1 = {"name": "f1", "signature": "def f1(): ...", "doc": "", "cases": []}
_C2 = {"name": "f2", "signature": "def f2(): ...", "doc": "", "cases": []}


def _build_state(budget=None, **ov):
    st = {
        "build_contracts": [_C1, _C2],
        "build_workspace": None,
        "team": "demo-fleet",
        "messages": [],
    }
    st.update(ov)
    if budget is not None:
        st["budget"] = budget
    return st


# ── BIND-01: reservation at dispatch ──────────────────────────────────────────
def test_bind01_chat_reservation_injected():
    sends = _route_after_dispatch(_chat_state(budget={"pool": 30, "per_branch": 10}))
    assert all(isinstance(s, Send) for s in sends)
    assert [s.arg["branch_budget"]["reservation"] for s in sends] == [10, 10, 10]
    assert all(s.arg["branch_budget"]["trigger"] == 1.5 for s in sends)
    assert sum(s.arg["branch_budget"]["reservation"] for s in sends) <= 30


def test_bind01_pool_exhaustion_gives_zero_reservation():
    # pool=15, per_branch=10, 3 branches -> [10, 0, 0] (after the first, pool=5 < 10).
    sends = _route_after_dispatch(_chat_state(budget={"pool": 15, "per_branch": 10}))
    assert [s.arg["branch_budget"]["reservation"] for s in sends] == [10, 0, 0]


def test_bind01_even_split_when_no_per_branch():
    sends = _route_after_dispatch(_chat_state(budget={"pool": 30}))
    assert [s.arg["branch_budget"]["reservation"] for s in sends] == [10, 10, 10]


# ── NON-BUDGETED BYTE-IDENTICAL (load-bearing) ────────────────────────────────
def test_chat_fanout_byte_identical_without_budget():
    sends = _route_after_dispatch(_chat_state(budget=None))
    assert len(sends) == 3
    for s in sends:
        assert "branch_budget" not in s.arg


def test_chat_fanout_args_identical_modulo_branch_budget():
    base = _route_after_dispatch(_chat_state(budget=None))
    budg = _route_after_dispatch(_chat_state(budget={"pool": 30, "per_branch": 10}))
    assert len(base) == len(budg) == 3
    for b, g in zip(base, budg):
        stripped = {k: v for k, v in g.arg.items() if k != "branch_budget"}
        assert stripped == b.arg  # every other key byte-identical to today


def test_build_fanout_byte_identical_without_budget():
    sends = _route_after_dispatch(_build_state(budget=None))
    assert len(sends) == 2
    for s in sends:
        assert "branch_budget" not in s.arg


def test_build_fanout_args_identical_modulo_branch_budget():
    base = _route_after_dispatch(_build_state(budget=None))
    budg = _route_after_dispatch(_build_state(budget={"pool": 20, "per_branch": 8}))
    assert [s.arg["branch_budget"]["reservation"] for s in budg] == [8, 8]
    for b, g in zip(base, budg):
        stripped = {k: v for k, v in g.arg.items() if k != "branch_budget"}
        assert stripped == b.arg


# ── BIND-02: in-branch breaker (worker_node) ──────────────────────────────────
def _fake_send(text):
    async def _s(messages, backend, *a, **k):
        return text
    return _s


def _worker_sub(branch_budget=None, task="hi"):
    sub = {"task": task, "backend": "deepseek_v4_flash", "subtask_idx": 0, "messages": []}
    if branch_budget is not None:
        sub["branch_budget"] = branch_budget
    return sub


async def test_bind02_breaker_trips_in_branch_on_tiny_reservation():
    with patch("core.nodes.worker._send_to_backend", _fake_send("x" * 400)):
        res = await worker_node(_worker_sub(branch_budget={"reservation": 1, "trigger": 1.5}))
    entry = res["worker_results"][0]
    assert entry["status"] == "ok"
    b = entry["budget"]
    assert b["tripped"] is True
    assert b["escalate"] is True
    assert b["reason"] == "OVERSPEND"
    assert b["spent"] > b["reservation"]


async def test_bind02_breaker_not_tripped_on_generous_reservation():
    with patch("core.nodes.worker._send_to_backend", _fake_send("short reply")):
        res = await worker_node(
            _worker_sub(branch_budget={"reservation": 100_000, "trigger": 1.5})
        )
    b = res["worker_results"][0]["budget"]
    assert b["tripped"] is False
    assert b["escalate"] is False
    assert b["reason"] is None


async def test_worker_no_budget_key_without_branch_budget():
    with patch("core.nodes.worker._send_to_backend", _fake_send("hi")):
        res = await worker_node(_worker_sub(branch_budget=None))
    assert "budget" not in res["worker_results"][0]


def _build_sub(branch_budget=None, name="f1"):
    sub = {
        "build_contract": {"name": name, "signature": f"def {name}():", "doc": "", "cases": []},
        "backend": "deepseek_v4_flash", "subtask_idx": 0, "messages": [],
    }
    if branch_budget is not None:
        sub["branch_budget"] = branch_budget
    return sub


async def test_bind02_build_branch_breaker_trips():
    # audit r1 gap: the build worker (_build_worker) meters in-branch too.
    impl = 'def f1():\n    """d"""\n    return 1\n'
    with patch("core.nodes.worker._send_to_backend", _fake_send(impl)):
        res = await worker_node(_build_sub(branch_budget={"reservation": 1, "trigger": 1.5}))
    entry = res["build_impls"][0]
    assert entry["budget"]["tripped"] is True
    assert entry["budget"]["escalate"] is True
    assert entry["budget"]["reason"] == "OVERSPEND"


async def test_bind02_build_branch_no_budget_key_without_budget():
    impl = "def f1():\n    return 1\n"
    with patch("core.nodes.worker._send_to_backend", _fake_send(impl)):
        res = await worker_node(_build_sub(branch_budget=None))
    assert "budget" not in res["build_impls"][0]


async def test_bind02_two_branches_trip_independently():
    # Each branch meters against ITS OWN reservation — no shared/sibling read.
    with patch("core.nodes.worker._send_to_backend", _fake_send("y" * 200)):
        tiny = await worker_node(_worker_sub(branch_budget={"reservation": 1, "trigger": 1.5}))
        huge = await worker_node(_worker_sub(branch_budget={"reservation": 9_999, "trigger": 1.5}))
    assert tiny["worker_results"][0]["budget"]["tripped"] is True
    assert huge["worker_results"][0]["budget"]["tripped"] is False


# ── reconcile-on-merge + §2.7 surface (join_node) ─────────────────────────────
def _wr(idx, content, budget=None, status="ok"):
    e = {"idx": idx, "text": "t", "status": status, "content": content}
    if budget is not None:
        e["budget"] = budget
    return e


def _bud(reservation, spent, escalate=False, reason=None):
    return {
        "reservation": reservation, "spent": spent,
        "tripped": spent >= reservation, "overspend_ratio": (spent / reservation if reservation else 0.0),
        "escalate": escalate, "reason": reason,
    }


async def test_join_reconcile_and_surface_overspend():
    results = [
        _wr(0, "a", _bud(10, 30, escalate=True, reason="OVERSPEND")),
        _wr(1, "b", _bud(10, 5)),
    ]
    out = await join_node({"worker_results": results, "budget": {"pool": 100, "per_branch": 10}})
    rep = out["budget_report"]
    assert rep["interrupt"]["surfaced"] is True
    assert rep["interrupt"]["reason"] == "OVERSPEND"
    assert rep["interrupt"]["contested_branches"] == [0]
    assert rep["total_spent"] == 35
    # reserve-pool: pool - total_reserved + total_returned = 100 - 20 + (0 + 5) = 85
    assert rep["pool_after"] == 85
    assert "contested by cost" in out["messages"][0]["content"]
    # §2.7 is a SURFACE, never a gate:
    assert "approval_required" not in out


async def test_join_surface_overspend_and_run_ceiling_combined():
    # audit r1 gap: both conditions at once -> reason "OVERSPEND+RUN_CEILING", still NOT a gate.
    results = [
        _wr(0, "a", _bud(10, 30, escalate=True, reason="OVERSPEND")),
        _wr(1, "b", _bud(10, 15)),
    ]
    out = await join_node(
        {"worker_results": results, "budget": {"pool": 100, "run_total": 60, "run_ceiling": 90}}
    )
    rep = out["budget_report"]
    # total_spent = 45, run_total = 60 + 45 = 105 >= 90
    assert rep["interrupt"]["reason"] == "OVERSPEND+RUN_CEILING"
    assert rep["interrupt"]["contested_branches"] == [0]
    assert rep["interrupt"]["ceiling_hit"] is True
    assert "approval_required" not in out


async def test_join_does_not_mutate_worker_entry_budget():
    # audit r1 purity fix: _reconcile_budget must NOT stamp idx onto the shared entry dict.
    b0 = _bud(10, 30, escalate=True, reason="OVERSPEND")
    results = [_wr(0, "a", b0)]
    await join_node({"worker_results": results, "budget": {"pool": 100}})
    assert "idx" not in b0  # the original per-branch budget dict is untouched
    assert "idx" not in results[0]["budget"]


async def test_join_surface_run_ceiling():
    results = [_wr(0, "a", _bud(100, 40))]
    out = await join_node(
        {"worker_results": results, "budget": {"pool": 1000, "run_total": 70, "run_ceiling": 100}}
    )
    rep = out["budget_report"]
    assert rep["run_total"] == 110  # 70 + 40
    assert rep["interrupt"]["ceiling_hit"] is True
    assert rep["interrupt"]["reason"] == "RUN_CEILING"
    assert rep["interrupt"]["surfaced"] is True


async def test_join_no_budget_report_without_budget():
    out = await join_node({"worker_results": [_wr(0, "a"), _wr(1, "b")]})
    assert "budget_report" not in out


async def test_join_body_byte_identical_without_surface():
    base = await join_node({"worker_results": [_wr(0, "alpha"), _wr(1, "beta")]})
    # A budget with no per-branch spend entries -> report present, but nothing surfaced,
    # so the message body is identical to the no-budget body.
    budg = await join_node(
        {"worker_results": [_wr(0, "alpha"), _wr(1, "beta")], "budget": {"pool": 100}}
    )
    assert budg["messages"][0]["content"] == base["messages"][0]["content"]
    assert "budget_report" in budg and "budget_report" not in base
    assert budg["budget_report"]["interrupt"]["surfaced"] is False


# ── build join reconcile (surface = the report field; verify stays the emitter) ─
async def test_build_join_reconcile_surface():
    impls = [
        {"idx": 0, "name": "f1", "source": "def f1(): pass", "status": "ok",
         "budget": _bud(5, 20, escalate=True, reason="OVERSPEND")},
    ]
    out = await _build_join(
        {"build_contracts": [_C1], "build_workspace": None, "build_impls": impls,
         "budget": {"pool": 50, "per_branch": 5}}
    )
    assert "verify_report" in out  # verify_node remains the sole message emitter
    assert out["budget_report"]["interrupt"]["surfaced"] is True
    assert out["budget_report"]["interrupt"]["contested_branches"] == [0]


async def test_build_join_no_report_without_budget():
    impls = [{"idx": 0, "name": "f1", "source": "def f1(): pass", "status": "ok"}]
    out = await _build_join(
        {"build_contracts": [_C1], "build_workspace": None, "build_impls": impls}
    )
    assert "budget_report" not in out


# ── routing no-regression + compile ───────────────────────────────────────────
def test_budget_is_not_a_routing_arm():
    # budget present, no other trigger -> still "dispatch" (byte-identical routing).
    assert _route_after_recall({"budget": {"pool": 100}}) == "dispatch"
    assert _route_after_recall({}) == "dispatch"
    # an explicit trigger still wins (budget never hijacks an arm).
    assert _route_after_recall({"budget": {"pool": 1}, "build_request": True}) == "plan_contracts"


def test_graph_compiles_with_budget_fields():
    g = build_graph(MemorySaver())
    assert g is not None
    assert "budget" in AgentState.__annotations__
    assert "budget_report" in AgentState.__annotations__
