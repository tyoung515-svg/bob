"""Core-side message index for context management (derived data, rebuildable from the gateway's history).

Why: when messages are left out of a prompt the model is told their ids and must be able to get the exact
text back. The gateway's Postgres rows stay the source of truth; this index keeps a content-hashed copy keyed
by the SAME message ids, ordered, with parent links (a list, so forks and joins fit later), so ``conv_expand``,
``conv_search`` and the compactor resolve citations without a gateway round trip. It has its own SQLite file
(WAL), like Bob's other per-concern stores, and never touches the hash-chained memory database.

Access rule: message reads are scoped to (conversation_id, user_id). Conversation discovery is scoped
to user_id within a verified lane-local index. A row for someone else is indistinguishable from none.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conv_messages (
    message_id      TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    created_at      TEXT,
    seq             INTEGER NOT NULL,
    parents_json    TEXT NOT NULL DEFAULT '[]',
    first_seen_ts   TEXT NOT NULL,
    PRIMARY KEY (conversation_id, user_id, message_id)
);
CREATE INDEX IF NOT EXISTS ix_conv_messages_conv ON conv_messages (conversation_id, user_id, seq);
CREATE TABLE IF NOT EXISTS conv_backfill_sources (
    user_id TEXT PRIMARY KEY, last_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conv_backfill_jobs (
    user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
    after_time TEXT, after_id TEXT, offset INTEGER NOT NULL DEFAULT 0,
    previous_id TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS compactions (
    compaction_id    TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    user_id          TEXT NOT NULL DEFAULT '',
    region_first_id  TEXT NOT NULL,
    region_last_id   TEXT NOT NULL,
    region_count     INTEGER NOT NULL,
    input_sha256     TEXT NOT NULL,
    status           TEXT NOT NULL,
    backend          TEXT,
    prompt_version   TEXT,
    attempts         INTEGER,
    claims_json      TEXT,
    narrative_json   TEXT,
    validation_json  TEXT,
    tokens_in_est    INTEGER,
    tokens_out_est   INTEGER,
    elapsed_ms       INTEGER,
    error            TEXT,
    created_ts       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_compactions_conv ON compactions (conversation_id, created_ts);
"""

_READY: set[str] = set()
_READY_LOCK = threading.Lock()


def db_path() -> str:
    """``CONTEXT_DB`` if set; else next to the lane's memory database (``MEMORY_SQLITE_PATH``), so every lane keeps
    its own copy beside its own data; else ``.memory/bobclaw_context.db`` under the working directory."""
    explicit = os.getenv("CONTEXT_DB", "").strip()
    if explicit:
        return explicit
    memory_db = os.getenv("MEMORY_SQLITE_PATH", "").strip()
    if memory_db:
        return os.path.join(os.path.dirname(memory_db), "bobclaw_context.db")
    return ".memory/bobclaw_context.db"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    with _READY_LOCK:
        if path not in _READY:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            _READY.add(path)
    return conn


# ── messages ────────────────────────────────────────────────────────────────

