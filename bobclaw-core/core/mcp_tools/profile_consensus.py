"""T1 flight-aware routing (Lane 1b) — cross-project consensus ledger.

The two-tier namespacing (``core.mcp_tools.profile_routing``) keeps a project's learned
routing scoped to ``proj:<project>:<job_shape>`` so one project never silently drives
another. A profile earns the ``global:<job_shape>`` tier only after it has CRYSTALLIZED in
``>= N`` DISTINCT projects — cross-project consensus, not one project's say-so.

This ledger records those per-project crystallizations exactly-once and answers "has this
job-shape crystallized in enough projects to promote globally?". It mirrors
``core/scheduler.py``'s ``SchedulerLedger`` (INSERT OR IGNORE on a composite PRIMARY KEY —
atomic at SQLite, correct even if two cores race the same (job_shape, project)).
"""
from __future__ import annotations

from pathlib import Path

from core.memory._db import connection


class ProfileConsensusLedger:
    """``profile_crystallizations(job_shape, project)`` — exactly-once per pair."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with connection(self._db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS profile_crystallizations ("
                "  job_shape TEXT NOT NULL,"
                "  project   TEXT NOT NULL,"
                "  at        TEXT NOT NULL,"
                "  PRIMARY KEY (job_shape, project)"
                ")"
            )
            await db.commit()

    async def record_crystallization(self, job_shape: str, project: str, at: str) -> bool:
        """Record that ``job_shape`` crystallized in ``project``. True iff this was a NEW
        (job_shape, project) pair (a repeat crystallization in the same project is a no-op —
        consensus counts DISTINCT projects, so a single project can't inflate the count)."""
        async with connection(self._db_path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO profile_crystallizations (job_shape, project, at) "
                "VALUES (?, ?, ?)",
                (job_shape, project, at),
            )
            await db.commit()
            return cur.rowcount == 1

    async def project_count(self, job_shape: str) -> int:
        """Distinct projects ``job_shape`` has crystallized in."""
        async with connection(self._db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM profile_crystallizations WHERE job_shape = ?",
                (job_shape,),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def should_promote_global(self, job_shape: str, threshold: int) -> bool:
        """True iff ``job_shape`` has crystallized in ``>= threshold`` distinct projects."""
        return await self.project_count(job_shape) >= max(1, threshold)


__all__ = ["ProfileConsensusLedger"]
