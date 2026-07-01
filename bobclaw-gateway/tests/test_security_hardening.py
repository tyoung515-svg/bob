"""Security-hardening sprint tests (Copilot audit response).

Covers the auth/db-layer behaviors added in the hardening sprint:
  C1 — bcrypt admin password (verify prefers the hash, falls back to plaintext)
  B1 — per-IP failed-login lockout
  B2 — refresh-token rotation-chain absolute max age
  C2 — revoke-all refresh tokens

The autouse `_isolated_gateway_db` fixture (conftest) gives each test a fresh,
init_db-initialised SQLite file, so the db functions below hit real tables.
"""
import asyncio
import datetime as dt

import bcrypt

import auth
import db
from config import config


def _run(coro):
    return asyncio.run(coro)


# ── C1: bcrypt admin password ────────────────────────────────────────────────

def test_verify_password_prefers_hash():
    plain = "s3cret-admin-pw"
    hashed = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
    old_hash, old_plain = config.BOBCLAW_PASSWORD_HASH, config.BOBCLAW_PASSWORD
    try:
        config.BOBCLAW_PASSWORD_HASH = hashed
        config.BOBCLAW_PASSWORD = "unused-plaintext"  # must be ignored when a hash is set
        assert auth.verify_password_plain(plain) is True
        assert auth.verify_password_plain("wrong") is False
    finally:
        config.BOBCLAW_PASSWORD_HASH, config.BOBCLAW_PASSWORD = old_hash, old_plain


def test_verify_password_falls_back_to_plaintext():
    old_hash, old_plain = config.BOBCLAW_PASSWORD_HASH, config.BOBCLAW_PASSWORD
    try:
        config.BOBCLAW_PASSWORD_HASH = ""
        config.BOBCLAW_PASSWORD = "plaintext-pw"
        assert auth.verify_password_plain("plaintext-pw") is True
        assert auth.verify_password_plain("nope") is False
    finally:
        config.BOBCLAW_PASSWORD_HASH, config.BOBCLAW_PASSWORD = old_hash, old_plain


# ── B1: per-IP failed-login lockout ──────────────────────────────────────────

def test_login_lockout_after_threshold():
    async def _t():
        ip = "test-ip-lockout"
        kw = dict(threshold=3, base_seconds=30, max_seconds=900)
        # Below threshold → not locked.
        for _ in range(2):
            await db.record_failed_login(ip, **kw)
        assert await db.check_login_locked(ip) is None
        # At threshold → locked with a positive backoff.
        await db.record_failed_login(ip, **kw)
        locked = await db.check_login_locked(ip)
        assert locked is not None and locked > 0
        # A success clears it.
        await db.clear_login_attempts(ip)
        assert await db.check_login_locked(ip) is None
    _run(_t())


def test_login_lockout_backoff_is_capped():
    async def _t():
        ip = "test-ip-cap"
        kw = dict(threshold=1, base_seconds=1, max_seconds=5)
        for _ in range(10):  # backoff would explode past the cap without clamping
            await db.record_failed_login(ip, **kw)
        locked = await db.check_login_locked(ip)
        assert locked is not None and locked <= 6  # <= max_seconds (+1s rounding)
    _run(_t())


# ── C2: revoke-all refresh tokens ────────────────────────────────────────────

def test_revoke_all_refresh_tokens():
    async def _t():
        user = "revoke-test-user"
        t1 = await db.create_refresh_token(user)
        t2 = await db.create_refresh_token(user)
        removed = await db.revoke_all_refresh_tokens(user)
        assert removed >= 2
        assert await db.validate_and_rotate_refresh_token(t1) is None
        assert await db.validate_and_rotate_refresh_token(t2) is None
    _run(_t())


# ── B2: refresh rotation-chain absolute max age ──────────────────────────────

def test_refresh_chain_absolute_cap_rejects_old_chain():
    async def _t():
        user = "abs-cap-user"
        token = await db.create_refresh_token(user)
        # Age the chain origin just past the absolute cap.
        aged = (
            dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=config.REFRESH_TOKEN_ABSOLUTE_DAYS + 1)
        ).isoformat()
        async with db._sqlite_connection() as conn:
            await conn.execute(
                "UPDATE refresh_tokens SET chain_created_at = ? WHERE token = ?",
                (aged, token),
            )
            await conn.commit()
        # Rotation must reject a chain past its absolute maximum age, even though
        # the per-token TTL is still valid.
        assert await db.validate_and_rotate_refresh_token(token) is None
    _run(_t())


def test_refresh_within_absolute_cap_still_rotates():
    async def _t():
        user = "abs-ok-user"
        token = await db.create_refresh_token(user)
        rotated = await db.validate_and_rotate_refresh_token(token)
        assert rotated is not None and rotated != token
    _run(_t())
