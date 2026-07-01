from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.exceptions import SchemaEvolutionError
from core.memory.schema_evolution import (
    CURRENT_SCHEMA_VERSION,
    _UPCASTERS,
    apply_upcaster_chain,
    get_upcaster_chain,
    register_upcaster,
    upgrade_body_to_latest,
)


def _remove_test_upcasters():
    keys_to_remove = [
        k for k in _UPCASTERS if k[0] not in (
            "*section_split*", "*section_merge*", "*type_change*",
        )
    ]
    for k in keys_to_remove:
        _UPCASTERS.pop(k, None)


@pytest.fixture(autouse=True)
def _clean_test_upcasters():
    _remove_test_upcasters()
    yield
    _remove_test_upcasters()


def test_register_and_lookup():

    @register_upcaster(generation_method="test_gm", from_version="1.0", to_version="2.0")
    def _u(old: dict) -> dict:
        old["upgraded"] = True
        return old

    chain = get_upcaster_chain("test_gm", "1.0", "2.0")
    assert len(chain) == 1


def test_apply_upcaster():

    @register_upcaster(generation_method="test_gm", from_version="1.0", to_version="2.0")
    def _u(old: dict) -> dict:
        old["version"] = "2.0"
        old.pop("_schema_version", None)
        return old

    chain = get_upcaster_chain("test_gm", "1.0", "2.0")
    result = apply_upcaster_chain(chain, {"_schema_version": "1.0", "data": "x"})
    assert result == {"data": "x", "version": "2.0"}


def test_lookup_no_chain_raises():
    with pytest.raises(SchemaEvolutionError):
        get_upcaster_chain("unknown_gm", "1.0", "2.0")


def test_stub_registrations_raise_on_apply():
    for method in ("*section_split*", "*section_merge*", "*type_change*"):
        chain = get_upcaster_chain(method, "1.0", "2.0")
        assert len(chain) == 1
        with pytest.raises(SchemaEvolutionError) as exc:
            apply_upcaster_chain(chain, {"_schema_version": "1.0"})
        assert "Phase 2 deferred" in str(exc.value)


def test_chained_upcasters():

    @register_upcaster(generation_method="chain_gm", from_version="1.0", to_version="1.1")
    def _v1_to_v11(old: dict) -> dict:
        old["step"] = "v1.1"
        return old

    @register_upcaster(generation_method="chain_gm", from_version="1.1", to_version="2.0")
    def _v11_to_v2(old: dict) -> dict:
        old["step"] = "v2.0"
        return old

    chain = get_upcaster_chain("chain_gm", "1.0", "2.0")
    assert len(chain) == 2

    result = apply_upcaster_chain(chain, {"_schema_version": "1.0"})
    assert result["step"] == "v2.0"


def test_cyclic_chain_detected():

    @register_upcaster(generation_method="cyclic_gm", from_version="1.0", to_version="2.0")
    def _v1_to_v2(old: dict) -> dict:
        return old

    @register_upcaster(generation_method="cyclic_gm", from_version="2.0", to_version="1.0")
    def _v2_to_v1(old: dict) -> dict:
        return old

    # Request v1→v3, but the only path is v1→v2→v1 (cycle)
    with pytest.raises(SchemaEvolutionError) as exc:
        get_upcaster_chain("cyclic_gm", "1.0", "3.0")
    assert "cyclic" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_event_log_replay_with_upcasters(tmp_path):
    from core.memory._db import init_schema
    from core.memory._hashing import _compute_event_hash
    from core.memory.event_log import SQLiteEventLog
    from core.memory.models import Event

    db_path = tmp_path / "test.db"
    await init_schema(db_path)

    @register_upcaster(generation_method="test_kind", from_version="1.0", to_version="2.0")
    def _u(old: dict) -> dict:
        old["upgraded"] = True
        return old

    body = {"_schema_version": "1.0", "val": 1}
    h = _compute_event_hash(body, None)

    log = SQLiteEventLog(db_path)
    await log.append(Event(
        event_id="e1", kind="test_kind",
        body=body,
        ts="2026-01-01T00:00:00", hash=h, prev_hash=None,
    ))

    events_default = [e async for e in log.replay()]
    assert events_default[0].body.get("upgraded") is None

    events_upcast = [e async for e in log.replay(with_upcasters=True)]
    assert events_upcast[0].body.get("upgraded") is True


def test_fact_store_put_with_upcaster(tmp_path):
    from core.memory._db import init_schema, connection
    from core.memory.fact_store import SQLiteFactStore
    from core.memory.models import ConfidenceStub, Fact
    import asyncio

    from core.memory._hashing import _compute_event_hash

    @register_upcaster(
        generation_method="test_fact_gm", from_version="1.0", to_version="2.0",
    )
    def _u(old: dict) -> dict:
        old["upgraded"] = True
        return old

    db_path = tmp_path / "test.db"
    asyncio.run(init_schema(db_path))

    e_hash = _compute_event_hash({"test": True}, None)
    async def _setup():
        async with connection(db_path) as db:
            await db.execute(
                "INSERT INTO memory_events (event_id, kind, body_json, ts, hash, prev_hash, insertion_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("e1", "test", '{}', "2026-01-01T00:00:00", e_hash, None, 1),
            )
            await db.commit()
    asyncio.run(_setup())

    store = SQLiteFactStore(db_path)
    asyncio.run(store.put(Fact(
        fact_id="f1", generation_method="test_fact_gm",
        body={"_schema_version": "1.0", "text": "x"},
        source_event_id="e1",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(),
        ts="2026-01-01T00:00:00",
    )))

    stored = asyncio.run(store.get("f1"))
    assert stored.body.get("upgraded") is True


def test_upgrade_body_to_latest_noop_when_already_current():
    body = {"_schema_version": CURRENT_SCHEMA_VERSION, "data": "x"}
    result = upgrade_body_to_latest("any_method", body)
    assert result is body
