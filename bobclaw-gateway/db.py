"""
BoBClaw Gateway — Database Layer

SQLite  : refresh_tokens, rate_limits (local gateway state)
Postgres: proxied queries to conversations/messages (shared pool with core)
"""
import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
import asyncpg

from config import config

logger = logging.getLogger(__name__)

# SQLite database path. Defaults relative to CWD (bobclaw-gateway/) but can be
# redirected by tests/CI so SQLite sidecars do not land in the repo tree.
_SQLITE_PATH = os.environ.get("GATEWAY_SQLITE_PATH", "gateway.db")
_SQLITE_JOURNAL_MODES = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}

# Module-level Postgres pool (lazy-initialized, cached)
_postgres_pool: Optional[asyncpg.Pool] = None


# ---------------------------------------------------------------------------
# SQLite — setup & refresh tokens
# ---------------------------------------------------------------------------


def _sqlite_path() -> str:
    return os.environ.get("GATEWAY_SQLITE_PATH", _SQLITE_PATH)


@asynccontextmanager
async def _sqlite_connection():
    sqlite_path = _sqlite_path()
    parent = Path(sqlite_path).expanduser().parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(sqlite_path) as db:
        journal_mode = os.environ.get("GATEWAY_SQLITE_JOURNAL_MODE", "").upper()
        if journal_mode:
            if journal_mode not in _SQLITE_JOURNAL_MODES:
                raise ValueError(f"Unsupported GATEWAY_SQLITE_JOURNAL_MODE={journal_mode!r}")
            await db.execute(f"PRAGMA journal_mode={journal_mode}")
        yield db


