"""Flight substrate FU1 — live serial-provider (GLM) enforcement gate.

Covers ``core.flight.enforcement.acquire_or_escalate`` / ``release``: inert when the flag is
off (byte-identical), cross-flight serialization, priority preemption, D1 escalation back-off
on contention, ambient⇒priority-0, and fail-open on a dead KV. Reuses the ProviderSlots fakes.
"""
from __future__ import annotations

import pytest

from core.config import config
from core.flight import enforcement as enf
from core.flight import service as flight_service
from core.flight.supervisor import ProviderSlots
from core.telemetry import spend as spend_mod


class _FakeKV:
    def __init__(self):
        self.d = {}

    async def set(self, key, val, nx=False, ex=None):
        if nx and key in self.d:
            return None
        self.d[key] = val
        return True

    async def get(self, key):
        return self.d.get(key)

    async def delete(self, key):
        self.d.pop(key, None)


class _BoomKV:
    async def set(self, *a, **k):
        raise RuntimeError("redis down")

    async def get(self, *a, **k):
        raise RuntimeError("redis down")

    async def delete(self, *a, **k):
        raise RuntimeError("redis down")


@pytest.fixture(autouse=True)
def _isolate():
    """Fresh slots + no bound flight around each test."""
    enf._reset_for_tests()
    spend_mod.set_current_flight(None)
    yield
    enf._reset_for_tests()
    spend_mod.set_current_flight(None)


def _slots(kv) -> ProviderSlots:
    return ProviderSlots(redis_getter=lambda: kv)


# ── inert when off ────────────────────────────────────────────────────────────
async def test_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", False)
    kv = _FakeKV()
    enf.set_provider_slots(_slots(kv))
    spend_mod.set_current_flight("proj-x")
    assert await enf.acquire_or_escalate("glm_5_2") == ("glm_5_2", None)
    assert await enf.acquire_or_escalate("deepseek_v4_flash") == ("deepseek_v4_flash", None)
    assert kv.d == {}  # slot never touched


async def test_non_serial_backend_never_slots(monkeypatch):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", True)
    kv = _FakeKV()
    enf.set_provider_slots(_slots(kv))
    assert await enf.acquire_or_escalate("deepseek_v4_flash") == ("deepseek_v4_flash", None)
    assert kv.d == {}


# ── acquire / release ─────────────────────────────────────────────────────────
async def test_flag_on_acquires_and_releases(monkeypatch):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", True)
    kv = _FakeKV()
    enf.set_provider_slots(_slots(kv))
    spend_mod.set_current_flight(None)  # ⇒ "ambient", priority 0
    run_backend, holder = await enf.acquire_or_escalate("glm_5_2")
    assert run_backend == "glm_5_2" and holder == "ambient"
    assert kv.d  # slot held
    await enf.release("glm_5_2", holder)
    assert kv.d == {}  # slot freed


# ── D1 escalation back-off on contention ──────────────────────────────────────
async def test_contention_escalates(monkeypatch):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", True)
    kv = _FakeKV()
    slots = _slots(kv)
    enf.set_provider_slots(slots)
    # another flight already holds the serial slot at priority 0
    assert await slots.try_acquire("glm_5_2", "other-flight", 0) is True
    spend_mod.set_current_flight(None)  # ambient, priority 0 ⇒ cannot preempt
    run_backend, holder = await enf.acquire_or_escalate("glm_5_2")
    assert run_backend == enf.SERIAL_ESCALATION["glm_5_2"]  # deepseek_v4_flash
    assert holder is None  # nothing to release


# ── priority preemption (named flight outranks the holder) ────────────────────
async def test_higher_priority_named_flight_preempts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", True)
    monkeypatch.setattr(config, "FLIGHT_DB", str(tmp_path / "flights.db"))
    flight_service._reset_for_tests()
    await flight_service.create_flight("proj-hi", "hi", priority=5)
    kv = _FakeKV()
    slots = _slots(kv)
    enf.set_provider_slots(slots)
    assert await slots.try_acquire("glm_5_2", "low-flight", 0) is True  # low holder
    spend_mod.set_current_flight("proj-hi")  # priority 5 from the store
    run_backend, holder = await enf.acquire_or_escalate("glm_5_2")
    assert run_backend == "glm_5_2" and holder == "proj-hi"  # preempted, not escalated
    flight_service._reset_for_tests()


