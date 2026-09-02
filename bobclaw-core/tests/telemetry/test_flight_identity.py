"""Flight substrate L0.1 — flight identity + fan-out threading (PURE).

Proves:
  * ``resolve_flight_id`` precedence: explicit named flight > ``chat:<conv>`` > ambient.
  * ``chat_flight_id`` / ``is_ambient`` classification.
  * The fan-out threads an explicit flight onto every Send (chat AND build branches),
    and is BYTE-IDENTICAL (no ``flight_id`` key) when no flight is set — the additive
    discipline the KICKOFF §6 L0.1 requires.
  * The graph still compiles with the new AgentState field.

No network (pytest runs --disable-socket).
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Send

from core.graph import build_graph
from core.nodes.dispatch import _route_after_dispatch, dispatch_node
from core.telemetry.flight import (
    AMBIENT_FLIGHT,
    chat_flight_id,
    is_ambient,
    resolve_flight_id,
)


# ── shared fan-out states (mirror test_budget_wiring) ─────────────────────────
def _chat_state(**ov):
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
    st.update(dispatch_node(st))
    return st


_C1 = {"name": "f1", "signature": "def f1(): ...", "doc": "", "cases": []}
_C2 = {"name": "f2", "signature": "def f2(): ...", "doc": "", "cases": []}


def _build_state(**ov):
    st = {
        "build_contracts": [_C1, _C2],
        "build_workspace": None,
        "team": "demo-fleet",
        "messages": [],
    }
    st.update(ov)
    return st


# ── resolve_flight_id precedence ──────────────────────────────────────────────
def test_resolve_explicit_named_flight_wins():
    assert resolve_flight_id({"flight_id": "ms-5", "conversation_id": "abc"}) == "ms-5"


def test_resolve_explicit_flight_is_stripped():
    assert resolve_flight_id({"flight_id": "  ms-5  "}) == "ms-5"


def test_resolve_falls_back_to_chat_conversation():
    assert resolve_flight_id({"conversation_id": "abc123"}) == "chat:abc123"


def test_resolve_falls_back_to_ambient():
    assert resolve_flight_id({}) == AMBIENT_FLIGHT
    assert resolve_flight_id(None) == AMBIENT_FLIGHT
    assert resolve_flight_id({"flight_id": None, "conversation_id": None}) == AMBIENT_FLIGHT
    assert resolve_flight_id({"flight_id": "   "}) == AMBIENT_FLIGHT  # blank ⇒ not named


def test_resolve_coerces_nonstr_flight_id():
    # A caller bug (int flight id) must not crash telemetry — coerce, don't repr a None.
    assert resolve_flight_id({"flight_id": 42}) == "42"


def test_chat_flight_id_blank_is_ambient():
    assert chat_flight_id(None) == AMBIENT_FLIGHT
    assert chat_flight_id("") == AMBIENT_FLIGHT
    assert chat_flight_id("   ") == AMBIENT_FLIGHT
    assert chat_flight_id("c1") == "chat:c1"


def test_is_ambient_classification():
    assert is_ambient(None) is True
    assert is_ambient("") is True
    assert is_ambient(AMBIENT_FLIGHT) is True
    assert is_ambient("chat:abc") is True          # live-face conversation flight
    assert is_ambient("ms-5") is False             # named block of work
    assert is_ambient("nightly-sweep") is False


# ── fan-out threading (chat branch) ───────────────────────────────────────────
def test_chat_fanout_threads_explicit_flight():
    sends = _route_after_dispatch(_chat_state(flight_id="ms-5"))
    assert all(isinstance(s, Send) for s in sends)
    assert [s.arg["flight_id"] for s in sends] == ["ms-5", "ms-5", "ms-5"]


def test_chat_fanout_byte_identical_without_flight():
    base = _route_after_dispatch(_chat_state())
    for s in base:
        assert "flight_id" not in s.arg


def test_chat_fanout_args_identical_modulo_flight():
    base = _route_after_dispatch(_chat_state())
    flighted = _route_after_dispatch(_chat_state(flight_id="ms-5"))
    assert len(base) == len(flighted) == 3
    for b, g in zip(base, flighted):
        stripped = {k: v for k, v in g.arg.items() if k != "flight_id"}
        assert stripped == b.arg  # every other key byte-identical to today


def test_chat_fanout_blank_flight_adds_no_key():
    # A blank/whitespace flight is not a named flight ⇒ no key (byte-identical).
    for s in _route_after_dispatch(_chat_state(flight_id="   ")):
        assert "flight_id" not in s.arg


# ── fan-out threading (build branch) ──────────────────────────────────────────
def test_build_fanout_threads_explicit_flight():
    sends = _route_after_dispatch(_build_state(flight_id="build-42"))
    assert [s.arg["flight_id"] for s in sends] == ["build-42", "build-42"]


def test_build_fanout_byte_identical_without_flight():
    for s in _route_after_dispatch(_build_state()):
        assert "flight_id" not in s.arg


# ── graph compiles with the new field ─────────────────────────────────────────
def test_graph_compiles_with_flight_id_field():
    g = build_graph(checkpointer=MemorySaver())
    assert g is not None
