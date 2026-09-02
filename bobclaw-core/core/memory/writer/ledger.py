"""Durable completion and replay ledgers for the W3 writer."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.memory._db import connection


_DDL = """
CREATE TABLE IF NOT EXISTS memory_writer_completions (
    source_content_hash TEXT NOT NULL,
    task_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    task_name TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    claim_token TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    chunk_count INTEGER NOT NULL DEFAULT 0,
    chunk_ids_json TEXT NOT NULL DEFAULT '[]',
    attempt_count INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (source_content_hash, task_version, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_memory_writer_completion_event
    ON memory_writer_completions(source_event_id, task_name, status);
CREATE TABLE IF NOT EXISTS memory_writer_checkpoints (
    task_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    processed_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_version, prompt_version),
    FOREIGN KEY (last_event_id) REFERENCES memory_events(event_id)
);
"""


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    status: str
    source_event_id: str | None = None
    claim_token: str | None = None


@dataclass(frozen=True)
class Checkpoint:
    last_event_id: str
    processed_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CompletionLedger:
    """SQLite-backed task claims keyed by the ratified three-part identity."""

    def __init__(self, db_path: Path, *, stale_after_seconds: int = 900) -> None:
        self.db_path = Path(db_path)
        self.stale_after = timedelta(seconds=stale_after_seconds)

    async def initialize(self) -> None:
        async with connection(self.db_path) as db:
            await db.executescript(_DDL)
            cursor = await db.execute("PRAGMA table_info(memory_writer_completions)")
            columns = {row[1] for row in await cursor.fetchall()}
            if "claim_token" not in columns:
                # Forward-compatible with databases initialized by an early W3
                # candidate before claim ownership was made explicit.
                await db.execute(
                    "ALTER TABLE memory_writer_completions "
                    "ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"
                )
            await db.commit()

    async def claim(
        self,
        *,
        source_content_hash: str,
        task_name: str,
        task_version: str,
        prompt_version: str,
        source_event_id: str,
    ) -> ClaimResult:
        """Claim work, reclaiming FAILED or stale RUNNING rows.

        A concurrent process observing a fresh RUNNING row returns ``busy``.
        Deterministic point ids make stale-claim replay safe after a crash.
        """
        now = _now()
        claim_token = uuid.uuid4().hex
        async with connection(self.db_path, timeout=5) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT status, source_event_id, updated_at FROM memory_writer_completions "
                "WHERE source_content_hash=? AND task_version=? AND prompt_version=?",
                (source_content_hash, task_version, prompt_version),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    "INSERT INTO memory_writer_completions "
                    "(source_content_hash, task_version, prompt_version, task_name, "
                    "source_event_id, claim_token, status, started_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)",
                    (
                        source_content_hash, task_version, prompt_version,
                        task_name, source_event_id, claim_token, now, now,
                    ),
                )
                await db.commit()
                return ClaimResult(True, "claimed", source_event_id, claim_token)

            status, prior_event_id, updated_at = row
            if status == "COMPLETED":
                await db.commit()
                return ClaimResult(False, "completed", prior_event_id)

            stale = False
            if status == "RUNNING":
                try:
                    stale = datetime.fromisoformat(updated_at) <= (
                        datetime.now(timezone.utc) - self.stale_after
                    )
                except (TypeError, ValueError):
                    stale = True
            if status == "RUNNING" and not stale:
                await db.commit()
                return ClaimResult(False, "busy", prior_event_id)

            await db.execute(
                "UPDATE memory_writer_completions SET task_name=?, source_event_id=?, claim_token=?, "
                "status='RUNNING', chunk_count=0, chunk_ids_json='[]', "
                "attempt_count=attempt_count+1, error=NULL, started_at=?, updated_at=?, "
                "completed_at=NULL WHERE source_content_hash=? AND task_version=? "
                "AND prompt_version=?",
                (
                    task_name, source_event_id, claim_token, now, now,
                    source_content_hash, task_version, prompt_version,
                ),
            )
            await db.commit()
            return ClaimResult(True, "reclaimed", source_event_id, claim_token)

    async def complete(
        self,
        *,
        source_content_hash: str,
        task_version: str,
        prompt_version: str,
        claim_token: str,
        chunk_ids: list[str],
    ) -> None:
        now = _now()
        async with connection(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE memory_writer_completions SET status='COMPLETED', "
                "chunk_count=?, chunk_ids_json=?, error=NULL, updated_at=?, completed_at=? "
                "WHERE source_content_hash=? AND task_version=? AND prompt_version=? "
                "AND status='RUNNING' AND claim_token=?",
                (
                    len(chunk_ids), json.dumps(chunk_ids), now, now,
                    source_content_hash, task_version, prompt_version, claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("writer completion lost its RUNNING claim")
            await db.commit()

    async def fail(
        self,
        *,
        source_content_hash: str,
        task_version: str,
        prompt_version: str,
        claim_token: str,
        error: str,
    ) -> None:
        async with connection(self.db_path) as db:
            await db.execute(
                "UPDATE memory_writer_completions SET status='FAILED', error=?, updated_at=? "
                "WHERE source_content_hash=? AND task_version=? AND prompt_version=? "
                "AND status='RUNNING' AND claim_token=?",
                (
                    error[:2000], _now(), source_content_hash, task_version,
                    prompt_version, claim_token,
                ),
            )
            await db.commit()

    async def get_checkpoint(
        self, task_version: str, prompt_version: str
    ) -> Checkpoint | None:
        async with connection(self.db_path) as db:
            cursor = await db.execute(
                "SELECT last_event_id, processed_count FROM memory_writer_checkpoints "
                "WHERE task_version=? AND prompt_version=?",
                (task_version, prompt_version),
            )
            row = await cursor.fetchone()
        return Checkpoint(row[0], row[1]) if row else None

    async def advance_checkpoint(
        self,
        *,
        task_version: str,
        prompt_version: str,
        last_event_id: str,
        processed_count: int,
    ) -> None:
        async with connection(self.db_path) as db:
            await db.execute(
                "INSERT INTO memory_writer_checkpoints "
                "(task_version, prompt_version, last_event_id, processed_count, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(task_version, prompt_version) "
                "DO UPDATE SET last_event_id=excluded.last_event_id, "
                "processed_count=excluded.processed_count, updated_at=excluded.updated_at",
                (task_version, prompt_version, last_event_id, processed_count, _now()),
            )
            await db.commit()

    async def count_completed(
        self, *, task_version: str | None = None, prompt_version: str | None = None
    ) -> int:
        where = ["status='COMPLETED'"]
        params: list[str] = []
        if task_version is not None:
            where.append("task_version=?")
            params.append(task_version)
        if prompt_version is not None:
            where.append("prompt_version=?")
            params.append(prompt_version)
        async with connection(self.db_path) as db:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM memory_writer_completions WHERE {' AND '.join(where)}",
                params,
            )
            row = await cursor.fetchone()
        return int(row[0])

    async def completed_chunk_count(
        self, *, task_version: str, prompt_version: str
    ) -> int:
        async with connection(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(SUM(chunk_count), 0) FROM memory_writer_completions "
                "WHERE status='COMPLETED' AND task_version=? AND prompt_version=?",
                (task_version, prompt_version),
            )
            row = await cursor.fetchone()
        return int(row[0])
