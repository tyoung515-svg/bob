"""Gateway generation checks, durable factor attempts and single-use admission."""
import hashlib
import json
import os
import time

from core import credential_state
from core.execution.api import lane_id
from core.execution.models import ExecutionError
from db import _sqlite_connection


async def initialize():
    async with _sqlite_connection() as db:
        await db.execute('CREATE TABLE IF NOT EXISTS credential_requests (request_key TEXT PRIMARY KEY)')
        async with db.execute('PRAGMA table_info(refresh_tokens)') as c:
            columns = [r[1] for r in await c.fetchall()]
        if 'credential_epoch' not in columns:
            await db.execute('ALTER TABLE refresh_tokens ADD COLUMN credential_epoch INTEGER')
        if credential_state.enabled():
            credential_state.require_loaded()
            epoch = credential_state.loaded_epoch()
            await db.execute('UPDATE owner_epochs SET epoch=? WHERE lane=? AND owner=?',
                             (epoch, lane_id(), os.environ['BOB_DEFAULT_OWNER']))
            await db.execute('UPDATE owner_sessions SET revoked=1 WHERE epoch<>?', (epoch,))
            await db.execute('UPDATE owner_challenges SET used=1 WHERE epoch<>?', (epoch,))
            await db.execute('DELETE FROM refresh_tokens WHERE credential_epoch IS NULL OR credential_epoch<>?', (epoch,))
            floor = int(os.environ.get('BOB_TOTP_FLOOR', '-1'))
            if floor >= 0:
                for user in {os.environ['BOB_DEFAULT_OWNER'], 'admin'}:
                    await db.execute('INSERT INTO totp_timesteps VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET '
                                     'last_timestep=MAX(last_timestep,excluded.last_timestep)', (user, floor))
        await db.commit()


def require_loaded():
    try:
        credential_state.require_loaded()
    except Exception:
        raise ExecutionError('credential_generation_mismatch', 503) from None


def keys(operation, dimension):
    owner = os.environ.get('BOB_DEFAULT_OWNER', 'admin')
    return [hashlib.sha256(json.dumps([lane_id(), owner, op, d]).encode()).hexdigest()
            for op, d in (('owner.all', 'aggregate'), (operation, 'aggregate'), (operation, dimension))]


async def check_attempt(db, operation, dimension, now):
    for key in keys(operation, dimension):
        async with db.execute('SELECT next_at FROM owner_attempts WHERE key=?', (key,)) as c:
            row = await c.fetchone()
        if row and row[0] > now:
            raise ExecutionError('authentication_throttled', 429,
                                 {'retry_after': max(1, int(row[0] - now + 1))})


async def failed_attempt(db, operation, dimension, now):
    for key in keys(operation, dimension):
        await db.execute('INSERT INTO owner_attempts VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET '
                         'failures=MIN(failures+1,16),next_at=?+MIN(300,(1 << MIN(failures,8)))',
                         (key, now + 1, now))


async def proof(password, code, operation, dimension, *, request_key=None, require_totp=True):
    """Serialize limits, credential admission and TOTP spend on one SQLite txn."""
    from auth import verify_password_plain, _matched_totp_step
    from config import config
    require_loaded()
    now = time.time()
    accepted = False
    async with _sqlite_connection() as db:
        await db.execute('BEGIN IMMEDIATE')
        await check_attempt(db, operation, dimension, now)
        if request_key is not None:
            async with db.execute('SELECT 1 FROM credential_requests WHERE request_key=?', (request_key,)) as c:
                if await c.fetchone():
                    raise ExecutionError('request_key_used', 409)
            await db.execute('INSERT INTO credential_requests VALUES (?)', (request_key,))
        step = _matched_totp_step(code, config.TOTP_VALID_WINDOW) if isinstance(code, str) else None
        if verify_password_plain(password) and (step is not None or (not require_totp and not config.TOTP_SECRET)):
            owner = os.environ.get('BOB_DEFAULT_OWNER', 'admin')
            async with db.execute('SELECT MAX(last_timestep) FROM totp_timesteps WHERE user_id IN (?,?)',
                                  (owner, 'admin')) as c:
                previous = (await c.fetchone())[0]
            accepted = step is None or previous is None or step > previous
            if accepted and step is not None:
                for user in {owner, 'admin'}:
                    await db.execute('INSERT INTO totp_timesteps VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET '
                                     'last_timestep=MAX(last_timestep,excluded.last_timestep)', (user, step))
        if not accepted:
            await failed_attempt(db, operation, dimension, now)
        # Check again immediately before issuing proof; an epoch may commit during txn.
        require_loaded()
        await db.commit()
    return accepted
