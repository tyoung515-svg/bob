"""P1 notebook-grounded surface — SQLite system-of-record (MS#4 · Slice 2).

SQLite holds the structured app objects (notebooks / sources / notes / jobs) + the
``surface_manifest`` (per-notebook centroid + crystallized digest + ingest bookkeeping). LKS
never holds structured app objects (SPEC §1); it is retrieval + provenance only. Deterministic
given an injected clock; the DB is the one I/O surface here.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS surface_notebooks(
    notebook_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    embedding_model TEXT,
    embedding_model_sha256 TEXT,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS surface_sources(
    source_id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    uri TEXT NOT NULL,
    kind TEXT NOT NULL,
    added_ts REAL NOT NULL,
    FOREIGN KEY(notebook_id) REFERENCES surface_notebooks(notebook_id)
);
CREATE TABLE IF NOT EXISTS surface_notes(
    note_id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_ts REAL NOT NULL,
    FOREIGN KEY(notebook_id) REFERENCES surface_notebooks(notebook_id)
);
CREATE TABLE IF NOT EXISTS surface_jobs(
    job_id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_ts REAL NOT NULL,
    FOREIGN KEY(notebook_id) REFERENCES surface_notebooks(notebook_id)
);
CREATE TABLE IF NOT EXISTS surface_manifest(
    notebook_id TEXT PRIMARY KEY,
    centroid BLOB,
    crystallized_digest TEXT,
    last_ingest_ts REAL,
    source_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(notebook_id) REFERENCES surface_notebooks(notebook_id)
);
"""


class SurfaceStoreError(RuntimeError):
    """A surface store constraint violation (unknown notebook, duplicate id, …)."""


class SurfaceStore:
    def __init__(self, db_path: str = ":memory:", *, now: Optional[Callable[[], float]] = None):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._now = now or time.time
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── notebooks ────────────────────────────────────────────────────────────
    def create_notebook(
        self, notebook_id: str, name: str, *,
        embedding_model: Optional[str] = None, embedding_model_sha256: Optional[str] = None,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO surface_notebooks(notebook_id, name, embedding_model, "
                "embedding_model_sha256, created_ts) VALUES(?,?,?,?,?)",
                (notebook_id, name, embedding_model, embedding_model_sha256, self._now()),
            )
            # seed an empty manifest row so get_manifest is defined right after creation
            self._conn.execute(
                "INSERT OR IGNORE INTO surface_manifest(notebook_id, source_count) VALUES(?,0)",
                (notebook_id,),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise SurfaceStoreError(f"notebook {notebook_id!r}: {exc}") from exc

    def get_notebook(self, notebook_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM surface_notebooks WHERE notebook_id=?", (notebook_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── sources ──────────────────────────────────────────────────────────────
    def add_source(self, notebook_id: str, source_id: str, uri: str, kind: str) -> None:
        if self.get_notebook(notebook_id) is None:
            raise SurfaceStoreError(f"unknown notebook {notebook_id!r}")
        try:
            self._conn.execute(
                "INSERT INTO surface_sources(source_id, notebook_id, uri, kind, added_ts) "
                "VALUES(?,?,?,?,?)",
                (source_id, notebook_id, uri, kind, self._now()),
            )
            self._refresh_source_count(notebook_id)
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise SurfaceStoreError(f"source {source_id!r}: {exc}") from exc

    def source_count(self, notebook_id: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM surface_sources WHERE notebook_id=?", (notebook_id,)
        ).fetchone()[0]

    def _refresh_source_count(self, notebook_id: str) -> None:
        self._conn.execute(
            "UPDATE surface_manifest SET source_count=("
            "SELECT COUNT(*) FROM surface_sources WHERE notebook_id=?) WHERE notebook_id=?",
            (notebook_id, notebook_id),
        )

    # ── notes (grounded text) ────────────────────────────────────────────────
    def add_note(self, notebook_id: str, note_id: str, body: str) -> None:
        if self.get_notebook(notebook_id) is None:
            raise SurfaceStoreError(f"unknown notebook {notebook_id!r}")
        try:
            self._conn.execute(
                "INSERT INTO surface_notes(note_id, notebook_id, body, created_ts) VALUES(?,?,?,?)",
                (note_id, notebook_id, body, self._now()),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise SurfaceStoreError(f"note {note_id!r}: {exc}") from exc

    def search_notes(self, notebook_id: str, query: str, *, k: int = 10) -> list[dict]:
        """Substring text search over THIS notebook's notes only (SQLite; the notebook_id filter
        is not optional — cross-notebook notes are structurally unreachable)."""
        rows = self._conn.execute(
            "SELECT note_id, body, created_ts FROM surface_notes "
            "WHERE notebook_id=? AND body LIKE ? ORDER BY created_ts LIMIT ?",
            (notebook_id, f"%{query}%", k),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── manifest ─────────────────────────────────────────────────────────────
    def get_manifest(self, notebook_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM surface_manifest WHERE notebook_id=?", (notebook_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_manifest(
        self, notebook_id: str, *,
        centroid: Optional[bytes] = None, crystallized_digest: Optional[str] = None,
        last_ingest_ts: Optional[float] = None,
    ) -> None:
        """Update the manifest's centroid / digest / ingest ts (source_count is derived). Only the
        provided fields change (COALESCE keeps the rest)."""
        if self.get_notebook(notebook_id) is None:
            raise SurfaceStoreError(f"unknown notebook {notebook_id!r}")
        self._conn.execute(
            "INSERT INTO surface_manifest(notebook_id, centroid, crystallized_digest, last_ingest_ts) "
            "VALUES(?,?,?,?) ON CONFLICT(notebook_id) DO UPDATE SET "
            "centroid=COALESCE(excluded.centroid, surface_manifest.centroid), "
            "crystallized_digest=COALESCE(excluded.crystallized_digest, surface_manifest.crystallized_digest), "
            "last_ingest_ts=COALESCE(excluded.last_ingest_ts, surface_manifest.last_ingest_ts)",
            (notebook_id, centroid, crystallized_digest, last_ingest_ts),
        )
        self._refresh_source_count(notebook_id)
        self._conn.commit()


__all__ = ["SurfaceStore", "SurfaceStoreError"]
