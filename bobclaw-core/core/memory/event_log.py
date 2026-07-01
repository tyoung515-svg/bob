from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from core.memory._db import connection
from core.memory._hashing import _compute_event_hash
from core.memory.exceptions import L0AppendFailed
from core.memory.models import Event


class SQLiteEventLog:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = asyncio.Lock()

    async def append(self, event: Event) -> str:
        expected_hash = _compute_event_hash(event.body, event.prev_hash)
        if event.hash != expected_hash:
            raise L0AppendFailed(
                event.event_id,
                f"hash mismatch: expected {expected_hash}, got {event.hash}",
            )
        async with connection(self._db_path) as db:
            cursor = await db.execute(
                "SELECT MAX(insertion_order) FROM memory_events"
            )
            row = await cursor.fetchone()
            next_order = (row[0] if row[0] is not None else 0) + 1
            if next_order == 1:
                if event.prev_hash is not None:
                    raise L0AppendFailed(
                        event.event_id,
                        "first event must have prev_hash=None",
                    )
            else:
                cursor = await db.execute(
                    "SELECT hash FROM memory_events WHERE insertion_order = ?",
                    (next_order - 1,),
                )
                prev = await cursor.fetchone()
                actual_prev_hash = prev[0] if prev else None
                if event.prev_hash != actual_prev_hash:
                    raise L0AppendFailed(
                        event.event_id,
                        f"prev_hash mismatch: expected {actual_prev_hash}, got {event.prev_hash}",
                    )
            await db.execute(
                "INSERT INTO memory_events (event_id, kind, body_json, ts, hash, prev_hash, insertion_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.kind,
                    json.dumps(event.body, sort_keys=True),
                    event.ts,
                    event.hash,
                    event.prev_hash,
                    next_order,
                ),
            )
            await db.commit()
        return event.event_id

    async def atomic_append(self, body: dict) -> Event:
        async with self._lock:
            async with connection(self._db_path, timeout=5) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT MAX(insertion_order) FROM memory_events"
                )
                row = await cursor.fetchone()
                last_order = row[0]

                if last_order is None:
                    prev_hash = None
                    next_order = 1
                else:
                    cursor = await db.execute(
                        "SELECT hash FROM memory_events WHERE insertion_order = ?",
                        (last_order,),
                    )
                    prev_row = await cursor.fetchone()
                    prev_hash = prev_row[0]
                    next_order = last_order + 1

                event_hash = _compute_event_hash(body, prev_hash)
                event_id = uuid.uuid4().hex
                ts = datetime.now(timezone.utc).isoformat()
                kind = "agent_turn"

                await db.execute(
                    "INSERT INTO memory_events (event_id, kind, body_json, ts, hash, prev_hash, insertion_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        kind,
                        json.dumps(body, sort_keys=True),
                        ts,
                        event_hash,
                        prev_hash,
                        next_order,
                    ),
                )
                await db.commit()

        return Event(
            event_id=event_id,
            kind=kind,
            body=body,
            ts=ts,
            hash=event_hash,
            prev_hash=prev_hash,
        )

    async def get(self, event_id: str) -> Event:
        async with connection(self._db_path) as db:
            cursor = await db.execute(
                "SELECT event_id, kind, body_json, ts, hash, prev_hash "
                "FROM memory_events WHERE event_id = ?",
                (event_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise L0AppendFailed(event_id, "event not found")
        return Event(
            event_id=row[0],
            kind=row[1],
            body=json.loads(row[2]),
            ts=row[3],
            hash=row[4],
            prev_hash=row[5],
        )

    async def replay(
        self,
        since_event_id: str | None = None,
        with_upcasters: bool = False,
    ) -> AsyncIterator[Event]:
        from core.memory.schema_evolution import (
            CURRENT_SCHEMA_VERSION,
            SchemaEvolutionError,
            upgrade_body_to_latest,
        )

        async with connection(self._db_path) as db:
            if since_event_id is not None:
                cursor = await db.execute(
                    "SELECT insertion_order FROM memory_events WHERE event_id = ?",
                    (since_event_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise L0AppendFailed(
                        since_event_id, "event not found for replay"
                    )
                cursor = await db.execute(
                    "SELECT event_id, kind, body_json, ts, hash, prev_hash "
                    "FROM memory_events WHERE insertion_order > ? "
                    "ORDER BY insertion_order",
                    (row[0],),
                )
            else:
                cursor = await db.execute(
                    "SELECT event_id, kind, body_json, ts, hash, prev_hash "
                    "FROM memory_events ORDER BY insertion_order"
                )
            async for row in cursor:
                event = Event(
                    event_id=row[0],
                    kind=row[1],
                    body=json.loads(row[2]),
                    ts=row[3],
                    hash=row[4],
                    prev_hash=row[5],
                )
                if with_upcasters:
                    sv = event.body.get("_schema_version")
                    if sv is not None and sv != CURRENT_SCHEMA_VERSION:
                        try:
                            new_body = upgrade_body_to_latest(
                                event.kind, event.body
                            )
                            event = Event(
                                event_id=event.event_id,
                                kind=event.kind,
                                body=new_body,
                                ts=event.ts,
                                hash=event.hash,
                                prev_hash=event.prev_hash,
                            )
                        except SchemaEvolutionError:
                            pass
                yield event
