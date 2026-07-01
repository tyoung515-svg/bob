from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


async def init_schema(db_path: Path) -> None:
    here = Path(__file__).resolve().parent.parent.parent
    sql_path = here / "sql" / "memory_schema.sql"
    ddl = sql_path.read_text(encoding="utf-8")
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(ddl)
        await db.commit()


@asynccontextmanager
async def connection(db_path: Path, timeout: float | None = None):
    kwargs = {"timeout": timeout} if timeout is not None else {}
    async with aiosqlite.connect(str(db_path), **kwargs) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db
