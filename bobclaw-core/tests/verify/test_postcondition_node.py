"""Unit tests for core.nodes.postcondition.postcondition_node + the graph routing arm.

PURE — the critic call is driven by monkeypatching ``core.verify.postcondition._default_send``
with a fake; a real backend is never touched. Also asserts the §2.6 tier-1 routing arm is
guarded (non-postcondition turns are byte-identical) and the node is registered in the graph.
"""
from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import MemorySaver

from core.graph import _route_after_recall, build_graph
from core.nodes.postcondition import postcondition_node


def _fake_send(reply: str):
    async def _send(messages, backend):
        return reply
    return _send


# ── node verdict surfacing ──────────────────────────────────────────────────────
async def test_node_holds_passes(monkeypatch):
    monkeypatch.setattr("core.verify.postcondition._default_send",
                        _fake_send('{"verdict":"holds","reasons":["OK"]}'))
    state = {"post_condition": {
        "step": "rename", "statement": "new.txt exists, old.txt gone",
        "result": "renamed", "actor_backend": "deepseek_v4_flash", "critic_backend": "glm_5_2"}}
    out = await postcondition_node(state)
    v = out["post_condition_verdict"]
    assert v["passed"] is True
    assert v["verdict"] == "holds"
    assert v["actor_backend"] == "deepseek_v4_flash"
    assert v["critic_backend"] == "glm_5_2"
    assert v["decorrelated"] is True
    assert v["reasons"] == ["OK"]


async def test_node_violated_does_not_pass(monkeypatch):
    monkeypatch.setattr("core.verify.postcondition._default_send",
                        _fake_send('{"verdict":"violated","reasons":["unmet"]}'))
    state = {"post_condition": {
        "step": "close account", "statement": "balance $0, closed",
        "result": "balance $4521.77 ACTIVE", "actor_backend": "deepseek_v4_flash",
        "critic_backend": "glm_5_2"}}
    out = await postcondition_node(state)
    assert out["post_condition_verdict"]["passed"] is False
    assert out["post_condition_verdict"]["verdict"] == "violated"


async def test_node_actor_backend_falls_back_to_state_backend(monkeypatch):
    monkeypatch.setattr("core.verify.postcondition._default_send",
                        _fake_send('{"verdict":"holds","reasons":[]}'))
    state = {"backend": "deepseek_v4_flash",
             "post_condition": {"step": "s", "statement": "x", "result": "y",
                                "critic_backend": "glm_5_2"}}
    out = await postcondition_node(state)
    assert out["post_condition_verdict"]["actor_backend"] == "deepseek_v4_flash"
    assert out["post_condition_verdict"]["passed"] is True


async def test_node_no_post_condition_guard(monkeypatch):
    monkeypatch.setattr("core.verify.postcondition._default_send", _fake_send("unused"))
    out = await postcondition_node({})
    v = out["post_condition_verdict"]
    assert v["passed"] is False
    assert v["verdict"] == "unknown"
    assert "no post-condition declared" in v["reasons"]


async def test_node_no_statement_guard(monkeypatch):
    monkeypatch.setattr("core.verify.postcondition._default_send", _fake_send("unused"))
    state = {"post_condition": {"step": "s", "result": "r", "actor_backend": "deepseek_v4_flash"}}
    out = await postcondition_node(state)
    v = out["post_condition_verdict"]
    assert v["passed"] is False
    assert "no post-condition declared" in v["reasons"]


async def test_node_non_dict_post_condition_is_failsafe(monkeypatch):
    monkeypatch.setattr("core.verify.postcondition._default_send", _fake_send("unused"))
    out = await postcondition_node({"post_condition": "not-a-dict"})
    v = out["post_condition_verdict"]
    assert v["passed"] is False
    assert v["verdict"] == "unknown"
    assert "no post-condition declared" in v["reasons"]


async def test_node_postcondition_error_caught(monkeypatch):
    monkeypatch.setattr("core.verify.postcondition._default_send", _fake_send("unused"))
    # same-family critic → verify_post_condition raises PostConditionError → node catches it.
    state = {"post_condition": {
        "step": "s", "statement": "x", "result": "y",
        "actor_backend": "deepseek_v4_flash", "critic_backend": "deepseek_v4_flash"}}
    out = await postcondition_node(state)
    v = out["post_condition_verdict"]
    assert v["passed"] is False
    assert v["verdict"] == "unknown"
    assert v["reasons"][0].startswith("postcondition_error:")
    assert v["decorrelated"] is False
    assert set(v) == {"verdict", "passed", "reasons", "actor_backend", "critic_backend", "decorrelated"}


async def test_node_reads_team_for_decorrelation(monkeypatch):
    captured = {}

    def fake_decorrelated(actor_backend, *, team=None, candidates=None):
        captured["team"] = team
        return "glm_5_2"

    monkeypatch.setattr("core.verify.postcondition.decorrelated_critic_backend", fake_decorrelated)
    monkeypatch.setattr("core.verify.postcondition._default_send",
                        _fake_send('{"verdict":"holds","reasons":[]}'))
    state = {"team": "my-team",
             "post_condition": {"step": "s", "statement": "x", "result": "y",
                                "actor_backend": "deepseek_v4_flash"}}  # no critic_backend
    out = await postcondition_node(state)
    assert captured["team"] == "my-team"
    assert out["post_condition_verdict"]["critic_backend"] == "glm_5_2"
    assert out["post_condition_verdict"]["passed"] is True


