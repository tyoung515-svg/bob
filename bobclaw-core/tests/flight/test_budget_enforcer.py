"""Flight substrate FU1 — budget-enforcer engine (run_tick).

One sweep pauses over-budget ACTIVE flights, leaves under/unbudgeted active, is idempotent,
and survives a store failure. Mirrors the spend-injection pattern in test_supervisor.py.
"""
from __future__ import annotations

from core.flight.budget_enforcer import run_tick
from core.flight.store import STATUS_ACTIVE, STATUS_PAUSED, FlightStore
from core.telemetry import spend as spend_mod

TS = "2026-07-04T00:00:00"


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


async def _store(tmp_path) -> FlightStore:
    s = FlightStore(tmp_path / "flights.db")
    await s.init()
    return s


def _wire_spend(monkeypatch):
    fake = _FakeHashRedis()  # ONE shared instance so spend accumulates + reads back
    monkeypatch.setattr(spend_mod, "_get_redis", lambda: fake)
    spend_mod._LOCAL_SPEND.clear()


async def test_run_tick_pauses_over_budget_only(tmp_path, monkeypatch):
    _wire_spend(monkeypatch)
    store = await _store(tmp_path)
    await store.create("over", "O", created=TS, budget_usd=0.5)
    await spend_mod.record_flight_spend("over", "kimi_platform", 0.9, emit=False)
    await store.create("under", "U", created=TS, budget_usd=5.0)
    await spend_mod.record_flight_spend("under", "kimi_platform", 0.1, emit=False)
    await store.create("free", "F", created=TS)  # unbudgeted — never paused
    await spend_mod.record_flight_spend("free", "kimi_platform", 99.0, emit=False)

    summary = await run_tick(store)
    assert summary["checked"] == 3
    assert summary["paused"] == ["over"]
    assert (await store.get("over")).status == STATUS_PAUSED
    assert (await store.get("under")).status == STATUS_ACTIVE
    assert (await store.get("free")).status == STATUS_ACTIVE


async def test_run_tick_idempotent(tmp_path, monkeypatch):
    _wire_spend(monkeypatch)
    store = await _store(tmp_path)
    await store.create("over", "O", created=TS, budget_usd=0.5)
    await spend_mod.record_flight_spend("over", "kimi_platform", 0.9, emit=False)

    first = await run_tick(store)
    assert first["paused"] == ["over"]
    # second sweep: "over" is now paused ⇒ not in the ACTIVE list ⇒ nothing to do
    second = await run_tick(store)
    assert second == {"checked": 0, "paused": []}
    assert (await store.get("over")).status == STATUS_PAUSED


async def test_run_tick_survives_store_failure():
    class _BoomStore:
        async def list(self, **k):
            raise RuntimeError("db down")

    summary = await run_tick(_BoomStore())
    assert summary == {"checked": 0, "paused": []}
