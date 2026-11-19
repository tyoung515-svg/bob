"""Resume owner-scoped history ingestion from proven lane-local L0 turn ids.

No shared conversation discovery is performed. Only structured conversation and
owner fields emitted by _l0_events in this lane's memory DB establish membership.
Unattributed legacy events and conversations without that evidence are skipped.
"""
import asyncio
from contextlib import closing
from datetime import datetime
import logging
from pathlib import Path
import sqlite3
from uuid import UUID

from core.context import store
from core.context.ingest import PAGE_SIZE
from core.paths import memory_sqlite_path

logger = logging.getLogger(__name__)
EVENT_PAGE_SIZE = 100
MAX_STEPS_PER_PASS = 60
IMPORT_WAIT_SECONDS = 3.0
_RUNNING = {}
_DONE = set()


def source_path():
    from core.lane_config import configured_mode, get_lane
    path = memory_sqlite_path().resolve()
    if (not configured_mode()
            or get_lane().owned_path(str(path)) is None
            or get_lane().owned_path(str(Path(store.db_path()).resolve())) is None
            or not path.is_file()):
        return None
    return path


def _source_page(path, user):
    with closing(store._connect()) as db:
        row = db.execute('SELECT last_order FROM conv_backfill_sources WHERE user_id=?', (user,)).fetchone()
        after = row[0] if row else 0
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        # Extract only provenance, never event text or instructions. CASE avoids
        # malformed legacy JSON; scanning is bounded even when no events match.
        events = db.execute('''SELECT insertion_order,
            CASE WHEN kind='agent_turn' AND json_valid(body_json) THEN
                CASE WHEN json_extract(body_json,'$.user_id')=?
                     THEN json_extract(body_json,'$.conversation_id') END END
            FROM memory_events WHERE insertion_order>? ORDER BY insertion_order LIMIT ?''',
            (user, after, EVENT_PAGE_SIZE)).fetchall()
    candidates = []
    for _, conversation in events:
        if not isinstance(conversation, str):
            continue
        try:
            candidates.append((user, str(UUID(conversation))))
        except ValueError:
            continue
    with closing(store._connect()) as db:
        db.executemany('INSERT OR IGNORE INTO conv_backfill_jobs(user_id,conversation_id) VALUES(?,?)', candidates)
        if events:
            db.execute('''INSERT INTO conv_backfill_sources(user_id,last_order) VALUES(?,?)
                ON CONFLICT(user_id) DO UPDATE SET last_order=excluded.last_order''', (user, events[-1][0]))
        db.commit()
        job = db.execute('''SELECT * FROM conv_backfill_jobs
            WHERE user_id=? AND done=0 ORDER BY conversation_id LIMIT 1''', (user,)).fetchone()
    return len(events) < EVENT_PAGE_SIZE, dict(job) if job else None


def _checkpoint(user, job, rows):
    # Content is committed first. A crash before the cursor commit replays the
    # same page idempotently, rather than skipping any stored message.
    items = [dict(id=str(row['id']), role=row['role'], content=row['content'] or '',
                  created_at=row['created_at'].isoformat()) for row in rows]
    if items:
        store.record_stored_history(job['conversation_id'], user, items, job['offset'], job['previous_id'])
    with closing(store._connect()) as db:
        db.execute('''UPDATE conv_backfill_jobs SET after_time=?,after_id=?,offset=?,previous_id=?,done=?
            WHERE user_id=? AND conversation_id=?''',
            (items[-1]['created_at'] if items else job['after_time'],
             items[-1]['id'] if items else job['after_id'], job['offset'] + len(items),
             items[-1]['id'] if items else job['previous_id'], int(len(rows) < PAGE_SIZE),
             user, job['conversation_id']))
        db.commit()


async def step(user):
    """Read at most 100 local events and one 100-row canonical message page."""
    from core.execution import chat_records
    path = source_path()
    if not path or chat_records.POOL is None:
        return True
    source_done, job = await asyncio.to_thread(_source_page, path, user)
    if job is None:
        return source_done
    rows = await chat_records.POOL.fetch('''SELECT m.id, m.role, m.content, m.created_at
        FROM messages m JOIN conversations c ON c.id=m.conversation_id
        WHERE c.id=$1 AND c.user_id=$2 AND c.is_archived=FALSE
          AND ($3::timestamptz IS NULL OR (m.created_at,m.id)>($3,$4::uuid))
        ORDER BY m.created_at,m.id LIMIT $5''',
        UUID(job['conversation_id']), user,
        datetime.fromisoformat(job['after_time']) if job['after_time'] else None,
        UUID(job['after_id']) if job['after_id'] else None, PAGE_SIZE, timeout=5)
    await asyncio.to_thread(_checkpoint, user, job, rows)
    return False


async def _run(user, key):
    try:
        # Bound both the pass and each page; durable cursors allow another find
        # or a core restart to continue. A slow/unavailable DB ends this pass.
        async with asyncio.timeout(30):
            for _ in range(MAX_STEPS_PER_PASS):
                if await step(user):
                    _DONE.add(key)
                    return True
                await asyncio.sleep(0)
    except Exception:
        logger.warning('Earlier conversation backfill failed; a later search will resume it', exc_info=True)
    finally:
        _RUNNING.pop(key, None)
    return False


def ensure_started(user):
    """Return the owner's shared import task without waiting or crossing lanes."""
    if not user or source_path() is None:
        return
    key = (str(Path(store.db_path()).resolve()), user)
    if key not in _DONE and key not in _RUNNING:
        _RUNNING[key] = asyncio.create_task(_run(user, key))
    return _RUNNING.get(key)


async def wait_for_import(user):
    """Return whether import remains incomplete; a timed-out reader cannot cancel it."""
    try:
        task = ensure_started(user)
        if task is None:
            return False
        return not await asyncio.wait_for(asyncio.shield(task), IMPORT_WAIT_SECONDS)
    except TimeoutError:
        return True
    except Exception:
        logger.warning('Earlier conversation backfill could not start or finish', exc_info=True)
        return True
