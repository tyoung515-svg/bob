from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.memory._db import init_schema
from core.memory.event_log import SQLiteEventLog
from core.memory.exceptions import HashingError, L1ValidationFailed
from core.memory._hashing import _compute_event_hash
from core.memory.fact_store import SQLiteFactStore
from core.memory.interfaces import FactStore
from core.memory.models import ConfidenceStub, Event, Fact


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    asyncio.run(init_schema(p))
    return p


@pytest.fixture
async def seeded_store(db_path: Path) -> SQLiteFactStore:
    log = SQLiteEventLog(db_path)
    ev = Event(
        event_id="evt_source",
        kind="observation",
        body={"text": "seed"},
        ts="2026-05-12T00:00:00Z",
        hash=_compute_event_hash({"text": "seed"}, None),
        prev_hash=None,
    )
    await log.append(ev)
    return SQLiteFactStore(db_path)


def _make_event(event_id: str) -> Event:
    return Event(
        event_id=event_id,
        kind="observation",
        body={"text": event_id},
        ts="2026-05-12T00:00:00Z",
        hash=_compute_event_hash({"text": event_id}, None),
        prev_hash=None,
    )


def _make_fact(
    fact_id: str,
    generation_method: str = "extract_facts_from_event",
    body: dict | None = None,
    source_event_id: str = "evt_source",
    input_hash: str = "blake3:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    confidence: ConfidenceStub | None = None,
    ts: str = "2026-05-12T00:00:00Z",
) -> Fact:
    return Fact(
        fact_id=fact_id,
        generation_method=generation_method,
        body=body or {"text": f"fact {fact_id}"},
        source_event_id=source_event_id,
        input_hash=input_hash,
        confidence=confidence or ConfidenceStub(),
        ts=ts,
    )


@pytest.mark.asyncio
class TestPutGet:
    async def test_put_get_round_trip(self, seeded_store: SQLiteFactStore):
        fact = _make_fact("fct_001")
        returned_id = await seeded_store.put(fact)
        assert returned_id == "fct_001"
        fetched = await seeded_store.get("fct_001")
        assert fetched.fact_id == "fct_001"
        assert fetched.body == fact.body
        assert fetched.confidence == fact.confidence

    async def test_put_malformed_input_hash_rejected(self, seeded_store: SQLiteFactStore):
        fact = _make_fact("fct_bad", input_hash="sha256:abc")
        with pytest.raises(HashingError):
            await seeded_store.put(fact)

    async def test_put_malformed_short_hash_rejected(self, seeded_store: SQLiteFactStore):
        fact = _make_fact("fct_short", input_hash="blake3:abc")
        with pytest.raises(HashingError):
            await seeded_store.put(fact)

    async def test_put_idempotent_last_write_wins(self, seeded_store: SQLiteFactStore):
        f1 = _make_fact("fct_same", body={"v": 1})
        f2 = _make_fact("fct_same", body={"v": 2})
        await seeded_store.put(f1)
        await seeded_store.put(f2)
        assert len(await seeded_store.all_ids()) == 1
        fetched = await seeded_store.get("fct_same")
        assert fetched.body == {"v": 2}


@pytest.mark.asyncio
class TestQuery:
    async def test_query_by_generation_method(self, seeded_store: SQLiteFactStore):
        await seeded_store.put(_make_fact("fct_a", generation_method="extract_facts_from_event"))
        await seeded_store.put(_make_fact("fct_b", generation_method="splice_section"))
        results = await seeded_store.query({"generation_method": "extract_facts_from_event"})
        assert [f.fact_id for f in results] == ["fct_a"]

    async def test_query_by_source_event_id(self, db_path, seeded_store: SQLiteFactStore):
        log = SQLiteEventLog(db_path)
        seed_event = await log.get("evt_source")
        evt_other = Event(
            event_id="evt_other",
            kind="observation",
            body={"text": "other"},
            ts="2026-05-12T00:00:00Z",
            hash=_compute_event_hash({"text": "other"}, seed_event.hash),
            prev_hash=seed_event.hash,
        )
        await log.append(evt_other)
        await seeded_store.put(_make_fact("fct_a", source_event_id="evt_source"))
        await seeded_store.put(_make_fact("fct_b", source_event_id="evt_other"))
        results = await seeded_store.query({"source_event_id": "evt_source"})
        assert [f.fact_id for f in results] == ["fct_a"]

    async def test_query_by_rank(self, seeded_store: SQLiteFactStore):
        await seeded_store.put(_make_fact("fct_normal"))
        await seeded_store.put(
            _make_fact("fct_dep", confidence=ConfidenceStub(rank="deprecated"))
        )
        results = await seeded_store.query({"rank": "deprecated"})
        assert [f.fact_id for f in results] == ["fct_dep"]

    async def test_query_unknown_filter_key_raises(self, seeded_store: SQLiteFactStore):
        with pytest.raises(L1ValidationFailed):
            await seeded_store.query({"nonexistent_key": "x"})


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_missing_is_silent_noop(self, seeded_store: SQLiteFactStore):
        await seeded_store.delete("nonexistent")

    async def test_delete_removes_fact(self, seeded_store: SQLiteFactStore):
        await seeded_store.put(_make_fact("fct_del"))
        await seeded_store.delete("fct_del")
        assert "fct_del" not in await seeded_store.all_ids()


@pytest.mark.asyncio
class TestAllIds:
    async def test_all_ids_sorted_no_duplicates(self, seeded_store: SQLiteFactStore):
        await seeded_store.put(_make_fact("fct_b"))
        await seeded_store.put(_make_fact("fct_a"))
        await seeded_store.put(_make_fact("fct_c"))
        ids = await seeded_store.all_ids()
        assert ids == ["fct_a", "fct_b", "fct_c"]


class TestProtocolConformance:
    def test_isinstance_factstore(self, db_path: Path):
        assert isinstance(SQLiteFactStore(db_path), FactStore)
