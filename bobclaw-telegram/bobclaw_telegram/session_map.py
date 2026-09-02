"""bobclaw-telegram — telegram chat_id → BoB conversation id, persisted.

A small SQLite db under the package's own data dir
(``bobclaw-telegram/.data/sessions.db``, gitignored) — NOT the repo root.
Also persists the last-processed Telegram ``update_id`` so replays after a
restart are skipped (idempotency).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PACKAGE_DIR / ".data" / "sessions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id         INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_LAST_UPDATE_KEY = "last_update_id"


class SessionMap:
    """Sync SQLite store; PTB handlers call it from the event-loop thread."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: PTB may touch handlers from its own
        # dispatcher thread; an RLock serializes access instead (reentrant
        # because mark_processed reads the watermark under the same lock).
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── chat_id → conversation mapping ──────────────────────────────────

    def conversation_for(self, chat_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT conversation_id FROM chats WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return row[0] if row else None

    def ensure_conversation(self, chat_id: int, conversation_id: str) -> str:
        """Store *conversation_id* for *chat_id* if unset; return the stored id.

        First mapping wins (find-or-create stability): a concurrent creator's
        id is discarded, the existing one returned, so every turn for a chat
        converges on one BoB conversation.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO chats (chat_id, conversation_id) VALUES (?, ?)",
                (chat_id, conversation_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT conversation_id FROM chats WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return row[0]

    # ── update_id idempotency ───────────────────────────────────────────

    def last_update_id(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (_LAST_UPDATE_KEY,)
            ).fetchone()
        return int(row[0]) if row else 0

    def is_replay(self, update_id: int) -> bool:
        """True when *update_id* was already processed (restart replay)."""
        return update_id <= self.last_update_id()

    def mark_processed(self, update_id: int) -> None:
        """Persist *update_id* as last-processed (monotonic — never regresses)."""
        with self._lock:
            current = self.last_update_id()
            if update_id > current:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_LAST_UPDATE_KEY, str(update_id)),
                )
                self._conn.commit()
