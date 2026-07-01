"""Claude Code session continuity storage.

Maps BoBClaw conversation ids to Claude Code session ids in SQLite and writes
the small JSONL sidecar consumed by the LKS transcript adapter.
"""
from __future__ import annotations

import json
import os
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


def _sidecar_path() -> Path:
    return _resolve_core_path(config.CC_SIDECAR_PATH)


async def _ensure_cc_sessions_table(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with connection(db_path, timeout=5) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS cc_sessions (
                conversation_id TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def _lookup_cc_session(conversation_id: str) -> str | None:
    conversation_id = conversation_id.strip()
    if not conversation_id:
        return None

    db_path = _session_db_path()
    await _ensure_cc_sessions_table(db_path)
    async with connection(db_path, timeout=5) as db:
        row = await db.execute_fetchall(
            """
            SELECT session_id
            FROM cc_sessions
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        )
    return row[0][0] if row else None


def _append_sidecar_line(record: dict) -> None:
    path = _sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


async def _record_cc_session(
    conversation_id: str,
    session_id: str,
    project_dir: str,
) -> None:
    """Persist CC continuity and append the transcript-link sidecar line."""
    conversation_id = conversation_id.strip()
    session_id = session_id.strip()
    if not conversation_id or not session_id:
        return

    ts = datetime.now(timezone.utc).isoformat()
    db_path = _session_db_path()
    await _ensure_cc_sessions_table(db_path)
    async with connection(db_path, timeout=5) as db:
        await db.execute(
            """
            INSERT INTO cc_sessions (conversation_id, session_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                session_id = excluded.session_id,
                updated_at = excluded.updated_at
            """,
            (conversation_id, session_id, ts),
        )
        await db.commit()

    _append_sidecar_line(
        {
            "session_id": session_id,
            "conversation_id": conversation_id,
            "project_dir": project_dir,
            "ts": ts,
        }
    )
