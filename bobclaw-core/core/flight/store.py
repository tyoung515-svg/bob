"""BoBClaw — Flight registry (Lane 1a, slice 1).

A first-class **Flight** record: a named, budgeted, prioritized task-stream (a "block of
work"). Durable in its own tiny SQLite file (``config.FLIGHT_DB``), independent of
``MEMORY_ENABLED`` — mirrors ``SchedulerLedger`` (``core/scheduler.py``). Cross-process
safe: SQLite serializes writes (WAL); ``INSERT OR IGNORE`` makes ``ensure`` race-safe.

The Flight's ``flight_id`` IS the tag Layer 0 threads through the substrate; its
``budget_usd`` is enforced (``supervisor``) against the L0.4 Redis spend meter. Ambient
flights (``chat:*`` / ``ambient``) are NOT registered here — they are unbudgeted stray work;
only explicit named flights (blocks of work) get a record.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from core.memory._db import connection

# Flight lifecycle states.
STATUS_ACTIVE = "active"        # accepting/running work
STATUS_PAUSED = "paused"        # temporarily yielded (e.g. preempted / over budget)
STATUS_DONE = "done"            # completed
STATUS_CANCELLED = "cancelled"  # aborted

_VALID_STATUS = {STATUS_ACTIVE, STATUS_PAUSED, STATUS_DONE, STATUS_CANCELLED}

# Columns a caller may update after creation.
_MUTABLE = {"name", "project", "budget_usd", "priority", "status"}


@dataclass
class Flight:
    """One flight (a named budgeted task-stream). ``priority``: higher = more important
    (wins a contended provider slot). ``budget_usd``: None ⇒ unbudgeted (no USD ceiling)."""
    flight_id: str
    name: str
    project: Optional[str] = None
    budget_usd: Optional[float] = None
    priority: int = 0
    status: str = STATUS_ACTIVE
    created: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _row_to_flight(row) -> Flight:
    return Flight(
        flight_id=row[0], name=row[1], project=row[2],
        budget_usd=row[3], priority=row[4], status=row[5], created=row[6],
    )


class FlightStore:
    """SQLite-backed Flight registry. Async; all methods open a short-lived WAL
    connection (mirrors ``SchedulerLedger``)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    async def init(self) -> None:
        """Create the flights table (idempotent). Call once before use."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with connection(self._db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS flights ("
                "  flight_id  TEXT PRIMARY KEY,"
                "  name       TEXT NOT NULL,"
                "  project    TEXT,"
                "  budget_usd REAL,"
                "  priority   INTEGER NOT NULL DEFAULT 0,"
                "  status     TEXT NOT NULL DEFAULT 'active',"
                "  created    TEXT NOT NULL"
                ")"
            )
            await db.commit()

    async def create(
        self,
        flight_id: str,
        name: str,
        *,
        created: str,
        project: Optional[str] = None,
        budget_usd: Optional[float] = None,
        priority: int = 0,
        status: str = STATUS_ACTIVE,
    ) -> Flight:
        """Create a flight. Raises ``ValueError`` if ``flight_id`` already exists (use
        :meth:`ensure` for idempotent create) or on an invalid status. ``created`` is
        injected (no clock in the store — the caller stamps ISO time)."""
        if status not in _VALID_STATUS:
            raise ValueError(f"invalid flight status {status!r}")
        if not flight_id or not str(flight_id).strip():
            raise ValueError("flight_id must be non-empty")
        async with connection(self._db_path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO flights "
                "(flight_id, name, project, budget_usd, priority, status, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (flight_id, name, project, budget_usd, priority, status, created),
            )
            await db.commit()
            if cur.rowcount != 1:
                raise ValueError(f"flight {flight_id!r} already exists")
        return Flight(flight_id, name, project, budget_usd, priority, status, created)

    async def ensure(
        self,
        flight_id: str,
        name: str,
        *,
        created: str,
        project: Optional[str] = None,
        budget_usd: Optional[float] = None,
        priority: int = 0,
        status: str = STATUS_ACTIVE,
    ) -> Flight:
        """Idempotent create: insert if absent (race-safe via INSERT OR IGNORE), else
        return the existing flight unchanged."""
        if status not in _VALID_STATUS:
            raise ValueError(f"invalid flight status {status!r}")
        async with connection(self._db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO flights "
                "(flight_id, name, project, budget_usd, priority, status, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (flight_id, name, project, budget_usd, priority, status, created),
            )
            await db.commit()
        existing = await self.get(flight_id)
        assert existing is not None  # just inserted or already there
        return existing

    async def get(self, flight_id: str) -> Optional[Flight]:
        async with connection(self._db_path) as db:
            cur = await db.execute(
                "SELECT flight_id, name, project, budget_usd, priority, status, created "
                "FROM flights WHERE flight_id = ?",
                (flight_id,),
            )
            row = await cur.fetchone()
        return _row_to_flight(row) if row else None

    async def list(self, *, status: Optional[str] = None) -> list[Flight]:
        """All flights (newest first), optionally filtered by status."""
        async with connection(self._db_path) as db:
            if status is not None:
                cur = await db.execute(
                    "SELECT flight_id, name, project, budget_usd, priority, status, created "
                    "FROM flights WHERE status = ? ORDER BY created DESC",
                    (status,),
                )
            else:
                cur = await db.execute(
                    "SELECT flight_id, name, project, budget_usd, priority, status, created "
                    "FROM flights ORDER BY created DESC"
                )
            rows = await cur.fetchall()
        return [_row_to_flight(r) for r in rows]

    async def update(self, flight_id: str, **fields) -> Optional[Flight]:
        """Update mutable fields (name/project/budget_usd/priority/status). Unknown keys
        raise ``ValueError``; an invalid status raises. Returns the updated flight, or
        None if the flight does not exist (no row updated)."""
        bad = set(fields) - _MUTABLE
        if bad:
            raise ValueError(f"cannot update fields {sorted(bad)}")
        if "status" in fields and fields["status"] not in _VALID_STATUS:
            raise ValueError(f"invalid flight status {fields['status']!r}")
        if not fields:
            return await self.get(flight_id)
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [flight_id]
        async with connection(self._db_path) as db:
            cur = await db.execute(
                f"UPDATE flights SET {cols} WHERE flight_id = ?", vals
            )
            await db.commit()
            if cur.rowcount < 1:
                return None
        return await self.get(flight_id)

    async def set_status(self, flight_id: str, status: str) -> Optional[Flight]:
        return await self.update(flight_id, status=status)

    async def delete(self, flight_id: str) -> bool:
        async with connection(self._db_path) as db:
            cur = await db.execute("DELETE FROM flights WHERE flight_id = ?", (flight_id,))
            await db.commit()
            return bool(cur.rowcount)