def record_history(conversation_id: Optional[str], user_id: Optional[str], history: list[dict]) -> int:
    """Index *history* (oldest-first ``{id, role, content, created_at}``); idempotent. Returns rows added.

    Messages without an id cannot be cited and are skipped. A new message's parent is the message before
    it in *history*, or the last stored message when it is the first of the batch.
    """
    if not conversation_id or not history:
        return 0
    items = [
        m for m in history
        if isinstance(m, dict) and m.get("id") and m.get("content") not in (None, "")
    ]
    if not items:
        return 0
    owner = user_id or ""
    with closing(_connect()) as c:
        known = {r[0] for r in c.execute(
            "SELECT message_id FROM conv_messages WHERE conversation_id = ? AND user_id = ?", (conversation_id, owner))}
        if all(str(m["id"]) in known for m in items):
            return 0
        last = c.execute(
            "SELECT message_id, seq FROM conv_messages WHERE conversation_id = ? AND user_id = ? "
            "ORDER BY seq DESC LIMIT 1", (conversation_id, owner)).fetchone()
        next_seq = (last["seq"] + 1) if last else 0
        last_stored = last["message_id"] if last else None
        rows = []
        for i, m in enumerate(items):
            mid = str(m["id"])
            if mid in known:
                continue
            parent = str(items[i - 1]["id"]) if i > 0 else last_stored
            content = str(m["content"])
            rows.append((
                mid, conversation_id, owner, str(m.get("role") or "user"), content,
                sha256_text(content), m.get("created_at"), next_seq,
                json.dumps([parent] if parent else []), _now(),
            ))
            next_seq += 1
        c.executemany(
            "INSERT OR IGNORE INTO conv_messages (message_id, conversation_id, user_id, role, content, "
            "content_sha256, created_at, seq, parents_json, first_seen_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        c.commit()
        return len(rows)


def record_stored_history(conversation_id: str, user_id: str, history: list[dict],
                          offset: int, previous_id: str = '') -> None:
    """Replay canonical rows, repairing truncated copies, order and parent links.

    Absolute positions make page retries idempotent, including pages preceding
    a tail already indexed by prompt history. Empty stored rows retain their ids.
    The authenticated gateway handoff and owner-scoped backfill use this path.
    """
    rows = []
    for seq, message in enumerate(history, offset):
        content = message['content']
        rows.append((message['id'], conversation_id, user_id, message['role'], content,
                     sha256_text(content), message['created_at'], seq,
                     json.dumps([previous_id] if previous_id else []), _now()))
        previous_id = message['id']
    with closing(_connect()) as c:
        c.executemany('''INSERT INTO conv_messages
            (message_id, conversation_id, user_id, role, content, content_sha256,
             created_at, seq, parents_json, first_seen_ts) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(conversation_id,user_id,message_id) DO UPDATE SET
                role=excluded.role, content=excluded.content, content_sha256=excluded.content_sha256,
                created_at=excluded.created_at, seq=excluded.seq, parents_json=excluded.parents_json''', rows)
        c.commit()


def conversation_count(user_id: str, current_conversation: str) -> int:
    """Count this owner's earlier indexed conversations without creating storage."""
    if not os.path.isfile(db_path()):
        return 0
    with closing(_connect_readonly()) as c:
        return c.execute('''SELECT count(DISTINCT conversation_id) FROM conv_messages
            WHERE user_id=? AND conversation_id<>?''', (user_id, current_conversation)).fetchone()[0]


def list_messages(conversation_id: str, user_id: Optional[str]) -> list[dict]:
    """The conversation in order (oldest first), scoped to its owner."""
    with closing(_connect()) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM conv_messages WHERE conversation_id = ? AND user_id = ? ORDER BY seq",
            (conversation_id, user_id or ""))]


def message_window(conversation_id: str, user_id: str, offset: int, message_id: str = ""):
    """A bounded page in one owned indexed conversation, optionally near an id."""
    with closing(_connect_readonly()) as c:
        scope = (conversation_id, user_id)
        total = c.execute('SELECT count(*) FROM conv_messages WHERE conversation_id=? AND user_id=?', scope).fetchone()[0]
        if message_id:
            anchor = c.execute('SELECT seq FROM conv_messages WHERE conversation_id=? AND user_id=? AND message_id=?',
                               (*scope, message_id)).fetchone()
            if anchor is None:
                return [], total, offset
            before = c.execute('SELECT count(*) FROM conv_messages WHERE conversation_id=? AND user_id=? AND seq<?',
                               (*scope, anchor['seq'])).fetchone()[0]
            # One preceding message guarantees the anchor itself fits in the
            # first response even when both messages hit the per-message cap.
            offset = max(0, before - 1)
        rows = [dict(r) for r in c.execute('SELECT * FROM conv_messages WHERE conversation_id=? AND user_id=? ORDER BY seq LIMIT 20 OFFSET ?',
                                         (*scope, offset))]
        return rows, total, offset


