"""
BoBClaw Core — Database layer

SQLite (aiosqlite) for hot-path caching (rate limits, JWT refresh tokens,
agent execution cache).  Postgres (asyncpg) for all persistent application
data (conversations, messages, builds, faces, langgraph checkpoints).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import asyncpg

from core.config import config

# ─── Module-level singletons ──────────────────────────────────────────────────
_sqlite_db: Optional[aiosqlite.Connection] = None
_pg_pool: Optional[asyncpg.Pool] = None

SQLITE_PATH = "bobclaw_cache.db"

# Canonical schema file lives at the repo root and is also consumed by
# docker-compose's postgres init (see docker-compose.yml).  This service reads
# the same file so that all services share exactly one source of truth for the
# Postgres schema (B7 — schema-drift mitigation).
_INIT_SQL_PATH = Path(__file__).resolve().parent.parent.parent / "init.sql"


# ─── SQLite — hot-path cache ──────────────────────────────────────────────────

async def init_sqlite(path: str = SQLITE_PATH) -> aiosqlite.Connection:
    """Open (or create) the local SQLite cache and migrate the schema."""
    global _sqlite_db

    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row

    await conn.executescript("""
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token_id   TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            token      TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked    INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS rate_limits (
            key          TEXT PRIMARY KEY,
            count        INTEGER DEFAULT 0,
            window_start TEXT NOT NULL,
            updated_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS execution_cache (
            cache_key  TEXT PRIMARY KEY,
            result     TEXT NOT NULL,
            model      TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT
        );
    """)
    await conn.commit()
    _sqlite_db = conn
    return conn


def get_sqlite() -> aiosqlite.Connection:
    if _sqlite_db is None:
        raise RuntimeError("SQLite not initialised — call init_sqlite() first")
    return _sqlite_db


# ─── Postgres schema ───────────────────────────────────────────────────────────


def _read_init_sql() -> str:
    """Return the canonical init.sql schema text.

    The repo-root ``init.sql`` is the single source of truth for the Postgres
    schema; docker-compose runs it on first container start and
    :func:`init_postgres` re-runs it (idempotent via ``CREATE TABLE IF NOT
    EXISTS``) so a service starting before postgres is volume-initialised
    still gets a working schema.
    """
    try:
        return _INIT_SQL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"init.sql not found at {_INIT_SQL_PATH}. "
            "This file is the canonical Postgres schema; it must be present "
            "alongside docker-compose.yml."
        ) from exc


async def init_postgres() -> asyncpg.Pool:
    """Create the asyncpg connection pool and apply the canonical schema."""
    global _pg_pool

    pool = await asyncpg.create_pool(config.POSTGRES_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(_read_init_sql())
    _pg_pool = pool
    return pool


def get_pool() -> asyncpg.Pool:
    if _pg_pool is None:
        raise RuntimeError("Postgres pool not initialised — call init_postgres() first")
    return _pg_pool


# ─── Conversation helpers ──────────────────────────────────────────────────────

async def create_conversation(
    user_id: str = "admin",
    title: Optional[str] = None,
    face_id: Optional[str] = None,
    model_preference: Optional[str] = None,
    backend_preference: Optional[str] = None,
) -> dict[str, Any]:
    pool = get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO conversations (user_id, title, face_id, model_preference, backend_preference)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        user_id,
        title,
        face_id,
        model_preference,
        backend_preference,
    )
    return dict(row)


async def archive_conversation(conv_id: str, user_id: str = "admin") -> bool:
    pool = get_pool()
    result = await pool.execute(
        "UPDATE conversations SET is_archived = TRUE, updated_at = NOW() WHERE id = $1 AND user_id = $2",
        uuid.UUID(conv_id),
        user_id,
    )
    return result == "UPDATE 1"


async def list_conversations(
    user_id: str = "admin",
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT * FROM conversations
        WHERE is_archived = FALSE AND user_id = $3
        ORDER BY updated_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
        user_id,
    )
    return [dict(r) for r in rows]


# ─── Message helpers ────────────────────────────────────────────────────────────

async def save_message(
    conversation_id: str,
    role: str,
    content: str,
    model_used: Optional[str] = None,
    backend: Optional[str] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cost_usd: Optional[float] = None,
    elapsed_ms: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> dict[str, Any]:
    pool = get_pool()
    conv_uuid = uuid.UUID(conversation_id)
    row = await pool.fetchrow(
        """
        INSERT INTO messages (
            conversation_id, role, content, model_used, backend,
            tokens_in, tokens_out, cost_usd, elapsed_ms, metadata
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        RETURNING *
        """,
        conv_uuid,
        role,
        content,
        model_used,
        backend,
        tokens_in,
        tokens_out,
        cost_usd,
        elapsed_ms,
        json.dumps(metadata) if metadata else None,
    )
    await pool.execute(
        "UPDATE conversations SET updated_at = NOW() WHERE id = $1",
        conv_uuid,
    )
    return dict(row)


async def get_conversation_messages(
    conv_id: str,
    limit: int = 50,
    before_cursor: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return messages in chronological order (oldest-first),
    optionally paged backwards via a created_at before_cursor."""
    pool = get_pool()
    conv_uuid = uuid.UUID(conv_id)

    if before_cursor:
        rows = await pool.fetch(
            """
            SELECT * FROM messages
            WHERE conversation_id = $1 AND created_at < $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            conv_uuid,
            before_cursor,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT * FROM messages
            WHERE conversation_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            conv_uuid,
            limit,
        )
    # Reverse so callers receive oldest → newest
    return [dict(r) for r in reversed(rows)]