async def init_db() -> None:
    """Create SQLite tables if they do not already exist."""
    sqlite_path = _sqlite_path()
    async with _sqlite_connection() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                token             TEXT PRIMARY KEY,
                user_id           TEXT NOT NULL,
                expires_at        TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                chain_created_at  TEXT
            )
            """
        )
        # Best-effort migration for a pre-existing DB created before chain_created_at
        # (the absolute refresh-chain max age, B2). SQLite has no ADD COLUMN IF NOT
        # EXISTS; ignore the "duplicate column" error when it already exists.
        try:
            await db.execute("ALTER TABLE refresh_tokens ADD COLUMN chain_created_at TEXT")
        except Exception:  # noqa: BLE001 — column already present
            pass
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limits (
                key          TEXT PRIMARY KEY,
                count        INTEGER NOT NULL DEFAULT 0,
                window_start TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS totp_timesteps (
                user_id        TEXT PRIMARY KEY,
                last_timestep  INTEGER NOT NULL
            )
            """
        )
        # Per-IP failed-login tracking for /auth/login lockout (B1).
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                ip            TEXT PRIMARY KEY,
                fail_count    INTEGER NOT NULL DEFAULT 0,
                locked_until  TEXT
            )
            """
        )
        await db.commit()
    logger.info("SQLite database initialised at %s", sqlite_path)


async def create_refresh_token(user_id: str = "admin") -> str:
    """Persist and return a new cryptographically-random refresh token.

    Stamps ``chain_created_at`` = now: this is the origin of a fresh rotation chain.
    Rotation (:func:`validate_and_rotate_refresh_token`) preserves that origin so the
    chain has an ABSOLUTE maximum age (``REFRESH_TOKEN_ABSOLUTE_DAYS``) — a stolen
    token cannot be rotated indefinitely to extend its life past the per-token TTL.
    """
    token = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=config.REFRESH_TOKEN_DAYS)

    async with _sqlite_connection() as db:
        await db.execute(
            "INSERT INTO refresh_tokens (token, user_id, expires_at, created_at, chain_created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (token, user_id, expires_at.isoformat(), now.isoformat(), now.isoformat()),
        )
        await db.commit()

    return token


async def validate_and_rotate_refresh_token(old_token: str) -> Optional[str]:
    """
    Validate *old_token*.  If valid and unexpired, delete it (rotation) and
    return a brand-new token.  Returns None if invalid or expired.
    """
    now = datetime.now(timezone.utc)

    async with _sqlite_connection() as db:
        async with db.execute(
            "SELECT user_id, expires_at, created_at, chain_created_at"
            " FROM refresh_tokens WHERE token = ?",
            (old_token,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        user_id, expires_at_str, created_at_str, chain_created_at_str = row
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        # Chain origin: fall back to created_at for rows migrated from before the
        # chain_created_at column existed.
        chain_str = chain_created_at_str or created_at_str
        chain_created = datetime.fromisoformat(chain_str)
        if chain_created.tzinfo is None:
            chain_created = chain_created.replace(tzinfo=timezone.utc)
        chain_deadline = chain_created + timedelta(days=config.REFRESH_TOKEN_ABSOLUTE_DAYS)

        # Reject if the per-token TTL expired OR the rotation chain has hit its
        # absolute maximum age (rotation cannot extend a chain past this).
        if now > expires_at or now > chain_deadline:
            await db.execute(
                "DELETE FROM refresh_tokens WHERE token = ?", (old_token,)
            )
            await db.commit()
            return None

        # Rotate: delete old, insert new — PRESERVING the chain origin.
        await db.execute(
            "DELETE FROM refresh_tokens WHERE token = ?", (old_token,)
        )

        new_token = secrets.token_urlsafe(48)
        new_expires = now + timedelta(days=config.REFRESH_TOKEN_DAYS)
        await db.execute(
            "INSERT INTO refresh_tokens (token, user_id, expires_at, created_at, chain_created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (new_token, user_id, new_expires.isoformat(), now.isoformat(), chain_created.isoformat()),
        )
        await db.commit()

    return new_token


async def invalidate_refresh_token(token: str) -> None:
    """Remove a refresh token from the store (logout)."""
    async with _sqlite_connection() as db:
        await db.execute(
            "DELETE FROM refresh_tokens WHERE token = ?", (token,)
        )
        await db.commit()


async def revoke_all_refresh_tokens(user_id: str = "admin") -> int:
    """Delete EVERY refresh token for *user_id* (revoke-all). Returns the count removed.

    Because refresh tokens are opaque server-side rows, this is an immediate,
    complete revocation — every outstanding session for the user is invalidated.
    """
    async with _sqlite_connection() as db:
        cursor = await db.execute(
            "DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,)
        )
        await db.commit()
        return cursor.rowcount


# ── Per-IP failed-login lockout (B1) ─────────────────────────────────


async def check_login_locked(ip: str) -> Optional[int]:
    """If *ip* is currently locked out of /auth/login, return seconds remaining; else None."""
    now = datetime.now(timezone.utc)
    async with _sqlite_connection() as db:
        async with db.execute(
            "SELECT locked_until FROM login_attempts WHERE ip = ?", (ip,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return None
    locked_until = datetime.fromisoformat(row[0])
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if now >= locked_until:
        return None
    return int((locked_until - now).total_seconds()) + 1


async def record_failed_login(
    ip: str, *, threshold: int, base_seconds: int, max_seconds: int
) -> None:
    """Increment the failure counter for *ip*; apply exponential backoff past *threshold*."""
    now = datetime.now(timezone.utc)
    async with _sqlite_connection() as db:
        async with db.execute(
            "SELECT fail_count FROM login_attempts WHERE ip = ?", (ip,)
        ) as cur:
            row = await cur.fetchone()
        fail_count = (row[0] if row else 0) + 1
        locked_until = None
        if fail_count >= threshold:
            backoff = min(max_seconds, base_seconds * (2 ** (fail_count - threshold)))
            locked_until = (now + timedelta(seconds=backoff)).isoformat()
        await db.execute(
            "INSERT INTO login_attempts (ip, fail_count, locked_until) VALUES (?, ?, ?)"
            " ON CONFLICT(ip) DO UPDATE SET"
            " fail_count = excluded.fail_count, locked_until = excluded.locked_until",
            (ip, fail_count, locked_until),
        )
        await db.commit()


async def clear_login_attempts(ip: str) -> None:
    """Reset the failure counter for *ip* (successful login)."""
    async with _sqlite_connection() as db:
        await db.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
        await db.commit()


# ── TOTP replay protection ───────────────────────────────────────────


async def check_totp_replay(user_id: str, timestep: int) -> bool:
    """Return True if *timestep* is a replay (<= last accepted for *user_id*).

    Uses the shared SQLite sidecar so the replay persists across gateway
    restarts.  Returns False on the first login for a user.
    """
    async with _sqlite_connection() as db:
        async with db.execute(
            "SELECT last_timestep FROM totp_timesteps WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return False
    return timestep <= row[0]


async def store_totp_timestep(user_id: str, timestep: int) -> None:
    """Persist the last accepted TOTP timestep for *user_id*."""
    async with _sqlite_connection() as db:
        await db.execute(
            "INSERT INTO totp_timesteps (user_id, last_timestep) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET last_timestep = excluded.last_timestep",
            (user_id, timestep),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Postgres — shared pool with core
# ---------------------------------------------------------------------------


async def get_postgres_pool() -> asyncpg.Pool:
    """
    Lazily initialise and return the asyncpg connection pool.
    Uses min_size=0 so the pool starts without establishing any connections,
    which allows the gateway to start even when Postgres is temporarily down.
    """
    global _postgres_pool
    if _postgres_pool is None:
        _postgres_pool = await asyncpg.create_pool(
            config.POSTGRES_URL,
            min_size=0,
            max_size=10,
        )
    return _postgres_pool


async def close_postgres_pool() -> None:
    """Close the asyncpg pool if it is open."""
    global _postgres_pool
    if _postgres_pool is not None:
        await _postgres_pool.close()
        _postgres_pool = None
        logger.info("Postgres pool closed")
