"""Native sensitive factor flow. No seed, secret, password or bearer in replies."""
import asyncio
import hashlib
import json
import time

from aiohttp import web

from core import credential_state, settings_client
from core.settings_contract import SettingsError, decode, identifier, password_result
from core.recovery_contract import validate, prepared_result, binding_result
from core.execution.models import ExecutionError
from db import _sqlite_connection
from owner_sessions import portal
from credential_auth import require_loaded, check_attempt, failed_attempt

router = web.RouteTableDef()


def available():
    return credential_state.enabled() and settings_client.unavailable_reason() is None


def context(claims, link):
    return hashlib.sha256(json.dumps([claims['sub'], claims['jti'], link],
                         separators=(',', ':')).encode()).hexdigest()


def errors(handler):
    async def wrapped(request):
        try:
            require_loaded()
            if not available():
                raise SettingsError('settings_unavailable')
            return await handler(request)
        except ExecutionError as exc:
            response = web.json_response({'error': {'code': exc.code}}, status=exc.status)
            if exc.status == 429:
                response.headers['Retry-After'] = str(exc.detail['retry_after'])
            return response
        except SettingsError as exc:
            allowed = {'invalid_args', 'invalid_password', 'invalid_factors', 'request_key_used',
                       'recovery_busy', 'authentication_throttled'}
            code = exc.code if exc.code in allowed else 'recovery_outcome_unknown'
            status = 429 if code == 'authentication_throttled' else 409 if code in {'request_key_used', 'recovery_busy'} else 422 if code in allowed else 503
            response = web.json_response({'error': {'code': code}}, status=status)
            if status == 429:
                response.headers['Retry-After'] = '300'
            return response
        except Exception:
            return web.json_response({'error': {'code': 'recovery_outcome_unknown'}}, status=503)
    return wrapped


@router.get('/settings/factors/binding')
@errors
async def get_binding(request):
    portal(request)
    return web.json_response(binding_result(await asyncio.to_thread(
        settings_client.call, {'version': 1, 'op': 'factors.binding'})))


async def body(request, op):
    claims, link = portal(request)
    value = await request.json(loads=decode)
    if not isinstance(value, dict) or type(value.get('contract_version')) is not int or value['contract_version'] != 2:
        raise SettingsError('invalid_args')
    value = dict(value)
    del value['contract_version']
    # These fields are server assertions, never accepted from native input.
    if set(value) & {'version', 'op', 'expected_epoch', 'context'}:
        raise SettingsError('invalid_args')
    value.update(version=1, op=op, expected_epoch=credential_state.loaded_epoch(), context=context(claims, link))
    return value, [claims['jti'], link]


# Helper/gateway outcomes that mean the presented factors were wrong.
FACTOR_REFUSALS = frozenset({'invalid_factors'})


async def admit(db, value, dimension, operation, now):
    await check_attempt(db, operation, dimension, now)
    key = identifier(value.get('request_key'))
    async with db.execute('SELECT 1 FROM credential_requests WHERE request_key=?', (key,)) as cursor:
        if await cursor.fetchone():
            raise ExecutionError('request_key_used', 409)
    await db.execute('INSERT INTO credential_requests VALUES (?)', (key,))


@router.post('/settings/factors/prepare')
@errors
async def prepare(request):
    from auth import verify_password_plain, _matched_totp_step
    from config import config
    import os
    value, dimension = await body(request, 'factors.prepare')
    pair = value.get('pair')
    supplied = set(value) & {'password', 'totp'}
    password, totp = value.pop('password', None), value.pop('totp', None)
    needs_password = pair in {'password_stick', 'password_totp'}
    needs_totp = pair in {'totp_stick', 'password_totp'}
    expected = ({'password'} if needs_password else set()) | ({'totp'} if needs_totp else set())
    if (supplied != expected or (needs_password and (not isinstance(password, str) or len(password) > 1024))
            or (needs_totp and (not isinstance(totp, str) or len(totp) > 16))
            or (not needs_password and password is not None) or (not needs_totp and totp is not None)):
        raise SettingsError('invalid_args')
    validate(value)
    now = time.time()
    operation = 'enrollment' if pair == 'setup' else 'recovery.prepare'
    failure, result = None, None
    async with _sqlite_connection() as db:
        await db.execute('BEGIN IMMEDIATE')
        await admit(db, value, dimension, operation, now)
        step = _matched_totp_step(totp, config.TOTP_VALID_WINDOW) if needs_totp else None
        owner = os.environ['BOB_DEFAULT_OWNER']
        async with db.execute('SELECT MAX(last_timestep) FROM totp_timesteps WHERE user_id IN (?,?)', (owner, 'admin')) as cursor:
            previous = (await cursor.fetchone())[0]
        valid = ((not needs_password or verify_password_plain(password))
                 and (not needs_totp or (step is not None and (previous is None or step > previous))))
        if valid and needs_totp:
            for user in {owner, 'admin'}:
                await db.execute('INSERT INTO totp_timesteps VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET '
                                 'last_timestep=MAX(last_timestep,excluded.last_timestep)', (user, step))
        if valid:
            try:
                # Prepare does not restart anything. Holding SQLite admission
                # serializes limits + proof spend + private helper claim.
                result = prepared_result(await asyncio.to_thread(settings_client.call, value))
            except SettingsError as exc:
                failure = exc
        else:
            failure = SettingsError('invalid_factors')
        if failure and failure.code in FACTOR_REFUSALS:
            await failed_attempt(db, operation, dimension, now)
        require_loaded()
        await db.commit()
    if failure:
        raise failure
    return web.json_response(result)


@router.post('/settings/factors/commit')
@router.post('/settings/factors/cancel')
@errors
async def complete(request):
    op = 'factors.cancel' if request.path.endswith('/cancel') else 'factors.commit'
    value, dimension = await body(request, op)
    validate(value)
    async with _sqlite_connection() as db:
        await db.execute('BEGIN IMMEDIATE')
        await admit(db, value, dimension, 'recovery.commit', time.time())
        # Durable single-use admission MUST precede the helper's gateway restart.
        require_loaded()
        await db.commit()
    try:
        result = password_result(await asyncio.to_thread(settings_client.call, value))
        if op == 'factors.commit' and not result['changed']:
            async with _sqlite_connection() as db:
                await db.execute('BEGIN IMMEDIATE')
                await failed_attempt(db, 'recovery.commit', dimension, time.time())
                await db.commit()
        return web.json_response(result)
    except SettingsError as exc:
        # Only a refused proof counts toward backoff. The helper restarts this
        # gateway during a successful commit, so a broken/unknown helper reply
        # here usually means success; the receipt is authoritative.
        if exc.code in FACTOR_REFUSALS:
            async with _sqlite_connection() as db:
                await db.execute('BEGIN IMMEDIATE')
                await failed_attempt(db, 'recovery.commit', dimension, time.time())
                await db.commit()
        raise