async def test_node_accepts_alt_post_condition_key(monkeypatch):
    # The spec accepts either "statement" or the alt "post_condition" key as the declaration.
    monkeypatch.setattr("core.verify.postcondition._default_send",
                        _fake_send('{"verdict":"holds","reasons":[]}'))
    state = {"post_condition": {"step": "s", "post_condition": "x holds", "result": "y",
                                "actor_backend": "deepseek_v4_flash", "critic_backend": "glm_5_2"}}
    out = await postcondition_node(state)
    assert out["post_condition_verdict"]["passed"] is True


async def test_node_never_raises_on_unparseable(monkeypatch):
    monkeypatch.setattr("core.verify.postcondition._default_send", _fake_send("not json at all"))
    state = {"post_condition": {"step": "s", "statement": "x", "result": "y",
                                "actor_backend": "deepseek_v4_flash", "critic_backend": "glm_5_2"}}
    out = await postcondition_node(state)
    assert out["post_condition_verdict"]["passed"] is False
    assert out["post_condition_verdict"]["verdict"] == "unknown"


async def test_node_actor_backend_defaults_to_local(monkeypatch):
    # No actor_backend in the spec AND no state["backend"] → "local".
    monkeypatch.setattr("core.verify.postcondition._default_send",
                        _fake_send('{"verdict":"holds","reasons":[]}'))
    out = await postcondition_node({"post_condition": {"step": "s", "statement": "x", "result": "y"}})
    assert out["post_condition_verdict"]["actor_backend"] == "local"
    # local resolves to a cross-family critic, so the call still completes
    assert out["post_condition_verdict"]["passed"] is True


async def test_node_broad_except_belt_never_raises(monkeypatch):
    # An unexpected (non-PostConditionError) failure inside verify must still surface as a
    # not-passed verdict, not propagate out of the node (the fail-safe belt).
    async def _boom(*a, **k):
        raise ValueError("unexpected internal error")

    monkeypatch.setattr("core.nodes.postcondition.verify_post_condition", _boom)
    out = await postcondition_node({"post_condition": {"statement": "x",
                                                       "actor_backend": "deepseek_v4_flash"}})
    v = out["post_condition_verdict"]
    assert v["passed"] is False
    assert v["verdict"] == "unknown"
    assert "postcondition_node_error" in v["reasons"][0]


# ── routing no-regression (the guarded arm) ─────────────────────────────────────
def test_route_postcondition_priority_over_other_triggers():
    # post_condition is checked FIRST → it wins even when other triggers are also set.
    st = {"post_condition": {"statement": "x"}, "build_request": True,
          "hierarchical": True, "council_spec": {}}
    assert _route_after_recall(st) == "postcondition"


# ── routing no-regression (the guarded arm) ─────────────────────────────────────
def test_route_postcondition_arm():
    assert _route_after_recall({"post_condition": {"statement": "x"}}) == "postcondition"


def test_route_empty_post_condition_dict_is_dispatch():
    # Regression guard: an empty dict (no declared statement) must be byte-identical
    # to no post_condition at all — i.e. it must fall through to dispatch.
    assert _route_after_recall({"post_condition": {}}) == "dispatch"


def test_route_post_condition_with_empty_statement_is_dispatch():
    assert _route_after_recall({"post_condition": {"statement": ""}}) == "dispatch"


def test_route_whitespace_only_statement_is_dispatch():
    # A whitespace-only statement is not a meaningful post-condition → byte-identical dispatch.
    assert _route_after_recall({"post_condition": {"statement": "   \n"}}) == "dispatch"


def test_route_alt_post_condition_key_arm():
    # The alt "post_condition" declaration key also triggers the routing arm.
    assert _route_after_recall({"post_condition": {"post_condition": "x"}}) == "postcondition"


def test_route_default_dispatch_byte_identical():
    assert _route_after_recall({}) == "dispatch"


def test_route_build_request_wins_when_no_postcondition():
    assert _route_after_recall({"build_request": True}) == "plan_contracts"


def test_route_hierarchical_wins_when_no_postcondition():
    assert _route_after_recall({"hierarchical": True}) == "manager_dispatch"


def test_route_council_spec_when_no_postcondition():
    assert _route_after_recall({"council_spec": {}}) == "panel_dispatch"


def test_build_graph_registers_postcondition_node():
    g = build_graph(MemorySaver())
    assert "postcondition" in g.get_graph().nodes


def test_postcondition_node_has_edge_to_end():
    # The post-condition critic is a LEAF: an edge postcondition → END must exist (a missing
    # edge would fail at runtime while leaving node-registration tests green).
    g = build_graph(MemorySaver())
    graph = g.get_graph()
    targets = {e.target for e in graph.edges if e.source == "postcondition"}
    # END is represented by the "__end__" sentinel node id in the drawable graph.
    assert any("end" in str(t).lower() for t in targets), f"no postcondition→END edge: {targets}"
