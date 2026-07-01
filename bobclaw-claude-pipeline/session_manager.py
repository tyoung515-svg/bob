"""
BoBClaw Claude Build Pipeline — Session Manager

Tracks in-memory build sessions and optionally persists completed/failed
sessions to Postgres via asyncpg.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class BuildStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BuildSession:
    id: str
    task: str
    model: str
    status: BuildStatus = BuildStatus.QUEUED
    messages: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, str]] = field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "model": self.model,
            "status": self.status.value,
            "messages": self.messages,
            "artifacts": self.artifacts,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
        }


class MaxConcurrentBuildsError(Exception):
    """Raised when the concurrent build limit would be exceeded."""


class SessionNotFoundError(KeyError):
    """Raised when a session_id is not found."""


class SessionManager:
    """
    Thread-safe (asyncio-safe) in-memory session store with optional Postgres
    persistence for terminal states (complete / failed / cancelled).
    """

    def __init__(
        self,
        max_concurrent_builds: int = 3,
        db_pool: Any = None,  # asyncpg.Pool | None
    ) -> None:
        self._sessions: dict[str, BuildSession] = {}
        self._lock = asyncio.Lock()
        self.max_concurrent_builds = max_concurrent_builds
        self._db_pool = db_pool

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_session(
        self, task: str, model: str | None = None
    ) -> BuildSession:
        """
        Create and enqueue a new build session.

        Raises MaxConcurrentBuildsError if the running + queued count already
        equals *max_concurrent_builds*.
        """
        from config import DEFAULT_MODEL  # local import to avoid circular issues

        chosen_model = model or DEFAULT_MODEL

        async with self._lock:
            active = self._count_active()
            if active >= self.max_concurrent_builds:
                raise MaxConcurrentBuildsError(
                    f"Concurrent build limit ({self.max_concurrent_builds}) reached. "
                    "Try again later."
                )

            session = BuildSession(
                id=str(uuid.uuid4()),
                task=task,
                model=chosen_model,
            )
            self._sessions[session.id] = session

        return session

    async def get_session(self, session_id: str) -> BuildSession:
        """Return the session or raise SessionNotFoundError."""
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session '{session_id}' not found.")
        return session

    async def list_sessions(
        self, status: BuildStatus | str | None = None
    ) -> list[BuildSession]:
        """Return all sessions, optionally filtered by status."""
        async with self._lock:
            sessions = list(self._sessions.values())

        if status is None:
            return sessions

        if isinstance(status, str):
            status = BuildStatus(status)

        return [s for s in sessions if s.status == status]

    async def cancel_session(self, session_id: str) -> bool:
        """
        Cancel a queued or running session.
        Returns True if cancelled, False if already in a terminal state.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(f"Session '{session_id}' not found.")

            terminal = {BuildStatus.COMPLETE, BuildStatus.FAILED, BuildStatus.CANCELLED}
            if session.status in terminal:
                return False

            session.status = BuildStatus.CANCELLED
            session.completed_at = datetime.now(timezone.utc)

        await self._persist(session)
        return True

    # ------------------------------------------------------------------
    # Internal state helpers (call with lock held or inside lock)
    # ------------------------------------------------------------------

    def _count_active(self) -> int:
        """Count sessions that are queued or running (without acquiring lock)."""
        active_statuses = {BuildStatus.QUEUED, BuildStatus.RUNNING}
        return sum(1 for s in self._sessions.values() if s.status in active_statuses)

    # ------------------------------------------------------------------
    # Lifecycle helpers called by the pipeline runner
    # ------------------------------------------------------------------

    async def mark_running(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions[session_id]
            session.status = BuildStatus.RUNNING
            session.started_at = datetime.now(timezone.utc)

    async def mark_complete(
        self, session_id: str, artifacts: list[dict[str, str]] | None = None
    ) -> None:
        async with self._lock:
            session = self._sessions[session_id]
            session.status = BuildStatus.COMPLETE
            session.completed_at = datetime.now(timezone.utc)
            if artifacts:
                session.artifacts = artifacts

        await self._persist(session)

    async def mark_failed(self, session_id: str, error: str) -> None:
        async with self._lock:
            session = self._sessions[session_id]
            session.status = BuildStatus.FAILED
            session.completed_at = datetime.now(timezone.utc)
            session.error = error

        await self._persist(session)

    async def append_message(
        self, session_id: str, message: dict[str, Any]
    ) -> None:
        async with self._lock:
            self._sessions[session_id].messages.append(message)

    # ------------------------------------------------------------------
    # Postgres persistence (no-op when db_pool is None)
    # ------------------------------------------------------------------

    async def _persist(self, session: BuildSession) -> None:
        if self._db_pool is None:
            return
        try:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO builds (id, task, model, status,
                        artifacts, started_at, completed_at, error)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
                    ON CONFLICT (id) DO UPDATE SET
                        status       = EXCLUDED.status,
                        artifacts    = EXCLUDED.artifacts,
                        started_at   = EXCLUDED.started_at,
                        completed_at = EXCLUDED.completed_at,
                        error        = EXCLUDED.error
                    """,
                    session.id,
                    session.task,
                    session.model,
                    session.status.value,
                    json.dumps(session.artifacts),
                    session.started_at,
                    session.completed_at,
                    session.error,
                )
        except Exception as exc:  # noqa: BLE001
            # Persistence failure must not crash the pipeline
            import logging
            logging.getLogger(__name__).error("Postgres persist failed: %s", exc)

    # ------------------------------------------------------------------
    # Postgres schema bootstrap (call once at startup)
    # ------------------------------------------------------------------

    async def ensure_schema(self) -> None:
        if self._db_pool is None:
            return
        async with self._db_pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS builds (
                    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    task         TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'queued',
                    model        TEXT,
                    face_id      TEXT,
                    artifacts    JSONB,
                    error        TEXT,
                    tokens_in    INTEGER,
                    tokens_out   INTEGER,
                    cost_usd     NUMERIC(10,6),
                    started_at   TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    created_at   TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
