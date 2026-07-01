"""Antigravity (agy) session continuity storage.

Maps BoBClaw conversation ids to agy-generated conversation UUIDs in SQLite so a
later turn can resume the same agy conversation (``agy -p --conversation <uuid>``).

Unlike ``cc_sessions``, agy OWNS the uuid (we capture it after each turn), so this
is purely a resume sidecar. No JSONL transcript sidecar — agy already persists the
full transcript under its own ``brain/<uuid>/`` tree, which the LKS adapter can read
directly if needed.
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


async def _ensure_agy_sessions_table(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with connection(db_path, timeout=5) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS agy_sessions (
                conversation_id TEXT PRIMARY KEY,
                agy_uuid        TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def _lookup_agy_session(conversation_id: str) -> str | None:
    conversation_id = conversation_id.strip()
    if not conversation_id:
        return None

    db_path = _session_db_path()
    await _ensure_agy_sessions_table(db_path)
    async with connection(db_path, timeout=5) as db:
        row = await db.execute_fetchall(
            "SELECT agy_uuid FROM agy_sessions WHERE conversation_id = ?",
            (conversation_id,),
        )
    return row[0][0] if row else None


async def _record_agy_session(conversation_id: str, agy_uuid: str) -> None:
    """Persist the conversation_id -> agy uuid mapping for resume."""
    conversation_id = conversation_id.strip()
    agy_uuid = (agy_uuid or "").strip()
    if not conversation_id or not agy_uuid:
        return

    ts = datetime.now(timezone.utc).isoformat()
    db_path = _session_db_path()
    await _ensure_agy_sessions_table(db_path)
    async with connection(db_path, timeout=5) as db:
        await db.execute(
            """
            INSERT INTO agy_sessions (conversation_id, agy_uuid, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                agy_uuid   = excluded.agy_uuid,
                updated_at = excluded.updated_at
            """,
            (conversation_id, agy_uuid, ts),
        )
        await db.commit()