# ── fail-open ─────────────────────────────────────────────────────────────────
async def test_fail_open_on_dead_kv(monkeypatch):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", True)
    enf.set_provider_slots(_slots(_BoomKV()))
    spend_mod.set_current_flight(None)  # ambient ⇒ no store touch; isolates the dead KV
    # ProviderSlots.try_acquire is itself fail-open (True on Redis error) ⇒ we still run
    # glm, just effectively unslotted; either way the turn must proceed, never raise/block.
    run_backend, holder = await enf.acquire_or_escalate("glm_5_2")
    assert run_backend == "glm_5_2"  # never blocked / never escalated on infra failure


# ── F1.3 pause-with-teeth (D2) ────────────────────────────────────────────────
async def _make_paused(tmp_path, monkeypatch, flight_id="proj-x", name="Sprint X"):
    monkeypatch.setattr(config, "FLIGHT_DB", str(tmp_path / "f.db"))
    flight_service._reset_for_tests()
    await flight_service.create_flight(flight_id, name, budget_usd=5.0)
    await flight_service.update_flight(flight_id, status="paused")


async def test_pause_refusal_flag_off_never_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", False)
    await _make_paused(tmp_path, monkeypatch)
    assert await enf.paused_flight_refusal({"flight_id": "proj-x"}) is None
    flight_service._reset_for_tests()


async def test_pause_refusal_ambient_and_livefaces_never_refused(monkeypatch):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", True)
    assert await enf.paused_flight_refusal({}) is None                    # ambient
    assert await enf.paused_flight_refusal({"flight_id": "chat:abc"}) is None  # live-face


async def test_pause_refusal_paused_named_flight_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", True)
    await _make_paused(tmp_path, monkeypatch)
    out = await enf.paused_flight_refusal({"flight_id": "proj-x"})
    assert out is not None
    assert out["flight_refused"] is True
    assert out["error"] == "flight_paused:proj-x"
    assert "PAUSED" in out["messages"][0]["content"]
    assert out["messages"][0]["role"] == "assistant"
    flight_service._reset_for_tests()


async def test_pause_refusal_active_flight_proceeds(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", True)
    monkeypatch.setattr(config, "FLIGHT_DB", str(tmp_path / "f.db"))
    flight_service._reset_for_tests()
    await flight_service.create_flight("proj-y", "Y", budget_usd=5.0)  # active
    assert await enf.paused_flight_refusal({"flight_id": "proj-y"}) is None
    flight_service._reset_for_tests()


# ── integrated: route_node → _route_after_recall wiring ───────────────────────
async def test_route_node_refuses_paused_flight_and_router_ends(monkeypatch, tmp_path):
    from langgraph.graph import END

    from core.graph import _route_after_recall
    from core.nodes.route import route_node

    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", True)
    await _make_paused(tmp_path, monkeypatch, flight_id="proj-z", name="Z")
    state = {"flight_id": "proj-z", "face_id": "assistant", "messages": [], "task": "do stuff"}
    out = await route_node(state)
    assert out.get("flight_refused") is True
    assert "PAUSED" in out["messages"][0]["content"]
    # the router then ENDs the turn (no work node runs)
    assert _route_after_recall({**state, **out}) == END
    flight_service._reset_for_tests()


async def test_route_node_flag_off_ignores_paused_flight(monkeypatch, tmp_path):
    from core.nodes.route import route_node

    monkeypatch.setattr(config, "FLIGHT_ENFORCE_ENABLED", False)
    await _make_paused(tmp_path, monkeypatch, flight_id="proj-z", name="Z")
    out = await route_node(
        {"flight_id": "proj-z", "face_id": "assistant", "messages": [], "task": "hello"}
    )
    assert not out.get("flight_refused")  # byte-identical: normal routing proceeds
    assert "backend" in out
    flight_service._reset_for_tests()
