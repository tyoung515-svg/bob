"""Codex (codex_code) session continuity storage.

Maps BoBClaw conversation ids to codex-generated ``thread_id``s in SQLite so a
later turn can resume the same codex thread (``codex exec resume <thread_id>``).

Like agy, codex OWNS the thread_id (we capture it from the ``thread.started``
``--json`` event after each turn), so this is purely a resume sidecar. Mirrors
``agy_sessions`` / ``cc_sessions``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.config import config
from core.memory._db import connection

_CORE_ROOT = Path(__file__).resolve().parent.parent


def _resolve_core_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = _CORE_ROOT / path
    return path


def _session_db_path() -> Path:
    return _resolve_core_path(config.MEMORY_SQLITE_PATH)


async def _ensure_codex_sessions_table(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with connection(db_path, timeout=5) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS codex_sessions (
                conversation_id TEXT PRIMARY KEY,
                thread_id       TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def _lookup_codex_session(conversation_id: str) -> str | None:
    conversation_id = conversation_id.strip()
    if not conversation_id:
        return None

    db_path = _session_db_path()
    await _ensure_codex_sessions_table(db_path)
    async with connection(db_path, timeout=5) as db:
        row = await db.execute_fetchall(
            "SELECT thread_id FROM codex_sessions WHERE conversation_id = ?",
            (conversation_id,),
        )
    return row[0][0] if row else None


async def _record_codex_session(conversation_id: str, thread_id: str) -> None:
    """Persist the conversation_id -> codex thread_id mapping for resume."""
    conversation_id = conversation_id.strip()
    thread_id = (thread_id or "").strip()
    if not conversation_id or not thread_id:
        return

    ts = datetime.now(timezone.utc).isoformat()
    db_path = _session_db_path()
    await _ensure_codex_sessions_table(db_path)
    async with connection(db_path, timeout=5) as db:
        await db.execute(
            """
            INSERT INTO codex_sessions (conversation_id, thread_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                thread_id  = excluded.thread_id,
                updated_at = excluded.updated_at
            """,
            (conversation_id, thread_id, ts),
        )
        await db.commit()
