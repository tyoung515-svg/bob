"""Flight substrate Lane 1a — FlightStore (SQLite registry)."""
from __future__ import annotations

import pytest

from core.flight.store import (
    STATUS_ACTIVE,
    STATUS_DONE,
    STATUS_PAUSED,
    Flight,
    FlightStore,
)

TS = "2026-07-04T00:00:00"


async def _new_store(tmp_path):
    s = FlightStore(tmp_path / "flights.db")
    await s.init()
    return s


async def test_create_and_get_roundtrip(tmp_path):
    s = await _new_store(tmp_path)
    f = await s.create("ms-5", "Mega Sprint 5", created=TS, project="bobclaw",
                       budget_usd=5.0, priority=3)
    assert f == Flight("ms-5", "Mega Sprint 5", "bobclaw", 5.0, 3, STATUS_ACTIVE, TS)
    got = await s.get("ms-5")
    assert got == f


async def test_create_duplicate_raises(tmp_path):
    s = await _new_store(tmp_path)
    await s.create("ms-5", "A", created=TS)
    with pytest.raises(ValueError, match="already exists"):
        await s.create("ms-5", "B", created=TS)


async def test_create_rejects_bad_status_and_empty_id(tmp_path):
    s = await _new_store(tmp_path)
    with pytest.raises(ValueError):
        await s.create("x", "X", created=TS, status="bogus")
    with pytest.raises(ValueError):
        await s.create("  ", "X", created=TS)


async def test_ensure_is_idempotent(tmp_path):
    s = await _new_store(tmp_path)
    a = await s.ensure("ms-5", "First", created=TS, budget_usd=1.0)
    b = await s.ensure("ms-5", "Second", created="2026-07-05T00:00:00", budget_usd=99.0)
    # second ensure does NOT overwrite — returns the existing row unchanged
    assert b == a
    assert b.name == "First" and b.budget_usd == 1.0


async def test_list_and_filter_by_status(tmp_path):
    s = await _new_store(tmp_path)
    await s.create("a", "A", created="2026-07-04T00:00:01")
    await s.create("b", "B", created="2026-07-04T00:00:02", status=STATUS_DONE)
    await s.create("c", "C", created="2026-07-04T00:00:03")
    all_ids = [f.flight_id for f in await s.list()]
    assert all_ids == ["c", "b", "a"]  # newest first
    active = [f.flight_id for f in await s.list(status=STATUS_ACTIVE)]
    assert set(active) == {"a", "c"}
    done = [f.flight_id for f in await s.list(status=STATUS_DONE)]
    assert done == ["b"]


async def test_update_fields(tmp_path):
    s = await _new_store(tmp_path)
    await s.create("ms-5", "A", created=TS, priority=0, budget_usd=1.0)
    updated = await s.update("ms-5", priority=5, budget_usd=10.0, project="p")
    assert updated.priority == 5 and updated.budget_usd == 10.0 and updated.project == "p"


async def test_update_rejects_unknown_field_and_bad_status(tmp_path):
    s = await _new_store(tmp_path)
    await s.create("ms-5", "A", created=TS)
    with pytest.raises(ValueError, match="cannot update"):
        await s.update("ms-5", created="hacked")  # 'created' is immutable
    with pytest.raises(ValueError):
        await s.update("ms-5", status="bogus")


async def test_set_status_and_missing_returns_none(tmp_path):
    s = await _new_store(tmp_path)
    await s.create("ms-5", "A", created=TS)
    assert (await s.set_status("ms-5", STATUS_PAUSED)).status == STATUS_PAUSED
    assert await s.update("nope", priority=1) is None


async def test_delete(tmp_path):
    s = await _new_store(tmp_path)
    await s.create("ms-5", "A", created=TS)
    assert await s.delete("ms-5") is True
    assert await s.get("ms-5") is None
    assert await s.delete("ms-5") is False
