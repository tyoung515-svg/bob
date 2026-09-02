"""Flight substrate Lane 1a — supervisor (budget enforcement + GLM-serial fair-share)."""
from __future__ import annotations

import pytest

from core.flight.store import STATUS_ACTIVE, STATUS_PAUSED, FlightStore
from core.flight.supervisor import (
    ProviderSlots,
    budget_status,
    enforce_budget,
    plan_provider_grants,
    provider_capacity,
    spill_target,
)
from core.telemetry import spend as spend_mod

TS = "2026-07-04T00:00:00"


# ── in-memory fakes ───────────────────────────────────────────────────────────
class _FakeHashRedis:
    def __init__(self):
        self.h = {}

    async def hincrbyfloat(self, key, field, amount):
        self.h.setdefault(key, {})
        self.h[key][field] = self.h[key].get(field, 0.0) + float(amount)
        return self.h[key][field]

    async def hgetall(self, key):
        return {k: str(v) for k, v in self.h.get(key, {}).items()}

    async def delete(self, key):
        self.h.pop(key, None)


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


async def _new_store(tmp_path):
    s = FlightStore(tmp_path / "flights.db")
    await s.init()
    return s


# ── pure allocator ────────────────────────────────────────────────────────────
def test_provider_capacity():
    assert provider_capacity("glm_5_2") == 1
    assert provider_capacity("glm") == 1
    assert provider_capacity("deepseek_v4_flash") == 20
    assert provider_capacity("totally_unknown") == 100  # global cap


def test_spill_target():
    assert spill_target("glm_5_2") == "glm_payg"
    assert spill_target("deepseek_v4_flash") is None


def test_glm_serial_grants_one_by_priority():
    reqs = [
        {"flight_id": "low", "priority": 1},
        {"flight_id": "high", "priority": 9},
        {"flight_id": "mid", "priority": 5},
    ]
    plan = plan_provider_grants("glm_5_2", reqs)
    assert plan["capacity"] == 1
    assert plan["granted"] == ["high"]
    assert set(plan["deferred"]) == {"low", "mid"}
    assert plan["spill"] == "glm_payg"  # deferred GLM work spills to PAYG


def test_ties_break_fifo():
    reqs = [
        {"flight_id": "first", "priority": 5},
        {"flight_id": "second", "priority": 5},
    ]
    assert plan_provider_grants("glm_5_2", reqs)["granted"] == ["first"]


def test_non_serial_provider_grants_all_under_cap():
    reqs = [{"flight_id": f"f{i}", "priority": 0} for i in range(3)]
    plan = plan_provider_grants("deepseek_v4_flash", reqs)
    assert set(plan["granted"]) == {"f0", "f1", "f2"}
    assert plan["deferred"] == []
    assert plan["spill"] is None


# ── budget enforcement ────────────────────────────────────────────────────────
async def test_budget_status_under_over_and_unbudgeted(tmp_path, monkeypatch):
    store = await _new_store(tmp_path)
    _spend_redis = _FakeHashRedis()
    monkeypatch.setattr(spend_mod, "_get_redis", lambda: _spend_redis)
    spend_mod._LOCAL_SPEND.clear()

    await store.create("b1", "budgeted", created=TS, budget_usd=1.0)
    await spend_mod.record_flight_spend("b1", "kimi_platform", 0.4, emit=False)
    st = await budget_status(store, "b1")
    assert st["spent_usd"] == 0.4 and st["remaining_usd"] == 0.6 and st["over_budget"] is False

    await spend_mod.record_flight_spend("b1", "kimi_platform", 0.7, emit=False)  # now 1.1
    st = await budget_status(store, "b1")
    assert st["over_budget"] is True and st["remaining_usd"] < 0

    await store.create("u1", "unbudgeted", created=TS)  # budget_usd None
    await spend_mod.record_flight_spend("u1", "kimi_platform", 99.0, emit=False)
    assert (await budget_status(store, "u1"))["over_budget"] is False

    missing = await budget_status(store, "ghost")
    assert missing["exists"] is False and missing["budget_usd"] is None


async def test_enforce_budget_pauses_over_flight(tmp_path, monkeypatch):
    store = await _new_store(tmp_path)
    _spend_redis = _FakeHashRedis()
    monkeypatch.setattr(spend_mod, "_get_redis", lambda: _spend_redis)
    spend_mod._LOCAL_SPEND.clear()

    await store.create("b1", "b", created=TS, budget_usd=0.5)
    await spend_mod.record_flight_spend("b1", "kimi_platform", 0.6, emit=False)
    res = await enforce_budget(store, "b1")
    assert res["over_budget"] is True and res["paused"] is True
    assert (await store.get("b1")).status == STATUS_PAUSED


async def test_enforce_budget_leaves_under_flight_active(tmp_path, monkeypatch):
    store = await _new_store(tmp_path)
    _spend_redis = _FakeHashRedis()
    monkeypatch.setattr(spend_mod, "_get_redis", lambda: _spend_redis)
    spend_mod._LOCAL_SPEND.clear()

    await store.create("b1", "b", created=TS, budget_usd=5.0)
    await spend_mod.record_flight_spend("b1", "kimi_platform", 0.6, emit=False)
    res = await enforce_budget(store, "b1")
    assert res["over_budget"] is False and res["paused"] is False
    assert (await store.get("b1")).status == STATUS_ACTIVE


# ── live provider slot (GLM serial) ───────────────────────────────────────────
async def test_provider_slot_serializes_glm():
    kv = _FakeKV()
    slots = ProviderSlots(redis_getter=lambda: kv)
    assert await slots.try_acquire("glm_5_2", "f1", priority=5) is True
    # a lower-priority flight cannot take the single slot
    assert await slots.try_acquire("glm_5_2", "f2", priority=1) is False
    assert await slots.holder("glm_5_2") == "f1"


async def test_provider_slot_priority_preemption():
    kv = _FakeKV()
    slots = ProviderSlots(redis_getter=lambda: kv)
    await slots.try_acquire("glm_5_2", "low", priority=1)
    # a higher-priority flight preempts
    assert await slots.try_acquire("glm_5_2", "high", priority=9) is True
    assert await slots.holder("glm_5_2") == "high"


async def test_provider_slot_reentrant_same_holder():
    kv = _FakeKV()
    slots = ProviderSlots(redis_getter=lambda: kv)
    await slots.try_acquire("glm_5_2", "f1", priority=5)
    assert await slots.try_acquire("glm_5_2", "f1", priority=5) is True  # re-acquire own


async def test_provider_slot_release_only_by_holder():
    kv = _FakeKV()
    slots = ProviderSlots(redis_getter=lambda: kv)
    await slots.try_acquire("glm_5_2", "f1", priority=5)
    await slots.release("glm_5_2", "f2")   # not the holder — no-op
    assert await slots.holder("glm_5_2") == "f1"
    await slots.release("glm_5_2", "f1")
    assert await slots.holder("glm_5_2") is None


async def test_non_serial_provider_always_acquires():
    kv = _FakeKV()
    slots = ProviderSlots(redis_getter=lambda: kv)
    assert await slots.try_acquire("deepseek_v4_flash", "f1") is True
    assert await slots.try_acquire("deepseek_v4_flash", "f2") is True


async def test_provider_slot_fails_open_on_redis_error():
    slots = ProviderSlots(redis_getter=lambda: _BoomKV())
    # Redis down → do NOT wedge work; a resulting GLM 429 is handled by the throttle path.
    assert await slots.try_acquire("glm_5_2", "f1", priority=5) is True
