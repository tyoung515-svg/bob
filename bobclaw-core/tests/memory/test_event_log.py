from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.memory._db import init_schema
from core.memory._hashing import _compute_event_hash, canonical_json, blake3_hex
from core.memory.event_log import SQLiteEventLog
from core.memory.exceptions import L0AppendFailed
from core.memory.interfaces import EventLog
from core.memory.models import Event


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    asyncio.run(init_schema(p))
    return p


def _make_event(
    event_id: str,
    body: dict,
    prev_hash: str | None = None,
    kind: str = "observation",
    ts: str = "2026-05-12T00:00:00Z",
) -> Event:
    h = _compute_event_hash(body, prev_hash)
    return Event(
        event_id=event_id,
        kind=kind,
        body=body,
        ts=ts,
        hash=h,
        prev_hash=prev_hash,
    )


@pytest.mark.asyncio
class TestAppendGet:
    async def test_append_get_round_trip(self, db_path: Path):
        log = SQLiteEventLog(db_path)
        event = _make_event("evt_001", {"msg": "hello"})
        returned_id = await log.append(event)
        assert returned_id == "evt_001"
        fetched = await log.get("evt_001")
        assert fetched.event_id == "evt_001"
        assert fetched.body == {"msg": "hello"}

    async def test_get_unknown_raises(self, db_path: Path):
        log = SQLiteEventLog(db_path)
        with pytest.raises(L0AppendFailed):
            await log.get("nonexistent")


@pytest.mark.asyncio
class TestReplay:
    async def test_replay_yields_events_in_insertion_order(self, db_path: Path):
        log = SQLiteEventLog(db_path)
        e1 = _make_event("evt_001", {"seq": 1})
        await log.append(e1)
        e2 = _make_event("evt_002", {"seq": 2}, prev_hash=e1.hash)
        await log.append(e2)
        e3 = _make_event("evt_003", {"seq": 3}, prev_hash=e2.hash)
        await log.append(e3)
        events = [e async for e in log.replay()]
        assert [e.event_id for e in events] == ["evt_001", "evt_002", "evt_003"]

    async def test_replay_since_yields_strictly_after(self, db_path: Path):
        log = SQLiteEventLog(db_path)
        e1 = _make_event("evt_001", {"seq": 1})
        await log.append(e1)
        e2 = _make_event("evt_002", {"seq": 2}, prev_hash=e1.hash)
        await log.append(e2)
        e3 = _make_event("evt_003", {"seq": 3}, prev_hash=e2.hash)
        await log.append(e3)
        events = [e async for e in log.replay(since_event_id="evt_001")]
        assert [e.event_id for e in events] == ["evt_002", "evt_003"]


@pytest.mark.asyncio
class TestHashChain:
    async def test_three_events_chain_correctly(self, db_path: Path):
        log = SQLiteEventLog(db_path)
        e1 = _make_event("evt_001", {"seq": 1})
        await log.append(e1)
        e2 = _make_event("evt_002", {"seq": 2}, prev_hash=e1.hash)
        await log.append(e2)
        e3 = _make_event("evt_003", {"seq": 3}, prev_hash=e2.hash)
        await log.append(e3)
        fetched = await log.get("evt_002")
        assert fetched.prev_hash == e1.hash
        fetched3 = await log.get("evt_003")
        assert fetched3.prev_hash == e2.hash

    async def test_reject_mismatched_hash(self, db_path: Path):
        log = SQLiteEventLog(db_path)
        bad_event = Event(
            event_id="evt_bad",
            kind="observation",
            body={"msg": "hi"},
            ts="2026-05-12T00:00:00Z",
            hash="blake3:0000000000000000000000000000000000000000000000000000000000000000",
            prev_hash=None,
        )
        with pytest.raises(L0AppendFailed, match="hash mismatch"):
            await log.append(bad_event)

    async def test_reject_mismatched_prev_hash(self, db_path: Path):
        log = SQLiteEventLog(db_path)
        e1 = _make_event("evt_001", {"seq": 1})
        await log.append(e1)
        bad_event = _make_event(
            "evt_002",
            {"seq": 2},
            prev_hash="blake3:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        with pytest.raises(L0AppendFailed, match="prev_hash mismatch"):
            await log.append(bad_event)


@pytest.mark.asyncio
class TestDeterminism:
    async def test_replay_determinism(self, db_path: Path):
        log = SQLiteEventLog(db_path)
        e1 = _make_event("evt_001", {"msg": "first"})
        await log.append(e1)
        e2 = _make_event("evt_002", {"msg": "second"}, prev_hash=e1.hash)
        await log.append(e2)
        run1 = [e async for e in log.replay()]
        run2 = [e async for e in log.replay()]
        assert len(run1) == len(run2)
        for a, b in zip(run1, run2):
            assert a.body == b.body


class TestSchemaIdempotency:
    def test_init_schema_twice_does_not_error(self, tmp_path: Path):
        p = tmp_path / "idem.db"
        asyncio.run(init_schema(p))
        asyncio.run(init_schema(p))


class TestProtocolConformance:
    def test_isinstance_eventlog(self, db_path: Path):
        assert isinstance(SQLiteEventLog(db_path), EventLog)