def _connect_readonly():
    c = sqlite3.connect(Path(db_path()).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def has_conversation(conversation_id: str, user_id: str) -> bool:
    """Check local membership without loading messages or creating an index."""
    with closing(_connect_readonly()) as c:
        return c.execute('SELECT 1 FROM conv_messages WHERE conversation_id=? AND user_id=? LIMIT 1',
                         (conversation_id, user_id)).fetchone() is not None


def get_by_ids(conversation_id: str, user_id: Optional[str], ids: list[str]) -> tuple[list[dict], list[str]]:
    """Rows for *ids* in the requested order, plus the ids that could not be resolved for this caller."""
    found: dict[str, dict] = {}
    if ids:
        with closing(_connect()) as c:
            marks = ",".join("?" * len(ids))
            for r in c.execute(
                f"SELECT * FROM conv_messages WHERE conversation_id = ? AND user_id = ? "
                f"AND message_id IN ({marks})", (conversation_id, user_id or "", *ids)):
                found[r["message_id"]] = dict(r)
    ordered = [found[i] for i in ids if i in found]
    missing = [i for i in ids if i not in found]
    return ordered, missing


def resolve_range(conversation_id: str, user_id: Optional[str], first_id: str, last_id: str) -> Optional[list[dict]]:
    """Every message from *first_id* to *last_id* inclusive (either order); None if an end is unknown."""
    with closing(_connect()) as c:
        ends = [c.execute(
            "SELECT seq FROM conv_messages WHERE conversation_id = ? AND user_id = ? AND message_id = ?",
            (conversation_id, user_id or "", mid)).fetchone() for mid in (first_id, last_id)]
        if not all(ends):
            return None
        lo, hi = sorted(e["seq"] for e in ends)
        return [dict(r) for r in c.execute(
            "SELECT * FROM conv_messages WHERE conversation_id = ? AND user_id = ? AND seq BETWEEN ? AND ? "
            "ORDER BY seq", (conversation_id, user_id or "", lo, hi))]


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(conversation_id: str, user_id: Optional[str], query: str, limit: int = 8) -> list[dict]:
    """Messages containing ALL whitespace-separated terms (case-insensitive), newest first, with a snippet."""
    terms = [t for t in re.split(r"\s+", (query or "").strip()) if t][:6]
    if not terms:
        return []
    clauses = " AND ".join("lower(content) LIKE ? ESCAPE '\\'" for _ in terms)
    params = [conversation_id, user_id or "", *[f"%{_like_escape(t.lower())}%" for t in terms], max(1, min(limit, 50))]
    with closing(_connect()) as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM conv_messages WHERE conversation_id = ? AND user_id = ? AND {clauses} "
            f"ORDER BY seq DESC LIMIT ?", params)]
    for r in rows:
        lowered = r["content"].lower()
        at = min((lowered.find(t.lower()) for t in terms if t.lower() in lowered), default=0)
        start = max(0, at - 100)
        snippet = r["content"][start:start + 260].replace("\n", " ")
        r["snippet"] = ("..." if start else "") + snippet + ("..." if start + 260 < len(r["content"]) else "")
    return rows


def find_conversations(user_id: str, current_conversation: str, query: str, limit: int = 8) -> list[dict]:
    """One matching message per earlier indexed conversation, scoped to this DB and owner.

    The caller must first verify this index is inside its configured lane root.
    This is deliberately not a search of the shared gateway database.
    """
    terms = query.lower().split()[:6]
    if not user_id or not terms or not os.path.isfile(db_path()):
        return []
    clauses = " AND ".join("lower(content) LIKE ? ESCAPE '\\'" for _ in terms)
    params = [terms[0], user_id, current_conversation,
              *[f"%{_like_escape(t)}%" for t in terms], max(1, min(limit, 10))]
    path = Path(db_path()).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(path, uri=True, timeout=10)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            f"""WITH matches AS (
                SELECT conversation_id, message_id, created_at,
                    substr(content, max(1, instr(lower(content), ?) - 100), 300) AS snippet,
                    row_number() OVER (PARTITION BY conversation_id ORDER BY seq DESC) AS rank
                FROM conv_messages WHERE user_id = ? AND conversation_id <> ? AND {clauses}
            ) SELECT conversation_id, message_id, created_at, snippet FROM matches
              WHERE rank = 1 ORDER BY created_at DESC, conversation_id LIMIT ?""", params)]


# ── compactions (shadow results) ────────────────────────────────────────────

def get_compaction(compaction_id: str) -> Optional[dict]:
    with closing(_connect()) as c:
        row = c.execute("SELECT * FROM compactions WHERE compaction_id = ?", (compaction_id,)).fetchone()
        return dict(row) if row else None


def save_compaction(rec: dict) -> None:
    cols = ("compaction_id", "conversation_id", "user_id", "region_first_id", "region_last_id", "region_count",
            "input_sha256", "status", "backend", "prompt_version", "attempts", "claims_json", "narrative_json",
            "validation_json", "tokens_in_est", "tokens_out_est", "elapsed_ms", "error")
    values = [rec.get(k) for k in cols]
    with closing(_connect()) as c:
        c.execute(
            f"INSERT OR REPLACE INTO compactions ({','.join(cols)}, created_ts) VALUES ({','.join('?' * (len(cols) + 1))})",
            (*values, _now()))
        c.commit()


def list_compactions(conversation_id: str, user_id: Optional[str] = None) -> list[dict]:
    """Stored compactions of a conversation, oldest first. With *user_id* only that owner's rows are returned
    (a compaction made for someone else is indistinguishable from none)."""
    sql, params = "SELECT * FROM compactions WHERE conversation_id = ?", [conversation_id]
    if user_id is not None:
        sql, params = sql + " AND user_id = ?", [*params, user_id]
    with closing(_connect()) as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY created_ts", params)]
