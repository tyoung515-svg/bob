from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import aiosqlite

from core.memory._db import init_schema
from core.memory._hashing import _compute_event_hash
from core.memory.event_log import SQLiteEventLog
from core.memory.models import Event

BOBCLAW_CORE_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    asyncio.run(init_schema(p))
    # Pre-set WAL mode so subprocess connections don't race on the PRAGMA
    async def _set_wal():
        async with aiosqlite.connect(str(p), timeout=5) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.commit()
    asyncio.run(_set_wal())
    return p


@pytest.mark.asyncio
async def test_atomic_append_first_event_has_null_prev_hash(db_path: Path):
    log = SQLiteEventLog(db_path)
    event = await log.atomic_append({"msg": "first"})
    assert event.prev_hash is None
    assert event.kind == "agent_turn"
    assert event.body == {"msg": "first"}

    # Verify via replay
    events = [e async for e in log.replay()]
    assert len(events) == 1
    assert events[0].prev_hash is None
    assert events[0].hash == event.hash


@pytest.mark.asyncio
async def test_atomic_append_sequential_chain_is_valid(db_path: Path):
    log = SQLiteEventLog(db_path)

    events: list[Event] = []
    for i in range(5):
        event = await log.atomic_append({"seq": i, "payload": f"msg-{i}"})
        events.append(event)

    assert len(events) == 5
    assert events[0].prev_hash is None
    for i in range(1, 5):
        assert events[i].prev_hash == events[i - 1].hash

    # Verify via replay
    replayed = [e async for e in log.replay()]
    assert len(replayed) == 5
    assert replayed[0].prev_hash is None
    for i in range(1, 5):
        assert replayed[i].prev_hash == replayed[i - 1].hash

    # Verify hash correctness for each event
    for ev in replayed:
        expected_hash = _compute_event_hash(ev.body, ev.prev_hash)
        assert ev.hash == expected_hash, f"hash mismatch for event {ev.event_id}"


@pytest.mark.asyncio
async def test_atomic_append_under_concurrent_calls(db_path: Path):
    log = SQLiteEventLog(db_path)
    n = 20

    async def append_one(i: int) -> Event:
        return await log.atomic_append({"idx": i, "data": f"concurrent-{i}"})

    results = await asyncio.gather(*[append_one(i) for i in range(n)])

    assert len(results) == n

    # Walk replay and verify chain integrity
    replayed = [e async for e in log.replay()]
    assert len(replayed) == n

    seen_indices = set()
    for ev in replayed:
        seen_indices.add(ev.body["idx"])
        expected_hash = _compute_event_hash(ev.body, ev.prev_hash)
        assert ev.hash == expected_hash, f"hash mismatch for idx={ev.body['idx']}"

    assert seen_indices == set(range(n))

    # Chain validity: each event's prev_hash matches the prior event's hash
    assert replayed[0].prev_hash is None
    for i in range(1, n):
        assert replayed[i].prev_hash == replayed[i - 1].hash


@pytest.mark.asyncio
async def test_atomic_append_returns_event_with_populated_fields(db_path: Path):
    log = SQLiteEventLog(db_path)
    body = {"msg": "field-test"}
    event = await log.atomic_append(body)

    assert isinstance(event.event_id, str) and len(event.event_id) > 0
    assert event.kind == "agent_turn"
    assert event.body == body
    assert isinstance(event.ts, str) and len(event.ts) > 0
    assert isinstance(event.hash, str) and event.hash.startswith("blake3:")
    assert event.prev_hash is None

    # Replay should yield an identical event
    replayed = [e async for e in log.replay()]
    assert len(replayed) == 1
    r = replayed[0]
    assert r.event_id == event.event_id
    assert r.kind == event.kind
    assert r.body == event.body
    assert r.ts == event.ts
    assert r.hash == event.hash
    assert r.prev_hash == event.prev_hash


@pytest.mark.asyncio
async def test_append_still_works_for_handcrafted_events(db_path: Path):
    log = SQLiteEventLog(db_path)

    # First event via atomic_append
    first = await log.atomic_append({"msg": "first"})

    # Second event via handcrafted Event + append (Wave 4 path)
    body_2 = {"msg": "handcrafted"}
    h2 = _compute_event_hash(body_2, first.hash)
    handcrafted = Event(
        event_id="handmade-001",
        kind="observation",
        body=body_2,
        ts="2026-05-18T00:00:00Z",
        hash=h2,
        prev_hash=first.hash,
    )
    returned_id = await log.append(handcrafted)
    assert returned_id == "handmade-001"

    # Both events present with valid chain
    replayed = [e async for e in log.replay()]
    assert len(replayed) == 2
    assert replayed[0].prev_hash is None
    assert replayed[1].prev_hash == replayed[0].hash


@pytest.mark.asyncio
async def test_atomic_append_under_multi_process_load(db_path: Path):
    """Two OS processes call atomic_append concurrently; chain stays valid.

    The asyncio.Lock alone can't serialize across processes — this test would
    have failed before BEGIN IMMEDIATE was added.
    """
    n_per_proc = 10

    worker_src = textwrap.dedent('''
        import asyncio
        import sys
        from pathlib import Path
        sys.path.insert(0, r"{src_dir}")
        from core.memory.event_log import SQLiteEventLog

        async def main():
            log = SQLiteEventLog(Path(r"{db}"))
            for i in range({n_per_proc}):
                await log.atomic_append({{"pid": {pid_token}, "idx": i}})

        asyncio.run(main())
    ''')

    procs = []
    for pid_token in ("A", "B"):
        src = worker_src.format(
            src_dir=str(BOBCLAW_CORE_ROOT),
            db=str(db_path),
            n_per_proc=n_per_proc,
            pid_token=repr(pid_token),
        )
        procs.append(subprocess.Popen([sys.executable, "-c", src]))

    for p in procs:
        rc = p.wait(timeout=30)
        assert rc == 0, f"worker {p.pid} exited with {rc}"

    log = SQLiteEventLog(db_path)
    replayed = [e async for e in log.replay()]
    assert len(replayed) == 2 * n_per_proc

    assert replayed[0].prev_hash is None
    for i in range(1, len(replayed)):
        assert replayed[i].prev_hash == replayed[i - 1].hash, \
            f"chain broken at index {i}"

    pids_seen = {r.body["pid"] for r in replayed}
    assert pids_seen == {"A", "B"}
